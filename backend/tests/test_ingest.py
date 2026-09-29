import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain.ingest import RawEvent, ingest_events

pytestmark = pytest.mark.anyio
MSK = ZoneInfo("Europe/Moscow")


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


def ev(channel_id: int, ts: str, value: str, alarm: bool = False, event_id: int | None = None) -> RawEvent:
    return RawEvent(channel_id, dt.datetime.fromisoformat(ts), value, alarm, event_id)


async def rows(session, sql: str, **params) -> list[tuple]:
    return [tuple(r) for r in await session.execute(text(sql), params)]


async def test_ingest_writes_and_deduplicates(session):
    batch = [
        ev(1001, "2026-05-01T12:36:57+03:00", "Обнаружен дым", True, 1),
        ev(1002, "2026-05-01T12:36:58+03:00", "0.05", event_id=2),
        ev(1003, "2026-05-01T12:37:00+03:00", "-127", event_id=3),
        ev(9999, "2026-05-01T12:37:00+03:00", "Норма"),
    ]
    first = await ingest_events(None, batch, session=session)
    assert (first.accepted, first.duplicates, first.unknown_channels) == (3, 0, 1)

    again = await ingest_events(None, batch[:2] + batch[:1], session=session)
    assert (again.accepted, again.duplicates) == (0, 3)

    assert await rows(session, "SELECT channel_id, value_num FROM events ORDER BY channel_id") == [
        (1001, None),
        (1002, 0.05),
        (1003, None),
    ]
    assert await rows(session, "SELECT channel_id FROM events_raw") == [(1002,)]  # газ — ещё и в сыром потоке


async def test_naive_time_is_moscow(session):
    await ingest_events(None, [ev(1001, "2026-05-01T12:00:00", "Норма")], session=session)
    assert await session.scalar(text("SELECT ts FROM events")) == at("2026-05-01T12:00:00")


async def test_layers_aggregates_state_faults(session):
    gas = [
        ev(1002, "2026-05-01T10:00:00+03:00", "0.01"),
        ev(1002, "2026-05-01T10:01:00+03:00", "0.02"),
        ev(1002, "2026-05-01T10:02:00+03:00", "0.03"),
        ev(1002, "2026-05-01T10:03:00+03:00", "0.04"),
        ev(1002, "2026-05-01T10:04:00+03:00", "Неисправен", True),
    ]
    await ingest_events(None, gas[:2], session=session)  # зона нечувствительности держится между пакетами
    await ingest_events(None, gas[2:], session=session)
    await ingest_events(None, [ev(1002, "2026-05-01T11:00:00+03:00", "0.04")], session=session)

    kept = await rows(session, "SELECT value_raw FROM events WHERE channel_id = 1002 ORDER BY ts")
    assert kept == [("0.01",), ("0.04",), ("Неисправен",), ("0.04",)]
    assert await session.scalar(text("SELECT count(*) FROM events_raw")) == 6

    hourly = await rows(
        session,
        "SELECT hour, n_events, n_fault, n_num, min, max, mean FROM channel_hourly WHERE channel_id = 1002 "
        "ORDER BY hour",
    )
    assert hourly[0][:6] == (at("2026-05-01T10:00:00"), 5, 1, 4, 0.01, 0.04)
    assert hourly[0][6] == pytest.approx(0.025)  # итог — до отсечения, склеен из двух пакетов
    daily = await rows(session, "SELECT date, n_events, n_num FROM channel_daily WHERE channel_id = 1002")
    assert daily == [(dt.date(2026, 5, 1), 6, 5)]

    faults = await rows(session, "SELECT start_ts, end_ts, state FROM fault_episodes WHERE channel_id = 1002")
    assert faults == [(at("2026-05-01T10:04:00"), at("2026-05-01T11:00:00"), "Неисправен")]
    state = await rows(
        session, "SELECT value_raw, is_alarm, last_alarm_at, fault_since FROM channel_state WHERE channel_id = 1002"
    )
    assert state == [("0.04", False, at("2026-05-01T10:04:00"), None)]


