"""Evaluate hierarchy-aware daily models for the six selected channel types."""

import argparse
import gc
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.features import DAY, build_features, load_inputs
from ml.each_channel.train import assess, choose_threshold, quality, write_json

SELECTED = {
    "Датчик дыма": "smoke",
    "Состояние фазы": "phase",
    "ИБП": "ups",
    "Состояние насоса": "pump",
    "Состояние вентилятора": "fan",
    "Ручной извещатель": "manual_alarm",
}
PERIODS = {
    "train": ("2022-01-01", "2025-01-01"),
    "threshold_a": ("2025-01-01", "2025-04-01"),
    "audit_a": ("2025-04-01", "2025-07-01"),
    "selection": ("2025-07-01", "2025-10-01"),
    "calibration": ("2025-10-01", "2026-01-01"),
    # The label horizon must close by 2026-06-01, so decisions run through 2026-05-02.
    "test": ("2026-01-01", "2026-06-01"),
}
PARAMS = {
    "iterations": 160,
    "depth": 5,
    "learning_rate": 0.05,
    "l2_leaf_reg": 20,
    "loss_function": "Logloss",
    "random_seed": 42,
    "thread_count": 6,
    "has_time": True,
    "verbose": False,
    "allow_writing_files": False,
}
POSTPROCESSORS = ("raw", "mean_3d", "confirm_2d")
ADDED_FEATURES = {
    "object_kind",
    "object_channel_count_catalog",
    "parent_channel_count_catalog",
    "parent_type_channel_count",
    "parent_subobject_count",
    "picket_number_catalog",
    "picket_missing",
    "tag_depth",
    "parent_other_active",
    "parent_other_onsets_7d",
    "parent_other_onsets_30d",
    "parent_other_onsets_90d",
    "recent_event_share",
    "recent_alarm_share",
    "recent_fault_message_share",
    "active_density_30d",
    "active_density_90d",
    "events_per_active_day_30d",
    "onset_acceleration",
}
EXCLUDE = {
    "channel_id",
    "date",
    "status",
    "target",
    "lead_days",
    "target_start",
    "object_key",
    "group_key",
    "channel_key",
}


def load_objects(path):
    return pd.read_csv(path).rename(
        columns={
            "ид_объект": "object_id",
            "иерархия_уровень": "level",
            "родитель": "parent_id",
            "вид_объекта": "object_kind",
        }
    )


def _episode_context(episodes, dates):
    starts = np.sort(episodes.start.to_numpy())
    ends = np.sort(episodes.end.fillna(pd.Timestamp("2262-01-01")).to_numpy())
    observed = np.sort(episodes.loc[episodes.start_is_observed, "start"].to_numpy())
    t = dates.to_numpy()
    result = {"active": np.searchsorted(starts, t, "left") - np.searchsorted(ends, t, "left")}
    for window in (7, 30, 90):
        result[f"onsets_{window}d"] = np.searchsorted(observed, t, "left") - np.searchsorted(
            observed, t - window * DAY, "left"
        )
    return result


def _picket(value):
    text = str(value).upper()
    if "-" in text:
        return np.nan
    matches = re.findall(r"ПК\s*(\d+(?:[,.]\d+)?)", text)
    if len(matches) != 1:
        return np.nan
    return float(matches[0].replace(",", "."))


