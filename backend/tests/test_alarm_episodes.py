import dataclasses
import datetime as dt
import itertools

import duckdb
import pytest

from app.domain.alarm_episodes import MSK, classify, rules, sql_kind

R = rules()
R_HOURS = dataclasses.replace(R, ppr_lever_or_hours=True)


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


def test_config_loads():
    assert R.alarm_value == "Обнаружен дым"
    assert R.gap == dt.timedelta(minutes=25)
    assert R.ppr_min_channels == 10


def test_classify():
    monday_morning = at("2026-05-04T09:30:00")
    monday_evening = at("2026-05-04T19:00:00")
    saturday_morning = at("2026-05-09T09:30:00")
    assert classify(1, monday_morning, False, R) == "solitary"
    assert classify(3, monday_morning, True, R) == "group"
    assert classify(10, monday_morning, False, R) == "group"  # без рычага — не проверка
    assert classify(10, monday_evening, True, R) == "ppr"  # рычаг — признак проверки в любое время буднего дня
    assert classify(40, saturday_morning, True, R) == "group"  # в выходные проверок нет
    assert classify(10, monday_morning, False, R_HOURS) == "ppr"  # вариант «рычаг или рабочие часы»
    assert classify(10, monday_evening, False, R_HOURS) == "group"


@pytest.mark.parametrize("r", [R, R_HOURS])
def test_sql_matches_python(r):
    starts = [at(f"2026-05-{d:02d}T{h:02d}:59:59") for d in (4, 8, 9, 10) for h in (8, 9, 11, 12, 23)]
    cases = list(itertools.product([1, 2, 9, 10, 25], starts, [False, True]))
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE c (n INTEGER, s TIMESTAMPTZ, lever BOOLEAN)")
    con.executemany("INSERT INTO c VALUES (?, ?, ?)", cases)
    rows = con.execute(f"SELECT n, s, lever, {sql_kind('n', 's', 'lever', r)} FROM c").fetchall()
    for n, s, lever, kind in rows:
        assert kind == classify(n, s, lever, r), (n, s, lever)


# --- загрузчик и поток дают одни эпизоды ------------------------------------------------------------------------

SMOKE_CHANNELS = list(range(2001, 2013))
LEVER_CHANNEL = 3001
LEVER_TS = at("2026-05-04T09:20:00")
# Понедельник 09:30: 12 датчиков по минуте + повтор первого, рычаг в 09:20 → плановая проверка; 15:00 — группа
# из двух; 15:40 — через 40 мин, новый эпизод; суббота — одиночная.
ALARMS = (
    [(ch, at(f"2026-05-04T09:{30 + i:02d}:00")) for i, ch in enumerate(SMOKE_CHANNELS)]
    + [(2001, at("2026-05-04T09:45:00"))]
    + [(2001, at("2026-05-04T15:00:00")), (2002, at("2026-05-04T15:05:00"))]
    + [(2003, at("2026-05-04T15:40:00"))]
    + [(2001, at("2026-05-09T10:00:00"))]
)
EXPECTED = [  # (начало, вид, датчиков, тревог)
    (at("2026-05-04T09:30:00"), "ppr", 12, 13),
    (at("2026-05-04T15:00:00"), "group", 2, 2),
    (at("2026-05-04T15:40:00"), "solitary", 1, 1),
    (at("2026-05-09T10:00:00"), "solitary", 1, 1),
]


def _write_raw(raw):
    objects = (
        '"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"\n'
        "1,1,3831,district,Район\n10,2,1,controlHouse,Объект А\n100,3,10,controlHouse,Подобъект А1\n"
    )
    channels = (
        '"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы","название_датчика","ид_объект"\n'
    )
    channels += "".join(f"{c},Пожарная охрана,Датчик дыма,15-11.1.{c}.1.,ДД ПК{c},100\n" for c in SMOKE_CHANNELS)
    channels += f"{LEVER_CHANNEL},Диспетчерский контроль,Состояние УИР-Р,15-11.1.9.1.,УИР-Р ПК1,100\n"
    journal = "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
    journal += f"999,{LEVER_CHANNEL},{LEVER_TS:%Y-%m-%d},{LEVER_TS:%H:%M:%S},t,Рычаг сдернут\n"
    for i, (ch, ts) in enumerate(ALARMS):
        journal += f"{i + 1},{ch},{ts:%Y-%m-%d},{ts:%H:%M:%S},f,Обнаружен дым\n"
    journal += f"{len(ALARMS) + 1},2001,2026-05-04,09:30:00,f,Обнаружен дым\n"  # повтор в ту же секунду
    raw.mkdir()
    (raw / "справочник_объектов_диспетчер.csv").write_text(objects, encoding="utf-8")
    (raw / "справочник_каналов_датчиков.csv").write_text(channels, encoding="utf-8")
    (raw / "ext-journal-2026.csv").write_text(journal, encoding="utf-8")


