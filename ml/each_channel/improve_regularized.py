"""Compare a fixed set of regularized phase and smoke CatBoost candidates."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
from ml.each_channel.features import load_inputs, selected_types
from ml.each_channel.train import ROOT, fit_type, write_json

RECIPES = {
    "phase": [
        dict(
            name=name,
            mode="base",
            depth=depth,
            l2=10,
            weight="None",
            since="2022-01-01",
            fixed_trees=trees,
            refit_all_days=True,
        )
        for name, depth, trees in (
            ("phase_shallow_100", 4, 100),
            ("phase_shallow_250", 4, 250),
            ("phase_base_100", 6, 100),
        )
    ],
    "smoke": [
        dict(
            name=name,
            mode="extended",
            depth=4,
            l2=10,
            weight="None",
            since="2022-01-01",
            fixed_trees=trees,
            refit_all_days=True,
        )
        for name, trees in (("smoke_simple_100", 100), ("smoke_simple_250", 250))
    ],
}


def validate_artifact(folder):
    """A JSON marker alone is never enough to resume a trained model."""
    config_file = folder / "config.json"
    model_file = folder / "model.cbm"
    if not config_file.exists() and not model_file.exists():
        return False
    if not config_file.exists() or not model_file.exists():
        raise RuntimeError(f"Incomplete model artifact: {folder}")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if config.get("status") != "trained" or not config.get("sha256"):
        raise RuntimeError(f"Invalid model config: {config_file}")
    actual = hashlib.sha256(model_file.read_bytes()).hexdigest()
    if actual != config["sha256"]:
        raise RuntimeError(f"Model SHA256 mismatch: {model_file}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/interim/each_channel/source")
    parser.add_argument(
        "--features-dir", type=Path, default=ROOT / "data/interim/each_channel/features"
    )
    parser.add_argument("--out", type=Path, default=ROOT / "ml/each_channel/stage4/models")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/interim/each_channel/stage4")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    incumbents = ROOT / "ml/each_channel/models"
    for key in RECIPES:
        if not validate_artifact(incumbents / key):
            raise RuntimeError(f"Missing incumbent model: {incumbents / key}")
        existing = validate_artifact(args.out / key)
        if existing and not args.resume:
            raise RuntimeError(
                f"Output already exists: {args.out / key}; use --resume or a new --out"
            )

    smoke_old = json.loads((incumbents / "smoke" / "config.json").read_text(encoding="utf-8"))
    smoke_selected = smoke_old["selected"]
    recipes_by_type = {
        **RECIPES,
        "smoke": [
            *RECIPES["smoke"],
            dict(
                name="smoke_full_100",
                mode=smoke_selected["recipe"]["mode"],
                depth=4,
                l2=20,
                weight="None",
                since="2022-01-01",
                fixed_trees=100,
                refit_all_days=True,
                feature_names=smoke_selected["features"],
                categorical=smoke_selected["categorical"],
            ),
        ],
    }
    protocol = {
        "candidates": recipes_by_type,
        "selection": "Q3 2025: compare with incumbent on min(P/0.7,R/0.5); Q4 2025: calibrate threshold",
        "test": "2026 is previously inspected, so this is retrospective diagnostic evidence",
        "production": "No automatic promotion to ml-service",
    }
    args.out.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    write_json(args.out.parent / "protocol.json", protocol)

    channels, _, episodes = load_inputs(args.data_dir)
    types = selected_types(channels)
    for key, recipes in recipes_by_type.items():
        if args.resume and validate_artifact(args.out / key):
            print(f"{key}: verified existing artifact, skipped", flush=True)
            continue
        frame = pd.read_parquet(args.features_dir / f"{key}_advanced.parquet")
        fit_type(
            key,
            types[key],
            frame,
            episodes,
            channels,
            args.out,
            args.cache,
            recipes=recipes,
            incumbent=incumbents / key,
        )


if __name__ == "__main__":
    main()