def add_hierarchy_features(frame, episodes, channels, objects):
    """Add catalog and parent context using only episode facts before T."""
    level3 = objects.loc[objects.level.eq(3)].set_index("object_id")
    meta = channels[["channel_id", "object_id", "sensor_type"]].copy()
    meta["parent_id"] = meta.object_id.map(level3.parent_id)
    meta["object_kind"] = meta.object_id.map(level3.object_kind).fillna("missing")
    meta["picket_number"] = channels["название_датчика"].map(_picket)
    meta["tag_depth"] = (
        channels["tag"].fillna("").map(lambda value: len([x for x in str(value).split(".") if x]))
    )
    ep = episodes.merge(
        meta[["channel_id", "object_id", "parent_id"]],
        on="channel_id",
        how="left",
        validate="many_to_one",
    )
    dates = pd.DatetimeIndex(sorted(frame.date.unique()))
    empty = ep.iloc[:0]
    parent_context = {
        key: _episode_context(group, dates)
        for key, group in ep.dropna(subset=["parent_id"]).groupby("parent_id")
    }
    object_context = {key: _episode_context(group, dates) for key, group in ep.groupby("object_id")}
    object_counts = meta.groupby("object_id").channel_id.nunique()
    parent_counts = meta.groupby("parent_id").channel_id.nunique()
    parent_type_counts = meta.groupby(["parent_id", "sensor_type"]).channel_id.nunique()
    subobject_counts = level3.groupby("parent_id").size()
    lookup = meta.set_index("channel_id")
    blocks = []
    date_positions = pd.Series(np.arange(len(dates)), index=dates)
    for channel_id, block in frame.groupby("channel_id", sort=False):
        block = block.copy()
        row = lookup.loc[channel_id]
        object_id, parent_id = row.object_id, row.parent_id
        positions = block.date.map(date_positions).to_numpy()
        parent = parent_context.get(parent_id, _episode_context(empty, dates))
        own_object = object_context.get(object_id, _episode_context(empty, dates))
        block["object_kind"] = row.object_kind
        block["object_channel_count_catalog"] = object_counts.get(object_id, 0)
        block["parent_channel_count_catalog"] = parent_counts.get(parent_id, 0)
        block["parent_type_channel_count"] = parent_type_counts.get((parent_id, row.sensor_type), 0)
        block["parent_subobject_count"] = subobject_counts.get(parent_id, 0)
        block["picket_number_catalog"] = row.picket_number
        block["picket_missing"] = int(pd.isna(row.picket_number))
        block["tag_depth"] = row.tag_depth
        for suffix in ("active", "onsets_7d", "onsets_30d", "onsets_90d"):
            block[f"parent_other_{suffix}"] = (
                parent[suffix][positions] - own_object[suffix][positions]
            )
        blocks.append(block)
    result = pd.concat(blocks, ignore_index=True)
    result["recent_event_share"] = result.events_7d / result.events_30d.clip(lower=1)
    result["recent_alarm_share"] = result.alarms_7d / result.alarms_30d.clip(lower=1)
    result["recent_fault_message_share"] = result.faults_msg_7d / result.faults_msg_30d.clip(
        lower=1
    )
    result["active_density_30d"] = result.days_active_30d / 30
    result["active_density_90d"] = result.days_active_90d / 90
    result["events_per_active_day_30d"] = result.events_30d / result.days_active_30d.clip(lower=1)
    result["onset_acceleration"] = (result.onsets_7d + 1) / (result.onsets_30d + 1)
    return result.sort_values(["date", "channel_id"], kind="stable").reset_index(drop=True)


def subset(frame, name):
    lo, hi = map(pd.Timestamp, PERIODS[name])
    mask = (
        frame.date.ge(lo)
        & frame.date.add(pd.Timedelta(days=30)).le(hi)
        & frame.status.eq("scored")
        & frame.target.notna()
    )
    return frame.loc[mask].copy()


def feature_columns(frame):
    columns = [column for column in frame.columns if column not in EXCLUDE]
    categorical = [
        column
        for column in columns
        if frame[column].dtype == object or isinstance(frame[column].dtype, pd.CategoricalDtype)
    ]
    return columns, categorical


def episode_balanced_weights(frame):
    """Give every represented positive episode equal total weight."""
    weights = np.ones(len(frame), dtype="float64")
    positive = frame.target.eq(1) & frame.target_start.notna()
    if not positive.any():
        return weights
    counts = frame.loc[positive].groupby(["channel_id", "target_start"]).size()
    keys = pd.MultiIndex.from_frame(frame.loc[positive, ["channel_id", "target_start"]])
    row_counts = counts.reindex(keys).to_numpy()
    target_total = positive.sum() / len(counts)
    weights[np.flatnonzero(positive.to_numpy())] = target_total / row_counts
    return weights


def transform_scores(frame, scores, mode):
    values = pd.Series(np.asarray(scores), index=frame.index)
    ordered = frame[["channel_id", "date"]].copy()
    ordered["score"] = values
    ordered = ordered.sort_values(["channel_id", "date"], kind="stable")
    groups = ordered.groupby("channel_id", sort=False).score
    if mode == "raw":
        transformed = ordered.score
    elif mode == "mean_3d":
        transformed = groups.rolling(3, min_periods=1).mean().reset_index(level=0, drop=True)
    elif mode == "confirm_2d":
        previous = groups.shift(1)
        transformed = np.minimum(ordered.score, previous.fillna(ordered.score))
    else:
        raise ValueError(mode)
    return pd.Series(transformed, index=ordered.index).reindex(frame.index).to_numpy()


