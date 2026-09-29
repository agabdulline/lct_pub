"""Evaluate causal fleet-level fault context for the three hard channel types."""

import argparse
import gc
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.baseline.daily import FEATURES as BASE_FEATURES
from ml.each_channel.calendar_study import CALENDAR, calendar_features, rank
from ml.each_channel.features import build_features, load_inputs
from ml.each_channel.hard_channel_study import TYPES, read_spec
from ml.each_channel.train import assess, choose_threshold, write_json

PERIODS = {
    "train": ("2022-01-01", "2025-01-01"),
    "threshold_a": ("2025-01-01", "2025-04-01"),
    "audit_a": ("2025-04-01", "2025-07-01"),
    "selection": ("2025-07-01", "2025-10-01"),
    "calibration": ("2025-10-01", "2026-01-01"),
    "test": ("2026-01-01", "2026-06-01"),
}
WINDOWS = (7, 30, 90, 365)
FLEET_FEATURES = [
    *(f"fleet_onsets_{window}d" for window in WINDOWS),
    *(f"fleet_channel_days_{window}d" for window in WINDOWS),
    *(f"fleet_object_days_{window}d" for window in WINDOWS),
    "fleet_in_fault",
    "fleet_days_since_onset",
    "fleet_onset_acceleration",
    "fleet_active_fraction",
    "all_type_onsets_7d",
    "all_type_onsets_30d",
]
PARAMS = {
    "iterations": 160,
    "depth": 5,
    "learning_rate": 0.05,
    "l2_leaf_reg": 20,
    "loss_function": "Logloss",
    "random_seed": 42,
    "thread_count": 6,
    "has_time": True,
    "cat_features": ["sensor_type"],
    "verbose": False,
    "allow_writing_files": False,
}
RECIPES = {
    "fleet_base": {"features": BASE_FEATURES + FLEET_FEATURES, "since": "2022-01-01"},
    "fleet_calendar": {
        "features": BASE_FEATURES + FLEET_FEATURES + CALENDAR,
        "since": "2022-01-01",
    },
    "fleet_recent": {"features": BASE_FEATURES + FLEET_FEATURES, "since": "2024-01-01"},
}
OLD_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def window_counts(points, dates, window):
    points = np.sort(np.asarray(points, dtype="datetime64[ns]"))
    dates = np.asarray(dates, dtype="datetime64[ns]")
    return np.searchsorted(points, dates, "left") - np.searchsorted(
        points, dates - np.timedelta64(window, "D"), "left"
    )


def add_fleet_context(frame, episodes, channels, sensor_type):
    """Add same-type context using only episode facts strictly before each decision date."""
    result = frame.copy()
    ids = channels.loc[channels.sensor_type.eq(sensor_type), "channel_id"]
    typed = episodes.loc[episodes.channel_id.isin(ids)].copy()
    observed = typed.loc[typed.start_is_observed].copy()
    observed["day"] = observed.start.dt.normalize()
    meta = channels.set_index("channel_id")
    observed["object_id"] = observed.channel_id.map(meta.object_id)

    unique_dates, inverse = np.unique(
        result.date.to_numpy(dtype="datetime64[ns]"), return_inverse=True
    )
    onsets = observed.start.to_numpy(dtype="datetime64[ns]")
    channel_days = observed.drop_duplicates(["day", "channel_id"]).day.to_numpy(
        dtype="datetime64[ns]"
    )
    object_days = (
        observed.dropna(subset=["object_id"])
        .drop_duplicates(["day", "object_id"])
        .day.to_numpy(dtype="datetime64[ns]")
    )

    for window in WINDOWS:
        total = window_counts(onsets, unique_dates, window)[inverse]
        own = result[f"onsets_{window}d"].fillna(0).to_numpy()
        result[f"fleet_onsets_{window}d"] = np.maximum(total - own, 0)
        result[f"fleet_channel_days_{window}d"] = window_counts(channel_days, unique_dates, window)[
            inverse
        ]
        result[f"fleet_object_days_{window}d"] = window_counts(object_days, unique_dates, window)[
            inverse
        ]

    starts = np.sort(typed.start.to_numpy(dtype="datetime64[ns]"))
    ends = np.sort(typed.end.fillna(pd.Timestamp("2262-01-01")).to_numpy(dtype="datetime64[ns]"))
    result["fleet_in_fault"] = (
        np.searchsorted(starts, unique_dates, "left") - np.searchsorted(ends, unique_dates, "left")
    )[inverse]
    previous = np.searchsorted(np.sort(onsets), unique_dates, "left")
    last = np.full(len(unique_dates), np.datetime64("NaT", "ns"))
    available = previous > 0
    if len(onsets):
        last[available] = np.sort(onsets)[previous[available] - 1]
    result["fleet_days_since_onset"] = np.where(
        available,
        (unique_dates - last) / np.timedelta64(1, "D"),
        np.nan,
    )[inverse]
    result["fleet_onset_acceleration"] = (result.fleet_onsets_7d / 7) / (
        result.fleet_onsets_30d / 30 + 1
    )
    result["fleet_active_fraction"] = result.fleet_in_fault / max(len(ids), 1)

    all_observed = episodes.loc[episodes.start_is_observed, "start"].to_numpy(
        dtype="datetime64[ns]"
    )
    for window in (7, 30):
        result[f"all_type_onsets_{window}d"] = window_counts(all_observed, unique_dates, window)[
            inverse
        ]
    return result


