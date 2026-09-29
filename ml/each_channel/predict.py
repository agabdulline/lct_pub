"""Score a type using only history available strictly before the requested date."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
from catboost import CatBoostClassifier
from ml.each_channel.advanced import add_advanced
from ml.each_channel.features import build_features, load_inputs


def score_day(data_dir, model_dir, date):
    snapshot = pd.Timestamp(date).normalize()
    channels, daily, episodes = load_inputs(Path(data_dir))
    config = json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
    model_file = Path(model_dir) / "model.cbm"
    if hashlib.sha256(model_file.read_bytes()).hexdigest() != config["sha256"]:
        raise ValueError("Model SHA256 mismatch")
    daily = daily.loc[daily.date.lt(snapshot)].copy()
    episodes = episodes.loc[episodes.start.lt(snapshot)].copy()
    episodes.loc[episodes.end.ge(snapshot), "end"] = pd.NaT
    ids = channels.loc[channels.sensor_type.eq(config["name"]), "channel_id"].tolist()
    frame = build_features(daily, episodes, channels, [snapshot], ids, labels=False)
    if "last_state" in config["selected"]["features"]:
        frame = add_advanced(frame, daily, episodes, channels)
    frame = frame.loc[frame.status.eq("scored")].copy()
    model = CatBoostClassifier().load_model(str(model_file))
    features = config["selected"]["features"]
    if model.feature_names_ != features:
        raise ValueError("Model feature schema mismatch")
    result = frame[["channel_id", "date", "sensor_type"]].copy()
    result["score"] = model.predict_proba(frame[features])[:, 1] if len(frame) else []
    result["threshold"] = config["threshold"]
    result["alert"] = result.score.ge(config["threshold"])
    result["window_start"] = snapshot + pd.Timedelta(days=1)
    result["window_end_exclusive"] = snapshot + pd.Timedelta(days=30)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = score_day(args.data_dir, args.model_dir, args.date)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"eligible={len(result)}, alerts={result.alert.sum()}")
