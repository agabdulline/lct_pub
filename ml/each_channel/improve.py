"""Extend the preregistered 2025 selection without choosing against 2026 scores."""

import gc
import shutil
import sys
from pathlib import Path

import pandas as pd
from ml.each_channel.advanced import add_advanced
from ml.each_channel.features import load_inputs, selected_types
from ml.each_channel.train import fit_type, write_json

RECIPES = [
    {
        "name": "matured_d6",
        "mode": "advanced",
        "depth": 6,
        "l2": 5,
        "weight": "None",
        "since": "2022-01-01",
    },
    {
        "name": "matured_weighted_d4",
        "mode": "advanced",
        "depth": 4,
        "l2": 10,
        "weight": "SqrtBalanced",
        "since": "2022-01-01",
    },
    {
        "name": "matured_d8",
        "mode": "advanced",
        "depth": 8,
        "l2": 10,
        "weight": "None",
        "since": "2022-01-01",
    },
    {
        "name": "matured_recent_d6",
        "mode": "advanced",
        "depth": 6,
        "l2": 10,
        "weight": "None",
        "since": "2024-01-01",
    },
    {
        "name": "matured_object_d6",
        "mode": "advanced_identity",
        "depth": 6,
        "l2": 20,
        "weight": "None",
        "since": "2022-01-01",
    },
    {
        "name": "matured_channel_d6",
        "mode": "channel_identity",
        "depth": 6,
        "l2": 20,
        "weight": "None",
        "since": "2022-01-01",
    },
]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    folder = Path("ml/each_channel")
    cache = Path("data/interim/each_channel/features")
    archive = folder / "stage1"
    if not archive.exists():
        shutil.copytree(folder / "models", archive / "models")
        shutil.copy2(folder / "protocol.json", archive / "protocol.json")
    write_json(
        folder / "improvement_protocol.json",
        {
            "recipes": RECIPES,
            "selection": "Same Q3 2025 rank, incumbent retained unless strictly improved; Q4 threshold",
            "limitation": "2026 was previously inspected; it is a historical holdout, not a new blind test",
            "skip": {
                "switch": "insufficient training/early-stop classes",
                "uir": "no positive validation or test outcomes",
            },
        },
    )
    channels, daily, episodes = load_inputs(Path("data/interim/each_channel/source"))
    types = selected_types(channels)
    for key in ("motion", "smoke", "temperature", "door", "phase"):
        complete = folder / "models" / key / "stage2_complete.json"
        if complete.exists():
            continue
        print(f"Advanced features: {key}", flush=True)
        path = cache / f"{key}_advanced.parquet"
        if path.exists():
            frame = pd.read_parquet(path)
        else:
            frame = add_advanced(
                pd.read_parquet(cache / f"{key}.parquet"), daily, episodes, channels
            )
            frame.to_parquet(path, index=False)
        result = fit_type(
            key,
            types[key],
            frame,
            episodes,
            channels,
            folder / "models",
            cache,
            recipes=RECIPES,
            incumbent=archive / "models" / key,
        )
        write_json(complete, {"selected": result["selected"]["recipe"]["name"]})
        del frame
        gc.collect()


if __name__ == "__main__":
    main()