def subset(frame, period):
    lo, hi = map(pd.Timestamp, PERIODS[period])
    mask = frame.date.ge(lo) & frame.date.add(pd.Timedelta(days=30)).le(hi)
    return frame.loc[mask & frame.status.eq("scored") & frame.target.notna()].copy()


def load_frame(key, args, channels, daily, episodes, calendar):
    name = next(name for name, value in TYPES.items() if value == key)
    if key == "manual_alarm" and args.manual_frame is None:
        dates = pd.date_range("2022-01-03", "2024-12-30", freq="W-MON").union(
            pd.date_range("2025-01-01", "2026-05-02")
        )
        ids = channels.loc[channels.sensor_type.eq(name), "channel_id"]
        frame = build_features(daily, episodes, channels, dates, ids)
    else:
        path = (
            args.manual_frame
            if key == "manual_alarm"
            else args.features_dir / f"{key}_advanced.parquet"
        )
        frame = pd.read_parquet(path)
    frame = frame.merge(calendar, on="date", how="left", validate="many_to_one")
    return add_fleet_context(frame, episodes, channels, name).sort_values(
        ["date", "channel_id"], kind="stable"
    )


def fit_model(frame, features):
    model = CatBoostClassifier(**PARAMS)
    model.fit(frame[features], frame.target)
    return model


def fit(args):
    if (args.out / "protocol.json").exists():
        raise ValueError("Use a new output directory")
    channels, daily, episodes = load_inputs(args.data_dir)
    calendar, calendar_sources = calendar_features(args.calendar_dir)
    args.out.mkdir(parents=True, exist_ok=True)
    write_json(
        args.out / "protocol.json",
        {
            "created_before_test_scoring": True,
            "types": TYPES,
            "periods": PERIODS,
            "fleet_features": FLEET_FEATURES,
            "recipes": RECIPES,
            "parameters": PARAMS,
            "old_weights": OLD_WEIGHTS,
            "selection": "recipe on Q2 after Q1 threshold; ensemble weight on Q3; threshold on Q4",
            "test": "2026-01-01 through 2026-05-02; previously viewed",
            "calendar_sources": calendar_sources,
            "source_hashes": {
                name: sha256(args.data_dir / name)
                for name in ("channels.csv", "fault_episodes.parquet", "sensor_daily.parquet")
            },
        },
    )
    for key in TYPES.values():
        print(f"BUILD {key}", flush=True)
        frame = load_frame(key, args, channels, daily, episodes, calendar)
        parts = {period: subset(frame, period) for period in PERIODS}
        diagnostics = {}
        for recipe, config in RECIPES.items():
            train = parts["train"].loc[parts["train"].date.ge(config["since"])]
            model = fit_model(train, config["features"])
            q1_scores = model.predict_proba(parts["threshold_a"][config["features"]])[:, 1]
            threshold = choose_threshold(parts["threshold_a"].target, q1_scores)
            q2_scores = model.predict_proba(parts["audit_a"][config["features"]])[:, 1]
            diagnostics[recipe] = {
                "threshold_a": threshold,
                "threshold_a_metrics": assess(parts["threshold_a"], q1_scores, threshold),
                "audit_a": assess(parts["audit_a"], q2_scores, threshold),
            }
            del model
            gc.collect()
        winner = max(diagnostics, key=lambda name: rank(diagnostics[name]["audit_a"]))
        config = RECIPES[winner]
        refit = pd.concat(
            [parts["train"], parts["threshold_a"], parts["audit_a"]], ignore_index=True
        ).sort_values(["date", "channel_id"], kind="stable")
        refit = refit.loc[refit.date.ge(config["since"])]
        model = fit_model(refit, config["features"])

        _, old_spec, old_model = read_spec(args.calendar_run, key)
        pretest = pd.concat(
            [parts["selection"], parts["calibration"]], ignore_index=True
        ).sort_values(["date", "channel_id"], kind="stable")
        new_scores = model.predict_proba(pretest[config["features"]])[:, 1]
        old_scores = old_model.predict_proba(pretest[old_spec["features"]])[:, 1]
        q3 = pretest.date.lt("2025-10-01").to_numpy()
        ensembles = {}
        for old_weight in OLD_WEIGHTS:
            scores = old_weight * old_scores + (1 - old_weight) * new_scores
            threshold = choose_threshold(pretest.loc[q3, "target"], scores[q3])
            ensembles[str(old_weight)] = assess(pretest.loc[q3], scores[q3], threshold)
        old_weight = max(OLD_WEIGHTS, key=lambda value: rank(ensembles[str(value)]))
        combined = old_weight * old_scores + (1 - old_weight) * new_scores
        q4 = ~q3
        threshold = choose_threshold(pretest.loc[q4, "target"], combined[q4])

        folder = args.out / key
        folder.mkdir(parents=True, exist_ok=True)
        model_path = folder / "model.cbm"
        model.save_model(str(model_path))
        frozen = {
            "key": key,
            "winner": winner,
            "features": config["features"],
            "diagnostics": diagnostics,
            "ensembles": ensembles,
            "old_weight": old_weight,
            "threshold": threshold,
            "calibration": assess(pretest.loc[q4], combined[q4], threshold),
            "model_path": str(model_path.resolve()),
            "sha256": sha256(model_path),
            "old_model_path": old_spec["path"],
            "old_sha256": old_spec["sha256"],
            "old_features": old_spec["features"],
            "old_threshold": old_spec["threshold"],
            "counts": {
                period: {"rows": len(part), "positives": int(part.target.sum())}
                for period, part in parts.items()
            },
        }
        write_json(folder / "selection_frozen.json", frozen)
        evaluation = pd.concat(
            [parts["calibration"], parts["test"]], ignore_index=True
        ).sort_values(["date", "channel_id"], kind="stable")
        evaluation.to_parquet(folder / "evaluation.parquet", index=False)
        print(f"FROZEN {key} {winner} old_weight={old_weight}", flush=True)
        del frame, parts, model, refit, pretest, evaluation
        gc.collect()
    write_json(args.out / "all_frozen.json", {"keys": list(TYPES.values())})


