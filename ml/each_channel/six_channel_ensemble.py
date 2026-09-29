"""Select old/new blends on 2025 and evaluate them only after all types freeze."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.features import load_inputs
from ml.each_channel.six_channel_study import (
    SELECTED,
    load_objects,
    prepare_frames,
    rank,
    subset,
    transform_scores,
)
from ml.each_channel.train import assess, choose_threshold, write_json

WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)
FREQUENT = {"smoke", "phase"}


def read_model(folder):
    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    path = folder / "model.cbm"
    if hashlib.sha256(path.read_bytes()).hexdigest() != config["sha256"]:
        raise ValueError(f"SHA256 mismatch: {path}")
    model = CatBoostClassifier().load_model(str(path))
    features = config.get("selected", {}).get("features", config.get("features"))
    if model.feature_names_ != features:
        raise ValueError(f"Feature schema mismatch: {path}")
    return config, model, path, features


def new_model(folder):
    config = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    path = folder / "model.cbm"
    if hashlib.sha256(path.read_bytes()).hexdigest() != config["sha256"]:
        raise ValueError(f"SHA256 mismatch: {path}")
    model = CatBoostClassifier().load_model(str(path))
    if model.feature_names_ != config["features"]:
        raise ValueError(f"Feature schema mismatch: {path}")
    return config, model, path


def old_folder(args, key):
    if key in FREQUENT:
        return args.previous_frequent / "ml/each_channel/models" / key
    return args.previous_remaining / key


def old_feature_frame(args, key, frame, features):
    if key not in FREQUENT:
        return frame
    path = args.previous_frequent / "data/interim/each_channel/features" / f"{key}_advanced.parquet"
    source = pd.read_parquet(
        path,
        columns=["channel_id", "date", *features],
        filters=[
            ("date", ">=", frame.date.min()),
            ("date", "<=", frame.date.max()),
        ],
    ).set_index(["channel_id", "date"])
    if not source.index.is_unique:
        raise ValueError(f"Duplicate advanced feature rows: {key}")
    keys = pd.MultiIndex.from_frame(frame[["channel_id", "date"]])
    if not keys.isin(source.index).all():
        raise ValueError(f"Advanced feature rows do not align: {key}")
    return source.loc[keys].reset_index(drop=True)


def score_pair(args, key, frame, old, old_features, new, new_config):
    old_frame = old_feature_frame(args, key, frame, old_features)
    old_scores = old.predict_proba(old_frame[old_features])[:, 1]
    new_raw = new.predict_proba(frame[new_config["features"]])[:, 1]
    new_scores = transform_scores(frame, new_raw, new_config["postprocess"])
    return old_scores, new_scores


def fit(args):
    if (args.out / "protocol.json").exists():
        raise ValueError("Use a new output directory")
    channels, daily, episodes = load_inputs(args.data_dir)
    objects = load_objects(args.objects)
    frames = prepare_frames(args, channels, daily, episodes, objects)
    write_json(
        args.out / "protocol.json",
        {
            "selected": SELECTED,
            "weights": WEIGHTS,
            "selection": "blend weight on Q3 2025; threshold on Q4 2025",
            "test": "2026-01-01 through 2026-05-02; previously viewed",
            "warning": "adaptive follow-up after earlier 2026 diagnostics; not an independent blind test",
        },
    )
    for key in SELECTED.values():
        frame = frames[key]
        selection = subset(frame, "selection")
        calibration = subset(frame, "calibration")
        pretest = pd.concat([selection, calibration], ignore_index=True).sort_values(
            ["date", "channel_id"], kind="stable"
        )
        old_config, old, old_path, old_features = read_model(old_folder(args, key))
        new_config, new, new_path = new_model(args.new_run / key)
        old_scores, new_scores = score_pair(args, key, pretest, old, old_features, new, new_config)
        selection_mask = pretest.date.lt("2025-10-01").to_numpy()
        candidates = {}
        for weight in WEIGHTS:
            scores = (1 - weight) * old_scores + weight * new_scores
            selected_scores = scores[selection_mask]
            threshold = choose_threshold(selection.target, selected_scores)
            candidates[str(weight)] = {
                "weight_new": weight,
                "selection_threshold": threshold,
                "selection": assess(selection, selected_scores, threshold),
            }
        winner = max(candidates, key=lambda weight: rank(candidates[weight]["selection"]))
        weight = candidates[winner]["weight_new"]
        blended = (1 - weight) * old_scores + weight * new_scores
        calibration_scores = blended[~selection_mask]
        threshold = choose_threshold(calibration.target, calibration_scores)
        result = {
            "key": key,
            "name": new_config["name"],
            "weight_new": weight,
            "weight_old": 1 - weight,
            "candidates": candidates,
            "threshold": threshold,
            "calibration": assess(calibration, calibration_scores, threshold),
            "old": {
                "path": str(old_path.resolve()),
                "sha256": old_config["sha256"],
                "features": old_features,
            },
            "new": {
                "path": str(new_path.resolve()),
                "sha256": new_config["sha256"],
                "features": new_config["features"],
                "postprocess": new_config["postprocess"],
            },
        }
        write_json(args.out / key / "selection_frozen.json", result)
        print(
            key,
            f"old={1 - weight:.2f}",
            f"new={weight:.2f}",
            result["calibration"]["precision"],
            result["calibration"]["recall"],
            flush=True,
        )
    write_json(args.out / "all_frozen.json", {"keys": list(SELECTED.values())})


def evaluate(args):
    keys = json.loads((args.out / "all_frozen.json").read_text(encoding="utf-8"))["keys"]
    results = []
    for key in keys:
        result = json.loads((args.out / key / "selection_frozen.json").read_text(encoding="utf-8"))
        old_config, old, old_path, old_features = read_model(old_folder(args, key))
        new_config, new, new_path = new_model(args.new_run / key)
        if (
            str(old_path.resolve()) != result["old"]["path"]
            or old_config["sha256"] != result["old"]["sha256"]
        ):
            raise ValueError(f"Old model changed after freeze: {key}")
        if (
            str(new_path.resolve()) != result["new"]["path"]
            or new_config["sha256"] != result["new"]["sha256"]
        ):
            raise ValueError(f"New model changed after freeze: {key}")
        frame = pd.read_parquet(args.new_run / key / "evaluation.parquet")
        old_scores, new_scores = score_pair(args, key, frame, old, old_features, new, new_config)
        scores = result["weight_old"] * old_scores + result["weight_new"] * new_scores
        test_mask = frame.date.ge("2026-01-01").to_numpy()
        test = frame.loc[test_mask]
        result["test"] = assess(test, scores[test_mask], result["threshold"])
        result["old_test"] = assess(test, old_scores[test_mask], old_config["threshold"])
        result["new_test"] = assess(test, new_scores[test_mask], new_config["threshold"])
        write_json(args.out / key / "report.json", result)
        results.append(result)
        print(
            key,
            result["test"]["precision"],
            result["test"]["recall"],
            result["test"]["alerts"],
            flush=True,
        )
    write_json(args.out / "summary.json", results)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("fit", "evaluate"), required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--objects", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--previous-frequent", type=Path, required=True)
    parser.add_argument("--previous-remaining", type=Path, required=True)
    parser.add_argument("--new-run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "fit":
        for value in (args.data_dir, args.objects, args.previous):
            if value is None:
                parser.error("fit requires --data-dir, --objects and --previous")
        fit(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
