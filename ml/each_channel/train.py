"""Select one daily CatBoost per frequent channel type without tuning on 2026."""

import argparse
import gc
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.features import (
    build_features,
    feature_columns,
    load_inputs,
    selected_types,
)
from sklearn.metrics import average_precision_score, precision_recall_curve

ROOT = Path(__file__).resolve().parents[2]
PERIODS = {
    "train": ["2022-01-01", "2025-01-01"],
    "early_stop": ["2025-01-01", "2025-07-01"],
    "selection": ["2025-07-01", "2025-10-01"],
    "calibration": ["2025-10-01", "2026-01-01"],
    "test": ["2026-01-01", "2026-06-30"],
}
CANDIDATES = [
    {
        "name": "v0_d6",
        "mode": "base",
        "depth": 6,
        "l2": 3,
        "weight": "None",
        "since": "2022-01-01",
    },
    {
        "name": "extended_d6",
        "mode": "extended",
        "depth": 6,
        "l2": 5,
        "weight": "None",
        "since": "2022-01-01",
    },
    {
        "name": "weighted_d4",
        "mode": "extended",
        "depth": 4,
        "l2": 10,
        "weight": "SqrtBalanced",
        "since": "2022-01-01",
    },
    {
        "name": "weighted_d8",
        "mode": "extended",
        "depth": 8,
        "l2": 10,
        "weight": "SqrtBalanced",
        "since": "2022-01-01",
    },
    {
        "name": "recent_d6",
        "mode": "extended",
        "depth": 6,
        "l2": 10,
        "weight": "None",
        "since": "2024-01-01",
    },
    {
        "name": "recent_weighted",
        "mode": "extended",
        "depth": 5,
        "l2": 10,
        "weight": "SqrtBalanced",
        "since": "2024-01-01",
    },
    {
        "name": "object_context",
        "mode": "identity",
        "depth": 6,
        "l2": 20,
        "weight": "None",
        "since": "2022-01-01",
    },
]
COMMON = {
    "iterations": 1500,
    "learning_rate": 0.05,
    "loss_function": "Logloss",
    "eval_metric": "PRAUC",
    "od_type": "Iter",
    "od_wait": 120,
    "random_seed": 42,
    "has_time": True,
    "thread_count": 6,
    "verbose": False,
    "allow_writing_files": False,
}


def split(frame, name):
    lo, hi = map(pd.Timestamp, PERIODS[name])
    mask = frame.date.ge(lo) & frame.date.add(pd.Timedelta(days=30)).le(hi)
    return frame.loc[mask & frame.status.eq("scored") & frame.target.notna()].copy()


def choose_threshold(y, score):
    if not np.any(y):
        return 1.000001
    p, r, thresholds = precision_recall_curve(y, score)
    objective = np.minimum(p[:-1] / 0.7, r[:-1] / 0.5)
    counts = len(score) - np.searchsorted(np.sort(score), thresholds, side="left")
    objective[counts < 10] = -1
    return float(thresholds[np.argmax(objective)]) if len(thresholds) else 1.000001


