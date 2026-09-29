"""Эпизоды оборудования в ленте (app/domain/equipment.py): правило на синтетике и на срезе сценария power-outage."""

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain.equipment import episodes_where
from app.domain.ingest import RawEvent, ingest_events
from app.domain.runs import create_run

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "data" / "archive"
SLICE = ROOT / "data" / "processed" / "emulator" / "power-outage.parquet"


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


async def add_channels(session, first_id: int, n: int, sensor_type: str) -> list[int]:
    ids = list(range(first_id, first_id + n))
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) SELECT id, 100, "
            "'Диспетчерский контроль', :t, '15-11.9.' || id, 'ОБ ' || id FROM unnest(CAST(:ids AS int[])) id"
        ),
        {"ids": ids, "t": sensor_type},
    )
    return ids


async def feed(session, run_id, events) -> list[str]:
    result = await ingest_events(run_id, events, session=session)
    return [n.type for n in result.notifications if n.type.startswith("equipment_episode")]


async def run_episodes(session, run_id) -> list[dict]:
    return await episodes_where(session, run_id, "e.run_id = :run AND e.qualified", {"run": run_id}, "ORDER BY e.id")


@pytest.mark.anyio
async def test_equipment_episode_rule(session):
    phases = await add_channels(session, 5000, 6, "Состояние фазы")
    pumps = await add_channels(session, 5100, 3, "Состояние насоса")
    base = [
        RawEvent(c, at("2026-04-09T11:00:00"), "Есть питание" if c in phases else "Выключен", False)
        for c in phases + pumps
    ]
    await ingest_events(None, base, session=session)
    run = await create_run(session, scenario="t", sim_start=at("2026-04-09T12:00:00"))

    # 4 канала за окно — не эпизод; пятый позже окна — кандидат начинается заново
    first = [RawEvent(c, at("2026-04-09T12:01:00"), "Обесточен", False) for c in phases[:4]]
    assert await feed(session, run.id, first) == []
    assert await feed(session, run.id, [RawEvent(phases[4], at("2026-04-09T12:07:00"), "Обесточен", False)]) == []
    assert await run_episodes(session, run.id) == []

    # за 5 минут от 12:07 ещё четыре канала: 5 — порог; насосы — «Обесточен» и «Неисправен» в одну секунду
    more = [RawEvent(phases[5], at("2026-04-09T12:08:00"), "Обесточен", False)]
    more += [RawEvent(c, at("2026-04-09T12:09:00"), v, True) for c in pumps for v in ("Неисправен", "Обесточен")]
    assert await feed(session, run.id, more) == ["equipment_episode.created"]
    [ep] = await run_episodes(session, run.id)
    assert (ep["kind"], ep["value"], ep["channels"], ep["end_ts"]) == ("power_outage", "Обесточен", 5, None)
    assert ep["start_ts"] == at("2026-04-09T12:07:00") and ep["active_channels"] == 5
    assert ep["by_type"] == [
        {"sensor_type": "Состояние насоса", "count": 3},
        {"sensor_type": "Состояние фазы", "count": 2},
    ]
    assert ep["sub_objects"] == [{"id": 100, "name": "Подобъект А1", "channels": 5}]

    # два вернулись — открыт; третий — половина из пяти, закрыт
    back = [RawEvent(c, at("2026-04-09T12:20:00"), "Есть питание", False) for c in (phases[4], phases[5])]
    await feed(session, run.id, back)
    assert (await run_episodes(session, run.id))[0]["end_ts"] is None
    assert await feed(session, run.id, [RawEvent(pumps[0], at("2026-04-09T12:21:00"), "Выключен", False)]) == [
        "equipment_episode.updated"
    ]
    [ep] = await run_episodes(session, run.id)
    assert ep["end_ts"] == at("2026-04-09T12:21:00") and ep["returned_channels"] == 3 and ep["active_channels"] == 2
    # возврат после закрытия: эпизод не меняется, но канал считается вернувшимся
    assert await feed(session, run.id, [RawEvent(pumps[1], at("2026-04-09T15:00:00"), "Выключен", False)]) == []
    [ep] = await run_episodes(session, run.id)
    assert (ep["end_ts"], ep["returned_channels"], ep["active_channels"]) == (at("2026-04-09T12:21:00"), 4, 1)


