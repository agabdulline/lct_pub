"""Внешние наборы в схеме (миграция 0010, D28): прогон-основа и представления прогонов не смешивают базу хакатона
и историю внешнего набора."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain.ingest import RawEvent, ingest_events
from app.domain.runs import RunError, create_run, delete_run

MSK = ZoneInfo("Europe/Moscow")
EXT_OBJECT, EXT_SUB, EXT_CHANNEL = 9_010_001, 9_010_002, 901_000_001


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


async def external_dataset(session) -> int:
    """Набор `test`: объект, подобъект, канал; история — служебный прогон-основа."""
    await session.execute(
        text(
            "INSERT INTO objects (id, parent_id, level, kind, name, source) VALUES "
            "(:o, NULL, 1, 'site', 'Цех', 'test'), (:s, :o, 2, 'node', 'Узел 1', 'test')"
        ),
        {"o": EXT_OBJECT, "s": EXT_SUB},
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, source, unit) "
            "VALUES (:c, :s, 'Пожарная охрана', 'Датчик CO', 'node1/CO', 'CO узла 1', 'test', 'ppm')"
        ),
        {"c": EXT_CHANNEL, "s": EXT_SUB},
    )
    base = await session.scalar(
        text(
            "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, sim_now, params) VALUES "
            "('dataset:test', 'finished', 'dataset', :a, :b, :b, '{\"dataset\": \"test\"}') RETURNING id"
        ),
        {"a": at("2021-01-01T00:00:00"), "b": at("2021-02-01T00:00:00")},
    )
    await ingest_events(base, [RawEvent(EXT_CHANNEL, at("2021-01-10T10:00:00"), "12.5", False)], session=session)
    await ingest_events(base, [RawEvent(EXT_CHANNEL, at("2021-01-20T10:00:00"), "13.0", False)], session=session)
    return base


async def channels_of(session, fn: str, run: int | None) -> set[int]:
    return set((await session.scalars(text(f"SELECT DISTINCT channel_id FROM {fn}(:r)"), {"r": run})).all())


@pytest.mark.anyio
async def test_dataset_base_run_is_isolated(session):
    await ingest_events(None, [RawEvent(1003, at("2021-01-05T10:00:00"), "18", False)], session=session)
    base = await external_dataset(session)
    hackathon = await create_run(session, scenario="t", sim_start=at("2021-01-15T00:00:00"))
    scenario = await create_run(session, scenario="ext", sim_start=at("2021-01-15T00:00:00"))
    await session.execute(text("UPDATE runs SET base_run_id = :b WHERE id = :r"), {"b": base, "r": scenario.id})

    for fn in ("run_events", "run_channel_daily", "run_channel_hourly"):
        assert await channels_of(session, fn, None) == {1003}  # ?run=0 — только хакатон
        assert await channels_of(session, fn, hackathon.id) == {1003}
        assert await channels_of(session, fn, base) == {EXT_CHANNEL}  # у основы нет базовой части
    # сценарий на наборе видит историю основы до своего старта и не видит хакатон
    ts = (await session.scalars(text("SELECT ts FROM run_events(:r)"), {"r": scenario.id})).all()
    assert ts == [at("2021-01-10T10:00:00")]
    assert await session.scalar(text("SELECT run_base(:r)"), {"r": hackathon.id}) is None
    assert await session.scalar(text("SELECT run_base(:r)"), {"r": base}) == -1
    assert await session.scalar(text("SELECT run_base(:r)"), {"r": scenario.id}) == base


@pytest.mark.anyio
async def test_external_reference_tables(session):
    await external_dataset(session)
    await session.execute(
        text(
            "INSERT INTO external_labels (dataset, object_id, channel_id, start_ts, end_ts, label, meta) "
            "VALUES ('test', :o, NULL, :a, :b, 'fire', '{\"experiment\": 3}')"
        ),
        {"o": EXT_OBJECT, "a": at("2021-01-10T10:00:00"), "b": at("2021-01-10T10:30:00")},
    )
    await session.execute(
        text("INSERT INTO external_credentials (dataset, credential_id, kind) VALUES ('test', 'car-1', 'vehicle')")
    )
    await session.execute(
        text("INSERT INTO external_permits (dataset, credential_id, object_id) VALUES ('test', 'car-1', :o)"),
        {"o": EXT_SUB},
    )
    row = (await session.execute(text("SELECT label, meta->>'experiment' FROM external_labels"))).one()
    assert tuple(row) == ("fire", "3")
    source = await session.scalar(text("SELECT source FROM channels WHERE id = 1001"))
    assert source == "hackathon"  # по умолчанию — данные хакатона


async def rows(session, sql: str, **params) -> list[tuple]:
    return [tuple(r) for r in (await session.execute(text(sql), params)).all()]


@pytest.mark.anyio
async def test_create_run_on_dataset(session):
    start = at("2021-01-10T10:30:00")
    with pytest.raises(RunError) as e:
        await create_run(session, scenario="ext", sim_start=start, dataset="test")
    assert e.value.code == "dataset_not_loaded"
    base = await external_dataset(session)
    with pytest.raises(RunError) as e:
        await create_run(session, scenario="ext", sim_start=at("2022-01-01T00:00:00"), dataset="test")
    assert e.value.code == "dataset_period_mismatch"

    await ingest_events(None, [RawEvent(1003, at("2021-01-05T10:00:00"), "18", False)], session=session)
    run = await create_run(session, scenario="ext", sim_start=start, dataset="test")
    assert run.base_run_id == base
    # снимок — из основы на старт: состояние, итоги часа и дня старта; каналов хакатона в нём нет
    state = "SELECT channel_id, value_raw FROM channel_state WHERE run_id = :r"
    assert await rows(session, state, r=run.id) == [(EXT_CHANNEL, "12.5")]
    hourly = "SELECT channel_id, n_events FROM channel_hourly WHERE run_id = :r"
    assert await rows(session, hourly, r=run.id) == [(EXT_CHANNEL, 1)]
    hackathon = await create_run(session, scenario="t", sim_start=start)
    assert await rows(session, state, r=hackathon.id) == [(1003, "18")]

    await ingest_events(run.id, [RawEvent(EXT_CHANNEL, at("2021-01-10T11:00:00"), "14.0", False)], session=session)
    with pytest.raises(RunError) as e:
        await delete_run(session, base)
    assert e.value.code == "dataset_in_use"
    deleted = await delete_run(session, run.id)
    assert deleted["events"] == 1 and deleted["channel_state"] == 1
    assert await rows(session, "SELECT count(*) FROM events WHERE run_id = :r", r=base) == [(2,)]
    await delete_run(session, base)
    assert await rows(session, "SELECT count(*) FROM events WHERE channel_id = :c", c=EXT_CHANNEL) == [(0,)]
