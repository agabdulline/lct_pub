"""Вскрытие под охраной в прогоне (docs/architecture/access.md §4–5): приём событий, такт по водяному знаку,
заявка, конец прогона, API ленты."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain.access_runs import sync_run
from app.domain.ingest import RawEvent, ingest_events
from app.domain.runs import create_run, get_run, set_run_status

MSK = ZoneInfo("Europe/Moscow")
GUARD, DOOR, HATCH, MOTION = 1101, 1102, 1103, 1104
OTHERS = [1105, 1106, 1107, 1108, 1109]


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"2026-04-16T{s}").replace(tzinfo=MSK)


async def catalog(session) -> None:
    rows = [
        (GUARD, "Диспетчерский контроль", "Состояние охраны", "Охрана А", "significant"),
        (DOOR, "Контроль доступа", "КД Дверь", "КД дверь ПК50", "significant"),
        (HATCH, "Контроль доступа", "КД Люк", "КД люк ПК45", "significant"),
        (MOTION, "Охранная сигнализация", "Датчик движения", "ОД ПК49", "raw"),
    ] + [(c, "Температурная подсистема", "Датчик температуры", f"Темп {c}", "significant") for c in OTHERS]
    for cid, system, sensor, name, stream in rows:
        await session.execute(
            text(
                "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, stream) "
                "VALUES (:id, 100, :system, :sensor, :tag, :name, :stream)"
            ),
            {"id": cid, "system": system, "sensor": sensor, "tag": f"15-11.9.{cid}.", "name": name, "stream": stream},
        )
    await session.commit()


def e(ts: str, channel: int, value: str, alarm: bool = False) -> RawEvent:
    return RawEvent(channel, at(ts), value, alarm)


async def ingest(session, run_id, *events) -> None:
    await ingest_events(run_id, list(events), session=session)
    await session.commit()


async def incidents(session, run_id) -> list[tuple]:
    rows = await session.execute(
        text(
            "SELECT to_char(start_ts AT TIME ZONE 'Europe/Moscow', 'HH24:MI:SS'), status, priority, end_reason, "
            "openings, hatch_openings, motion, work_order_id IS NOT NULL FROM access_incidents "
            "WHERE run_id = :r ORDER BY start_ts"
        ),
        {"r": run_id},
    )
    return [tuple(r) for r in rows]


async def build(session) -> tuple[int, list]:
    """Охрана с 08:00 в базе; прогон с 09:00: инцидент со снятием, волна переопроса, второй инцидент при открытой
    заявке — до конца прогона."""
    await catalog(session)
    await ingest(session, None, e("08:00:00", GUARD, "На охране"), e("08:00:00", GUARD, "16.04.2026 08:00:00"))
    run = await create_run(session, scenario="guard", sim_start=at("09:00:00"), sim_end=at("12:30:00"))
    await session.commit()
    steps = []

    async def step(*events):
        await ingest(session, run.id, *events)
        found = await get_run(session, run.id)
        await session.refresh(found)
        summary, notes = await sync_run(session, found)
        await session.commit()
        steps.append((summary, [n.type for n in notes], await incidents(session, run.id)))

    # дверь под охраной, движение; водяной знак 10:06 — кандидат дожил до 5 мин (с запасом 2 с на волну)
    await step(
        e("10:00:00", DOOR, "Не замкнут", True),
        e("10:00:30", DOOR, "Норма"),
        e("10:01:00", MOTION, "Обнаружено движение"),
        e("10:06:00", OTHERS[0], "18"),
    )
    # люк, снятие охраны
    await step(
        e("10:08:00", HATCH, "Не замкнут", True), e("10:13:00", GUARD, "Снято с охраны"), e("10:14:00", OTHERS[0], "19")
    )
    # снова на охране; волна переопроса — не открытие; потом второе открытие при открытой заявке
    await step(
        e("11:00:00", GUARD, "На охране"),
        e("11:30:00", DOOR, "Не замкнут"),
        *[e("11:30:01", c, "20") for c in OTHERS[:4]],
        e("11:40:00", OTHERS[0], "21"),
    )
    await step(e("12:00:00", DOOR, "Не замкнут", True), e("12:10:00", OTHERS[0], "22"))
    return run.id, steps


@pytest.mark.anyio
async def test_access_incidents_in_run(session):
    run_id, steps = await build(session)
    first, second, wave, repeat = steps
    assert first[1] == ["work_order.created", "access_incident.created"]
    assert first[2] == [("10:00:00", "open", "medium", None, 1, 0, True, True)]
    # снятие закрывает инцидент; люк повышает приоритет инцидента
    assert second[1] == ["access_incident.updated"]
    assert second[2] == [("10:00:00", "closed", "high", "disarmed", 2, 1, True, True)]
    assert wave[1] == [] and len(wave[2]) == 1  # волна переопроса — не открытие
    # второй инцидент: заявка подобъекта ещё открыта — ссылка на неё, новой нет
    assert repeat[1] == ["access_incident.created"]
    assert repeat[2][1] == ("12:00:00", "open", "medium", None, 1, 0, False, True)
    orders = await session.execute(
        text("SELECT rule, channel_id, priority FROM work_orders WHERE run_id = :r"), {"r": run_id}
    )
    assert [tuple(o) for o in orders] == [("access_open_armed", GUARD, "medium")]
    ids = (
        await session.execute(
            text("SELECT DISTINCT work_order_id FROM access_incidents WHERE run_id = :r"), {"r": run_id}
        )
    ).all()
    assert len(ids) == 1

    # повторный такт ничего не меняет; конец прогона закрывает открытый инцидент с run_end
    run = await get_run(session, run_id)
    assert (await sync_run(session, run))[1] == []
    await set_run_status(session, run_id, "finished")
    await session.commit()
    await session.refresh(run)
    _, notes = await sync_run(session, run, final=True)
    await session.commit()
    assert [n.type for n in notes] == ["access_incident.updated"]
    last = (await incidents(session, run_id))[1]
    assert last[1:4] == ("closed", "medium", "run_end")
    end = await session.scalar(
        text("SELECT end_ts FROM access_incidents WHERE run_id = :r AND end_reason = 'run_end'"), {"r": run_id}
    )
    assert end == at("12:30:00")


def msk(iso: str) -> str:
    return dt.datetime.fromisoformat(iso).astimezone(MSK).strftime("%H:%M:%S")


def test_access_api(client):
    import asyncio

    from app.db.session import SessionLocal

    async def setup() -> int:
        async with SessionLocal() as s:
            run_id, _ = await build(s)
            # базовый инцидент до старта прогона — в представлении прогона; начавшийся после старта — нет
            for start, end in ((at("07:00:00"), at("07:20:00")), (at("09:30:00"), at("09:40:00"))):
                await s.execute(
                    text(
                        "INSERT INTO access_incidents (run_id, object_id, sub_object_id, guard_channel_id, status, "
                        "start_ts, qualified_ts, end_ts, end_reason, priority) "
                        "VALUES (NULL, 10, 100, :g, 'closed', :s, :s, :e, 'quiet', 'medium')"
                    ),
                    {"g": GUARD, "s": start, "e": end},
                )
            await s.execute(text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at("12:20:00"), "r": run_id})
            await s.commit()
            return run_id

    run_id = asyncio.run(setup())
    page = client.get("/api/v1/access-incidents", params={"run": run_id}).json()
    assert [(msk(i["start_ts"]), i["status"], i["run_id"]) for i in page["items"]] == [
        ("12:00:00", "open", run_id),
        ("10:00:00", "closed", run_id),
        ("07:00:00", "closed", None),
    ]
    assert page["counts"] == {"candidate": 0, "open": 1, "closed": 2, "dismissed": 0}
    item = page["items"][1]
    assert item["channels"] == [
        {"id": DOOR, "name": "КД дверь ПК50", "sensor_type": "КД Дверь"},
        {"id": HATCH, "name": "КД люк ПК45", "sensor_type": "КД Люк"},
    ]
    assert (item["disarmed_after_min"], item["flags"]["motion"], item["priority"]) == (13, True, "high")
    assert item["source_note"].startswith("Метка правил охраны")
    assert client.get("/api/v1/access-incidents", params={"run": run_id, "priority": "high"}).json()["total"] == 1

    detail = client.get(f"/api/v1/access-incidents/{item['id']}", params={"run": run_id}).json()
    assert [(o["channel_id"], msk(o["ts"]), o["is_alarm"]) for o in detail["opening_events"]] == [
        (DOOR, "10:00:00", True),
        (HATCH, "10:08:00", True),
    ]
    assert [(msk(g["ts"]), g["value"]) for g in detail["guard_events"]] == [
        ("08:00:00", "На охране"),
        ("10:13:00", "Снято с охраны"),
    ]
    r = client.patch(
        f"/api/v1/access-incidents/{item['id']}",
        params={"run": run_id},
        json={"resolution": "works", "comment": "бригада"},
    )
    assert r.status_code == 200 and (r.json()["resolution"], r.json()["comment"]) == ("works", "бригада")
    assert (
        client.patch(
            f"/api/v1/access-incidents/{item['id']}", params={"run": run_id}, json={"resolution": "x"}
        ).status_code
        == 422
    )
    assert client.get("/api/v1/access-incidents/999999", params={"run": run_id}).status_code == 404


@pytest.mark.anyio
async def test_ingest_asks_access_sync(session):
    """Приём просит пересчёт правила (задача с дебаунсом 2 с), когда в пачке двери, люки или охрана либо у прогона
    идут кандидаты и инциденты: их двигает любое новое событие — водяной знак."""
    await catalog(session)
    run = await create_run(session, scenario="guard", sim_start=at("09:00:00"), sim_end=at("12:30:00"))
    await session.commit()

    async def asks(*events) -> bool:
        result = await ingest_events(run.id, list(events), session=session)
        await session.commit()
        return result.access_sync

    assert not await asks(e("09:10:00", OTHERS[0], "18"))  # ни дверей, ни инцидентов
    assert await asks(e("09:20:00", DOOR, "Не замкнут"))
    assert await asks(e("09:21:00", GUARD, "На охране"))
    await session.execute(
        text(
            "INSERT INTO access_incidents (run_id, object_id, sub_object_id, guard_channel_id, status, start_ts) "
            "VALUES (:r, 10, 100, :g, 'candidate', :t)"
        ),
        {"r": run.id, "g": GUARD, "t": at("09:30:00")},
    )
    await session.commit()
    assert await asks(e("09:40:00", OTHERS[0], "19"))  # идёт кандидат — двигает любое событие
    assert not (await ingest_events(None, [e("08:00:00", DOOR, "Норма")], session=session)).access_sync  # база


def test_sync_task_holds_run_lock(seeded, monkeypatch):
    """Задача `access.sync`: один пересчёт прогона за раз — занятая блокировка ничего не пишет."""
    import asyncio

    from app.core.redis import try_lock, unlock
    from app.db.session import SessionLocal
    from app.domain.access_runs import lock_key
    from app.tasks import access as task

    async def setup() -> int:
        async with SessionLocal() as s:
            await catalog(s)
            await ingest(s, None, e("08:00:00", GUARD, "На охране"))
            run = await create_run(s, scenario="guard", sim_start=at("09:00:00"), sim_end=at("12:30:00"))
            await s.commit()
            await ingest(s, run.id, e("10:00:00", DOOR, "Не замкнут", True), e("10:06:00", OTHERS[0], "18"))
            return run.id

    async def count(run_id: int) -> int:
        async with SessionLocal() as s:
            return await s.scalar(text("SELECT count(*) FROM access_incidents WHERE run_id = :r"), {"r": run_id})

    run_id = asyncio.run(setup())
    assert asyncio.run(try_lock(lock_key(run_id), 60))
    try:
        assert task.sync.run(run_id) == {"run_id": run_id, "skipped": "locked"}
        assert asyncio.run(count(run_id)) == 0
    finally:
        asyncio.run(unlock(lock_key(run_id)))
    assert task.sync.run(run_id)["created"] == 1
    assert asyncio.run(count(run_id)) == 1 and task.sync.run(run_id)["created"] == 0


def test_enqueue_sync_one_pending_task_per_run(monkeypatch):
    """Пока задача ждёт (ключ pending), новые вызовы ничего не шлют; после снятия ключа — снова ровно одна."""
    import asyncio

    from app.core.config import settings
    from app.core.redis import async_client
    from app.domain.access_runs import clear_pending, enqueue_sync, pending_key
    from app.tasks.celery_app import celery_app

    sent: list[tuple] = []
    monkeypatch.setattr(settings, "enqueue_tasks", True)
    monkeypatch.setattr(celery_app, "send_task", lambda name, **kw: sent.append((name, kw["args"])))
    run_id = 987654

    async def flow() -> None:
        r = async_client()
        await r.delete(pending_key(run_id, False), pending_key(run_id, True))
        await r.aclose()
        results = [await enqueue_sync(run_id) for _ in range(20)]
        assert results.count(True) == 1 and len(sent) == 1
        assert await enqueue_sync(run_id, final=True) and len(sent) == 2  # final has its own key
        await clear_pending(run_id)  # the task has started
        assert await enqueue_sync(run_id) and not await enqueue_sync(run_id)
        assert len(sent) == 3
        await clear_pending(run_id)
        await clear_pending(run_id, True)

    asyncio.run(flow())
