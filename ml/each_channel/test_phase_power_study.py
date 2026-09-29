import pandas as pd
from ml.each_channel.phase_power_study import add_power_features, counts


def test_power_counts_exclude_event_at_decision_time():
    points = pd.to_datetime(["2025-01-03", "2025-01-10"])
    dates = pd.to_datetime(["2025-01-10"])

    assert counts(points, dates, 7).tolist() == [1]


def test_power_features_use_only_previous_events_and_exclude_own_from_object_context():
    channels = pd.DataFrame({"channel_id": [1, 2], "object_id": [10, 10]})
    frame = pd.DataFrame({"channel_id": [1], "date": pd.to_datetime(["2025-01-10"])})
    events = pd.DataFrame(
        {
            "sensor_id": [1, 1, 2, 2],
            "ts": pd.to_datetime(["2025-01-09", "2025-01-10", "2025-01-08", "2025-01-11"]),
            "value": ["Обесточен", "Есть питание", "Обесточен", "Есть питание"],
        }
    )

    result = add_power_features(frame, events, channels).iloc[0]

    assert result.power_off_events_7d == 1
    assert result.power_on_events_7d == 0
    assert result.object_power_off_7d == 1
    assert result.object_power_on_7d == 0
    assert result.last_power_state == "off"