@pytest.mark.anyio
async def test_equipment_fault_kind_and_smoke_excluded(session):
    doors = await add_channels(session, 6000, 5, "КД Дверь")
    smoke = await add_channels(session, 6100, 5, "Датчик дыма")
    run = await create_run(session, scenario="t", sim_start=at("2026-04-09T12:00:00"))
    events = [RawEvent(c, at("2026-04-09T12:01:00"), "Неисправен", True) for c in smoke]
    assert await feed(session, run.id, events) == []  # дымовые — лента тревог
    events = [RawEvent(c, at("2026-04-09T12:02:00"), "Отключено устройство", True) for c in doors]
    assert await feed(session, run.id, events) == ["equipment_episode.created"]
    [ep] = await run_episodes(session, run.id)
    assert (ep["kind"], ep["value"], ep["channels"]) == ("equipment_fault", "Отключено устройство", 5)


@pytest.mark.anyio
async def test_equipment_flapping_reopens_closed_episode(session):
    """Дребезг: объект снова набрал порог через 25 с после закрытия — тот же эпизод, а не новый (прогон 60, Кси)."""
    phases = await add_channels(session, 7000, 8, "Состояние фазы")
    await ingest_events(
        None, [RawEvent(c, at("2026-04-09T11:00:00"), "Есть питание", False) for c in phases], session=session
    )
    run = await create_run(session, scenario="t", sim_start=at("2026-04-09T12:00:00"))

    def events(ts: str, channels: list[int], value: str) -> list[RawEvent]:
        return [RawEvent(c, at(f"2026-04-09T{ts}"), value, False) for c in channels]

    assert await feed(session, run.id, events("12:01:00", phases[:6], "Обесточен")) == ["equipment_episode.created"]
    await feed(session, run.id, events("12:01:05", phases[:3], "Есть питание"))  # половина вернулась — закрыт
    [ep] = await run_episodes(session, run.id)
    assert ep["end_ts"] == at("2026-04-09T12:01:05")

    # три вернувшихся и два новых — порог через 25 с после закрытия: эпизод открыт снова, без нового тоста
    flap = events("12:01:30", phases[:3] + phases[6:], "Обесточен")
    assert await feed(session, run.id, flap) == ["equipment_episode.updated"]
    [ep] = await run_episodes(session, run.id)
    assert (ep["start_ts"], ep["end_ts"], ep["channels"], ep["returned_channels"]) == (
        at("2026-04-09T12:01:00"),
        None,
        8,
        0,
    )
    candidates = await session.scalar(
        text("SELECT count(*) FROM equipment_episodes WHERE run_id = :r AND NOT qualified"), {"r": run.id}
    )
    assert candidates == 0

    await feed(session, run.id, events("12:01:40", phases[:4], "Есть питание"))  # 4 из 8 — закрыт снова
    [ep] = await run_episodes(session, run.id)
    assert (ep["end_ts"], ep["returned_channels"]) == (at("2026-04-09T12:01:40"), 4)

    # через 5 мин после закрытия и позже — уже новое событие
    await feed(session, run.id, events("12:05:00", phases[4:], "Есть питание"))
    assert await feed(session, run.id, events("12:10:00", phases[:5], "Обесточен")) == ["equipment_episode.created"]
    first, second = await run_episodes(session, run.id)
    assert first["end_ts"] == at("2026-04-09T12:01:40") and second["start_ts"] == at("2026-04-09T12:10:00")


