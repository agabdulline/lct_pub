"""Событийная v3 в сервисе против полного расчёта их кода (ml/v3, PR #114) — на синтетическом журнале (synthetic_v3).

Эталон — их код целиком по всем объектам сразу (build_alarm_rows + build_extended, исходы — до конца данных, как
у их predict_alarms). Сервис получает тревоги пачками в порядке времени, как их присылает worker, и считает по
истории одного объекта с кэшем решений. Совпадение на журнале СМВУ — все тревоги I полугодия 2026 — проверяет
`python -m ml.v3_bridge.offline` (ml/reports/v3/SERVICE.md).
"""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from synthetic_v3 import END, SMOKE, channels, coverage, events

from app import history
from app.main import app, scoring_model
from app.scoring import score_object_v3
from ml.v3.src.event_model import build_alarm_rows
from ml.v3.src.neighbors import build_extended
from ml.v3_bridge.alarm import CATALOG_COLUMNS, AlarmRows, alarm_rows, decide, frame, registry
from ml.v3_bridge.offline import batches

KEY = ["sensor_id", "decision_time"]


@pytest.fixture(scope="module")
def world() -> dict:
    ch, ev, cov = channels(), events(), coverage()
    raw = ch.rename(columns=CATALOG_COLUMNS)[list(CATALOG_COLUMNS.values())]
    v3ev = frame(ev)
    rows = build_alarm_rows(v3ev, pd.DatetimeIndex(sorted(cov)), str(END.date()), 24)
    alarms = ev[ev.value == SMOKE][["channel_id", "ts"]].drop_duplicates()
    return {
        "channels": ch,
        "events": ev,
        "coverage": cov,
        "reference": build_extended(rows, v3ev, raw),
        "alarms": alarms.rename(columns={"channel_id": "sensor_id", "ts": "alarm_ts"}),
    }


@pytest.fixture(scope="module")
def v3():
    with TestClient(app):
        yield scoring_model(version="v3")


def by_object(world: dict):
    obj_of = world["channels"].set_index("channel_id").object_id
    alarms = world["alarms"].assign(object_id=world["alarms"].sensor_id.map(obj_of))
    for obj, a in alarms.groupby("object_id"):
        ch = world["channels"][world["channels"].object_id == obj]
        yield obj, ch, world["events"][world["events"].channel_id.isin(ch.channel_id)], a[["sensor_id", "alarm_ts"]]


def test_service_scores_match_reference(world, v3):
    """Решения и оценки сервиса по всем тревогам = эталон их кода (оценки в ответе округлены до 6 знаков)."""
    ref = world["reference"].set_index(KEY)
    ref_score = pd.Series(v3.model.predict_proba(ref[v3.config["features"]])[:, 1], index=ref.index)
    scored, reasons = {}, []
    for _, ch, ev, alarms in by_object(world):
        rows = AlarmRows()
        for batch in batches(alarms):
            until = max(ts for _, ts in batch)
            for r in score_object_v3(v3, ev[ev.ts <= until], ch, world["coverage"], rows, batch, explain=False):
                if r["status"] == "scored":
                    scored[(r["channel_id"], r["decision_time"])] = r
                else:
                    reasons.append(r["skip_reason"])
    assert set(scored) == set(ref.index), "набор решений сервиса и эталона разошёлся"
    assert max(abs(r["score"] - ref_score[k]) for k, r in scored.items()) <= 5e-7
    assert all(r["alert"] == (ref_score[k] >= v3.threshold) for k, r in scored.items())
    assert all(isinstance(r["seen_channel"], bool) and isinstance(r["object_history"], bool) for r in scored.values())
    expected = {"history_lt_7d", "coverage_gap", "fault_same_second", "state_not_normal", "fault_recent_24h",
                "decided_within_24h"}  # fmt: skip
    assert set(reasons) == expected


def test_features_bitwise_and_scores_exact(world, v3):
    """Признаки пути сервиса побитово равны эталону, оценки — с допуском 1e-12 (docs/architecture/ml-service.md §6)."""
    names = v3.config["features"]
    _, ch, ev, alarms = next(by_object(world))
    ev, reg, rows, parts = frame(ev), registry(ch), AlarmRows(), []
    for batch in batches(alarms):
        until = max(ts for _, ts in batch) + pd.Timedelta(seconds=1)
        parts.append(decide(ev, reg, batch, rows.get(ev, world["coverage"], until)))
    got = pd.concat([p for p in parts if len(p)], ignore_index=True)
    want = world["reference"][world["reference"].sensor_id.isin(ch.channel_id)]
    x = got.merge(want, on=KEY, suffixes=("", "_ref"))
    assert len(x) == len(got) == len(want) > 50
    for c in names:
        a, b = x[c], x[f"{c}_ref"]
        assert ((a == b) | (a.isna() & b.isna())).all(), c
    scores = v3.model.predict_proba(x[names])[:, 1]
    ref = v3.model.predict_proba(x[[f"{c}_ref" for c in names]].set_axis(names, axis=1))[:, 1]
    assert np.abs(scores - ref).max() <= 1e-12


