import os
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
from conftest import write_prepared, write_scenario, write_slice, write_upload

from app.catalog import Catalog, add_external_catalogs
from app.config import settings
from app.player import load_slice
from app.prepare import main as prepare_main
from app.prepare import prepare
from app.scenarios import all_scenarios, density_max_speed, load_scenarios, parse_scenario

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"

# Дефекты журнала (docs/data-format.md §3): строки не по времени, заголовок внутри файла, повтор строки,
# канал вне справочника. 300001 — газовый датчик.
JOURNAL = """\
ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика
12,287267,2026-03-28,05:58:02,f,Дыма нет
10,287267,2026-03-28,05:56:40,t,Обнаружен дым
10,287267,2026-03-28,05:56:40,t,Обнаружен дым
ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика
13,120298,2026-03-28,05:58:02,f,18
11,999999,2026-03-28,05:57:00,t,Обнаружен дым
14,300001,2026-03-28,05:59:00,f,0.01
9,287267,2026-03-28,04:59:59,f,Норма
15,287268,2026-03-28,06:00:00,f,Рычаг сдернут
"""


def scenario(**overrides):
    data = {
        "title": "Тест",
        "description": "Короткий\n  сценарий",
        "period": {"start": datetime(2026, 3, 28, 5), "end": datetime(2026, 3, 28, 6)},
        "objects": [7],
        "speed": 60,
    }
    data.update(overrides)
    return parse_scenario("test", data)


def test_repository_scenarios_are_valid():
    scenarios = load_scenarios(SCENARIOS_DIR)
    assert {"ppr", "solitary-fault", "power-outage", "ordinary-day", "subobject-week", "new-object"} <= set(scenarios)
    for s in scenarios.values():
        assert all(s.start <= m.at < s.end and (m.until is None or m.at <= m.until) for m in s.timeline)
        assert s.speed <= s.max_speed
        if s.dataset:
            # внешние наборы — в своих датах (external-data.md §5.2), разметка — только с пометкой источника
            assert s.name.startswith("ext-")
            assert all(m.source == "dataset" for m in s.timeline)
            continue
        # журнал с 2022 года в рабочей базе — история для признаков; данные кончаются 30.06.2026,
        # пример организаторов (source) — 01.08.2026
        assert datetime(2022, 1, 1) <= s.start and s.end <= (datetime(2026, 8, 2) if s.source else datetime(2026, 7, 1))
        assert all(m.source == "journal" for m in s.timeline)


def test_parse_scenario_rules():
    s = scenario()
    assert s.description == "Короткий сценарий"
    assert s.max_speed == 60
    with pytest.raises(ValueError, match="без пояса"):
        scenario(period={"start": "2026-03-28T05:00:00+03:00", "end": "2026-03-28T06:00:00+03:00"})
    with pytest.raises(ValueError, match="скорость"):
        scenario(speed=600)
    assert scenario(speed=600, max_speed=1000).speed == 600


def test_prepare_slice_follows_reading_rules(tmp_path, catalog):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ext-journal-2026.csv").write_text(JOURNAL, encoding="utf-8")
    report = prepare(scenario(), catalog, raw, tmp_path / "out")
    rows = duckdb.sql(f"SELECT event_id, channel_id, ts, is_alarm, value FROM '{report['path']}'").fetchall()
    assert rows == [
        (10, 287267, datetime(2026, 3, 28, 5, 56, 40), True, "Обнаружен дым"),
        (12, 287267, datetime(2026, 3, 28, 5, 58, 2), False, "Дыма нет"),
        (13, 120298, datetime(2026, 3, 28, 5, 58, 2), False, "18"),
    ]
    assert report["events"] == 3 and report["alarms"] == 1

    with_gas = prepare(scenario(include_gas=True), catalog, raw, tmp_path / "gas")
    assert with_gas["events"] == 4


def test_prepare_requires_journal_for_every_year(tmp_path, catalog):
    s = scenario(period={"start": datetime(2025, 12, 31, 23), "end": datetime(2026, 1, 1, 1)})
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ext-journal-2026.csv").write_text(JOURNAL, encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="2025"):
        prepare(s, catalog, raw, tmp_path / "out")


