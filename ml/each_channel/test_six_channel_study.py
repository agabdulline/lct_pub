import numpy as np
import pandas as pd
from ml.each_channel.six_channel_ensemble import old_feature_frame
from ml.each_channel.six_channel_study import (
    add_hierarchy_features,
    episode_balanced_weights,
    transform_scores,
)


def test_old_advanced_features_align_by_channel_and_date(tmp_path):
    folder = tmp_path / "data/interim/each_channel/features"
    folder.mkdir(parents=True)
    source = pd.DataFrame(
        {
            "channel_id": [2, 1],
            "date": pd.to_datetime(["2025-01-01", "2025-01-02"]),
            "advanced": [20.0, 10.0],
        }
    )
    source.to_parquet(folder / "smoke_advanced.parquet", index=False)
    frame = pd.DataFrame(
        {
            "channel_id": [1, 2],
            "date": pd.to_datetime(["2025-01-02", "2025-01-01"]),
        }
    )
    args = type("Args", (), {"previous_frequent": tmp_path})()
    result = old_feature_frame(args, "smoke", frame, ["advanced"])
    assert result.advanced.tolist() == [10.0, 20.0]


def test_episode_weights_equalize_positive_episodes():
    frame = pd.DataFrame(
        {
            "channel_id": [1, 1, 1, 2],
            "target": [1, 1, 1, 1],
            "target_start": pd.to_datetime(
                ["2025-02-01", "2025-02-01", "2025-03-01", "2025-04-01"]
            ),
        }
    )
    weights = episode_balanced_weights(frame)
    totals = frame.assign(weight=weights).groupby(["channel_id", "target_start"]).weight.sum()
    assert np.allclose(totals, totals.iloc[0])
    assert np.isclose(weights.sum(), len(frame))


def test_score_postprocessing_is_causal():
    frame = pd.DataFrame(
        {
            "channel_id": [1, 1, 1, 2, 2],
            "date": pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-01", "2025-01-02"]
            ),
        }
    )
    scores = np.array([0.1, 0.3, 0.9, 0.2, 0.4])
    before = transform_scores(frame, scores, "mean_3d")
    changed = scores.copy()
    changed[2] = 0.0
    after = transform_scores(frame, changed, "mean_3d")
    assert np.allclose(before[:2], after[:2])
    assert np.allclose(before[3:], after[3:])


def test_parent_context_excludes_own_object_and_future():
    channels = pd.DataFrame(
        {
            "channel_id": [1, 2, 3],
            "object_id": [10, 10, 11],
            "sensor_type": ["x", "x", "x"],
            "название_датчика": ["ПК 1", "ПК 2", "ПК 3"],
            "tag": ["1.2.", "1.3.", "1.4."],
        }
    )
    objects = pd.DataFrame(
        {
            "object_id": [10, 11],
            "level": [3, 3],
            "parent_id": [100, 100],
            "object_kind": ["controlHouse", "guardObject"],
        }
    )
    episodes = pd.DataFrame(
        {
            "channel_id": [1, 3, 3],
            "start": pd.to_datetime(["2025-01-05", "2025-01-03", "2025-01-20"]),
            "end": pd.to_datetime(["2025-01-06", "2025-01-04", "2025-01-21"]),
            "start_is_observed": [True, True, True],
        }
    )
    frame = pd.DataFrame(
        {
            "channel_id": [1],
            "date": pd.to_datetime(["2025-01-10"]),
            "events_7d": [1],
            "events_30d": [2],
            "alarms_7d": [0],
            "alarms_30d": [0],
            "faults_msg_7d": [0],
            "faults_msg_30d": [1],
            "days_active_30d": [2],
            "days_active_90d": [2],
            "onsets_7d": [1],
            "onsets_30d": [1],
        }
    )
    result = add_hierarchy_features(frame, episodes, channels, objects).iloc[0]
    assert result.parent_other_onsets_7d == 1
    assert result.parent_other_onsets_30d == 1
    assert result.parent_other_active == 0
    assert result.object_kind == "controlHouse"
    assert result.picket_number_catalog == 1