def rank(metrics):
    return (
        int(metrics["passes_both"]),
        min((metrics["precision"] or 0) / 0.7, (metrics["recall"] or 0) / 0.5),
        metrics["ap"] or 0,
    )


def fit_model(train, features, categorical, *, balanced):
    model = CatBoostClassifier(**PARAMS, cat_features=categorical)
    weights = episode_balanced_weights(train) if balanced else None
    model.fit(train[features], train.target, sample_weight=weights)
    return model


def prepare_frames(args, channels, daily, episodes, objects):
    dates = pd.date_range("2022-01-03", "2024-12-30", freq="W-MON").union(
        pd.date_range("2025-01-01", "2026-05-02")
    )
    frames = {}
    uncached = []
    for name, key in SELECTED.items():
        cache = args.previous / "data/interim/each_channel/features" / f"{key}.parquet"
        if cache.exists():
            frames[key] = pd.read_parquet(cache)
        else:
            uncached.extend(channels.loc[channels.sensor_type.eq(name), "channel_id"])
    if uncached:
        built = build_features(daily, episodes, channels, dates, uncached)
        for name, key in SELECTED.items():
            if key not in frames:
                ids = set(channels.loc[channels.sensor_type.eq(name), "channel_id"])
                frames[key] = built.loc[built.channel_id.isin(ids)].copy()
    for key in frames:
        frames[key] = add_hierarchy_features(frames[key], episodes, channels, objects)
    return frames


