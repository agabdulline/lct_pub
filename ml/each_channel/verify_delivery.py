"""Verify delivered each-channel CatBoost artifacts and ensemble references."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from catboost import CatBoostClassifier

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "models" / "each-channel"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root: Path = DEFAULT_ROOT) -> list[str]:
    checked: list[str] = []
    for model_path in sorted(root.rglob("*.cbm")):
        config_path = model_path.parent / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        actual_sha = digest(model_path)
        if actual_sha != config["sha256"]:
            raise RuntimeError(f"{model_path}: SHA-256 mismatch")

        model = CatBoostClassifier()
        model.load_model(str(model_path))
        if model.feature_names_ != config["features"]:
            raise RuntimeError(f"{model_path}: feature names or order differ from config")
        categorical = [model.feature_names_[index] for index in model.get_cat_feature_indices()]
        if categorical != config["categorical"]:
            raise RuntimeError(f"{model_path}: categorical features differ from config")
        checked.append(model_path.relative_to(root).as_posix())

    ensemble_path = root / "ups" / "config.json"
    ensemble = json.loads(ensemble_path.read_text(encoding="utf-8"))
    if abs(sum(component["weight"] for component in ensemble["components"]) - 1.0) > 1e-12:
        raise RuntimeError("UPS ensemble weights must sum to one")
    for component in ensemble["components"]:
        model_path = ensemble_path.parent / component["path"]
        config_path = ensemble_path.parent / component["config"]
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if digest(model_path) != component["sha256"] or config["sha256"] != component["sha256"]:
            raise RuntimeError(f"UPS component reference mismatch: {model_path}")

    expected = {
        "fan/fan.cbm",
        "manual-alarm/manual_alarm.cbm",
        "pump/pump.cbm",
        "ups/new/ups_context.cbm",
        "ups/old/ups_old.cbm",
    }
    if set(checked) != expected:
        raise RuntimeError(f"Incomplete delivery: {sorted(set(expected) - set(checked))}")
    return checked


def main() -> None:
    for path in verify():
        print(f"ok {path}")


if __name__ == "__main__":
    main()