@pytest.mark.anyio
async def test_equipment_single_channel_flapping(session):
    """Один канал уходит и возвращается чаще окна: у кандидата прежнее начало выпадает из окна, повторное — новое.
    Раньше кандидат оставался без каналов и приём пакета падал (`min()` пустого), шлюз повторял пакет без конца."""
    [pump] = await add_channels(session, 7100, 1, "Состояние насоса")
    run = await create_run(session, scenario="t", sim_start=at("2026-04-09T12:00:00"))
    for minute in range(0, 13, 4):
        assert (
            await feed(session, run.id, [RawEvent(pump, at(f"2026-04-09T12:{minute:02d}:00"), "Обесточен", False)])
            == []
        )
        await feed(session, run.id, [RawEvent(pump, at(f"2026-04-09T12:{minute + 1:02d}:00"), "Выключен", False)])
    assert await run_episodes(session, run.id) == []
    onset = await session.scalar(
        text(
            "SELECT ec.onset_ts FROM equipment_episode_channels ec JOIN equipment_episodes e ON e.id = ec.episode_id "
            "WHERE e.run_id = :r"
        ),
        {"r": run.id},
    )
    assert onset == at("2026-04-09T12:08:00")  # в 12:08 начало 12:00 выпало из окна — обновлено; 12:12 — ещё в окне


def _slice_events(con) -> tuple[list[RawEvent], list[RawEvent], list[int]]:
    """Срез сценария и последнее состояние его каналов до старта — из архива журнала."""
    rows = con.execute(
        f"SELECT channel_id, ts, value, is_alarm, event_id FROM read_parquet('{SLICE}') ORDER BY ts, event_id"
    ).fetchall()
    channels = sorted({r[0] for r in rows})
    before = con.execute(
        f"""
        SELECT channel_id, arg_max(ts, (ts, event_id)), arg_max(value_raw, (ts, event_id)),
            arg_max(is_alarm, (ts, event_id)), arg_max(event_id, (ts, event_id))
        FROM read_parquet('{ARCHIVE}/events/year=2026/*/*.parquet', hive_partitioning = true)
        WHERE channel_id IN ({",".join(map(str, channels))}) AND ts < TIMESTAMPTZ '2026-04-09 12:30:00+03'
        GROUP BY channel_id
        """
    ).fetchall()
    base = [RawEvent(c, ts, v, a, e) for c, ts, v, a, e in before]
    run = [RawEvent(c, ts, v, a, e) for c, ts, v, a, e in rows]
    return base, run, channels


@pytest.mark.anyio
@pytest.mark.skipif(not (SLICE.exists() and (ARCHIVE / "catalog").exists()), reason="нет среза power-outage или архива")
async def test_power_outage_slice(session):
    import duckdb

    con = duckdb.connect()
    base, events, channels = _slice_events(con)
    objects = con.execute(
        f"""
        WITH RECURSIVE up AS (
            SELECT o.* FROM read_parquet('{ARCHIVE}/catalog/objects.parquet') o
            WHERE o.id IN (SELECT object_id FROM read_parquet('{ARCHIVE}/catalog/channels.parquet')
                           WHERE id IN ({",".join(map(str, channels))}))
            UNION SELECT p.* FROM read_parquet('{ARCHIVE}/catalog/objects.parquet') p JOIN up ON p.id = up.parent_id)
        SELECT DISTINCT id, parent_id, level, kind, name FROM up ORDER BY level
        """
    ).fetchall()
    chans = con.execute(
        f"SELECT id, object_id, system_type, sensor_type, tag, name, picket_number, picket_offset, tag_group "
        f"FROM read_parquet('{ARCHIVE}/catalog/channels.parquet') WHERE id IN ({','.join(map(str, channels))})"
    ).fetchall()
    for o in objects:
        await session.execute(
            text(
                "INSERT INTO objects (id, parent_id, level, kind, name) VALUES (:id, :parent, :level, :kind, :name) "
                "ON CONFLICT DO NOTHING"
            ),
            dict(zip(("id", "parent", "level", "kind", "name"), o, strict=True)),
        )
    for c in chans:
        await session.execute(
            text(
                "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, picket_number, "
                "picket_offset, tag_group) VALUES (:a, :b, :c, :d, :e, :f, :g, :h, :i)"
            ),
            dict(zip("abcdefghi", c, strict=True)),
        )
    await ingest_events(None, base, session=session)
    run = await create_run(session, scenario="power-outage", sim_start=at("2026-04-09T12:30:00"))
    created = updated = 0
    for i in range(0, len(events), 25):  # шлюз шлёт пакеты по полсекунды — здесь по 25 событий
        types = await feed(session, run.id, events[i : i + 25])
        created += types.count("equipment_episode.created")
        updated += types.count("equipment_episode.updated")

    [ep] = await run_episodes(session, run.id)
    assert (ep["kind"], ep["value"], ep["channels"], ep["object_id"]) == ("power_outage", "Обесточен", 122, 4573)
    assert {t["sensor_type"]: t["count"] for t in ep["by_type"]} == {
        "Состояние фазы": 71,
        "Состояние вентилятора": 30,
        "Состояние насоса": 21,
    }
    assert ep["start_ts"] == at("2026-04-09T12:53:00")
    assert at("2026-04-09T13:14:00") <= ep["end_ts"] < at("2026-04-09T13:15:00")
    assert created == 1 and updated >= 1
    assert ep["returned_channels"] >= 90  # к концу сценария; часть фаз остаётся «Обесточен» сутками


