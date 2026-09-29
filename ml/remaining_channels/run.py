"""Train a fixed daily CatBoost recipe for channel types omitted by each_channel."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
from catboost import CatBoostClassifier

from ml.each_channel.features import TYPE_KEYS, build_features, load_inputs
from ml.each_channel.train import assess, fit_type, split, write_json

KEYS = {
    "9-секционный люк": "nine_section_hatch",
    "Газовый датчик": "gas",
    "Датчик затопления": "flood",
    "ИБП": "ups",
    "КД АВ": "kd_av",
    "КД Люк": "kd_hatch",
    "Ручной извещатель": "manual_alarm",
    "Состояние вентилятора": "fan",
    "Состояние насоса": "pump",
    "Состояние охраны": "security",
    "Стекло": "glass",
    "Тепловой датчик": "heat",
}
RECIPE = {
    "name": "fixed_base_d6_120",
    "mode": "base",
    "depth": 6,
    "l2": 10,
    "weight": "None",
    "since": "2022-01-01",
    "fixed_trees": 120,
}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--groups", nargs="+")
    args = parser.parse_args()
    channels, daily, episodes = load_inputs(args.data_dir)
    assert set(KEYS) == set(channels.sensor_type) - set(TYPE_KEYS)
    config = json.loads((args.baseline_dir / "config.json").read_text(encoding="utf-8"))
    baseline_file = args.baseline_dir / "daily.cbm"
    assert hashlib.sha256(baseline_file.read_bytes()).hexdigest() == config["sha256"]
    baseline = CatBoostClassifier().load_model(str(baseline_file))
    assert baseline.feature_names_ == config["features"]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "scores").mkdir(parents=True, exist_ok=True)
    write_json(
        args.out / "protocol.json",
        {
            "recipe": RECIPE,
            "selection": "one prespecified recipe; threshold calibrated on Q4 2025",
            "test": "2026-01-01 through 2026-05-02, 30-day purge",
            "types": KEYS,
            "baseline_sha256": config["sha256"],
            "source": str(args.data_dir),
        },
    )
    dates = pd.date_range("2022-01-03", "2024-12-30", freq="W-MON").union(
        pd.date_range("2025-01-01", "2026-05-02", freq="D")
    )
    total = channels.channel_id.nunique()
    for name, key in KEYS.items():
        if args.groups and key not in args.groups:
            continue
        folder = args.out / key
        print(f"Building {key}: {name}", flush=True)
        ids = channels.loc[channels.sensor_type.eq(name), "channel_id"].tolist()
        frame = build_features(daily, episodes, channels, dates, ids)
        test = split(frame, "test")
        if name in config["types"]:
            score = baseline.predict_proba(test[config["features"]])[:, 1]
            compare = assess(test, score, config["threshold"])
            write_json(folder / "baseline_comparison.json", compare)
        info = {"name": name, "channels": len(ids), "share": len(ids) / total}
        result = fit_type(
            key,
            info,
            frame,
            episodes,
            channels,
            args.out,
            args.out / "scores",
            recipes=[RECIPE],
        )
        print(
            json.dumps(
                {
                    "type": name,
                    "status": result["status"],
                    "test": result.get("test"),
                    "baseline": compare if name in config["types"] else None,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
