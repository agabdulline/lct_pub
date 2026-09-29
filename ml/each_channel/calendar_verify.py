"""Verify new candidate scores from as-of history and original v0 inputs."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.baseline.daily import build_daily
from ml.each_channel.calendar_study import calendar_features, read_model
from ml.each_channel.features import build_features, load_inputs
from ml.each_channel.train import write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--calendar-dir", type=Path, required=True)
    p.add_argument("--baseline-dir", type=Path, default=Path("ml/models/baseline-v0-daily"))
    p.add_argument("--date", default="2026-02-01")
    args = p.parse_args()
    started = time.perf_counter()
    t = pd.Timestamp(args.date)
    rows = json.loads((args.run / "summary.json").read_text(encoding="utf-8"))
    channels, daily, episodes = load_inputs(args.data_dir)
    past_daily = daily.loc[daily.date.lt(t)].copy()
    past_eps = episodes.loc[episodes.start.lt(t)].copy()
    cfg, vmodel = read_model(args.baseline_dir, "daily.cbm")
    supported = channels.loc[channels.sensor_type.isin(cfg["types"])]
    base = build_daily(
        past_daily, past_eps[past_eps.channel_id.isin(supported.channel_id)], supported, [t]
    )
    base["score"] = vmodel.predict_proba(base[cfg["features"]])[:, 1]
    base = base.set_index("channel_id")
    wanted_types = [r["name"] for r in rows if r["winner"] in ("dense_base", "dense_calendar")]
    ids = channels.loc[channels.sensor_type.isin(wanted_types), "channel_id"].tolist()
    past_eps.loc[past_eps.end.ge(t), "end"] = pd.NaT
    f = build_features(past_daily, past_eps, channels, [t], ids, labels=False)
    cal, _ = calendar_features(args.calendar_dir)
    f = f.merge(cal, on="date", validate="many_to_one").set_index("channel_id")
    result = {"date": str(t), "models_hashed": 0, "comparisons": []}
    for row in rows:
        for spec in row["candidates"].values():
            path = Path(spec["path"])
            assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]
            result["models_hashed"] += 1
        saved = pd.read_parquet(args.run / row["key"] / "test_scored.parquet")
        saved = saved.loc[saved.date.eq(t)].set_index("channel_id")
        if row["name"] in cfg["types"]:
            expected = saved.v0_score.to_numpy()
            actual = base.loc[saved.index, "score"].to_numpy()
            delta = float(np.max(np.abs(expected - actual))) if len(saved) else 0.0
            assert delta < 1e-12, (row["key"], "v0", delta)
            result["comparisons"].append(
                {"key": row["key"], "model": "v0", "decisions": len(saved), "max_difference": delta}
            )
        if row["winner"] in ("dense_base", "dense_calendar"):
            tag = row["winner"]
            spec = row["candidates"][tag]
            model = CatBoostClassifier().load_model(spec["path"])
            actual = model.predict_proba(f.loc[saved.index, spec["features"]])[:, 1]
            expected = saved[f"score_{tag}"].to_numpy()
            delta = float(np.max(np.abs(expected - actual))) if len(saved) else 0.0
            assert delta < 1e-12, (row["key"], tag, delta)
            result["comparisons"].append(
                {"key": row["key"], "model": tag, "decisions": len(saved), "max_difference": delta}
            )
    result["verification_seconds"] = time.perf_counter() - started
    write_json(args.run / "verification.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
