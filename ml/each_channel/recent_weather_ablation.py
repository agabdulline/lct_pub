"""Compare two predeclared smoke/weather recipes with the existing smoke model."""

import argparse
import json
from pathlib import Path

import pandas as pd
from ml.each_channel.features import load_inputs, selected_types
from ml.each_channel.train import ROOT, fit_type
from ml.each_channel.weather_ablation import load_weather


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data/interim/each_channel/recent_weather",
    )
    args = parser.parse_args()
    (args.out / "cache").mkdir(parents=True, exist_ok=True)
    channels, _, episodes = load_inputs(ROOT / "data/interim/each_channel/source")
    weather, names = load_weather(ROOT / "data/interim/each_channel/weather_ablation")
    frame = pd.read_parquet(ROOT / "data/interim/each_channel/features/smoke_advanced.parquet")
    frame = frame.merge(weather, on="date", how="left", validate="many_to_one")
    old_dir = ROOT / "ml/each_channel/models/smoke"
    old = json.loads((old_dir / "config.json").read_text(encoding="utf-8"))["selected"]
    features = [*old["features"], *names]
    recipes = [
        dict(
            name="weather_shallow_100",
            mode="identity",
            depth=4,
            l2=20,
            weight="None",
            since="2022-01-01",
            fixed_trees=100,
            refit_all_days=True,
            feature_names=features,
            categorical=old["categorical"],
        ),
        dict(
            name="weather_recent_50",
            mode="identity",
            depth=6,
            l2=20,
            weight="None",
            since="2024-01-01",
            fixed_trees=50,
            refit_all_days=True,
            feature_names=features,
            categorical=old["categorical"],
        ),
    ]
    result = fit_type(
        "smoke",
        selected_types(channels)["smoke"],
        frame,
        episodes,
        channels,
        args.out / "models",
        args.out / "cache",
        recipes=recipes,
        incumbent=old_dir,
    )
    print(
        "RESULT",
        json.dumps(
            {
                "winner": result["selected"]["recipe"]["name"],
                "selection": result["selected"]["selection"],
                "calibration": result["calibration"],
                "test": result["test"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