def test_incremental_rows_equal_full_recompute(world):
    """Кэш решений пересчитывает только изменившиеся каналы — и на каждой пачке получает то же, что полный пересчёт."""
    _, ch, ev, alarms = next(by_object(world))
    ev, rows, cov = frame(ev), AlarmRows(), pd.DatetimeIndex(sorted(world["coverage"]))
    cuts = [max(ts for _, ts in b) + pd.Timedelta(seconds=1) for b in batches(alarms)]
    partial = 0
    for until in cuts:
        pd.testing.assert_frame_equal(rows.get(ev, world["coverage"], until), alarm_rows(ev, cov, until))
        partial += rows.recomputed < ch.channel_id.nunique()
    assert partial > len(cuts) // 2  # чаще всего пересчитываются не все каналы
    pd.testing.assert_frame_equal(rows.get(ev, world["coverage"], cuts[0]), alarm_rows(ev, cov, cuts[0]))  # назад


def test_alarm_score_endpoint_versions(monkeypatch, world):
    obj, ch, ev, alarms = next(by_object(world))
    rows = AlarmRows()
    monkeypatch.setattr(history, "objects_of", lambda ids: {i: obj for i in ids if i in set(ch.channel_id)})
    monkeypatch.setattr(history, "smoke_channels", lambda o: ch)
    monkeypatch.setattr(history, "smoke_events", lambda run, o, ids, since, until: ev[ev.ts <= pd.Timestamp(until)])
    monkeypatch.setattr(history, "coverage", lambda run: set(world["coverage"]))
    monkeypatch.setattr(history, "v3_rows", lambda run, o: rows)
    sid, ts = alarms.iloc[-1]
    body = {"run_id": None, "alarms": [{"channel_id": int(sid), "ts": f"{ts.isoformat()}+03:00"}]}
    with TestClient(app) as client:
        v3 = client.post("/v1/alarm/score", json={**body, "model_version": "v3"}).json()
        v0 = client.post("/v1/alarm/score", json={**body, "model_version": "baseline-v0"}).json()
        default = client.post("/v1/alarm/score", json=body).json()
        unknown = client.post("/v1/alarm/score", json={**body, "model_version": "v9"})
    assert v3["model"] == "v3" and v3["results"][0]["model"] == "v3"
    assert v0["model"] == default["model"] == "baseline-v0"
    assert v3["results"][0]["threshold"] == pytest.approx(0.48862178)
    assert set(v3["results"][0]) == set(v0["results"][0])  # один формат результата у обеих версий
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "unknown_version"


def test_models_describe_v3_and_compare():
    with TestClient(app) as client:
        models = client.get("/v1/models").json()["models"]
        compare = client.get("/v1/models/compare", params={"model": "smoke_alarm"})
        missing = client.get("/v1/models/compare", params={"model": "daily"})
    alarm = {m["label"]: m for m in models if m["task"] == "smoke_alarm"}
    assert set(alarm) == {"baseline-v0", "v3"}
    assert alarm["baseline-v0"]["active"] and not alarm["v3"]["active"]
    v3 = alarm["v3"]
    assert v3["family"] == "v3" and v3["scoring"] == "available" and v3["features"] == 227
    assert v3["offline"]["test"]["decisions"] == 6512 and v3["offline"]["test"]["tp"] == 324
    assert compare.status_code == 200 and set(compare.json()["versions"]) == {"baseline-v0", "v3"}
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "compare_not_found"


def test_v3_rows_cache_keeps_recent_runs():
    history._v3_rows.clear()
    first = history.v3_rows(1, 5)
    assert history.v3_rows(1, 5) is first
    history.v3_rows(2, 5)
    history.v3_rows(2, 6)
    history.v3_rows(3, 5)
    assert {k[0] for k in history._v3_rows} == {2, 3}
    history.reload(2)
    assert {k[0] for k in history._v3_rows} == {3}
    history._v3_rows.clear()
