"""Compare causal model families and monthly retraining for the phase channel."""

import argparse
import hashlib
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.features import feature_columns, load_inputs
from ml.each_channel.train import assess, choose_threshold, write_json
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.metrics import precision_recall_curve

EDGES = (0, 1, 2, 3, 7, 14, 30, 60, 90, 180, 365)
COUNT_COLUMNS = ("n_events", "n_alarm", "n_fault", "n_normal", "n_unknown", "n_disabled")
PERIODS = {
    "train": ("2023-07-01", "2025-07-01"),
    "selection": ("2025-07-01", "2025-10-01"),
    "calibration": ("2025-10-01", "2026-01-01"),
    "test": ("2026-01-01", "2026-06-01"),
}
ALTERNATIVES = {
    "histogram_boosting": {
        "max_iter": 250,
        "learning_rate": 0.05,
        "max_leaf_nodes": 31,
        "l2_regularization": 30,
        "min_samples_leaf": 80,
        "random_state": 42,
    },
    "extra_trees": {
        "n_estimators": 300,
        "max_depth": 16,
        "min_samples_leaf": 30,
        "max_features": 0.7,
        "n_jobs": 6,
        "class_weight": None,
        "random_state": 42,
    },
}
WALK_PARAMETERS = {
    "iterations": 100,
    "depth": 6,
    "learning_rate": 0.07,
    "l2_leaf_reg": 15,
    "loss_function": "Logloss",
    "random_seed": 42,
    "thread_count": 6,
    "has_time": True,
    "verbose": False,
    "allow_writing_files": False,
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def add_disjoint_history(frame, daily):
    """Add strictly past daily bins and EWMs; a fact at T never enters a T feature."""
    result = []
    first = frame.date.min() - pd.Timedelta(days=max(EDGES))
    last = frame.date.max()
    grid_size = (last - first).days + 1
    by_day = {channel_id: rows for channel_id, rows in daily.groupby("channel_id")}
    for channel_id, block in frame.groupby("channel_id", sort=False):
        block = block.copy()
        positions = ((block.date - first) / pd.Timedelta(days=1)).astype(int).to_numpy()
        grid = np.zeros((len(COUNT_COLUMNS), grid_size), dtype=np.float32)
        rows = by_day.get(channel_id)
        if rows is not None:
            rows = rows.loc[rows.date.ge(first) & rows.date.le(last)]
            indices = ((rows.date - first) / pd.Timedelta(days=1)).astype(int).to_numpy()
            for row, name in enumerate(COUNT_COLUMNS):
                grid[row, indices] = rows[name].to_numpy(dtype=np.float32)
        cumulative = np.pad(grid.cumsum(axis=1, dtype=np.float64), ((0, 0), (1, 0)))
        for lower, upper in zip(EDGES[:-1], EDGES[1:], strict=True):
            left = np.maximum(positions - upper, 0)
            right = np.maximum(positions - lower, 0)
            values = cumulative[:, right] - cumulative[:, left]
            for row, name in enumerate(COUNT_COLUMNS):
                short = name.removeprefix("n_")
                block[f"bin_{short}_{lower}_{upper}d"] = np.log1p(values[row]).astype(np.float32)
                if name in ("n_events", "n_fault"):
                    active = np.pad((grid[row] > 0).cumsum(), (1, 0))
                    block[f"bin_{short}_days_{lower}_{upper}d"] = (
                        active[right] - active[left]
                    ).astype(np.float32)
        fault_days = pd.Series(grid[COUNT_COLUMNS.index("n_fault")] > 0)
        for span in (7, 30, 90, 180):
            ewm = fault_days.shift(1, fill_value=False).ewm(span=span, adjust=False).mean()
            block[f"fault_ewm_{span}d"] = ewm.to_numpy()[positions].astype(np.float32)
        result.append(block)
    return pd.concat(result).sort_values(["date", "channel_id"], kind="stable")


def subset(frame, name):
    start, end = PERIODS[name]
    return frame.loc[
        frame.date.ge(start)
        & frame.date.add(pd.Timedelta(days=30)).le(end)
        & frame.status.eq("scored")
        & frame.target.notna()
    ].copy()


def build_model(name):
    if name == "histogram_boosting":
        return HistGradientBoostingClassifier(**ALTERNATIVES[name])
    if name == "extra_trees":
        return ExtraTreesClassifier(**ALTERNATIVES[name])
    raise ValueError(f"Unknown model: {name}")


def rank(metrics):
    return (
        metrics["ap"] or 0,
        min((metrics["precision"] or 0) / 0.7, (metrics["recall"] or 0) / 0.5),
    )


def score_ceiling(frame, scores):
    """Post-hoc threshold diagnostic, never a deployable test metric."""
    precision, recall, thresholds = precision_recall_curve(frame.target, scores)
    eligible = recall[:-1] >= 0.5
    if not eligible.any():
        return {"precision": None, "recall": None, "threshold": None}
    index = int(np.argmax(np.where(eligible, precision[:-1], -1)))
    return {
        "precision": float(precision[index]),
        "recall": float(recall[index]),
        "threshold": float(thresholds[index]),
    }


def detailed(frame, scores, threshold, seen):
    scores = np.asarray(scores)
    thresholds = np.asarray(threshold)
    if thresholds.ndim == 0:
        thresholds = np.full(len(frame), thresholds.item())
    metrics = assess(frame, scores, thresholds)
    history = frame.channel_id.isin(seen).to_numpy()
    metrics["by_fault_history"] = {
        label: assess(frame.loc[mask], scores[mask], thresholds[mask])
        for label, mask in (("with_history", history), ("without_history", ~history))
    }
    metrics["by_month"] = {}
    scored = frame.assign(_score=scores, _threshold=thresholds)
    for month, rows in scored.groupby(scored.date.dt.to_period("M")):
        metrics["by_month"][str(month)] = assess(rows, rows._score, rows._threshold)
    return metrics


def load_incumbent(folder):
    frozen = json.loads((folder / "selection_frozen.json").read_text(encoding="utf-8"))
    spec = frozen["candidates"]["dense_base"]
    path = folder / "dense_base.cbm"
    model = CatBoostClassifier().load_model(str(path))
    if model.feature_names_ != spec["features"]:
        raise ValueError("Incumbent feature schema mismatch")
    return model, spec, sha256(path)


def alternative_study(args):
    started = time.perf_counter()
    if (args.out / "alternative_protocol.json").exists():
        raise FileExistsError("Use a fresh output directory; previous protocol is immutable")
    channels, daily, episodes = load_inputs(args.data_dir)
    frame = add_disjoint_history(pd.read_parquet(args.phase_frame), daily)
    features, _ = feature_columns(frame, "advanced")
    features = [name for name in features if frame[name].dtype != object]
    parts = {name: subset(frame, name) for name in PERIODS}
    train = parts["train"]
    selection = parts["selection"]
    candidates = {}
    fitted = {}
    for name in ALTERNATIVES:
        model = build_model(name)
        model.fit(train[features], train.target)
        scores = model.predict_proba(selection[features])[:, 1]
        threshold = choose_threshold(selection.target, scores)
        metrics = assess(selection, scores, threshold)
        candidates[name] = {"threshold": threshold, "metrics": metrics}
        fitted[name] = model
        print(name, metrics["precision"], metrics["recall"], metrics["ap"], flush=True)
    winner = max(candidates, key=lambda name: rank(candidates[name]["metrics"]))
    model = fitted[winner]
    model_path = args.out / f"{winner}.pickle"
    args.out.mkdir(parents=True, exist_ok=True)
    with model_path.open("wb") as stream:
        pickle.dump(model, stream)
    protocol = {
        "created_before_calibration_and_test_scoring": True,
        "label_boundary_rule": "T + 30 days <= period_end; outcome windows do not overlap stages",
        "period_counts": {
            name: {"rows": len(part), "positives": int(part.target.sum())}
            for name, part in parts.items()
        },
        "scope": "phase only",
        "periods": PERIODS,
        "feature_rule": "disjoint windows [T-upper,T-lower); all exclude T",
        "edges": EDGES,
        "count_columns": COUNT_COLUMNS,
        "alternative_parameters": ALTERNATIVES,
        "features": features,
        "selection_rule": "maximum Q3 2025 AP; tie by weaker normalized P/R",
        "winner": winner,
        "selection": candidates,
        "model_file": model_path.name,
        "model_sha256": sha256(model_path),
        "source_sha256": {
            "channels.csv": sha256(args.data_dir / "channels.csv"),
            "sensor_daily.parquet": sha256(args.data_dir / "sensor_daily.parquet"),
            "fault_episodes.parquet": sha256(args.data_dir / "fault_episodes.parquet"),
            "phase_frame": sha256(args.phase_frame),
        },
        "test_warning": "2026 was viewed in earlier research and is retrospective diagnostics",
    }
    write_json(args.out / "alternative_protocol.json", protocol)
    del fitted

    calibration = parts["calibration"]
    calibration_scores = model.predict_proba(calibration[features])[:, 1]
    threshold = choose_threshold(calibration.target, calibration_scores)
    frozen = {**protocol, "threshold": threshold}
    write_json(args.out / "alternative_frozen.json", frozen)

    test = parts["test"]
    test_scores = model.predict_proba(test[features])[:, 1]
    seen = set(
        episodes.loc[
            episodes.start.ge("2022-01-01")
            & episodes.start.lt("2025-07-01")
            & episodes.start_is_observed,
            "channel_id",
        ]
    )
    incumbent, incumbent_spec, incumbent_sha = load_incumbent(args.calendar_phase)
    incumbent_scores = incumbent.predict_proba(test[incumbent_spec["features"]])[:, 1]
    result = {
        "winner": winner,
        "threshold": threshold,
        "calibration": assess(calibration, calibration_scores, threshold),
        "test": detailed(test, test_scores, threshold, seen),
        "test_score_ceiling_post_hoc": score_ceiling(test, test_scores),
        "incumbent": {
            "model_sha256": incumbent_sha,
            "threshold": incumbent_spec["threshold"],
            "test": detailed(test, incumbent_scores, incumbent_spec["threshold"], seen),
        },
        "seconds": time.perf_counter() - started,
    }
    write_json(args.out / "alternative_summary.json", result)
    test.assign(score=test_scores).to_parquet(
        args.out / "alternative_test_scores.parquet", index=False
    )
    print("ALTERNATIVE TEST", result["test"], flush=True)


def walk_partitions(frame, month):
    """Split one walk-forward month using only labels mature at its start."""
    month = pd.Timestamp(month)
    stop = month + pd.offsets.MonthBegin()
    known_end = month - pd.Timedelta(days=30)
    calibration_start = known_end - pd.Timedelta(days=90)
    train_start = calibration_start - pd.Timedelta(days=730)
    train = frame.loc[
        frame.date.ge(train_start) & frame.date.add(pd.Timedelta(days=30)).le(calibration_start)
    ]
    calibration = frame.loc[
        frame.date.ge(calibration_start) & frame.date.add(pd.Timedelta(days=30)).le(month)
    ]
    prediction = frame.loc[frame.date.ge(month) & frame.date.lt(stop)].copy()
    return train, calibration, prediction


def walk_month(frame, month, features, categorical):
    month = pd.Timestamp(month)
    train, calibration, prediction = walk_partitions(frame, month)
    model = CatBoostClassifier(**WALK_PARAMETERS, cat_features=categorical)
    model.fit(train[features], train.target)
    calibration_scores = model.predict_proba(calibration[features])[:, 1]
    threshold = choose_threshold(calibration.target, calibration_scores)
    prediction["score"] = model.predict_proba(prediction[features])[:, 1]
    prediction["threshold"] = threshold
    audit = {
        "month": str(month.date()),
        "train_start": str(train.date.min().date()),
        "train_max_decision": str(train.date.max().date()),
        "train_max_outcome": str((train.date.max() + pd.Timedelta(days=30)).date()),
        "calibration_start": str(calibration.date.min().date()),
        "calibration_max_decision": str(calibration.date.max().date()),
        "calibration_max_outcome": str((calibration.date.max() + pd.Timedelta(days=30)).date()),
        "prediction_start": str(prediction.date.min().date()),
        "train_rows": len(train),
        "train_positives": int(train.target.sum()),
        "calibration_rows": len(calibration),
        "calibration_positives": int(calibration.target.sum()),
        "threshold": threshold,
    }
    return prediction, audit


def assess_variable_threshold(frame):
    return assess(frame, frame.score, frame.threshold)


def walk_forward_study(args):
    started = time.perf_counter()
    _, _, episodes = load_inputs(args.data_dir)
    frame = pd.read_parquet(args.phase_frame)
    frame = frame.loc[frame.status.eq("scored") & frame.target.notna()].sort_values(
        ["date", "channel_id"], kind="stable"
    )
    features, categorical = feature_columns(frame, "advanced")
    blocks = []
    audits = []
    for month in pd.date_range("2025-07-01", "2026-05-01", freq="MS"):
        block, audit = walk_month(frame, month, features, categorical)
        blocks.append(block)
        audits.append(audit)
        print("WALK", audit["month"], len(block), flush=True)
    scored = pd.concat(blocks, ignore_index=True)
    seen = set(
        episodes.loc[
            episodes.start.ge("2022-01-01")
            & episodes.start.lt("2025-07-01")
            & episodes.start_is_observed,
            "channel_id",
        ]
    )
    pretest = scored.loc[scored.date.lt("2026-01-01")].copy()
    test = scored.loc[scored.date.ge("2026-01-01")].copy()
    result = {
        "scope": "phase only",
        "features": features,
        "categorical": categorical,
        "parameters": WALK_PARAMETERS,
        "train_window_days": 730,
        "calibration_window_days": 90,
        "label_maturity_lag_days": 30,
        "months": audits,
        "pretest": assess_variable_threshold(pretest),
        "test": detailed(test, test.score, test.threshold, seen),
        "test_score_ceiling_post_hoc": score_ceiling(test, test.score),
        "seconds": time.perf_counter() - started,
        "test_warning": "2026 was viewed in earlier research and is retrospective diagnostics",
    }
    write_json(args.out / "walk_forward_summary.json", result)
    scored.to_parquet(args.out / "walk_forward_scores.parquet", index=False)
    print("WALK TEST", result["test"], flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("alternative", "walk-forward", "all"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--phase-frame", type=Path, required=True)
    parser.add_argument("--calendar-phase", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command in ("alternative", "all"):
        alternative_study(args)
    if args.command in ("walk-forward", "all"):
        walk_forward_study(args)


if __name__ == "__main__":
    main()