@pytest.fixture
def pg(seeded):
    import psycopg

    from app.loader import pg_conninfo

    with psycopg.connect(pg_conninfo(seeded.render_as_string(hide_password=False)), autocommit=True) as conn:
        yield conn


def test_equipment_api(client, pg):
    api = "/api/v1"
    pg.execute(
        "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) SELECT id, 100, "
        "'Диспетчерский контроль', 'Состояние фазы', '15-11.9.' || id, 'Фаза ' || id "
        "FROM generate_series(7000, 7005) id"
    )
    events = [{"channel_id": c, "ts": "2026-04-09T11:00:00+03:00", "value": "Есть питание"} for c in range(7000, 7006)]
    events += [{"channel_id": c, "ts": "2026-04-09T12:53:00+03:00", "value": "Обесточен"} for c in range(7000, 7006)]
    events += [{"channel_id": c, "ts": "2026-04-09T13:14:00+03:00", "value": "Есть питание"} for c in range(7000, 7003)]
    assert client.post(f"{api}/ingest/events", json=events).status_code == 202

    page = client.get(f"{api}/equipment-episodes", params={"run": 0}).json()
    assert page["total"] == 1 and page["counts"] == {"active": 0, "power_outage": 1, "equipment_fault": 0}
    [ep] = page["items"]
    assert (ep["kind"], ep["channels"], ep["returned_channels"], ep["active_channels"]) == ("power_outage", 6, 3, 3)
    assert ep["end_ts"].startswith("2026-04-09T10:14:00") and ep["sub_object_name"] == "Подобъект А1"
    assert client.get(f"{api}/equipment-episodes", params={"run": 0, "kind": "equipment_fault"}).json()["total"] == 0
    assert client.get(f"{api}/equipment-episodes", params={"run": 0, "active": "true"}).json()["total"] == 0
    for obj in (10, 100):  # объект и подобъект
        assert client.get(f"{api}/equipment-episodes", params={"run": 0, "object_id": obj}).json()["total"] == 1
    before = {"run": 0, "to": "2026-04-09T12:00:00+03:00"}
    assert client.get(f"{api}/equipment-episodes", params=before).json()["total"] == 0

    detail = client.get(f"{api}/equipment-episodes/{ep['id']}", params={"run": 0}).json()
    assert detail["channels"] == 6 and len(detail["members"]) == 6
    assert {m["value"] for m in detail["members"]} == {"Обесточен"}
    assert sum(1 for m in detail["members"] if m["end_ts"]) == 3
    assert client.get(f"{api}/equipment-episodes/999999", params={"run": 0}).status_code == 404