def evaluate(args):
    keys = json.loads((args.out / "all_frozen.json").read_text(encoding="utf-8"))["keys"]
    reports = []
    for key in keys:
        folder = args.out / key
        frozen = json.loads((folder / "selection_frozen.json").read_text(encoding="utf-8"))
        model_path = Path(frozen["model_path"])
        old_path = Path(frozen["old_model_path"])
        if sha256(model_path) != frozen["sha256"] or sha256(old_path) != frozen["old_sha256"]:
            raise ValueError(f"Model changed after freeze: {key}")
        model = CatBoostClassifier().load_model(str(model_path))
        old_model = CatBoostClassifier().load_model(str(old_path))
        frame = pd.read_parquet(folder / "evaluation.parquet")
        new_scores = model.predict_proba(frame[frozen["features"]])[:, 1]
        old_scores = old_model.predict_proba(frame[frozen["old_features"]])[:, 1]
        scores = frozen["old_weight"] * old_scores + (1 - frozen["old_weight"]) * new_scores
        calibration = frame.date.lt("2026-01-01").to_numpy()
        test = frame.date.ge("2026-01-01").to_numpy()
        weight_ablation = {}
        for old_weight in OLD_WEIGHTS:
            ablation_scores = old_weight * old_scores + (1 - old_weight) * new_scores
            threshold = choose_threshold(
                frame.loc[calibration, "target"], ablation_scores[calibration]
            )
            weight_ablation[str(old_weight)] = {
                "threshold": threshold,
                "calibration": assess(
                    frame.loc[calibration], ablation_scores[calibration], threshold
                ),
                "test": assess(frame.loc[test], ablation_scores[test], threshold),
            }
        report = {
            **frozen,
            "test": assess(frame.loc[test], scores[test], frozen["threshold"]),
            "old_fixed_test": assess(frame.loc[test], old_scores[test], frozen["old_threshold"]),
            "weight_ablation_not_used_for_selection": weight_ablation,
        }
        report.pop("model_path")
        report.pop("old_model_path")
        reports.append(report)
        write_json(folder / "report.json", report)
        print(
            key,
            report["test"]["precision"],
            report["test"]["recall"],
            flush=True,
        )
    write_json(args.out / "summary.json", reports)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("fit", "evaluate"), required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--features-dir", type=Path)
    parser.add_argument("--manual-frame", type=Path)
    parser.add_argument("--calendar-run", type=Path)
    parser.add_argument("--calendar-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "fit":
        for value in (args.data_dir, args.features_dir, args.calendar_run, args.calendar_dir):
            if value is None:
                parser.error("fit requires data, features and calendar inputs")
        fit(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
