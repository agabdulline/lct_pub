"""Compare per-type controls with shared binary and horizon-bin CatBoost models."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.features import feature_columns
from ml.each_channel.train import assess, choose_threshold, write_json

KEYS = ("smoke", "phase", "manual_alarm")
PERIODS = {
    "train": ("2023-01-01", "2025-07-01"),
    "selection": ("2025-07-01", "2025-10-01"),
    "calibration": ("2025-10-01", "2026-01-01"),
    "test": ("2026-01-01", "2026-06-01"),
}
PARAMETERS = {
    "iterations": 160,
    "depth": 5,
    "learning_rate": 0.05,
    "l2_leaf_reg": 20,
    "has_time": True,
    "thread_count": 4,
    "random_seed": 42,
    "verbose": False,
    "allow_writing_files": False,
}
METADATA = ("channel_id", "date", "status", "target", "lead_days", "target_start")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def subset(frame, name, periods=None):
    """Require the entire label horizon to end before the next time block."""
    lower, upper = map(pd.Timestamp, (PERIODS if periods is None else periods)[name])
    return frame.loc[
        frame.date.ge(lower)
        & frame.date.add(pd.Timedelta(days=30)).le(upper)
        & frame.status.eq("scored")
        & frame.target.notna()
    ].copy()


def common_schema(frames):
    schemas = {key: feature_columns(frame, "advanced") for key, frame in frames.items()}
    first = schemas[KEYS[0]][0]
    common = set.intersection(*(set(names) for names, _ in schemas.values()))
    features = [name for name in first if name in common]
    categorical = [name for name in features if any(name in cats for _, cats in schemas.values())]
    if not features or "sensor_type" not in features:
        raise ValueError("Shared schema must contain sensor_type and common advanced features")
    return features, categorical


def horizon_classes(frame):
    """Encode the unchanged [T+1,T+30) endpoint using three positive lead bins."""
    positive = frame.target.eq(1).to_numpy()
    leads = frame.lead_days.to_numpy(dtype=float)
    if np.any(positive & (~np.isfinite(leads) | (leads < 1) | (leads >= 30))):
        raise ValueError("Positive targets require an observed lead in [1,30) days")
    classes = np.zeros(len(frame), dtype=np.int64)
    classes[positive] = np.where(leads[positive] < 7, 1, np.where(leads[positive] < 14, 2, 3))
    return classes


def positive_probability(probabilities, classes):
    """Sum positive classes by their labels, including when a class is absent."""
    probabilities = np.asarray(probabilities, dtype=float)
    classes = np.asarray(classes, dtype=int)
    if probabilities.ndim != 2 or probabilities.shape[1] != len(classes):
        raise ValueError("Probability columns must match model class labels")
    return probabilities[:, classes > 0].sum(axis=1)


def type_balanced_weights(types):
    types = pd.Series(np.asarray(types))
    counts = types.value_counts()
    return (len(types) / (len(counts) * types.map(counts))).to_numpy(dtype=float)


def candidate_rank(metrics):
    return (
        int(metrics["passes_both"]),
        min((metrics["precision"] or 0) / 0.7, (metrics["recall"] or 0) / 0.5),
        metrics["ap"] or 0,
    )


def load_frames(sources, features=None, categorical=None):
    frames = {}
    for key in KEYS:
        source = sources[key]
        path = Path(source["path"])
        if sha256(path) != source["sha256"]:
            raise ValueError(f"Input SHA256 changed: {key}")
        frame = pd.read_parquet(path)
        frame["date"] = pd.to_datetime(frame.date)
        frame = frame.sort_values(["date", "channel_id"], kind="stable").reset_index(drop=True)
        if frame.duplicated(["channel_id", "date"]).any():
            raise ValueError(f"Duplicate decisions: {key}")
        frames[key] = frame
    if features is None:
        features, categorical = common_schema(frames)
    for frame in frames.values():
        if not set(features).issubset(frame.columns):
            raise ValueError("Frozen features are missing from an input")
        for name in categorical:
            frame[name] = frame[name].fillna("missing").astype(str)
    return frames, features, categorical


def score(model, frame, features):
    if not len(frame):
        return np.empty(0, dtype=float)
    return positive_probability(model.predict_proba(frame[features]), model.classes_)


def detailed(frame, scores, threshold, seen_objects):
    result = assess(frame, scores, threshold)
    history = frame.onsets_365d.gt(0).to_numpy()
    result["by_fault_history_365d"] = {
        name: assess(frame.loc[mask], np.asarray(scores)[mask], threshold)
        for name, mask in (("with_history", history), ("without_history", ~history))
    }
    if "object_key" in frame:
        known = frame.object_key.astype(str).isin(seen_objects).to_numpy()
        result["by_object_in_type_training"] = {
            name: assess(frame.loc[mask], np.asarray(scores)[mask], threshold)
            for name, mask in (("known", known), ("new", ~known))
        }
    result["by_month"] = {}
    for month in frame.date.dt.to_period("M").unique():
        mask = frame.date.dt.to_period("M").eq(month).to_numpy()
        result["by_month"][str(month)] = assess(
            frame.loc[mask], np.asarray(scores)[mask], threshold
        )
    return result


def fit(args):
    started = time.perf_counter()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("Use a new empty output directory")
    sources = {
        key: {"path": str(path.resolve()), "sha256": sha256(path)}
        for key, path in zip(
            KEYS, (args.smoke_frame, args.phase_frame, args.manual_frame), strict=True
        )
    }
    frames, features, categorical = load_frames(sources)
    protocol = {
        "hypothesis": "Cross-type sharing with binary and lead-bin supervision",
        "scope": list(KEYS),
        "sources": sources,
        "periods": PERIODS,
        "period_rule": "start <= T and T+30 days <= end; scored rows with known target only",
        "sampling": "Cached dates unchanged: historical Mondays and dense 2025/2026",
        "parameters": PARAMETERS,
        "features": features,
        "categorical": categorical,
        "models": [
            "local_smoke",
            "local_phase",
            "local_manual_alarm",
            "pooled_binary",
            "pooled_horizon",
        ],
        "pooled_weights": "Equal total training weight per type; no class balancing",
        "horizon_classes": {"0": "no onset", "1": "[1,7)", "2": "[7,14)", "3": "[14,30)"},
        "selection_rule": "Q3 per type: passes_both, min(P/.7,R/.5), AP; threshold from Q3 only",
        "threshold_rule": "Chosen candidate and local control calibrated separately on Q4 only",
        "history_rule": "Observed onsets in [T-365d,T); known object appeared in that type training",
        "test_warning": "2026 is previously viewed retrospective diagnostics, not a blind test",
    }
    write_json(args.out / "protocol.json", protocol)
    partitions = {
        key: {name: subset(frame, name) for name in ("train", "selection", "calibration")}
        for key, frame in frames.items()
    }
    for key, parts in partitions.items():
        if any(part.empty for part in parts.values()) or parts["train"].target.nunique() < 2:
            raise ValueError(f"Insufficient training classes or empty stage: {key}")
    trained, specifications = {}, {}
    for key in KEYS:
        name = f"local_{key}"
        train = partitions[key]["train"]
        model = CatBoostClassifier(**PARAMETERS, loss_function="Logloss", cat_features=categorical)
        model.fit(train[features], train.target)
        path = args.out / f"{name}.cbm"
        model.save_model(str(path))
        trained[name] = model
        specifications[name] = {
            "file": path.name,
            "sha256": sha256(path),
            "classes": model.classes_.tolist(),
        }
        print(f"Fitted {name}: {len(train)} decisions", flush=True)
    pooled = pd.concat(
        [partitions[key]["train"].assign(_study_type=key) for key in KEYS], ignore_index=True
    ).sort_values(["date", "channel_id"], kind="stable")
    weights = type_balanced_weights(pooled._study_type)
    for name, loss in (("pooled_binary", "Logloss"), ("pooled_horizon", "MultiClass")):
        target = pooled.target if loss == "Logloss" else horizon_classes(pooled)
        model = CatBoostClassifier(**PARAMETERS, loss_function=loss, cat_features=categorical)
        model.fit(pooled[features], target, sample_weight=weights)
        path = args.out / f"{name}.cbm"
        model.save_model(str(path))
        trained[name] = model
        specifications[name] = {
            "file": path.name,
            "sha256": sha256(path),
            "classes": model.classes_.tolist(),
        }
        print(f"Fitted {name}: {len(pooled)} decisions", flush=True)
    choices = {}
    for key, parts in partitions.items():
        candidates = {}
        for name in (f"local_{key}", "pooled_binary", "pooled_horizon"):
            scores = score(trained[name], parts["selection"], features)
            threshold = choose_threshold(parts["selection"].target, scores)
            candidates[name] = {
                "threshold": threshold,
                "metrics": assess(parts["selection"], scores, threshold),
            }
        chosen = max(candidates, key=lambda name: candidate_rank(candidates[name]["metrics"]))
        calibrated = {}
        for name in dict.fromkeys((chosen, f"local_{key}")):
            scores = score(trained[name], parts["calibration"], features)
            threshold = choose_threshold(parts["calibration"].target, scores)
            calibrated[name] = {
                "threshold": threshold,
                "metrics": assess(parts["calibration"], scores, threshold),
            }
        train = parts["train"]
        choices[key] = {
            "selected": chosen,
            "selection": candidates,
            "calibration": calibrated,
            "train_decisions": len(train),
            "train_positives": int(train.target.sum()),
            "seen_objects": sorted(train.object_key.astype(str).unique().tolist())
            if "object_key" in train
            else [],
        }
        print(f"Selected {key}: {chosen}", flush=True)
    frozen = {
        "protocol_sha256": sha256(args.out / "protocol.json"),
        "models": specifications,
        "choices": choices,
        "fit_seconds": time.perf_counter() - started,
        "frozen_before_test_scoring": True,
    }
    write_json(args.out / "frozen.json", frozen)


def evaluate(args):
    started = time.perf_counter()
    if (args.out / "summary.json").exists() or list(args.out.glob("test_*.parquet")):
        raise ValueError("Test was already evaluated; do not overwrite results")
    protocol_path = args.out / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    frozen = json.loads((args.out / "frozen.json").read_text(encoding="utf-8"))
    if sha256(protocol_path) != frozen["protocol_sha256"]:
        raise ValueError("Protocol SHA256 changed")
    frames, features, _ = load_frames(
        protocol["sources"], protocol["features"], protocol["categorical"]
    )
    models = {}
    for name, specification in frozen["models"].items():
        path = args.out / specification["file"]
        if sha256(path) != specification["sha256"]:
            raise ValueError(f"Model SHA256 changed: {name}")
        models[name] = CatBoostClassifier().load_model(str(path))
        if models[name].feature_names_ != features:
            raise ValueError(f"Model schema changed: {name}")
    results = {}
    for key in KEYS:
        test = subset(frames[key], "test", protocol["periods"])
        if test.empty:
            raise ValueError(f"No fully observed test decisions: {key}")
        choice = frozen["choices"][key]
        output = test[list(METADATA)].copy()
        assessments = {}
        for role, name in (("selected", choice["selected"]), ("local_control", f"local_{key}")):
            scores = score(models[name], test, features)
            threshold = choice["calibration"][name]["threshold"]
            assessments[role] = {
                "model": name,
                "threshold": threshold,
                "metrics": detailed(test, scores, threshold, choice["seen_objects"]),
            }
            output[f"{role}_score"] = scores
            output[f"{role}_alert"] = scores >= threshold
        output.to_parquet(args.out / f"test_{key}.parquet", index=False)
        results[key] = assessments
        print(key, assessments["selected"]["metrics"], flush=True)
    write_json(
        args.out / "summary.json",
        {
            "test_warning": protocol["test_warning"],
            "frozen_sha256": sha256(args.out / "frozen.json"),
            "results": results,
            "evaluate_seconds": time.perf_counter() - started,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fitting = commands.add_parser("fit")
    for name in ("smoke", "phase", "manual"):
        fitting.add_argument(f"--{name}-frame", type=Path, required=True)
    fitting.add_argument("--out", type=Path, required=True)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    (fit if args.command == "fit" else evaluate)(args)


if __name__ == "__main__":
    main()
