import numpy as np
import pandas as pd
from ml.each_channel.robustness_study import PERIODS, add_disjoint_history, subset, walk_partitions


def test_disjoint_history_excludes_decision_day():
    decision = pd.Timestamp("2025-01-10")
    frame = pd.DataFrame({"channel_id": [1], "date": [decision]})
    daily = pd.DataFrame(
        {
            "channel_id": [1, 1],
            "date": [decision - pd.Timedelta(days=1), decision],
            "n_events": [2, 99],
            "n_alarm": [0, 99],
            "n_fault": [1, 99],
            "n_normal": [0, 99],
            "n_unknown": [0, 99],
            "n_disabled": [0, 99],
        }
    )

    result = add_disjoint_history(frame, daily).iloc[0]

    assert np.isclose(result.bin_events_0_1d, np.log1p(2))
    assert result.bin_alarm_0_1d == 0
    assert np.isclose(result.bin_fault_0_1d, np.log1p(1))


def test_walk_forward_labels_end_before_the_next_stage():
    dates = pd.date_range("2022-01-01", "2026-01-31")
    frame = pd.DataFrame({"date": dates})

    train, calibration, prediction = walk_partitions(frame, "2026-01-01")

    assert train.date.max() + pd.Timedelta(days=30) <= calibration.date.min()
    assert calibration.date.max() + pd.Timedelta(days=30) <= prediction.date.min()
    assert prediction.date.min() == pd.Timestamp("2026-01-01")
    assert prediction.date.max() == pd.Timestamp("2026-01-31")


def test_static_split_purges_labels_at_every_boundary():
    dates = pd.date_range("2023-01-01", "2026-06-30")
    frame = pd.DataFrame({"date": dates, "status": "scored", "target": 0.0})
    for name, (start, end) in PERIODS.items():
        part = subset(frame, name)
        assert part.date.min() == pd.Timestamp(start)
        assert part.date.max() + pd.Timedelta(days=30) <= pd.Timestamp(end)
        assert (part.date + pd.Timedelta(days=30)).le(end).all()
    assert subset(frame, "test").date.max() == pd.Timestamp("2026-05-02")
