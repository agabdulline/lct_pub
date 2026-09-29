"""Check daily H1 refitting at the winner's already chosen tree count and features."""

import gc
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
from ml.each_channel.features import load_inputs, selected_types
from ml.each_channel.train import fit_type, write_json


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    folder = Path("ml/each_channel")
    cache = Path("data/interim/each_channel/features")
    archive = folder / "stage2"
    keys = ("motion", "smoke", "temperature", "door", "phase")
    if not all((folder / "models" / key / "stage2_complete.json").exists() for key in keys):
        raise RuntimeError("Finish the advanced-feature comparison first")
    if not archive.exists():
        shutil.copytree(folder / "models", archive / "models")
    recipes = {}
    for key in keys:
        old = json.loads((archive / "models" / key / "config.json").read_text(encoding="utf-8"))
        selected = old["selected"]
        recipes[key] = dict(
            selected["recipe"],
            name="winner_daily_refit",
            refit_all_days=True,
            fixed_trees=selected["trees"],
            feature_names=selected["features"],
            categorical=selected["categorical"],
        )
    write_json(
        folder / "dense_protocol.json",
        {
            "recipes": recipes,
            "change": "Refit uses every eligible H1 2025 date instead of Mondays; tree count/features fixed",
            "selection": "Same Q3 2025 rank against the stage2 incumbent, Q4 calibration, no test-driven rollback",
        },
    )
    channels, _, episodes = load_inputs(Path("data/interim/each_channel/source"))
    types = selected_types(channels)
    for key in keys:
        complete = folder / "models" / key / "dense_complete.json"
        if complete.exists():
            continue
        frame = pd.read_parquet(cache / f"{key}_advanced.parquet")
        result = fit_type(
            key,
            types[key],
            frame,
            episodes,
            channels,
            folder / "models",
            cache,
            recipes=[recipes[key]],
            incumbent=archive / "models" / key,
        )
        write_json(complete, {"selected": result["selected"]["recipe"]["name"]})
        del frame
        gc.collect()


if __name__ == "__main__":
    main()
