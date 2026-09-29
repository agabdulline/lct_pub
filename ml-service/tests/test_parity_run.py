"""Сверка прогона с обучением: классификация расхождений на синтетике — БД и архива в тестах нет."""

import pandas as pd

from ml.baseline.parity_run import compare

T = pd.Timestamp("2026-01-16 10:00:00")
S = pd.Timedelta(seconds=1)


def frames():
    pred = pd.DataFrame(
        {
            "channel_id": [1, 2, 3],
            "decision_time": [T + S, T + 60 * S, T + 120 * S],
            "status": ["scored", "scored", "skipped"],
            "skip_reason": [None, None, "decided_within_24h"],
            "score": [0.500001, 0.2, None],
            "alert": [True, False, None],
        }
    )
    off = pd.DataFrame(
        {
            "channel_id": [1, 2],
            "decision_time": [T + S, T + 60 * S],
            "offline_score": [0.5000013, 0.2],
            "offline_alert": [True, False],
        }
    )
    alarms = pd.DataFrame({"channel_id": [1, 2, 3], "alarm_ts": [T, T + 59 * S, T + 119 * S]})
    return pred, off, alarms


def test_matching_run_is_ok_up_to_rounding():
    r = compare(*frames())
    assert r["ok"] and r["matched"] == 2 and r["max_abs_delta"] < 1e-6
    assert r["service_skipped"] == {"decided_within_24h": 1}


def test_mismatches_are_classified():
    pred, off, alarms = frames()
    # 4 — тревога в прогоне есть, прогноза нет; 5 — тревоги в прогоне нет; 3 — сервис пропустил допустимое
    off = pd.concat(
        [
            off.assign(offline_score=[0.6, 0.2], offline_alert=[True, False]),
            pd.DataFrame(
                {
                    "channel_id": [4, 5, 3],
                    "decision_time": [T + 300 * S, T + 400 * S, T + 120 * S],
                    "offline_score": [0.1, 0.1, 0.1],
                    "offline_alert": [False, False, False],
                }
            ),
        ],
        ignore_index=True,
    )
    alarms = pd.concat([alarms, pd.DataFrame({"channel_id": [4], "alarm_ts": [T + 299 * S]})], ignore_index=True)
    r = compare(pred, off, alarms)
    assert not r["ok"]
    assert list(r["score_mismatch"].channel_id) == [1] and r["alert_mismatch"].empty
    assert list(r["unscored"].channel_id) == [4] and list(r["missing_in_run"].channel_id) == [5]
    assert list(r["skipped_but_offline"].channel_id) == [3]
