"""Evaluate phase-specific power-state history without looking beyond decision time."""

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
from ml.each_channel.calendar_study import rank
from ml.each_channel.hard_channel_study import read_spec
from ml.each_channel.train import assess, choose_threshold, write_json

PERIODS = {
    "train": ("2022-01-01", "2025-01-01"),
    "threshold_a": ("2025-01-01", "2025-04-01"),
    "audit_a": ("2025-04-01", "2025-07-01"),
    "selection": ("2025-07-01", "2025-10-01"),
    "calibration": ("2025-10-01", "2026-01-01"),
    "test": ("2026-01-01", "2026-06-01"),
}
WINDOWS = (1, 7, 30, 90, 365)
CONTEXT_WINDOWS = (1, 7, 30)
POWER_FEATURES = [
    *(f"power_off_events_{window}d" for window in WINDOWS),
    *(f"power_on_events_{window}d" for window in WINDOWS),
    *(f"power_cycles_{window}d" for window in WINDOWS),
    *(f"power_off_fraction_{window}d" for window in WINDOWS),
    *(f"object_power_off_{window}d" for window in CONTEXT_WINDOWS),
    *(f"object_power_on_{window}d" for window in CONTEXT_WINDOWS),
    *(f"fleet_power_off_{window}d" for window in CONTEXT_WINDOWS),
    *(f"fleet_power_on_{window}d" for window in CONTEXT_WINDOWS),
    "days_since_power_off",
    "days_since_power_on",
    "power_state_age_hours",
    "last_power_state",
]
GENERIC_STATE_FEATURES = [
    *(
        f"{state}_{window}d"
        for state in ("n_unknown", "n_disabled", "n_normal")
        for window in WINDOWS[:-1]
    ),
    "days_since_event",
    "last_state",
]
PARAMS = {
    "iterations": 180,
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
RECIPES = {
    "power_base": {
        "features": BASE_FEATURES + POWER_FEATURES,
        "categorical": ["sensor_type", "last_power_state"],
        "since": "2022-01-01",
    },
    "power_context": {
        "features": BASE_FEATURES + POWER_FEATURES + GENERIC_STATE_FEATURES,
        "categorical": ["sensor_type", "last_power_state", "last_state"],
        "since": "2022-01-01",
    },
    "power_recent": {
        "features": BASE_FEATURES + POWER_FEATURES,
        "categorical": ["sensor_type", "last_power_state"],
        "since": "2024-01-01",
    },
}
OLD_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(points, dates, window):
    points = np.sort(np.asarray(points, dtype="datetime64[ns]"))
    dates = np.asarray(dates, dtype="datetime64[ns]")
    return np.searchsorted(points, dates, "left") - np.searchsorted(
        points, dates - np.timedelta64(window, "D"), "left"
    )


def recency(points, dates):
    points = np.sort(np.asarray(points, dtype="datetime64[ns]"))
    dates = np.asarray(dates, dtype="datetime64[ns]")
    positions = np.searchsorted(points, dates, "left")
    result = np.full(len(dates), np.nan)
    available = positions > 0
    if len(points):
        result[available] = (dates[available] - points[positions[available] - 1]) / np.timedelta64(
            1, "D"
        )
    return result


def load_power_events(data_dir, ids):
    blocks = []
    for path in sorted(data_dir.glob("events_*.parquet")):
        block = pd.read_parquet(path, columns=["sensor_id", "ts", "value"])
        block = block.loc[
            block.sensor_id.isin(ids) & block.value.isin(("Обесточен", "Есть питание"))
        ]
        blocks.append(block)
    return pd.concat(blocks, ignore_index=True).sort_values(["sensor_id", "ts"], kind="stable")


def add_power_features(frame, events, channels):
    """Add phase-state counts from intervals [T-window, T), excluding events exactly at T."""
    result = frame.reset_index(drop=True).copy()
    result["object_id_power"] = result.channel_id.map(channels.set_index("channel_id").object_id)
    events = events.rename(columns={"sensor_id": "channel_id"}).copy()
    events["object_id_power"] = events.channel_id.map(channels.set_index("channel_id").object_id)
    off = events.loc[events.value.eq("Обесточен")]
    on = events.loc[events.value.eq("Есть питание")]
    by_channel_off = {key: group.ts.to_numpy() for key, group in off.groupby("channel_id")}
    by_channel_on = {key: group.ts.to_numpy() for key, group in on.groupby("channel_id")}
    by_channel_all = {key: group for key, group in events.groupby("channel_id")}
    by_object_off = {key: group.ts.to_numpy() for key, group in off.groupby("object_id_power")}
    by_object_on = {key: group.ts.to_numpy() for key, group in on.groupby("object_id_power")}
    fleet_off, fleet_on = off.ts.to_numpy(), on.ts.to_numpy()

    numeric = [feature for feature in POWER_FEATURES if feature != "last_power_state"]
    for feature in numeric:
        result[feature] = np.nan
    result["last_power_state"] = "missing"

    for channel_id, positions in result.groupby("channel_id", sort=False).indices.items():
        dates = result.loc[positions, "date"].to_numpy(dtype="datetime64[ns]")
        own_off = by_channel_off.get(channel_id, np.array([], dtype="datetime64[ns]"))
        own_on = by_channel_on.get(channel_id, np.array([], dtype="datetime64[ns]"))
        for window in WINDOWS:
            off_count = counts(own_off, dates, window)
            on_count = counts(own_on, dates, window)
            result.loc[positions, f"power_off_events_{window}d"] = off_count
            result.loc[positions, f"power_on_events_{window}d"] = on_count
            result.loc[positions, f"power_cycles_{window}d"] = np.minimum(off_count, on_count)
            result.loc[positions, f"power_off_fraction_{window}d"] = off_count / np.maximum(
                off_count + on_count, 1
            )
        result.loc[positions, "days_since_power_off"] = recency(own_off, dates)
        result.loc[positions, "days_since_power_on"] = recency(own_on, dates)

        power = by_channel_all.get(channel_id)
        if power is not None:
            timestamps = power.ts.to_numpy(dtype="datetime64[ns]")
            location = np.searchsorted(timestamps, dates, "left")
            available = location > 0
            states = np.full(len(dates), "missing", dtype=object)
            states[available] = np.where(
                power.value.to_numpy()[location[available] - 1] == "Обесточен", "off", "on"
            )
            result.loc[positions, "last_power_state"] = states
            age = np.full(len(dates), np.nan)
            age[available] = (
                dates[available] - timestamps[location[available] - 1]
            ) / np.timedelta64(1, "h")
            result.loc[positions, "power_state_age_hours"] = age

        object_id = result.loc[positions[0], "object_id_power"]
        object_off = by_object_off.get(object_id, np.array([], dtype="datetime64[ns]"))
        object_on = by_object_on.get(object_id, np.array([], dtype="datetime64[ns]"))
        for window in CONTEXT_WINDOWS:
            result.loc[positions, f"object_power_off_{window}d"] = np.maximum(
                counts(object_off, dates, window) - counts(own_off, dates, window), 0
            )
            result.loc[positions, f"object_power_on_{window}d"] = np.maximum(
                counts(object_on, dates, window) - counts(own_on, dates, window), 0
            )
            result.loc[positions, f"fleet_power_off_{window}d"] = np.maximum(
                counts(fleet_off, dates, window) - counts(own_off, dates, window), 0
            )
            result.loc[positions, f"fleet_power_on_{window}d"] = np.maximum(
                counts(fleet_on, dates, window) - counts(own_on, dates, window), 0
            )
    return result.drop(columns="object_id_power")


def subset(frame, period):
    lo, hi = map(pd.Timestamp, PERIODS[period])
    mask = frame.date.ge(lo) & frame.date.add(pd.Timedelta(days=30)).le(hi)
    return frame.loc[mask & frame.status.eq("scored") & frame.target.notna()].copy()


def fit_model(frame, config):
    model = CatBoostClassifier(**PARAMS, cat_features=config["categorical"])
    model.fit(frame[config["features"]], frame.target)
    return model


def fit(args):
    if (args.out / "protocol.json").exists():
        raise ValueError("Use a new output directory")
    channels = pd.read_csv(args.data_dir / "channels.csv").rename(
        columns={
            "ид_канала_данных": "channel_id",
            "ид_объект": "object_id",
            "тип_датчика": "sensor_type",
        }
    )
    ids = channels.loc[channels.sensor_type.eq("Состояние фазы"), "channel_id"]
    frame = pd.read_parquet(args.phase_frame)
    events = load_power_events(args.data_dir, ids)
    frame = add_power_features(frame, events, channels).sort_values(
        ["date", "channel_id"], kind="stable"
    )
    parts = {period: subset(frame, period) for period in PERIODS}
    args.out.mkdir(parents=True, exist_ok=True)
    write_json(
        args.out / "protocol.json",
        {
            "created_before_test_scoring": True,
            "periods": PERIODS,
            "power_features": POWER_FEATURES,
            "recipes": RECIPES,
            "parameters": PARAMS,
            "old_weights": OLD_WEIGHTS,
            "selection": "recipe on Q2 after Q1 threshold; ensemble weight on Q3; threshold on Q4",
            "test": "2026-01-01 through 2026-05-02; previously viewed",
            "power_event_rows": len(events),
            "source_hashes": {
                "channels.csv": sha256(args.data_dir / "channels.csv"),
                "manifest.json": sha256(args.data_dir / "manifest.json"),
                "phase_advanced.parquet": sha256(args.phase_frame),
            },
        },
    )

    diagnostics = {}
    for recipe, config in RECIPES.items():
        train = parts["train"].loc[parts["train"].date.ge(config["since"])]
        model = fit_model(train, config)
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
    model = fit_model(refit, config)
    _, old_spec, old_model = read_spec(args.calendar_run, "phase")
    pretest = pd.concat([parts["selection"], parts["calibration"]], ignore_index=True).sort_values(
        ["date", "channel_id"], kind="stable"
    )
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

    model_path = args.out / "model.cbm"
    model.save_model(str(model_path))
    frozen = {
        "winner": winner,
        "features": config["features"],
        "categorical": config["categorical"],
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
    write_json(args.out / "selection_frozen.json", frozen)
    evaluation = pd.concat([parts["calibration"], parts["test"]], ignore_index=True).sort_values(
        ["date", "channel_id"], kind="stable"
    )
    evaluation.to_parquet(args.out / "evaluation.parquet", index=False)
    print(f"FROZEN phase {winner} old_weight={old_weight}", flush=True)


def evaluate(args):
    frozen = json.loads((args.out / "selection_frozen.json").read_text(encoding="utf-8"))
    model_path, old_path = Path(frozen["model_path"]), Path(frozen["old_model_path"])
    if sha256(model_path) != frozen["sha256"] or sha256(old_path) != frozen["old_sha256"]:
        raise ValueError("Model changed after freeze")
    model = CatBoostClassifier().load_model(str(model_path))
    old_model = CatBoostClassifier().load_model(str(old_path))
    frame = pd.read_parquet(args.out / "evaluation.parquet")
    new_scores = model.predict_proba(frame[frozen["features"]])[:, 1]
    old_scores = old_model.predict_proba(frame[frozen["old_features"]])[:, 1]
    scores = frozen["old_weight"] * old_scores + (1 - frozen["old_weight"]) * new_scores
    calibration = frame.date.lt("2026-01-01").to_numpy()
    test = frame.date.ge("2026-01-01").to_numpy()
    weight_ablation = {}
    for old_weight in OLD_WEIGHTS:
        ablation_scores = old_weight * old_scores + (1 - old_weight) * new_scores
        threshold = choose_threshold(frame.loc[calibration, "target"], ablation_scores[calibration])
        weight_ablation[str(old_weight)] = {
            "threshold": threshold,
            "calibration": assess(frame.loc[calibration], ablation_scores[calibration], threshold),
            "test": assess(frame.loc[test], ablation_scores[test], threshold),
        }
    report = {
        **frozen,
        "test": assess(frame.loc[test], scores[test], frozen["threshold"]),
        "new_only_test_at_selected_threshold": assess(
            frame.loc[test], new_scores[test], frozen["threshold"]
        ),
        "old_fixed_test": assess(frame.loc[test], old_scores[test], frozen["old_threshold"]),
        "weight_ablation_not_used_for_selection": weight_ablation,
    }
    report.pop("model_path")
    report.pop("old_model_path")
    write_json(args.out / "report.json", report)
    print(report["test"]["precision"], report["test"]["recall"])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("fit", "evaluate"), required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--phase-frame", type=Path)
    parser.add_argument("--calendar-run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "fit":
        for value in (args.data_dir, args.phase_frame, args.calendar_run):
            if value is None:
                parser.error("fit requires --data-dir, --phase-frame and --calendar-run")
        fit(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
