"""Check calendar availability and the original v0 comparison path."""

import json

import numpy as np
import pandas as pd
from ml.baseline.daily import FEATURES, build_daily
from ml.each_channel.calendar_study import baseline_rows, calendar_features


def test_calendar_transfers_and_no_future_effect(tmp_path):
    for year in range(2022, 2027):
        months = [{"month": m, "days": "1,2+,3*"} for m in range(1, 13)]
        payload = {"year": year, "months": months, "statistic": {"holidays": 24}}
        (tmp_path / f"ru-{year}.json").write_text(json.dumps(payload), encoding="utf-8")
    before, _ = calendar_features(tmp_path)
    before = before.set_index("date")
    assert before.loc["2025-01-02", "ru_day_off"] == 1
    assert before.loc["2025-01-03", "ru_day_off"] == 0
    assert before.loc["2025-01-03", "ru_short_day"] == 1
    assert before.loc["2025-01-02", "ru_off_prev1"] == 1
    path = tmp_path / "ru-2025.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["months"][6]["days"] += ",4"
    payload["statistic"]["holidays"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    after, _ = calendar_features(tmp_path)
    after = after.set_index("date")
    pd.testing.assert_frame_equal(before.loc[:"2025-07-03"], after.loc[:"2025-07-03"])


def test_v0_uses_only_supported_neighbor_types_and_past_starts():
    t = pd.Timestamp("2025-04-01")
    ch = pd.DataFrame(
        {
            "channel_id": [1, 2, 3],
            "object_id": [1, 1, 1],
            "sensor_type": ["known", "known", "unsupported"],
        }
    )
    d = pd.DataFrame(
        {
            "channel_id": [1],
            "date": [t - pd.Timedelta(days=1)],
            "n_events": [1],
            "n_alarm": [0],
            "n_fault": [0],
            "n_num": [0],
            "mean": [np.nan],
            "max": [np.nan],
        }
    )
    e = pd.DataFrame(
        {"channel_id": [2, 3], "start": [t, t - pd.Timedelta(days=1)], "end": [pd.NaT, pd.NaT]}
    )
    batch = baseline_rows(d, e, ch, [t], [1], ["known"])
    live = build_daily(
        d[d.date.lt(t)],
        e[e.start.lt(t) & e.channel_id.isin([1, 2])],
        ch[ch.sensor_type.eq("known")],
        [t],
        only=[1],
    )
    pd.testing.assert_frame_equal(batch[FEATURES], live[FEATURES], check_dtype=False)
    assert batch.loc[0, "obj_in_fault"] == 0