def quality(y, score, threshold):
    y = np.asarray(y, dtype=bool)
    alert = np.asarray(score) >= threshold
    tp, fp, fn = (
        int((y & alert).sum()),
        int((~y & alert).sum()),
        int((y & ~alert).sum()),
    )
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return {
        "decisions": len(y),
        "positives": int(y.sum()),
        "alerts": int(alert.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": p,
        "recall": r,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        "ap": float(average_precision_score(y, score)) if y.any() else None,
        "passes_both": p is not None and r is not None and p > 0.7 and r > 0.5,
    }


def assess(frame, score, threshold):
    result = quality(frame.target, score, threshold)
    alert = np.asarray(score) >= threshold
    hits = frame.loc[alert & frame.target.eq(1)].copy()
    leads = hits.lead_days
    result["lead_days"] = {
        str(q): float(leads.quantile(q)) if len(leads) else None for q in (0.25, 0.5, 0.75)
    }
    result["channels"] = int(frame.channel_id.nunique())
    result["positive_channels"] = int(frame.loc[frame.target.eq(1), "channel_id"].nunique())
    result["caught_channels"] = int(hits.channel_id.nunique())
    represented = frame.loc[frame.target.eq(1), ["channel_id", "target_start"]].drop_duplicates()
    caught = hits.groupby(["channel_id", "target_start"]).lead_days.max()
    result["represented_episodes"] = len(represented)
    result["caught_represented_episodes"] = len(caught)
    result["represented_episode_recall"] = (
        len(caught) / len(represented) if len(represented) else None
    )
    result["episode_first_alert_median_days"] = float(caught.median()) if len(caught) else None
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def fit_type(key, info, frame, episodes, channels, out, cache, recipes=None, incumbent=None):
    started = time.perf_counter()
    parts = {name: split(frame, name) for name in PERIODS}
    train = parts["train"]
    es, selection, calibration, test = (
        parts[k] for k in ("early_stop", "selection", "calibration", "test")
    )
    result = dict(key=key, **info, periods=PERIODS, candidates=[])
    if train.target.nunique() < 2 or es.target.nunique() < 2:
        result.update(
            status="insufficient_classes",
            counts={k: quality(v.target, np.zeros(len(v)), 1) for k, v in parts.items()},
        )
        write_json(out / key / "config.json", result)
        return result
    best_rank, best_model, best_config = None, None, None
    if incumbent is not None:
        previous = json.loads((incumbent / "config.json").read_text(encoding="utf-8"))
        best_config = previous["selected"]
        met = best_config["selection"]
        best_rank = (int(met["passes_both"]), best_config["objective"], met["ap"] or 0)
        best_model = CatBoostClassifier().load_model(str(incumbent / "model.cbm"))
        result["candidates"] = previous["candidates"]
    for recipe in CANDIDATES if recipes is None else recipes:
        features, cats = feature_columns(frame, recipe["mode"])
        if "feature_names" in recipe:
            features, cats = recipe["feature_names"], recipe["categorical"]
        tr = train.loc[train.date.ge(recipe["since"])]
        params = dict(
            COMMON,
            depth=recipe["depth"],
            l2_leaf_reg=recipe["l2"],
            auto_class_weights=recipe["weight"],
            cat_features=cats,
        )
        if recipe["weight"] != "None":
            params["eval_metric"] = "PRAUC:use_weights=false"
        if "fixed_trees" in recipe:
            trees = recipe["fixed_trees"]
        else:
            model = CatBoostClassifier(**params)
            model.fit(
                tr[features],
                tr.target,
                eval_set=(es[features], es.target),
                use_best_model=True,
            )
            trees = model.tree_count_
        # Refit at fixed tree count with now-known H1 2025 outcomes. All horizons end before July.
        extra = es if recipe.get("refit_all_days") else es.loc[es.date.dt.weekday.eq(0)]
        refit = pd.concat([tr, extra], ignore_index=True).sort_values(
            ["date", "channel_id"], kind="stable"
        )
        final_params = dict(params, iterations=trees)
        final_params.pop("od_type")
        final_params.pop("od_wait")
        model = CatBoostClassifier(**final_params)
        model.fit(refit[features], refit.target)
        score = model.predict_proba(selection[features])[:, 1]
        threshold = choose_threshold(selection.target, score)
        met = quality(selection.target, score, threshold)
        objective = min((met["precision"] or 0) / 0.7, (met["recall"] or 0) / 0.5)
        rank = (int(met["passes_both"]), objective, met["ap"] or 0)
        candidate = {
            "recipe": recipe,
            "trees": trees,
            "selection_threshold": threshold,
            "selection": met,
            "objective": objective,
            "refit_rows": len(refit),
            "refit_positives": int(refit.target.sum()),
        }
        result["candidates"].append(candidate)
        print(
            f"{key}: {recipe['name']} trees={trees} selection P={met['precision']} R={met['recall']}",
            flush=True,
        )
        if best_rank is None or rank > best_rank:
            best_rank, best_model = rank, model
            best_config = dict(
                candidate, features=features, categorical=cats, parameters=final_params
            )
        write_json(out / key / "search.json", result["candidates"])
        del model, refit
        gc.collect()
    result.update(status="trained", selected=best_config)
    features = best_config["features"]
    threshold = choose_threshold(
        calibration.target, best_model.predict_proba(calibration[features])[:, 1]
    )
    result["threshold"] = threshold
    result["calibration"] = assess(
        calibration, best_model.predict_proba(calibration[features])[:, 1], threshold
    )
    folder = out / key
    folder.mkdir(parents=True, exist_ok=True)
    model_file = folder / "model.cbm"
    best_model.save_model(str(model_file))
    result["sha256"] = hashlib.sha256(model_file.read_bytes()).hexdigest()
    # Freeze selection and threshold before opening the final test scores.
    write_json(folder / "selection_frozen.json", result)
    test_score = best_model.predict_proba(test[features])[:, 1]
    result["test"] = assess(test, test_score, threshold)
    seen = set(
        episodes.loc[
            episodes.start.lt("2025-07-01")
            & episodes.start.ge("2022-01-01")
            & episodes.start_is_observed,
            "channel_id",
        ]
    )
    in_train = test.channel_id.isin(seen).to_numpy()
    result["test_by_fault_history"] = {
        name: assess(test.loc[mask], test_score[mask], threshold)
        for name, mask in (("with_history", in_train), ("without_history", ~in_train))
    }
    chosen_recipe = best_config["recipe"]
    fitted_history = pd.concat(
        [
            train.loc[train.date.ge(chosen_recipe["since"])],
            es if chosen_recipe.get("refit_all_days") else es.loc[es.date.dt.weekday.eq(0)],
        ]
    )
    known_objects = set(fitted_history.object_key)
    obj_seen = test.object_key.isin(known_objects).to_numpy()
    result["test_by_object_history"] = {
        name: assess(test.loc[mask], test_score[mask], threshold)
        for name, mask in (("known_object", obj_seen), ("new_object", ~obj_seen))
    }
    result["test_by_month"] = {
        str(month): assess(group, test_score[test.index.get_indexer(group.index)], threshold)
        for month, group in test.groupby(test.date.dt.to_period("M"))
    }
    result["baselines"] = {
        "always_alert": assess(test, np.ones(len(test)), 0.5),
        "recent_fault": assess(test, test.onsets_30d.to_numpy(), 0.5),
    }
    result["feature_importance"] = dict(
        sorted(
            zip(features, map(float, best_model.get_feature_importance()), strict=False),
            key=lambda item: -item[1],
        )
    )
    result["seconds"] = time.perf_counter() - started
    test.assign(score=test_score, alert=test_score >= threshold).to_parquet(
        cache / f"{key}_test.parquet", index=False
    )
    write_json(folder / "config.json", result)
    print(f"FINAL {key}: {result['test']}", flush=True)
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "ml/each_channel/models")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/interim/each_channel/features")
    parser.add_argument("--groups", nargs="+")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    channels, daily, episodes = load_inputs(args.data_dir)
    types = selected_types(channels)
    args.cache.mkdir(parents=True, exist_ok=True)
    write_json(
        args.out.parent / "protocol.json",
        {
            "periods": PERIODS,
            "candidates": CANDIDATES,
            "common": COMMON,
            "selection": "max(min(P/0.7,R/0.5)) on Q3 2025; separate Q4 calibration",
            "types": types,
            "target": "observed onset of Неисправен in [T+1d,T+30d)",
            "data": str(args.data_dir),
        },
    )
    dates = pd.date_range("2022-01-03", "2024-12-30", freq="W-MON").union(
        pd.date_range("2025-01-01", "2026-05-02", freq="D")
    )
    for key, info in types.items():
        if args.groups and key not in args.groups:
            continue
        if args.resume and (args.out / key / "config.json").exists():
            continue
        print(f"Building {key}", flush=True)
        path = args.cache / f"{key}.parquet"
        if path.exists():
            frame = pd.read_parquet(path)
        else:
            ids = channels.loc[channels.sensor_type.eq(info["name"]), "channel_id"].tolist()
            frame = build_features(daily, episodes, channels, dates, ids)
            frame.to_parquet(path, index=False)
        fit_type(key, info, frame, episodes, channels, args.out, args.cache)
        del frame
        gc.collect()


if __name__ == "__main__":
    main()