def fit(args):
    if (args.out / "protocol.json").exists():
        raise ValueError("Use a new output directory")
    channels, daily, episodes = load_inputs(args.data_dir)
    objects = load_objects(args.objects)
    frames = prepare_frames(args, channels, daily, episodes, objects)
    source_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            args.data_dir / "channels.csv",
            args.data_dir / "fault_episodes.parquet",
            args.objects,
        )
    }
    write_json(
        args.out / "protocol.json",
        {
            "selected": SELECTED,
            "periods": PERIODS,
            "parameters": PARAMS,
            "recipes": [
                "extended_plain",
                "hierarchy_plain",
                "hierarchy_episode_balanced",
            ],
            "postprocessors": POSTPROCESSORS,
            "selection": "recipe on Q2 audit after Q1 threshold; postprocessor on Q3; threshold on Q4",
            "test": "2026-01-01 through 2026-05-02; previously viewed",
            "source_hashes": source_hashes,
            "objects_note": "78 anonymized level-3 objects and 16 parents; no addresses",
        },
    )
    for name, key in SELECTED.items():
        print(f"FIT {key}", flush=True)
        frame = frames[key]
        hierarchy_features, hierarchy_categorical = feature_columns(frame)
        extended_features = [
            feature for feature in hierarchy_features if feature not in ADDED_FEATURES
        ]
        extended_categorical = [
            feature for feature in hierarchy_categorical if feature in extended_features
        ]
        recipes = {
            "extended_plain": (False, extended_features, extended_categorical),
            "hierarchy_plain": (
                False,
                hierarchy_features,
                hierarchy_categorical,
            ),
            "hierarchy_episode_balanced": (
                True,
                hierarchy_features,
                hierarchy_categorical,
            ),
        }
        parts = {period: subset(frame, period) for period in PERIODS}
        train = parts["train"]
        diagnostics = {}
        for recipe, (balanced, features, categorical) in recipes.items():
            model = fit_model(train, features, categorical, balanced=balanced)
            q1_scores = model.predict_proba(parts["threshold_a"][features])[:, 1]
            threshold = choose_threshold(parts["threshold_a"].target, q1_scores)
            q2_scores = model.predict_proba(parts["audit_a"][features])[:, 1]
            diagnostics[recipe] = {
                "threshold_a": threshold,
                "threshold_a_metrics": assess(parts["threshold_a"], q1_scores, threshold),
                "audit_a": assess(parts["audit_a"], q2_scores, threshold),
            }
            del model
            gc.collect()
        winner = max(diagnostics, key=lambda recipe: rank(diagnostics[recipe]["audit_a"]))
        balanced, features, categorical = recipes[winner]
        refit = pd.concat(
            [
                train,
                parts["threshold_a"],
                parts["audit_a"],
            ],
            ignore_index=True,
        ).sort_values(["date", "channel_id"], kind="stable")
        model = fit_model(
            refit,
            features,
            categorical,
            balanced=balanced,
        )
        pretest = pd.concat(
            [parts["selection"], parts["calibration"]],
            ignore_index=True,
        ).sort_values(["date", "channel_id"], kind="stable")
        raw_scores = model.predict_proba(pretest[features])[:, 1]
        postprocessors = {}
        selection_mask = pretest.date.lt("2025-10-01").to_numpy()
        calibration_mask = ~selection_mask
        for mode in POSTPROCESSORS:
            scores = transform_scores(pretest, raw_scores, mode)
            selection_scores = scores[selection_mask]
            selection = pretest.loc[selection_mask]
            selection_threshold = choose_threshold(selection.target, selection_scores)
            postprocessors[mode] = {
                "selection_threshold": selection_threshold,
                "selection": assess(selection, selection_scores, selection_threshold),
            }
        postprocess = max(
            postprocessors,
            key=lambda mode: rank(postprocessors[mode]["selection"]),
        )
        transformed = transform_scores(pretest, raw_scores, postprocess)
        calibration = pretest.loc[calibration_mask]
        threshold = choose_threshold(calibration.target, transformed[calibration_mask])
        folder = args.out / key
        folder.mkdir(parents=True, exist_ok=True)
        model_path = folder / "model.cbm"
        model.save_model(str(model_path))
        result = {
            "name": name,
            "key": key,
            "channels": int(frame.channel_id.nunique()),
            "features": features,
            "categorical": categorical,
            "diagnostics": diagnostics,
            "winner": winner,
            "postprocessors": postprocessors,
            "postprocess": postprocess,
            "threshold": threshold,
            "calibration": assess(calibration, transformed[calibration_mask], threshold),
            "counts": {
                period: quality(part.target, np.zeros(len(part)), 1.0)
                for period, part in parts.items()
            },
            "model_path": str(model_path.resolve()),
            "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        }
        write_json(folder / "selection_frozen.json", result)
        evaluation = pd.concat(
            [parts["calibration"], parts["test"]],
            ignore_index=True,
        ).sort_values(["date", "channel_id"], kind="stable")
        evaluation.to_parquet(folder / "evaluation.parquet", index=False)
        del frame, model, refit, pretest, evaluation
        gc.collect()
    write_json(args.out / "all_frozen.json", {"keys": list(SELECTED.values())})


def evaluate(args):
    keys = json.loads((args.out / "all_frozen.json").read_text(encoding="utf-8"))["keys"]
    old = {row["key"]: row for row in json.loads(args.previous_summary.read_text(encoding="utf-8"))}
    results = []
    for key in keys:
        folder = args.out / key
        result = json.loads((folder / "selection_frozen.json").read_text(encoding="utf-8"))
        model_path = Path(result["model_path"])
        if hashlib.sha256(model_path.read_bytes()).hexdigest() != result["sha256"]:
            raise ValueError(f"Model changed after freeze: {key}")
        model = CatBoostClassifier().load_model(str(model_path))
        frame = pd.read_parquet(folder / "evaluation.parquet")
        raw = model.predict_proba(frame[result["features"]])[:, 1]
        scores = transform_scores(frame, raw, result["postprocess"])
        test_mask = frame.date.ge("2026-01-01").to_numpy()
        test = frame.loc[test_mask].copy()
        result["test"] = assess(test, scores[test_mask], result["threshold"])
        result["always_alert"] = assess(test, np.ones(len(test)), 0.5)
        result["previous"] = {
            tag: old[key]["test"].get(tag)
            for tag in ("v0_original", "incumbent", old[key]["winner"])
            if tag in old[key]["test"]
        }
        result["previous_winner"] = old[key]["winner"]
        write_json(folder / "report.json", result)
        results.append(result)
        print(
            key,
            result["winner"],
            result["postprocess"],
            result["test"]["precision"],
            result["test"]["recall"],
            flush=True,
        )
    write_json(args.out / "summary.json", results)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("fit", "evaluate"), required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--objects", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "fit":
        for value in (args.data_dir, args.objects, args.previous):
            if value is None:
                parser.error("fit requires --data-dir, --objects and --previous")
        fit(args)
    else:
        if args.previous_summary is None:
            parser.error("evaluate requires --previous-summary")
        evaluate(args)


if __name__ == "__main__":
    main()