def test_prepare_from_source_file_with_quotes_and_true_false(tmp_path, catalog):
    """Пример журнала организаторов: поля в кавычках, флаг true / false (docs/data-format.md §2)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "пример.csv").write_text(
        '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"\n'
        '4524243389,287267,"2026-08-01","03:09:27",true,"Обнаружен дым"\n'
        '4524243390,300001,"2026-08-01","03:09:28",false,"0.02"\n'
        '4524243391,120298,"2026-08-01","23:59:58",false,"28"\n',
        encoding="utf-8",
    )
    s = scenario(
        source="пример.csv",
        objects=[],
        include_gas=True,
        period={"start": datetime(2026, 8, 1), "end": datetime(2026, 8, 2)},
    )
    report = prepare(s, catalog, raw, tmp_path / "out")
    rows = duckdb.sql(f"SELECT channel_id, ts, is_alarm, value FROM '{report['path']}'").fetchall()
    assert rows == [
        (287267, datetime(2026, 8, 1, 3, 9, 27), True, "Обнаружен дым"),
        (300001, datetime(2026, 8, 1, 3, 9, 28), False, "0.02"),
        (120298, datetime(2026, 8, 1, 23, 59, 58), False, "28"),
    ]
    assert report["channels_in_scope"] == 4  # пустой список объектов — все каналы


UPLOAD_ROWS = [  # ид события нет — порядок в секунде задаёт порядок строк файла
    (None, 287267, "2026-08-01 00:10:00", True, "Обнаружен дым"),
    (None, 120298, "2026-08-01 00:10:00", False, "18"),
    (None, 287267, "2026-08-01 00:10:00", False, "Дыма нет"),
    (None, 999999, "2026-08-01 00:20:00", False, "Норма"),  # канал вне справочника остаётся в срезе
]


def test_uploads_appear_without_restart(tmp_path):
    scenarios_dir, slices = tmp_path / "sc", tmp_path / "sl"
    write_scenario(scenarios_dir, "a", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    assert list(all_scenarios(scenarios_dir, slices, 200)) == ["a"]

    write_upload(slices, "upload-20260927-abc123", UPLOAD_ROWS)
    listed = all_scenarios(scenarios_dir, slices, 200)
    assert list(listed) == ["a", "upload-20260927-abc123"]
    up = listed["upload-20260927-abc123"].describe(slices)
    assert up == {
        "name": "upload-20260927-abc123",
        "source": "upload",
        "title": "Мой журнал",
        "description": "Журнал из файла «журнал.xlsx», 01.08.2026 00:00 — 01.08.2026 01:00: событий — 4, каналов — 3.",
        "period": {"start": "2026-08-01T00:00:00+03:00", "end": "2026-08-01T01:00:00+03:00"},
        "objects": [7],
        "include_gas": True,
        "speed": 60.0,
        "max_speed": 1000.0,  # три события в минуту
        "timeline": [],
        "dataset": None,
        "ready": True,
        "source_file": "журнал.xlsx",
        "uploaded_at": "2026-09-27T10:00:00+03:00",
        "warnings": [{"code": "duplicates", "count": 1, "message": "Повтор строки удалён"}],
        "events": 4,
        "channels": 3,
    }
    assert listed["a"].describe(slices)["source"] == "builtin"

    # проигрыватель передаёт события без ид в порядке строк файла, канала вне справочника нет
    data = load_slice(listed["upload-20260927-abc123"].slice_path(slices), {287267, 120298})
    assert list(zip(data.channel, data.value, strict=True)) == [
        (287267, "Обнаружен дым"),
        (120298, "18"),
        (287267, "Дыма нет"),
    ]


def test_upload_edge_cases(tmp_path, caplog):
    scenarios_dir, slices = tmp_path / "sc", tmp_path / "sl"
    write_scenario(scenarios_dir, "a", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    # json без среза — видно, но не готово
    write_upload(slices, "upload-20260927-000001", UPLOAD_ROWS).with_suffix(".parquet").unlink()
    # имя встроенного, битый json, период задом наперёд — пропускаются, список не ломается
    write_upload(slices, "a", UPLOAD_ROWS)
    (slices / "uploaded" / "upload-20260927-000002.json").write_text("{", encoding="utf-8")
    write_upload(
        slices,
        "upload-20260927-000003",
        UPLOAD_ROWS,
        period={"start": "2026-08-01T01:00:00", "end": "2026-08-01T00:00:00"},
    )
    # описание от пользователя — как есть, поздняя загрузка — выше
    write_upload(
        slices, "upload-20260927-000004", UPLOAD_ROWS, description="Свой текст", uploaded_at="2026-09-27T11:00:00+03:00"
    )

    listed = all_scenarios(scenarios_dir, slices, 200)
    assert list(listed) == ["a", "upload-20260927-000004", "upload-20260927-000001"]
    assert listed["a"].origin == "builtin"
    assert listed["upload-20260927-000001"].describe(slices)["ready"] is False
    assert listed["upload-20260927-000004"].description == "Свой текст"
    assert "совпадает по имени со встроенным" in caplog.text and "000002.json пропущен" in caplog.text

    # изменённые метаданные перечитываются
    meta = slices / "uploaded" / "upload-20260927-000004.json"
    write_upload(slices, "upload-20260927-000004", UPLOAD_ROWS, title="Новое название")
    os.utime(meta, ns=(meta.stat().st_mtime_ns + 10**9,) * 2)
    assert all_scenarios(scenarios_dir, slices, 200)["upload-20260927-000004"].title == "Новое название"


def test_density_max_speed_steps(tmp_path):
    def minute_of(n: int) -> Path:
        path = tmp_path / f"{n}.parquet"
        write_slice(path, [(i, 287267, f"2026-08-01 00:00:{i % 60:02d}", False, "Норма") for i in range(n)])
        return path

    # 200 событий/с выхода: минута из 50 событий без растяжения проходит на ×240 → ступень ×180
    assert density_max_speed(minute_of(50), 200) == 180
    assert density_max_speed(minute_of(1000), 200) == 60  # плотнее предела — не ниже ×60
    assert density_max_speed(minute_of(5), 200) == 1000


def test_timeline_sources_and_dataset_field():
    s = scenario(
        dataset="magdeburg",
        timeline=[
            {"at": datetime(2026, 3, 28, 5, 10), "text": "Первая тревога"},
            {"at": "2026-03-28T05:20:00", "until": "2026-03-28T05:30:00", "text": "Опыт 1", "source": "dataset"},
        ],
    )
    described = s.describe(Path("/нет"))
    assert described["dataset"] == "magdeburg"
    assert described["timeline"] == [
        {"at": "2026-03-28T05:10:00+03:00", "until": None, "text": "Первая тревога", "source": "journal"},
        {
            "at": "2026-03-28T05:20:00+03:00",
            "until": "2026-03-28T05:30:00+03:00",
            "text": "Опыт 1",
            "source": "dataset",
        },
    ]
    with pytest.raises(ValueError, match="источник строки timeline"):
        scenario(timeline=[{"at": datetime(2026, 3, 28, 5, 10), "text": "т", "source": "модель"}])


def test_density_floor_for_dense_external_streams(tmp_path):
    # 9 узлов × 13 показаний раз в 10 с — 702 события в минуту: ×17 без растяжения → ступень ×15
    path = tmp_path / "hall.parquet"
    write_slice(path, [(i, 901000601, f"2023-07-13 14:{i // 702:02d}:00", False, "1.0") for i in range(702 * 3)])
    assert density_max_speed(path, 200) == 60  # загруженные журналы — не ниже ×60
    assert density_max_speed(path, 200, floor=1) == 15


EXTERNAL_EVENTS = [
    (1, 901000601, "2023-07-13 14:04:50", False, "400.0"),  # до периода
    (2, 901000601, "2023-07-13 14:05:00", False, "410.5"),
    (3, 901000602, "2023-07-13 14:05:00", False, "0.0"),
    (4, 901000601, "2023-07-13 14:55:00", False, "420.0"),  # конец не включается
]


def test_prepare_external_dataset_slice(tmp_path, monkeypatch):
    external, slices, scenarios_dir = tmp_path / "external", tmp_path / "slices", tmp_path / "sc"
    write_prepared(external / "magdeburg" / "prepared", EXTERNAL_EVENTS)
    write_scenario(
        scenarios_dir,
        "ext-hall",
        "2023-07-13T14:05:00",
        "2023-07-13T14:55:00",
        dataset="magdeburg",
        objects=[9010002],
        speed=10,
        max_speed=15,
    )
    write_scenario(
        scenarios_dir, "ext-missing", "2023-07-13T14:05:00", "2023-07-13T14:55:00", dataset="нет", objects=[1]
    )
    for name, value in (("scenarios_dir", scenarios_dir), ("slices_dir", slices), ("external_dir", external)):
        monkeypatch.setattr(settings, name, value)

    # все сценарии разом: набор без prepared/ пропускается, остальные готовятся
    assert prepare_main([]) == 0
    rows = duckdb.sql(f"SELECT event_id, channel_id, value FROM '{slices / 'ext-hall.parquet'}'").fetchall()
    assert rows == [(2, 901000601, "410.5"), (3, 901000602, "0.0")]
    assert (slices / "external" / "magdeburg" / "channels.parquet").is_file()
    assert not (slices / "ext-missing.parquet").exists()
    # по имени — ошибка, а не молчаливый пропуск
    with pytest.raises(FileNotFoundError, match="нет не подготовлен"):
        prepare_main(["ext-missing"])

    # эмулятор видит справочник набора рядом со срезами и передаёт события его каналов
    catalog = Catalog()
    add_external_catalogs(catalog, slices / "external")
    assert load_slice(slices / "ext-hall.parquet", set(catalog.channels)).channel == [901000601, 901000602]


def test_upload_summary_file_is_not_an_upload(tmp_path, caplog):
    """Сводка загрузки на диске (`<id>.summary.json`, scenario_data) — не метаданные ещё одной загрузки."""
    slices = tmp_path / "slices"
    path = write_upload(slices, "upload-20260928-aaa111", UPLOAD_ROWS)
    path.with_name("upload-20260928-aaa111.summary.json").write_text('{"key": [], "summary": {}}', encoding="utf-8")
    assert list(all_scenarios(tmp_path / "scenarios", slices, 200.0)) == ["upload-20260928-aaa111"]
    assert "пропущен" not in caplog.text