async def test_open_fault_sets_fault_since_and_status_events(session):
    result = await ingest_events(None, [ev(1003, "2026-05-01T10:00:00+03:00", "18")], session=session)
    assert [n.type for n in result.notifications] == ["channel.status"]
    result = await ingest_events(None, [ev(1003, "2026-05-01T11:00:00+03:00", "Неисправен")], session=session)
    status = [n.data for n in result.notifications if n.type == "channel.status"]
    assert status[0]["prev_status"] == "ok" and status[0]["status"] == "fault"
    fault_since = await session.scalar(text("SELECT fault_since FROM channel_state WHERE channel_id = 1003"))
    assert fault_since == at("2026-05-01T11:00:00")
    # повтор неисправности статус не меняет — события нет
    result = await ingest_events(None, [ev(1003, "2026-05-01T12:00:00+03:00", "Неисправен")], session=session)
    assert not [n for n in result.notifications if n.type == "channel.status"]


async def test_alarm_goes_to_feed_and_scoring(session):
    result = await ingest_events(None, [ev(1001, "2026-05-01T12:36:57+03:00", "Обнаружен дым")], session=session)
    assert result.alarms == 1 and result.scoring_objects == {10}
    types = [n.type for n in result.notifications]
    assert types == ["alarm.created", "alarm_episode.updated", "channel.status"]
    alarm = result.notifications[0].data
    assert alarm["verdict"] == "pending" and alarm["episode"]["kind"] == "solitary"
    assert alarm["object_id"] == 10 and alarm["sub_object_id"] == 100 and alarm["channel_name"] == "ДД ПК1"
    assert alarm["episode"]["end_ts"] is None  # эпизод открыт
    assert result.notifications[2].data["status"] == "alarm"


async def test_run_events_are_separate_from_base(session):
    """Событие прогона с той же меткой, что в базе, — не дубль; представление прогона видит базу до старта."""
    base = [ev(1001, "2026-05-01T10:00:00+03:00", "Норма"), ev(1001, "2026-05-01T14:00:00+03:00", "Норма")]
    await ingest_events(None, base, session=session)
    run_id = await session.scalar(
        text("INSERT INTO runs (scenario, sim_start) VALUES ('test', '2026-05-01T12:00:00+03:00') RETURNING id")
    )
    result = await ingest_events(run_id, [ev(1001, "2026-05-01T14:00:00+03:00", "Норма")], session=session)
    assert result.accepted == 1
    sim_now = await session.scalar(text("SELECT sim_now FROM runs WHERE id = :r"), {"r": run_id})
    assert sim_now == at("2026-05-01T14:00:00")

    view = await rows(session, "SELECT run_id FROM run_events(:r) ORDER BY ts", r=run_id)
    assert view == [(None,), (run_id,)]
    assert await session.scalar(text("SELECT count(*) FROM run_events(NULL)")) == 2


async def test_route_by_channel_attributes(session):
    """Маршрут приёма — из справочника канала (D31), а не по имени типа: сырой канал со своей зоной, сырой без зоны
    и значимый канал с именем типа газа хакатона."""
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, stream, deadband) VALUES "
            "(2001, 100, 'Диспетчерский контроль', 'Уровень', 'l1', 'Уровень 1', 'raw', 0.5), "
            "(2002, 100, 'Газовая охрана', 'Газовый датчик', 'g9', 'Газ 9', 'significant', NULL), "
            "(2003, 100, 'Охранная подсистема', 'Проход', 'p1', 'Проход 1', 'raw', NULL)"
        )
    )
    events = [ev(2001, f"2026-05-01T10:0{i}:00+03:00", v) for i, v in enumerate(["1.0", "1.3", "1.6", "1.7"])]
    events += [ev(2002, "2026-05-01T10:00:00+03:00", "0.01"), ev(2002, "2026-05-01T10:01:00+03:00", "0.02")]
    events += [ev(2003, "2026-05-01T10:00:00+03:00", "1"), ev(2003, "2026-05-01T10:01:00+03:00", "0")]
    result = await ingest_events(None, events, session=session)
    assert result.accepted == 8

    def by_channel(table: str) -> str:
        return f"SELECT channel_id, value_raw FROM {table} WHERE channel_id > 2000 ORDER BY channel_id, ts"

    assert await rows(session, by_channel("events_raw")) == [
        (2001, "1.0"),
        (2001, "1.3"),
        (2001, "1.6"),
        (2001, "1.7"),
        (2003, "1"),
        (2003, "0"),
    ]
    assert await rows(session, by_channel("events")) == [(2001, "1.0"), (2001, "1.6"), (2002, "0.01"), (2002, "0.02")]
