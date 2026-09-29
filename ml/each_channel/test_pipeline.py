"""Boundary tests for causal features and purged temporal splits."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from ml.each_channel.advanced import add_advanced
from ml.each_channel.features import STATE_COLUMNS, build_features, feature_columns
from ml.each_channel.improve_regularized import validate_artifact
from ml.each_channel.train import quality, split
from ml.each_channel.weather_ablation import build_weather_features


def test_future_and_exact_boundary_do_not_change_features():
    t = pd.Timestamp("2025-04-01")
    channels = pd.DataFrame(
        {
            "channel_id": [1, 2],
            "object_id": [10, 10],
            "sensor_type": ["Датчик дыма"] * 2,
            "tag_group": ["10:a"] * 2,
        }
    )
    daily = pd.DataFrame(
        {
            "channel_id": [1, 2, 1],
            "date": [t - pd.Timedelta(days=1)] * 2 + [t],
            "n_events": [3, 2, 9999],
            "n_alarm": [1, 0, 9999],
            "n_fault": [0, 0, 99],
            "n_num": [0, 0, 0],
            "mean": [np.nan] * 3,
            "max": [np.nan] * 3,
            "numeric_sum": [0] * 3,
            "numeric_sumsq": [0] * 3,
            **{col: [0, 0, 999] for col in STATE_COLUMNS},
        }
    )
    ep = pd.DataFrame(
        {
            "channel_id": [2, 1, 2, 1],
            "start": [
                t - pd.Timedelta(days=2),
                t + pd.Timedelta(days=2),
                t,
                t + pd.Timedelta(hours=12),
            ],
            "end": [
                t,
                t + pd.Timedelta(days=3),
                t + pd.Timedelta(days=1),
                t + pd.Timedelta(hours=13),
            ],
            "start_is_observed": [True] * 4,
            "coverage_uncertain": [False] * 4,
        }
    )
    full = build_features(daily, ep, channels, [t], [1], labels=True)
    past_ep = ep.loc[ep.start.lt(t)].copy()
    past_ep.loc[past_ep.end.ge(t), "end"] = pd.NaT
    past = build_features(daily.loc[daily.date.lt(t)], past_ep, channels, [t], [1], labels=False)
    features, _ = feature_columns(full, "identity")
    pd.testing.assert_frame_equal(full[features], past[features])
    assert full.loc[0, "events_1d"] == 3
    assert full.loc[0, "obj_in_fault"] == 1
    assert full.loc[0, "target"] == 1
    # An onset before 24 hours does not suppress a later onset inside the target window.
    assert full.loc[0, "lead_days"] == 2
    full_advanced = add_advanced(full, daily, ep, channels)
    past_advanced = add_advanced(past, daily.loc[daily.date.lt(t)], past_ep, channels)
    advanced_features, _ = feature_columns(full_advanced, "channel_identity")
    pd.testing.assert_frame_equal(
        full_advanced[advanced_features], past_advanced[advanced_features]
    )


def test_target_and_split_boundaries():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-12-02", "2024-12-03"]),
            "status": ["scored"] * 2,
            "target": [1, 1],
        }
    )
    assert len(split(frame, "train")) == 1
    assert quality([1] * 7 + [0] * 3 + [1] * 7, [1] * 10 + [0] * 7, 0.5)["passes_both"] is False


def test_no_alert_precision_is_undefined():
    assert quality([0, 1], [0.1, 0.2], 0.9)["precision"] is None


def test_matured_rates_use_only_closed_horizons():
    t = pd.Timestamp("2025-04-01")
    channels = pd.DataFrame({"channel_id": [1], "object_id": [10], "tag_group": ["10:a"]})
    dates = pd.date_range(t - pd.Timedelta(days=400), t + pd.Timedelta(days=31))
    daily = pd.DataFrame(
        {
            "channel_id": 1,
            "date": dates,
            "n_events": 1,
            "n_alarm": 0,
            "n_fault": 0,
            "last_value": "Норма",
            **{col: 0 for col in STATE_COLUMNS},
        }
    )
    episodes = pd.DataFrame(
        {
            "channel_id": [1, 1, 1],
            "start": [t - pd.Timedelta(days=15), t, t + pd.Timedelta(days=12)],
            "end": [t - pd.Timedelta(days=14), t + pd.Timedelta(days=1), t + pd.Timedelta(days=13)],
            "start_is_observed": [True] * 3,
        }
    )
    frame = pd.DataFrame({"channel_id": [1], "date": [t]})
    full = add_advanced(frame, daily, episodes, channels)
    past = add_advanced(
        frame, daily.loc[daily.date.lt(t)], episodes.loc[episodes.start.lt(t)], channels
    )
    pd.testing.assert_frame_equal(full, past)
    # The last closed horizon ends at T (exclusive), so the onset at T is unavailable.
    assert full.loc[0, "own_matured_count_180d"] == 151
    assert full.loc[0, "own_matured_positive_180d"] == 15


def test_resume_requires_model_with_matching_hash(tmp_path):
    folder = tmp_path / "model"
    folder.mkdir()
    assert validate_artifact(folder) is False
    data = b"model bytes"
    (folder / "model.cbm").write_bytes(data)
    with pytest.raises(RuntimeError, match="Incomplete"):
        validate_artifact(folder)
    config = {"status": "trained", "sha256": hashlib.sha256(data).hexdigest()}
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    assert validate_artifact(folder) is True
    (folder / "model.cbm").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        validate_artifact(folder)
    (folder / "model.cbm").unlink()
    with pytest.raises(RuntimeError, match="Incomplete"):
        validate_artifact(folder)


def test_weather_features_exclude_unavailable_recent_days():
    dates = pd.date_range("2024-01-01", "2024-02-10")
    weather = pd.DataFrame(
        {
            "time": dates,
            "temperature_2m_max": 1.0,
            "temperature_2m_min": 0.0,
            "precipitation_sum": 1.0,
            "snowfall_sum": 0.0,
        }
    )
    original, names = build_weather_features(weather)
    changed = weather.copy()
    changed.loc[changed.time.eq("2024-02-04"), "precipitation_sum"] = 999.0
    recent, _ = build_weather_features(changed)
    date = pd.Timestamp("2024-02-10")
    pd.testing.assert_frame_equal(
        original.loc[original.date.eq(date), names].reset_index(drop=True),
        recent.loc[recent.date.eq(date), names].reset_index(drop=True),
    )
    changed.loc[changed.time.eq("2024-02-03"), "precipitation_sum"] = 999.0
    available, _ = build_weather_features(changed)
    assert (
        original.loc[original.date.eq(date), names]
        .iloc[0]
        .ne(available.loc[available.date.eq(date), names].iloc[0])
        .any()
    )
