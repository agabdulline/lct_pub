import json
from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb
import pytest

from app.archive import build_archive, events_glob
from app.domain.catalog import parse_picket, read_objects, tag_group

MSK = ZoneInfo("Europe/Moscow")

OBJECTS = """"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"
1,1,3831,district,Район
10,2,1,controlHouse,Объект А
100,3,10,controlHouse,"Подобъект А1"
"""

CHANNELS = """"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы","название_датчика","ид_объект"
1001,Пожарная охрана,Датчик дыма,"15-11.1.131.2.","ДД ПК12",100
1002,Температурная,Датчик температуры,"15-11.1.4.1.","ТЕМП ПК511+5
верхний",100
"""

JOURNAL_2026 = """ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика
2,1001,2026-02-01,10:00:00,t,Обнаружен дым
1,1001,2026-01-31,23:59:59,f,Дыма нет
1,1001,2026-01-31,23:59:59,f,Дыма нет
ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика
3,1002,2026-02-01,11:00:00,f,-127
4,1002,2026-02-01,12:00:00,f,Не определено
5,7777,2026-02-01,12:00:00,f,Норма
"""


@pytest.fixture
def raw(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    (d / "справочник_объектов_диспетчер.csv").write_text(OBJECTS, encoding="utf-8")
    (d / "справочник_каналов_датчиков.csv").write_text(CHANNELS, encoding="utf-8")
    (d / "ext-journal-2026.csv").write_text(JOURNAL_2026, encoding="utf-8")
    return d


def test_build_archive(raw, tmp_path):
    out = tmp_path / "archive"
    report = build_archive(raw, out, memory_limit="1GB", threads=2)
    year = report["years"]["2026"]
    assert year["rows_read"] == 7
    assert year["header_rows"] == 1
    assert year["duplicates_dropped"] == 1
    assert year["events"] == 5
    assert (year["out_of_catalog_events"], year["out_of_catalog_channels"]) == (1, 1)
    assert year["events_by_month"] == {"01": 1, "02": 4}
    assert json.loads((out / "report.json").read_text())["total"]["events"] == 5

    con = duckdb.connect()
    con.execute("SET TimeZone = 'Europe/Moscow'")
    rows = con.execute(
        f"SELECT channel_id, ts, is_alarm, value_raw, value_num, state, in_catalog, month "
        f"FROM read_parquet('{events_glob(out)}', hive_partitioning = true) ORDER BY channel_id, ts"
    ).fetchall()
    assert rows[0][:2] == (1001, datetime(2026, 1, 31, 23, 59, 59, tzinfo=MSK))
    assert rows[0][7] == "01"  # месяц — по московскому времени
    assert rows[1][2:6] == (True, "Обнаружен дым", None, "Обнаружен дым")
    assert rows[2][3:6] == ("-127", None, "Нет данных")
    assert rows[3][3:6] == ("Не определено", None, "Неопределен")
    assert rows[4][0] == 7777 and rows[4][6] is False

    channels = con.execute(f"SELECT * FROM read_parquet('{out}/catalog/channels.parquet') ORDER BY id").fetchall()
    assert channels[1][5] == "ТЕМП ПК511+5\nверхний" and channels[1][6:9] == (511, 5.0, "15-11.1")


def test_rebuild_replaces_year(raw, tmp_path):
    out = tmp_path / "archive"
    build_archive(raw, out, memory_limit="1GB", threads=2)
    report = build_archive(raw, out, years=[2026], memory_limit="1GB", threads=2)
    assert report["total"]["events"] == 5
    n = duckdb.sql(f"SELECT count(*) FROM read_parquet('{events_glob(out)}')").fetchone()[0]
    assert n == 5


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ДД ПК12", (12, None)),
        ("ТЕМП ПК511+5", (511, 5.0)),
        ("ДД ПК4+4,5", (4, 4.5)),
        ("ДД ПК 136(3шт.)1-3 эт.", (136, None)),
        ("ТД ПК86-85", (86, None)),
        ("Темп. ВШ ПК88,5", (88, None)),
        ("ОС ЦДТ", (None, None)),
    ],
)
def test_parse_picket(name, expected):
    assert parse_picket(name) == expected


def test_tag_group():
    assert tag_group("15-11.1.131.2.") == "15-11.1"
    assert tag_group("15-11.") == "15-11"
    assert tag_group("") is None


def test_parent_outside_catalog_is_dropped(raw):
    objects = {o.id: o for o in read_objects(raw)}
    assert objects[1].parent_id is None and objects[100].parent_id == 10
