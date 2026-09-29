import pandas as pd
from fastapi.testclient import TestClient
from synthetic import at, channels, events

from app import history
from app.main import app
from app.scoring import object_history, score_object


def test_models_loaded_from_repository():
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["models_loaded"] >= 2
        assert health["scoring"] == "baseline-v0" and health["alarm_versions"] == ["baseline-v0", "v3"]
        models = {m["name"]: m for m in client.get("/v1/models").json()["models"]}
        v3 = models["alarm_all_d7"]
        assert v3["features"] == 227 and v3["scoring"] == "available" and v3["label"] == "v3" and not v3["active"]
        base = models["smoke_alarm"]
        assert base["family"] == "baseline" and base["threshold"] is not None and base["scoring"] == "available"
        assert base["task"] == "smoke_alarm" and base["active"]


def test_score_object_on_synthetic_history():
    from app.main import state

    with TestClient(app):
        model = next(m for m in state["models"] if m.scoring and m.active and m.task == "smoke_alarm")
        res = score_object(model, events(), channels(), [(11, at(20, 10)), (12, at(35, 12, 30))], explain=True)
    scored, skipped = res
    assert scored["status"] == "scored" and 0.0 <= scored["score"] <= 1.0
    assert isinstance(scored["alert"], bool) and len(scored["factors"]) == 5
    assert skipped["status"] == "skipped" and skipped["skip_reason"] == "fault_recent_24h"
    assert isinstance(scored["object_history"], bool) and skipped["object_history"] is None


def test_object_history_needs_both_object_and_loop_outcomes():
    row = {"obj_past_n": 5.0, "loop_past_n": 3.0}
    assert object_history(pd.Series(row))
    assert not object_history({**row, "loop_past_n": 0.0})  # новый шлейф на известном объекте — модель слепа
    assert not object_history({**row, "obj_past_n": 0.0})
    assert not object_history({**row, "loop_past_n": float("nan")})


def test_alarm_score_endpoint_with_history_from_run_view(monkeypatch):
    ev = events()
    monkeypatch.setattr(history, "objects_of", lambda ids: {i: 1 for i in ids if i in (11, 12, 13, 14)})
    monkeypatch.setattr(history, "smoke_channels", lambda obj: channels())
    monkeypatch.setattr(history, "smoke_events", lambda run, obj, ids, since, until: ev[ev.ts <= pd.Timestamp(until)])
    with TestClient(app) as client:
        body = client.post(
            "/v1/alarm/score",
            json={
                "run_id": 7,
                "alarms": [
                    {"channel_id": 11, "ts": "2025-03-23T10:00:00+03:00"},
                    {"channel_id": 99, "ts": "2025-03-23T10:00:00+03:00"},
                ],
            },
        ).json()
    first, unknown = body["results"]
    assert body["model"] == "baseline-v0"
    assert first["status"] == "scored" and first["decision_time"] == "2025-03-23T10:00:01+03:00"
    assert unknown["skip_reason"] == "unknown_channel"


def test_daily_score_on_synthetic_inputs(monkeypatch):
    from test_daily import BASE, inputs

    ch, daily, eps = inputs()
    monkeypatch.setattr(history, "daily_inputs", lambda run, date, types, ids, days: (ch, daily, eps))
    with TestClient(app) as client:
        body = client.post(
            "/v1/daily/score", json={"date": str((BASE + pd.Timedelta(days=20)).date()), "explain": True}
        ).json()
    assert body["model"] == "baseline-v0"
    by_id = {r["channel_id"]: r for r in body["results"]}
    assert by_id[1]["status"] == "scored" and 0.0 <= by_id[1]["score"] <= 1.0 and len(by_id[1]["factors"]) == 5
    assert by_id[2]["status"] == "skipped" and by_id[2]["skip_reason"] == "no_coverage"
    assert by_id[1]["target_start"].startswith("2025-03-24")


def test_daily_score_skip_reasons_for_requested_channels(monkeypatch):
    from test_daily import BASE, inputs

    ch, daily, eps = inputs()
    monkeypatch.setattr(history, "daily_inputs", lambda run, date, types, ids, days: (ch, daily, eps))
    monkeypatch.setattr(history, "known_channels", lambda ids: {i for i in ids if i == 50})  # 50 — не из 8 типов
    with TestClient(app) as client:
        body = client.post(
            "/v1/daily/score",
            json={"date": str((BASE + pd.Timedelta(days=20)).date()), "channel_ids": [999, 1, 50, 1]},
        ).json()
    by_id = {r["channel_id"]: r for r in body["results"]}
    # порядок — как в запросе, без повторов; канал 2 того же объекта не запрошен — в ответе его нет
    assert [r["channel_id"] for r in body["results"]] == [999, 1, 50]
    assert by_id[1]["status"] == "scored" and by_id[1]["factors"] == []
    assert by_id[50]["status"] == "skipped" and by_id[50]["skip_reason"] == "unsupported_type"
    assert by_id[999]["status"] == "skipped" and by_id[999]["skip_reason"] == "unknown_channel"
    assert by_id[999]["score"] is None and by_id[999]["target_end"].startswith("2025-04-22")
    assert all(r["seen_channel"] is None and r["object_history"] is None for r in body["results"])


def test_daily_score_without_supported_channels(monkeypatch):
    from test_daily import BASE, inputs

    ch, daily, eps = inputs()
    empty = (ch.iloc[:0], daily.iloc[:0], eps.iloc[:0])
    monkeypatch.setattr(history, "daily_inputs", lambda run, date, types, ids, days: empty)
    monkeypatch.setattr(history, "known_channels", lambda ids: {50})
    date = str((BASE + pd.Timedelta(days=20)).date())
    with TestClient(app) as client:
        none = client.post("/v1/daily/score", json={"date": date, "channel_ids": []})
        only_missing = client.post("/v1/daily/score", json={"date": date, "channel_ids": [50, 999]})
    assert none.status_code == 200 and none.json()["results"] == []
    assert [r["skip_reason"] for r in only_missing.json()["results"]] == ["unsupported_type", "unknown_channel"]