def test_loader_episodes(db_url, tmp_path):
    import psycopg

    from app.archive import build_archive
    from app.loader import Loader, pg_conninfo

    _write_raw(tmp_path / "raw")
    build_archive(tmp_path / "raw", tmp_path / "archive", memory_limit="1GB", threads=2)
    url = db_url.render_as_string(hide_password=False)
    with psycopg.connect(pg_conninfo(url), autocommit=True) as conn:
        conn.execute(
            "TRUNCATE work_orders, alarms, predictions, alarm_episodes, fault_episodes, channel_state, "
            "channel_daily, channel_hourly, events_raw, events, model_versions, channels, objects, runs "
            "RESTART IDENTITY CASCADE"
        )
        Loader(tmp_path / "archive", url, tmp_path / "work", workers=1, memory_limit="1GB", threads=2).run()
        episodes = conn.execute(
            "SELECT start_ts, kind, n_channels, n_alarms FROM alarm_episodes WHERE object_id = 10 ORDER BY start_ts"
        ).fetchall()
        assert episodes == EXPECTED
        verdicts = conn.execute("SELECT verdict, count(*) FROM alarms GROUP BY 1 ORDER BY 1").fetchall()
        assert verdicts == [("no_score", 4), ("ppr", 13)]
        assert conn.execute("SELECT count(*) FROM alarms WHERE episode_id IS NULL").fetchone()[0] == 0


@pytest.mark.anyio
async def test_stream_episodes(session):
    from sqlalchemy import text

    from app.domain.alarm_episodes import register_alarm

    await _smoke_channels(session)
    await session.execute(
        text("INSERT INTO events (channel_id, ts, is_alarm, value_raw) VALUES (:ch, :ts, true, 'Рычаг сдернут')"),
        {"ch": LEVER_CHANNEL, "ts": LEVER_TS},
    )
    changes = [await register_alarm(session, None, ch, 10, ts, [LEVER_CHANNEL]) for ch, ts in ALARMS]
    assert await register_alarm(session, None, 2001, 10, ALARMS[0][1], [LEVER_CHANNEL]) is None  # повтор
    rows = (
        await session.execute(text("SELECT start_ts, kind, n_channels, n_alarms FROM alarm_episodes ORDER BY start_ts"))
    ).all()
    assert [tuple(r) for r in rows] == EXPECTED
    # Первые 9 тревог проверки пришли до её распознания — вердикт уточнён; десятая и позже — сразу ppr
    assert len(changes[9].refined_alarm_ids) == 9
    assert [c.verdict for c in changes[9:13]] == ["ppr"] * 4
    refined = await session.scalar(text("SELECT count(*) FROM alarms WHERE verdict_refined"))
    assert refined == 9


async def _smoke_channels(session):
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) "
            "SELECT c, 100, 'Пожарная охрана', 'Датчик дыма', '15-11.1.' || c || '.1.', 'ДД' FROM unnest(:ids) c"
        ),
        {"ids": SMOKE_CHANNELS},
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) "
            "VALUES (:ch, 100, 'Диспетчерский контроль', 'Состояние УИР-Р', '15-11.1.9.1.', 'УИР-Р')"
        ),
        {"ch": LEVER_CHANNEL},
    )


@pytest.mark.anyio
async def test_lever_after_alarms_refines(session):
    """Рычаг пришёл позже тревог — эпизод пересматривается и становится плановой проверкой."""
    from sqlalchemy import text

    from app.domain.alarm_episodes import register_alarm, register_lever

    await _smoke_channels(session)
    for ch, ts in ALARMS[:12]:
        await register_alarm(session, None, ch, 10, ts, [LEVER_CHANNEL])
    assert await session.scalar(text("SELECT kind FROM alarm_episodes")) == "group"
    changed = await register_lever(session, None, 10, at("2026-05-04T09:50:00"))
    assert len(changed) == 1 and len(changed[0][1]) == 12
    assert await session.scalar(text("SELECT kind FROM alarm_episodes")) == "ppr"
