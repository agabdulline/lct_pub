"""Ежедневная модель на синтетике: два канала, известные эпизоды неисправности — данных хакатона нет."""

import pandas as pd

from ml.baseline.daily import FEATURES, build_daily

BASE = pd.Timestamp("2025-03-03")


def day(n: int, hh: int = 0) -> pd.Timestamp:
    return BASE + pd.Timedelta(days=n, hours=hh)


def inputs():
    channels = pd.DataFrame(
        {"channel_id": [1, 2], "object_id": [7, 7], "sensor_type": ["Датчик дыма", "Состояние насоса"]}
    )
    daily = pd.DataFrame(
        [
            {
                "channel_id": 1,
                "date": (BASE + pd.Timedelta(days=d)).date(),
                "n_events": 3,
                "n_alarm": 0,
                "n_fault": 0,
                "n_num": 0,
                "mean": None,
                "max": None,
            }
            for d in range(0, 80)
        ]
    )  # у канала 2 дневных итогов нет — нет покрытия
    episodes = pd.DataFrame({"channel_id": [1], "start": [day(40, 10)], "end": [day(42)]})
    return channels, daily, episodes


def row(d: pd.DataFrame, cid: int, n: int) -> pd.Series:
    return d[(d.channel_id == cid) & (d.date == BASE + pd.Timedelta(days=n))].iloc[0]


def test_target_window_and_statuses():
    ch, daily, eps = inputs()
    d = build_daily(daily, eps, ch, [BASE + pd.Timedelta(days=n) for n in (20, 39, 40, 41, 60)], data_end=day(120))
    assert row(d, 1, 20).target == 1.0 and row(d, 1, 20).status == "scored"  # начало через 20 сут 10 ч
    assert row(d, 1, 39).target == 1.0  # через 1 сут 10 ч — в окне
    assert row(d, 1, 40).target == 0.0  # через 10 ч — раньше суток, цель отрицательная
    assert row(d, 1, 41).status == "in_fault"
    assert row(d, 1, 60).onsets_30d == 1 and row(d, 1, 60).target == 0.0
    assert row(d, 2, 20).status == "no_coverage"


def test_unobserved_horizon_gives_unknown_label():
    ch, daily, eps = inputs()
    d = build_daily(daily, eps, ch, [BASE + pd.Timedelta(days=20)], data_end=day(30))
    assert pd.isna(row(d, 1, 20).target)


def test_onset_within_first_day_does_not_hide_next_one():
    ch, daily, _ = inputs()
    eps = pd.DataFrame({"channel_id": [1, 1], "start": [day(50, 10), day(55)], "end": [day(50, 12), day(56)]})
    d = build_daily(daily, eps, ch, [BASE + pd.Timedelta(days=50)], data_end=day(120))
    assert row(d, 1, 50).target == 1.0 and row(d, 1, 50).lead_days == 5.0


def service_window(daily, eps, date, history_days=400):
    """Та же выборка, что читает ml-service (app.history.daily_inputs): история за history_days до даты."""
    since = date - pd.Timedelta(days=history_days)
    d = pd.to_datetime(daily["date"])
    daily = daily[(d >= since) & (d < date)]
    eps = eps[(eps["start"] < date) & (eps["end"].isna() | (eps["end"] >= since))]
    return daily, eps


def test_features_do_not_depend_on_loaded_history():
    """Обучение видит всю историю, сервис — 400 суток: признаки и статус должны совпасть."""
    date = BASE + pd.Timedelta(days=700)
    ch = pd.DataFrame({"channel_id": [1, 2, 3], "object_id": [7, 7, 7], "sensor_type": ["Датчик дыма"] * 3})
    daily = pd.DataFrame(
        [
            {
                "channel_id": c,
                "date": (BASE + pd.Timedelta(days=n)).date(),
                "n_events": 2,
                "n_alarm": 1,
                "n_fault": 0,
                "n_num": 1,
                "mean": 0.5,
                "max": 1.0,
            }
            for c in (1, 2, 3)
            for n in range(0, 760, 3)
        ]
    )
    eps = pd.DataFrame(
        {
            "channel_id": [1, 1, 2, 3],
            "start": [day(100), day(600, 5), day(200), day(250)],  # у 2 — открытый эпизод с 500 суток назад
            "end": [day(101), day(600, 9), pd.NaT, day(251)],  # у 3 — последний эпизод 450 суток назад
        }
    )
    full = build_daily(daily, eps, ch, [date])
    served = build_daily(*service_window(daily, eps, date), ch, [date])
    cols = [c for c in FEATURES if c != "sensor_type"] + ["status"]
    pd.testing.assert_frame_equal(full[cols], served[cols])
    assert row(full, 2, 700).status == "in_fault"
    assert pd.isna(row(full, 3, 700).days_since_onset) and row(full, 1, 700).days_since_onset > 99


def test_object_features_use_all_object_channels():
    """Прогноз по части каналов: признаки объекта — по всем каналам объекта, как при обучении."""
    ch, daily, eps = inputs()
    date = [BASE + pd.Timedelta(days=41)]
    full = build_daily(daily, eps, ch, date)
    part = build_daily(daily, eps, ch, date, only=[2])
    assert list(part.channel_id) == [2]
    cols = [c for c in FEATURES if c != "sensor_type"]
    pd.testing.assert_frame_equal(full[full.channel_id == 2][cols].reset_index(drop=True), part[cols])
    assert row(part, 2, 41).obj_in_fault == 1
