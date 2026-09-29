"""Audit drift-aware rules and guard prospective evaluation of hard channels."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.advanced import add_advanced
from ml.each_channel.calendar_study import CALENDAR, calendar_features, rank
from ml.each_channel.features import DATA_END, GAPS, build_features, load_inputs
from ml.each_channel.train import assess, choose_threshold, write_json
from sklearn.metrics import precision_recall_curve

TYPES = {
    "Датчик дыма": "smoke",
    "Состояние фазы": "phase",
    "Ручной извещатель": "manual_alarm",
}
MODEL_TAGS = {
    "smoke": "incumbent",
    "phase": "dense_base",
    "manual_alarm": "dense_calendar",
}
WINDOWS = (30, 60, 90, 180, 365)
READINESS_MINIMUM = 1500
HOLDOUT_START = pd.Timestamp("2026-05-03")
HOLDOUT_END = pd.Timestamp("2026-06-01")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_spec(calendar_run, key):
    frozen = json.loads((calendar_run / key / "selection_frozen.json").read_text(encoding="utf-8"))
    tag = MODEL_TAGS[key]
    spec = frozen["candidates"][tag]
    path = Path(spec["path"])
    if sha256(path) != spec["sha256"]:
        raise ValueError(f"Model SHA256 mismatch: {path}")
    model = CatBoostClassifier().load_model(str(path))
    if model.feature_names_ != spec["features"]:
        raise ValueError(f"Feature schema mismatch: {path}")
    return tag, spec, model


def scored_frame(key, args, calendar, spec, model):
    if key == "manual_alarm" and args.manual_frame is None:
        channels, daily, episodes = load_inputs(args.data_dir)
        ids = channels.loc[channels.sensor_type.eq("Ручной извещатель"), "channel_id"]
        dates = pd.date_range("2022-01-03", "2024-12-30", freq="W-MON").union(
            pd.date_range("2025-01-01", "2026-05-02")
        )
        frame = build_features(daily, episodes, channels, dates, ids)
    else:
        path = (
            args.manual_frame
            if key == "manual_alarm"
            else args.features_dir / f"{key}_advanced.parquet"
        )
        frame = pd.read_parquet(path)
    frame = frame.sort_values(["date", "channel_id"], kind="stable")
    if any(feature in CALENDAR for feature in spec["features"]):
        frame = frame.merge(calendar, on="date", how="left", validate="many_to_one")
    frame["score"] = model.predict_proba(frame[spec["features"]])[:, 1]
    return frame.loc[frame.status.eq("scored") & frame.target.notna()].copy()


def matured_calibration(frame, decision_date, window):
    """Return decisions whose complete 30-day outcomes were known at decision_date."""
    known_end = pd.Timestamp(decision_date) - pd.Timedelta(days=30)
    return frame.loc[
        frame.date.ge(known_end - pd.Timedelta(days=window)) & frame.date.lt(known_end)
    ]


def label_complete(dates, data_end=DATA_END, gaps=GAPS):
    """Return whether each 30-day target window is fully observed."""
    dates = pd.DatetimeIndex(pd.to_datetime(dates))
    horizon_end = dates + pd.Timedelta(days=30)
    complete = horizon_end <= pd.Timestamp(data_end)
    for start, stop in gaps:
        complete &= ~((dates < pd.Timestamp(stop)) & (horizon_end > pd.Timestamp(start)))
    return np.asarray(complete)


def monthly_scores(frame, start, end, window, readiness_minimum=0):
    blocks = []
    for month in pd.date_range(start, end, freq="MS"):
        stop = month + pd.offsets.MonthBegin()
        calibration = matured_calibration(frame, month, window)
        block = frame.loc[frame.date.ge(month) & frame.date.lt(stop)].copy()
        block["threshold"] = choose_threshold(calibration.target, calibration.score)
        block["matured_positives"] = int(calibration.target.sum())
        block["eligible"] = block.matured_positives.ge(readiness_minimum)
        blocks.append(block)
    result = pd.concat(blocks, ignore_index=True)
    scores = result.score.where(result.eligible, -1)
    return assess(result, scores, result.threshold), result


def score_ceiling(frame):
    """Retrospective upper bound for thresholding the fixed score, never a test metric."""
    precision, recall, thresholds = precision_recall_curve(frame.target, frame.score)
    eligible = recall[:-1] >= 0.5
    index = int(np.argmax(np.where(eligible, precision[:-1], -1)))
    return {
        "precision": float(precision[index]),
        "recall": float(recall[index]),
        "threshold": float(thresholds[index]),
    }


def freeze(args):
    if (args.out / "protocol.json").exists():
        raise ValueError("Use a new output directory")
    calendar, sources = calendar_features(args.calendar_dir)
    result = {
        "created_before_holdout_build": True,
        "holdout": [str(HOLDOUT_START.date()), "2026-05-31"],
        "holdout_note": (
            "May 3-31 was not present in earlier feature/test tables, but its labels are "
            "incomplete because every 30-day window crosses the June 1-2 source gap"
        ),
        "holdout_label_complete": bool(
            label_complete(pd.date_range(HOLDOUT_START, HOLDOUT_END - pd.Timedelta(days=1))).all()
        ),
        "windows": WINDOWS,
        "selection": "adaptive threshold window selected on July-December 2025",
        "label_availability": "threshold window ends 30 days before prediction month",
        "manual_readiness_minimum": READINESS_MINIMUM,
        "manual_readiness_warning": (
            "chosen after viewing Jan-May 2, 2026; candidate requires the unused holdout"
        ),
        "calendar_sources": sources,
        "models": {},
    }
    for key in TYPES.values():
        tag, spec, model = read_spec(args.calendar_run, key)
        frame = scored_frame(key, args, calendar, spec, model)
        candidates = {}
        for window in WINDOWS:
            metrics, _ = monthly_scores(frame, "2025-07-01", "2025-12-01", window)
            candidates[str(window)] = metrics
        window = max(WINDOWS, key=lambda value: rank(candidates[str(value)]))
        retrospective, _ = monthly_scores(
            frame,
            "2026-01-01",
            "2026-05-01",
            window,
            READINESS_MINIMUM if key == "manual_alarm" else 0,
        )
        known = matured_calibration(frame, pd.Timestamp("2026-05-01"), window)
        result["models"][key] = {
            "tag": tag,
            "path": str(Path(spec["path"]).resolve()),
            "sha256": spec["sha256"],
            "features": spec["features"],
            "saved_threshold": spec["threshold"],
            "adaptive_window": window,
            "selection_candidates": candidates,
            "may_threshold": choose_threshold(known.target, known.score),
            "may_matured_positives": int(known.target.sum()),
            "retrospective_2026_through_may_2": retrospective,
            "fixed_score_ceiling_2026_through_may_2": score_ceiling(
                frame.loc[frame.date.ge("2026-01-01") & frame.date.lt(HOLDOUT_START)]
            ),
        }
    write_json(args.out / "protocol.json", result)
    write_json(args.out / "all_frozen.json", {"keys": list(TYPES.values())})


def build_holdout(key, args, channels, daily, episodes):
    name = next(name for name, value in TYPES.items() if value == key)
    ids = channels.loc[channels.sensor_type.eq(name), "channel_id"]
    dates = pd.date_range(HOLDOUT_START, HOLDOUT_END - pd.Timedelta(days=1))
    frame = build_features(daily, episodes, channels, dates, ids)
    if key == "smoke":
        frame = add_advanced(frame, daily, episodes, channels)
    return frame.loc[frame.status.eq("scored") & frame.target.notna()].copy()


def evaluate(args):
    protocol = json.loads((args.out / "protocol.json").read_text(encoding="utf-8"))
    keys = json.loads((args.out / "all_frozen.json").read_text(encoding="utf-8"))["keys"]
    channels, daily, episodes = load_inputs(args.data_dir)
    calendar, _ = calendar_features(args.calendar_dir)
    reports = []
    dates = pd.date_range(HOLDOUT_START, HOLDOUT_END - pd.Timedelta(days=1))
    if not label_complete(dates).all():
        raise ValueError(
            "The requested holdout has incomplete 30-day labels: May 3-31 crosses "
            "the June 1-2 source gap. Wait for a later fully observed period."
        )
    for key in keys:
        frozen = protocol["models"][key]
        path = Path(frozen["path"])
        if sha256(path) != frozen["sha256"]:
            raise ValueError(f"Model changed after freeze: {key}")
        model = CatBoostClassifier().load_model(str(path))
        frame = build_holdout(key, args, channels, daily, episodes)
        if any(feature in CALENDAR for feature in frozen["features"]):
            frame = frame.merge(calendar, on="date", how="left", validate="many_to_one")
        scores = model.predict_proba(frame[frozen["features"]])[:, 1]
        eligible = (
            frozen["may_matured_positives"] >= READINESS_MINIMUM if key == "manual_alarm" else True
        )
        candidate_scores = scores if eligible else np.full(len(frame), -1.0)
        report = {
            "key": key,
            "dates": [str(frame.date.min().date()), str(frame.date.max().date())],
            "eligible": eligible,
            "candidate": assess(frame, candidate_scores, frozen["may_threshold"]),
            "fixed": assess(frame, scores, frozen["saved_threshold"]),
        }
        reports.append(report)
        frame.assign(score=scores).to_parquet(
            args.out / f"{key}_holdout_scored.parquet", index=False
        )
        print(key, report["candidate"]["precision"], report["candidate"]["recall"])
    write_json(args.out / "summary.json", reports)


def diagnose(args):
    """Write post-hoc diagnostics for the already viewed 2026 test period."""
    protocol = json.loads((args.out / "protocol.json").read_text(encoding="utf-8"))
    calendar, _ = calendar_features(args.calendar_dir)
    reports = []
    for key in TYPES.values():
        tag, spec, model = read_spec(args.calendar_run, key)
        frame = scored_frame(key, args, calendar, spec, model)
        test = frame.loc[frame.date.ge("2026-01-01") & frame.date.lt(HOLDOUT_START)].copy()
        frozen = protocol["models"][key]
        adaptive, adaptive_rows = monthly_scores(
            frame,
            "2026-01-01",
            "2026-05-01",
            frozen["adaptive_window"],
        )
        result = {
            "key": key,
            "model": tag,
            "dates": [str(test.date.min().date()), str(test.date.max().date())],
            "fixed": assess(test, test.score, frozen["saved_threshold"]),
            "adaptive_threshold": adaptive,
            "fixed_score_ceiling_post_hoc": score_ceiling(test),
        }
        if key == "manual_alarm":
            result["readiness_sweep_post_hoc"] = {
                str(minimum): monthly_scores(
                    frame,
                    "2026-01-01",
                    "2026-05-01",
                    frozen["adaptive_window"],
                    minimum,
                )[0]
                for minimum in (0, 500, 1000, 1500, 1750, 2000)
            }
            result["readiness_1500_by_month_post_hoc"] = {
                str(month): assess(
                    group,
                    group.score.where(group.eligible, -1),
                    group.threshold,
                )
                for month, group in adaptive_rows.assign(
                    eligible=adaptive_rows.matured_positives.ge(READINESS_MINIMUM)
                ).groupby(adaptive_rows.date.dt.to_period("M"))
            }
        reports.append(result)
    write_json(args.out / "diagnostics.json", reports)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("freeze", "diagnose", "evaluate"), required=True)
    parser.add_argument("--features-dir", type=Path)
    parser.add_argument("--manual-frame", type=Path)
    parser.add_argument("--calendar-run", type=Path, required=True)
    parser.add_argument("--calendar-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.phase in ("freeze", "diagnose"):
        if args.features_dir is None:
            parser.error(f"{args.phase} requires --features-dir")
        if args.manual_frame is None and args.data_dir is None:
            parser.error(f"{args.phase} requires --manual-frame or --data-dir")
    if args.phase == "freeze":
        freeze(args)
    elif args.phase == "diagnose":
        diagnose(args)
    else:
        if args.data_dir is None:
            parser.error("evaluate requires --data-dir")
        evaluate(args)


if __name__ == "__main__":
    main()
