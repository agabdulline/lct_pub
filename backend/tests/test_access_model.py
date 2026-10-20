"""Модель доступа в прогоне (docs/architecture/access.md §4, lct-8f): модель ускоряет, правило страхует.

«Нужна реакция» — инцидент и заявка сразу при первом открытии; «вероятно свой» — кандидат в ленте без заявки, через
5 мин без снятия охраны инцидент заводит правило; сервис недоступен — правило, как без модели."""

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.domain.access_runs import sync_run
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.runs import create_run, get_run
from tests.test_access_runs import DOOR, GUARD, MSK, OTHERS, at, catalog, e, ingest

FIXTURES = Path(__file__).parent / "fixtures" / "ml"
pytestmark = pytest.mark.anyio


def answer(verdicts: dict[str, bool], calls: list):
    """Ответ сервиса в форме снимка `access_score.json`: тревога модели по времени открытия (МСК, ЧЧ:ММ:СС)."""
    shape = json.loads((FIXTURES / "access_score.json").read_text(encoding="utf-8"))

    async def score_access(self, run_id, points):
        calls.append(points)
        results = []
        for p in points:
            alert = verdicts[dt.datetime.fromisoformat(p["start_ts"]).astimezone(MSK).strftime("%H:%M:%S")]
            results.append(shape["results"][0] | {"sub_object_id": p["sub_object_id"], "start_ts": p["start_ts"],
                                                  "score": 0.9 if alert else 0.2, "alert": alert})  # fmt: skip
        return shape | {"results": results}

    return score_access


async def run_with(session, steps):
    await catalog(session)
    await ingest(session, None, e("08:00:00", GUARD, "На охране"))
    run = await create_run(session, scenario="guard", sim_start=at("09:00:00"), sim_end=at("12:30:00"))
    await session.commit()
    out = []
    for events in steps:
        await ingest(session, run.id, *events)
        found = await get_run(session, run.id)
        await session.refresh(found)
        _, notes = await sync_run(session, found)
        await session.commit()
        rows = await session.execute(
            text(
                "SELECT to_char(start_ts AT TIME ZONE 'Europe/Moscow', 'HH24:MI:SS'), status, verdict, decided_by, "
                "to_char(qualified_ts AT TIME ZONE 'Europe/Moscow', 'HH24:MI:SS'), end_reason, "
                "work_order_id IS NOT NULL, model_note FROM access_incidents WHERE run_id = :r ORDER BY start_ts"
            ),
            {"r": run.id},
        )
        out.append(([n.type for n in notes], [tuple(r) for r in rows]))
    return run, out


async def test_model_opens_incident_at_once(session, monkeypatch):
    calls = []
    monkeypatch.setattr(MlClient, "score_access", answer({"10:00:00": True}, calls))
    run, steps = await run_with(
        session,
        [
            [e("10:00:00", DOOR, "Не замкнут", True), e("10:00:05", OTHERS[0], "18")],
            # охрану сняли через 3 мин: по правилу — «снято после открытия», у модели — ложная тревога
            [e("10:03:00", GUARD, "Снято с охраны"), e("10:03:05", OTHERS[0], "19")],
        ],
    )
    (notes, rows), (_, after) = steps
    # водяной знак 10:00:05: правило ждало бы до 10:05, модель решила в момент классификации открытия (+2 с)
    assert rows == [("10:00:00", "open", "reaction", "model", "10:00:02", None, True, None)]
    assert "access_incident.created" in notes and "work_order.created" in notes
    assert after == [("10:00:00", "closed", "reaction", "model", "10:00:02", "disarmed", True, None)]
    (point,) = calls[0]  # оценка — один раз, точка — первое открытие кандидата
    assert len(calls) == 1
    assert (point["channel_id"], point["is_alarm"], point["hatch"]) == (DOOR, True, False)
    assert point["guard_channel_id"] == GUARD
    assert point["armed_since"].startswith("2026-04-16T05:00:00")


async def test_own_verdict_waits_for_rule(session, monkeypatch, client):
    monkeypatch.setattr(MlClient, "score_access", answer({"10:00:00": False}, []))
    run, steps = await run_with(
        session,
        [
            [e("10:00:00", DOOR, "Не замкнут"), e("10:00:05", OTHERS[0], "18")],
            [e("10:06:00", OTHERS[0], "19")],  # 5 мин без снятия — правило
        ],
    )
    (notes, rows), (later_notes, later) = steps
    assert rows == [("10:00:00", "candidate", "own", None, None, None, False, None)]
    assert notes == ["access_incident.updated"]  # строка ленты «вероятно свой» без заявки
    assert later == [("10:00:00", "open", "own", "rule", "10:05:00", None, True, None)]
    assert "access_incident.created" in later_notes
    feed = client.get("/api/v1/access-incidents", params={"run": run.id}).json()
    (item,) = feed["items"]
    assert (item["verdict"], item["decided_by"], item["score"]) == ("own", "rule", 0.2)
    assert item["source_note"].startswith("Метка правил охраны")


async def test_model_unavailable_rule_decides(session, monkeypatch):
    async def down(self, run_id, points):
        raise MlUnavailable("connection refused")

    monkeypatch.setattr(MlClient, "score_access", down)
    _, steps = await run_with(
        session, [[e("10:00:00", DOOR, "Не замкнут"), e("10:00:05", OTHERS[0], "18")], [e("10:06:00", OTHERS[0], "19")]]
    )
    (_, rows), (_, later) = steps
    assert rows == [("10:00:00", "candidate", None, None, None, None, False, "ml_unavailable")]
    assert later == [("10:00:00", "open", None, "rule", "10:05:00", None, True, "ml_unavailable")]


async def test_own_candidate_visible_in_feed(session, monkeypatch, client):
    monkeypatch.setattr(MlClient, "score_access", answer({"10:00:00": False}, []))
    run, _ = await run_with(session, [[e("10:00:00", DOOR, "Не замкнут"), e("10:00:05", OTHERS[0], "18")]])
    feed = client.get("/api/v1/access-incidents", params={"run": run.id}).json()
    (item,) = feed["items"]
    assert (item["status"], item["verdict"], item["priority"]) == ("candidate", "own", None)
    assert item["work_order_id"] is None
    assert feed["counts"]["candidate"] == 1
