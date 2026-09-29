import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain import runs as runs_module
from app.domain.ingest import RawEvent, ingest_events
from app.domain.runs import RunError, create_run, delete_run, resolve_run, set_run_status, update_run_clock

MSK = ZoneInfo("Europe/Moscow")


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


def ev(channel_id: int, ts: str, value: str, alarm: bool = False) -> RawEvent:
    return RawEvent(channel_id, at(ts), value, alarm)


BASE = [
    ev(1001, "2026-05-01T10:00:00", "Норма"),
    ev(1001, "2026-05-01T11:00:00", "Неисправен", True),
    ev(1001, "2026-05-01T13:00:00", "Норма"),  # после старта прогона — в прогоне не виден
    ev(1003, "2026-05-01T09:00:00", "18"),
    ev(1003, "2026-05-01T11:30:00", "20"),
    ev(1003, "2026-05-02T09:00:00", "25"),
]
START = at("2026-05-01T12:00:00")


async def rows(session, sql: str, **params) -> list[tuple]:
    return [tuple(r) for r in await session.execute(text(sql), params)]


@pytest.mark.anyio
async def test_snapshot_at_sim_start(session):
    await ingest_events(None, BASE, session=session)
    run = await create_run(session, scenario="test", sim_start=START, speed=10)

    state = await rows(
        session, "SELECT channel_id, value_raw, fault_since FROM run_channel_state(:r) ORDER BY 1", r=run.id
    )
    assert state == [(1001, "Неисправен", at("2026-05-01T11:00:00")), (1003, "20", None)]
    # открытый на старте эпизод — открытой копией в прогоне; базовый эпизод (закрыт в 13:00) в прогоне не виден
    faults = await rows(session, "SELECT run_id, start_ts, end_ts FROM run_fault_episodes(:r)", r=run.id)
    assert faults == [(run.id, at("2026-05-01T11:00:00"), None)]
    daily = await rows(
        session, "SELECT channel_id, date, n_events, n_fault FROM run_channel_daily(:r) ORDER BY 1, 2", r=run.id
    )
    assert daily == [(1001, dt.date(2026, 5, 1), 2, 1), (1003, dt.date(2026, 5, 1), 2, 0)]  # день старта — до 12:00
    hourly = await rows(
        session, "SELECT channel_id, hour, n_events FROM run_channel_hourly(:r) ORDER BY 1, 2", r=run.id
    )
    assert hourly == [
        (1001, at("2026-05-01T10:00:00"), 1),
        (1001, at("2026-05-01T11:00:00"), 1),
        (1003, at("2026-05-01T09:00:00"), 1),
        (1003, at("2026-05-01T11:00:00"), 1),
    ]

    # в прогоне канал восстанавливается — закрывается копия прогона, базовый эпизод не меняется
    await ingest_events(run.id, [ev(1001, "2026-05-01T12:30:00", "Норма")], session=session)
    faults = await rows(session, "SELECT run_id, end_ts FROM fault_episodes ORDER BY run_id NULLS FIRST")
    assert faults == [(None, at("2026-05-01T13:00:00")), (run.id, at("2026-05-01T12:30:00"))]
    status = await session.scalar(
        text("SELECT fault_since FROM channel_state WHERE run_id = :r AND channel_id = 1001"), {"r": run.id}
    )
    assert status is None


@pytest.mark.anyio
async def test_new_run_stops_previous_and_fires_after_commit(session, monkeypatch):
    closed: list[int] = []
    created: list[int] = []
    monkeypatch.setattr(runs_module, "on_run_closed", [closed.append])
    monkeypatch.setattr(runs_module, "on_run_created", [created.append])
    first = await create_run(session, scenario="a", sim_start=START)
    await session.commit()
    assert created == [first.id]
    second = await create_run(session, scenario="b", sim_start=START)
    assert closed == [] and created == [first.id]  # до коммита — ничего
    await session.commit()
    assert closed == [first.id] and created == [first.id, second.id]
    statuses = await rows(session, "SELECT id, status FROM runs ORDER BY id")
    assert statuses == [(first.id, "stopped"), (second.id, "running")]

    await set_run_status(session, second.id, "paused")
    await set_run_status(session, second.id, "running")
    await set_run_status(session, second.id, "finished")
    await session.commit()
    assert closed == [first.id, second.id]
    with pytest.raises(RunError):
        await set_run_status(session, second.id, "running")


