from __future__ import annotations

import pandas as pd
import pytest

from ml.v3_inputs.ingest import SOURCE_COLUMNS, aggregate_chunk
from ml.v3_inputs.prepare_episodes import reconstruct


def test_daily_aggregate_contains_v3_specific_counts_and_boundaries():
    raw = pd.DataFrame(
        [
            [1, 10, "2026-01-01", "00:00:01", "f", "1.5"],
            [2, 10, "2026-01-01", "00:00:02", "t", "Неисправен"],
            [3, 10, "2026-01-01", "00:00:03", "f", "Открыто"],
            [4, 10, "2026-01-01", "00:00:04", "t", "Сработал"],
            [5, 10, "2026-01-01", "00:00:05", "f", "Норма"],
        ],
        columns=SOURCE_COLUMNS,
    )

    daily, _, _ = aggregate_chunk(raw)
    row = daily.iloc[0]

    assert row.n_events == 5
    assert row.n_alarm == 2
    assert row.n_fault == 1
    assert row.n_numeric == 1
    assert row.numeric_sum == pytest.approx(1.5)
    assert row.numeric_sumsq == pytest.approx(2.25)
    assert row.n_open == 1
    assert row.n_trigger == 1
    assert row.first_event_id == 1 and row.first_value == "1.5"
    assert row.last_event_id == 5 and row.last_value == "Норма"


def test_fault_episode_is_reconstructed_from_past_state_boundaries():
    daily = pd.DataFrame(
        {
            "sensor_id": [10, 10, 10],
            "first_event_id": [1, 2, 3],
            "first_ts": pd.to_datetime(
                ["2026-01-01 01:00", "2026-01-02 01:00", "2026-01-03 01:00"]
            ),
            "first_value": ["Норма", "Неисправен", "Норма"],
            "last_event_id": [1, 2, 3],
            "last_ts": pd.to_datetime(
                ["2026-01-01 01:00", "2026-01-02 01:00", "2026-01-03 01:00"]
            ),
            "last_value": ["Норма", "Неисправен", "Норма"],
        }
    )
    raw = pd.DataFrame(
        {
            "event_id": pd.Series(dtype="int64"),
            "sensor_id": pd.Series(dtype="int64"),
            "ts": pd.Series(dtype="datetime64[ns]"),
            "value": pd.Series(dtype="object"),
        }
    )

    episodes, audit = reconstruct(daily, raw, pd.date_range("2026-01-01", "2026-01-03"))

    assert len(episodes) == 1
    episode = episodes.iloc[0]
    assert episode.start == pd.Timestamp("2026-01-02 01:00")
    assert episode.end == pd.Timestamp("2026-01-03 01:00")
    assert episode.duration_seconds == pytest.approx(86400)
    assert bool(episode.start_is_observed)
    assert not bool(episode.coverage_uncertain)
    assert audit["observed_onsets"] == 1
