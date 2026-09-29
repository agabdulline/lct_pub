"""Verify source totals, frozen bundles, and real as-of prediction parity."""

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

import catboost
import duckdb
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.advanced import add_advanced
from ml.each_channel.features import build_features, load_inputs
from ml.each_channel.train import write_json


def verify(data, folder, cache):
    started = time.perf_counter()
    channels, daily, episodes = load_inputs(data)
    result = {
        "source_load_seconds": time.perf_counter() - started,
        "python": platform.python_version(),
        "catboost": catboost.__version__,
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "models": {},
        "source_sha256": {},
        "input_sha256": {},
    }
    for name in ("channels.csv", "sensor_daily.parquet", "fault_episodes.parquet"):
        with (data / name).open("rb") as stream:
            result["input_sha256"][name] = hashlib.file_digest(stream, "sha256").hexdigest()
    for path in [*folder.glob("*.py"), folder.parent / "baseline/daily.py"]:
        result["source_sha256"][str(path.relative_to(folder.parent))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    result["data_manifest"] = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    result["data_range"] = [str(daily.date.min()), str(daily.date.max())]
    result["source_distribution"] = []
    for name, catalog in channels.groupby("sensor_type"):
        group = daily.loc[daily.channel_id.isin(catalog.channel_id)]
        result["source_distribution"].append(
            {
                "name": name,
                "catalog_channels": len(catalog),
                "active_channels": int(group.channel_id.nunique()),
                "daily_rows": len(group),
                "events": int(group.n_events.sum()),
                "fault_messages": int(group.n_fault.sum()),
            }
        )
    con = duckdb.connect()
    event_paths = str(data / "events_*.parquet").replace("\\", "/").replace("'", "''")
    control = con.execute(
        f"SELECT count(*), sum((value='Неисправен')::INT) FROM read_parquet('{event_paths}')"
    ).fetchone()
    restored = json.loads((data / "manifest.json").read_text(encoding="utf-8"))["restored_types"]
    extra = daily.loc[
        daily.channel_id.isin(channels.loc[channels.sensor_type.isin(restored), "channel_id"])
    ]
    actual = [int(extra.n_events.sum()), int(extra.n_fault.sum())]
    if list(control) != actual:
        raise AssertionError(f"Source aggregates do not reconcile: {control} != {actual}")
    result["restored_event_count"] = actual[0]
    result["restored_fault_messages"] = actual[1]
    configs = [
        (p, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted((folder / "models").glob("*/config.json"))
    ]
    for config_path, cfg in configs:
        if cfg["status"] != "trained":
            continue
        model_path = config_path.parent / "model.cbm"
        assert hashlib.sha256(model_path.read_bytes()).hexdigest() == cfg["sha256"]
        frozen = json.loads(
            (config_path.parent / "selection_frozen.json").read_text(encoding="utf-8")
        )
        assert frozen["threshold"] == cfg["threshold"]
        assert frozen["selected"] == cfg["selected"]
        model = CatBoostClassifier().load_model(str(model_path))
        assert model.feature_names_ == cfg["selected"]["features"]
        ids = channels.loc[channels.sensor_type.eq(cfg["name"]), "channel_id"].tolist()
        stored = pd.read_parquet(cache / f"{cfg['key']}_test.parquet")
        parity = []
        for date in ("2026-01-01", "2026-03-01", "2026-05-01"):
            score_started = time.perf_counter()
            snapshot = pd.Timestamp(date)
            past_ep = episodes.loc[episodes.start.lt(snapshot)].copy()
            past_ep.loc[past_ep.end.ge(snapshot), "end"] = pd.NaT
            rows = build_features(
                daily.loc[daily.date.lt(snapshot)],
                past_ep,
                channels,
                [snapshot],
                ids,
                labels=False,
            )
            if "last_state" in cfg["selected"]["features"]:
                rows = add_advanced(rows, daily.loc[daily.date.lt(snapshot)], past_ep, channels)
            rows = rows.loc[rows.status.eq("scored")].set_index("channel_id").sort_index()
            expected = stored.loc[stored.date.eq(snapshot)].set_index("channel_id").sort_index()
            assert rows.index.equals(expected.index)
            score = model.predict_proba(rows[cfg["selected"]["features"]])[:, 1]
            delta = float(np.max(np.abs(score - expected.score.to_numpy()))) if len(rows) else 0.0
            assert delta <= 1e-12, (cfg["key"], date, delta)
            parity.append(
                {
                    "date": date,
                    "rows": len(rows),
                    "max_score_difference": delta,
                    "build_and_score_seconds": time.perf_counter() - score_started,
                }
            )
        result["models"][cfg["key"]] = {
            "sha256_ok": True,
            "frozen_selection_ok": True,
            "parity": parity,
        }
        print(f"verified {cfg['key']}", flush=True)
    result["checks"] = [
        "source totals",
        "model hashes",
        "frozen selection",
        "feature schema",
        "as-of parity: three dates",
    ]
    write_json(folder / "verification.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/interim/each_channel/source"))
    parser.add_argument("--folder", type=Path, default=Path("ml/each_channel"))
    parser.add_argument("--cache", type=Path, default=Path("data/interim/each_channel/features"))
    args = parser.parse_args()
    verify(args.data_dir, args.folder, args.cache)
