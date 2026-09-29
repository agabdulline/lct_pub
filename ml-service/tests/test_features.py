import pandas as pd
from synthetic import at, channels, events

from ml.baseline.features import build, skip_reason


def decision(d: pd.DataFrame, cid: int, ts) -> pd.Series:
    rows = d[(d.channel_id == cid) & (d.alarm_ts == ts)]
    assert len(rows) == 1, f"нет решения по каналу {cid} на {ts}"
    return rows.iloc[0]


def test_solitary_alarm_followed_by_fault_is_positive():
    d = build(events(), channels(), data_end=at(45))
    r = decision(d, 11, at(20, 10))
    assert r.target == 1.0
    assert r.lead_hours < 1.01
    assert r.obj_alarm_channels_1h == 0 and r.solitary_1h == 1.0
    assert r.near_active_channels_90d == 2 and r.near_solitary_1h == 1.0  # соседи по пикету 12 и 13


def test_neighbour_by_picket_counted_only_from_the_past():
    d = build(events(), channels(), data_end=at(45))
    r = decision(d, 13, at(30, 9, 1))
    assert r.obj_alarm_channels_1h == 1  # 12 — минутой раньше; 14 — позже и не видна
    assert r.near_alarm_channels_1h == 1 and r.near_solitary_1h == 0.0
    assert r.target == 0.0


def test_skip_reasons():
    ev, ch = events(), channels()
    assert skip_reason(ev, ch, 11, at(20, 10)) is None
    assert skip_reason(ev, ch, 11, at(20, 10, 30)) == "decided_within_24h"
    assert skip_reason(ev, ch, 12, at(35, 12, 30)) == "fault_recent_24h"
    assert skip_reason(ev, ch, 14, at(3, 10)) == "history_lt_7d"
    assert skip_reason(ev, ch, 11, at(25, 10)) == "alarm_not_found"


def test_horizon_not_observed_gives_unknown_label():
    d = build(events(), channels(), data_end=at(20, 12))
    assert pd.isna(decision(d, 13, at(30, 9, 1)).target) if (d.channel_id == 13).any() else True
    assert decision(d, 11, at(20, 10)).target != decision(d, 11, at(20, 10)).target  # NaN: горизонт не закрыт


def test_only_rows_match_full_build():
    """Расчёт признаков для выбранных тревог совпадает с полным — основа совпадения обучения и сервиса."""
    import numpy as np

    from ml.baseline.features import FEATURES

    full = build(events(), channels(), data_end=at(45))
    wanted = [(11, at(20, 10)), (13, at(30, 9, 1))]
    part = build(events(), channels(), data_end=at(45), only=wanted)
    for cid, ts in wanted:
        a = decision(full, cid, ts)[FEATURES].to_numpy(dtype=float)
        b = decision(part, cid, ts)[FEATURES].to_numpy(dtype=float)
        assert np.allclose(a, b, equal_nan=True)
