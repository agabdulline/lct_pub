import datetime as dt
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app.archive import build_archive
from app.loader import Loader, Scope, pg_conninfo

MSK = ZoneInfo("Europe/Moscow")

OBJECTS = """"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"
1,1,3831,district,Район
10,2,1,controlHouse,Объект А
100,3,10,controlHouse,Подобъект А1
"""
CHANNELS = """"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы","название_датчика","ид_объект"
1001,Пожарная охрана,Датчик дыма,15-11.1.1.1.,ДД ПК1,100
1002,Газовая,Газовый датчик,15-11.1.2.1.,ГАЗ Д1 ПК2,100
1003,Охранная подсистема,Датчик движения,15-11.1.3.1.,ОД ПК3,100
1004,Температурная,Датчик температуры,15-11.1.4.1.,ТЕМП ПК4,100
"""
HEADER = "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
JOURNAL_2025 = (
    HEADER
    + """1,1001,2025-12-31,23:00:00,t,Неисправен
2,1002,2025-12-31,10:00:00,f,0.01
3,1002,2025-12-31,10:01:00,f,0.02
4,1002,2025-12-31,10:02:00,f,0.03
5,1002,2025-12-31,10:03:00,f,0.04
6,1002,2025-12-31,10:04:00,f,Неисправен
7,1002,2025-12-31,10:05:00,f,0.04
"""
)
JOURNAL_2026 = (
    HEADER
    + """8,1001,2026-01-01,01:00:00,f,Норма
10,1001,2026-03-01,10:00:00,t,Неисправен
11,1001,2026-03-01,10:00:00,f,Норма
12,1001,2026-03-01,10:00:00,t,Неисправен
13,1001,2026-03-01,11:00:00,f,Норма
14,1001,2026-06-30,12:00:00,t,Обнаружен дым
15,1003,2026-01-15,08:00:00,f,Движения нет
16,1003,2026-06-30,11:00:00,f,Обнаружено движение
17,1004,2026-06-30,09:00:00,f,18
18,1004,2026-06-30,10:00:00,f,-127
"""
)


def msk(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


@pytest.fixture
def archive(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "справочник_объектов_диспетчер.csv").write_text(OBJECTS, encoding="utf-8")
    (raw / "справочник_каналов_датчиков.csv").write_text(CHANNELS, encoding="utf-8")
    (raw / "ext-journal-2025.csv").write_text(JOURNAL_2025, encoding="utf-8")
    (raw / "ext-journal-2026.csv").write_text(JOURNAL_2026, encoding="utf-8")
    out = tmp_path / "archive"
    build_archive(raw, out, memory_limit="1GB", threads=2)
    return out


@pytest.fixture
def pg(db_url):
    url = db_url.render_as_string(hide_password=False)
    with psycopg.connect(pg_conninfo(url), autocommit=True) as conn:
        conn.execute(
            "TRUNCATE work_orders, alarms, predictions, alarm_episodes, fault_episodes, channel_state, "
            "channel_daily, channel_hourly, events_raw, events, model_versions, channels, objects, runs "
            "RESTART IDENTITY CASCADE"
        )
        yield conn, url


def load(archive, url, tmp_path, scope=None):
    return Loader(archive, url, tmp_path / "work", scope=scope, workers=2, memory_limit="1GB", threads=2).run()


def test_full_load(archive, pg, tmp_path):
    conn, url = pg
    report = load(archive, url, tmp_path)
    assert report["info"]["gas_readings_before_deadband"] == 6

    events = conn.execute("SELECT channel_id, event_id FROM events ORDER BY channel_id, ts, event_id").fetchall()
    gas = [e for c, e in events if c == 1002]
    assert gas == [2, 5, 6, 7]  # 0.02 и 0.03 — внутри зоны, после «Неисправен» число пишется снова
    assert not [c for c, _ in events if c == 1003]  # движение — только в сыром окне
    assert len(events) == 7 + 4 + 2  # дым, газ после зоны, температура

    raw = conn.execute("SELECT channel_id, event_id FROM events_raw").fetchall()
    assert raw == [(1003, 16)]  # окно 90 дней от последнего события данных

    route = conn.execute("SELECT id, source, stream, deadband FROM channels ORDER BY id").fetchall()
    assert route == [  # маршрут приёма — в справочнике (D31)
        (1001, "hackathon", "significant", None),
        (1002, "hackathon", "raw", 0.02),
        (1003, "hackathon", "raw", None),
        (1004, "hackathon", "significant", None),
    ]

    faults = conn.execute("SELECT channel_id, start_ts, end_ts, state FROM fault_episodes ORDER BY 1, 2").fetchall()
    assert faults == [
        (1001, msk("2025-12-31T23:00:00"), msk("2026-01-01T01:00:00"), "Неисправен"),  # через границу года
        (1001, msk("2026-03-01T10:00:00"), msk("2026-03-01T11:00:00"), "Неисправен"),  # дребезг в одну секунду
        (1002, msk("2025-12-31T10:04:00"), msk("2025-12-31T10:05:00"), "Неисправен"),
    ]

    daily = conn.execute(
        "SELECT n_events, n_alarm, n_fault, n_num, min, max, mean FROM channel_daily "
        "WHERE channel_id = 1002 AND date = '2025-12-31'"
    ).fetchone()
    assert daily == pytest.approx((6, 0, 1, 5, 0.01, 0.04, 0.028))  # итоги — до зоны нечувствительности
    temp = conn.execute("SELECT n_events, n_num, mean FROM channel_daily WHERE channel_id = 1004").fetchone()
    assert temp == (2, 1, 18.0)  # код −127 — не измерение

    state = conn.execute(
        "SELECT value_raw, is_alarm, last_alarm_at, fault_since FROM channel_state WHERE channel_id = 1001"
    ).fetchone()
    assert state == ("Обнаружен дым", True, msk("2026-06-30T12:00:00"), None)
    assert conn.execute("SELECT count(*) FROM channel_hourly").fetchone()[0] == 4


def test_partial_reload_is_idempotent(archive, pg, tmp_path):
    conn, url = pg
    load(archive, url, tmp_path)
    counts = (
        "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM fault_episodes), "
        "(SELECT count(*) FROM channel_daily), (SELECT count(*) FROM channel_state)"
    )
    before = conn.execute(counts).fetchone()
    conn.execute("INSERT INTO runs (scenario, sim_start) VALUES ('x', now())")  # есть прогон — путь через DELETE
    load(archive, url, tmp_path, Scope(since=dt.date(2026, 1, 1), object_ids=[10]))
    assert conn.execute(counts).fetchone() == before