@pytest.mark.anyio
async def test_resolve_and_delete(session):
    assert await resolve_run(session, None) is None  # прогонов нет — база
    run = await create_run(session, scenario="a", sim_start=START)
    await ingest_events(run.id, [ev(1001, "2026-05-01T12:10:00", "Обнаружен дым")], session=session)
    assert (await resolve_run(session, None)).id == run.id
    assert await resolve_run(session, 0) is None
    with pytest.raises(RunError):
        await resolve_run(session, 999)

    await session.execute(
        text(
            "INSERT INTO predictions (run_id, model, model_version, channel_id, alarm_id, decision_time, "
            "horizon_start, horizon_end, status) "
            "SELECT run_id, 'smoke_alarm', 'v0', channel_id, id, ts, ts, ts, 'scored' FROM alarms"
        )
    )
    await session.execute(text("UPDATE alarms SET prediction_id = (SELECT id FROM predictions)"))
    deleted = await delete_run(session, run.id)
    assert deleted["alarms"] == 1 and deleted["events"] == 1 and deleted["predictions"] == 1
    assert await session.scalar(text("SELECT count(*) FROM runs")) == 0


@pytest.mark.anyio
async def test_run_events_counter(session):
    """`runs.events` ведёт приём — то же число, что подсчёт по таблицам событий, которым `/runs` считал раньше:
    сырой поток и значимые события; газ, прошедший зону нечувствительности, и повторная доставка — не в счёт."""
    run = await create_run(session, scenario="a", sim_start=START)
    batch = [
        ev(1001, "2026-05-01T12:10:00", "Обнаружен дым", True),
        ev(1002, "2026-05-01T12:11:00", "0.01"),
        ev(1002, "2026-05-01T12:12:00", "0.09"),  # за зоной 0.02 — ещё и в events
        ev(1003, "2026-05-01T12:13:00", "21"),
    ]
    await ingest_events(run.id, batch, session=session)
    await ingest_events(run.id, batch[:2], session=session)  # повторная доставка
    await ingest_events(run.id, [ev(1003, "2026-05-01T12:20:00", "22")], session=session)
    by_tables = await session.scalar(
        text(
            "SELECT (SELECT count(*) FROM events_raw WHERE run_id = :run) + (SELECT count(*) FROM events e "
            "JOIN channels c ON c.id = e.channel_id WHERE e.run_id = :run AND c.stream = 'significant')"
        ),
        {"run": run.id},
    )
    assert await session.scalar(text("SELECT events FROM runs WHERE id = :run"), {"run": run.id}) == by_tables == 5


def test_runs_api(client, db_url):
    import psycopg

    from app.loader import pg_conninfo

    assert client.get("/api/v1/runs").json() == {"items": []}
    with psycopg.connect(pg_conninfo(db_url.render_as_string(hide_password=False)), autocommit=True) as conn:
        run_id = conn.execute(
            "INSERT INTO runs (scenario, sim_start, params) "
            "VALUES ('ppr', now(), '{\"title\": \"Плановая проверка\"}') RETURNING id"
        ).fetchone()[0]
    items = client.get("/api/v1/runs").json()["items"]
    assert items[0]["id"] == run_id and items[0]["scenario_title"] == "Плановая проверка"
    assert items[0]["events"] == 0 and items[0]["live"] is None
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "running"
    assert client.get("/api/v1/runs/999").json()["error"]["code"] == "run_not_found"


@pytest.mark.anyio
async def test_new_run_closes_played_run_as_finished(session):
    """Прошлый прогон, проигранный до конца, закрывается новым как `finished`: по состоянию источника (`played`) или
    по часам на `sim_end`. Прерванный посреди сценария — `stopped`."""
    start = dt.datetime(2026, 3, 28, 8, tzinfo=dt.UTC)
    end = start + dt.timedelta(hours=1)

    async def status(run_id: int) -> tuple:
        row = (await session.execute(text("SELECT status, sim_now FROM runs WHERE id = :r"), {"r": run_id})).one()
        return tuple(row)

    a = await create_run(session, scenario="a", sim_start=start, sim_end=end)
    await update_run_clock(session, a.id, start + dt.timedelta(minutes=55))  # шлюз ещё не догнал конец
    b = await create_run(session, scenario="b", sim_start=start, sim_end=end, played=[a.id])
    assert await status(a.id) == ("finished", end)
    await update_run_clock(session, b.id, end)  # часы дошли до конца, State ещё не перенесён
    c = await create_run(session, scenario="c", sim_start=start, sim_end=end)
    assert await status(b.id) == ("finished", end)
    await update_run_clock(session, c.id, start + dt.timedelta(minutes=10))
    await create_run(session, scenario="d", sim_start=start, sim_end=end)
    assert await status(c.id) == ("stopped", start + dt.timedelta(minutes=10))
