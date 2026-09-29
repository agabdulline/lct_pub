import pandas as pd
from ml.each_channel.hard_channel_study import label_complete, matured_calibration


def test_matured_calibration_excludes_unfinished_label_horizon():
    frame = pd.DataFrame(
        {"date": pd.to_datetime(["2025-12-31", "2026-01-01", "2026-03-31", "2026-04-01"])}
    )

    result = matured_calibration(frame, "2026-05-01", 90)

    assert result.date.tolist() == [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-31")]


def test_label_complete_rejects_windows_crossing_known_gap():
    complete = label_complete(pd.to_datetime(["2026-05-02", "2026-05-03", "2026-05-31"]))

    assert complete.tolist() == [True, False, False]
