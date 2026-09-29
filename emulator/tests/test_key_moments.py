"""Ключевые моменты сценария: синтетические срезы по каждому виду и срезы встроенных сценариев, если подготовлены."""

import csv
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from app.catalog import Catalog, add_external_catalogs, load_catalog
from app.config import settings
from app.key_moments import LIMIT, brief, key_moments, plural, verb

T0 = datetime(2026, 3, 28, 10, 0)
# слова, которые описывают реакцию системы, а не данные журнала
FORBIDDEN = ("реакц", "требует", "проверка", "ППР", "вероятн", "модель", "заявк", "авари", "ложн")

OBJECTS = [(1, 2, None, "объект Альфа"), (11, 3, 1, "ПС объект Альфа"), (2, 2, None, "объект Бета"),
           (21, 3, 2, "ПС объект Бета")]  # fmt: skip
CHANNELS = (
    [(100 + i, f"ДД ПК{i}", "Датчик дыма", 11) for i in range(1, 21)]
    + [(201, "ДД ПК1", "Датчик дыма", 21), (301, "УСРСБ ПК5", "Состояние УИР-Р", 11)]
    + [(400 + i, f"Насос {i}", "Состояние насоса", 11) for i in range(1, 11)]
    + [(501, "ГАЗ Д1", "Газовый датчик", 21), (601, "КД дверь", "КД Дверь", 21), (701, "ОД ВШ", "Датчик движения", 21)]
    + [(801, "Общие ворота 1", "Датчик проезда", 11), (802, "Въезд 1", "Датчик проезда", 21)]
    + [(901, "Коды ошибок · агрегат 1", "Код ошибки", 11), (902, "Коды ошибок · агрегат 2", "Код ошибки", 21)]
    + [(903, "Метка времени", "Часы", 21)]
    + [(904 + k, f"Отказы и ремонты · агрегат {k + 1}", "Отказы и ремонты", sub) for k, sub in enumerate((11, 21))]
)


def connect(events: list[tuple], channels=CHANNELS, objects=OBJECTS) -> duckdb.DuckDBPyConnection:
    """Таблицы s, ch, obj, как их регистрирует эмулятор. Событие — (канал, время, значение[, тревога])."""
    con = duckdb.connect()
    con.execute("CREATE TABLE s (event_id BIGINT, channel_id INTEGER, ts TIMESTAMP, is_alarm BOOLEAN, value VARCHAR)")
    if events:
        rows = [(i, e[0], e[1], e[3] if len(e) > 3 else False, e[2]) for i, e in enumerate(events)]
        con.executemany("INSERT INTO s VALUES (?, ?, ?, ?, ?)", rows)
    parent = {o[0]: o[2] for o in objects}
    con.execute(
        "CREATE TABLE ch AS SELECT unnest($1) AS channel_id, unnest($2) AS name, unnest($3) AS sensor_type, "
        "'' AS subsystem, '' AS tag, unnest($4) AS sub_object_id, unnest($5) AS object_id",
        [[c[0] for c in channels], [c[1] for c in channels], [c[2] for c in channels], [c[3] for c in channels],
         [parent[c[3]] for c in channels]],
    )  # fmt: skip
    con.execute(
        "CREATE TABLE obj AS SELECT unnest($1) AS id, unnest($2) AS level, unnest($3) AS parent_id, unnest($4) AS name",
        [[o[0] for o in objects], [o[1] for o in objects], [o[2] for o in objects], [o[3] for o in objects]],
    )
    return con


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def kinds(moments: list[dict]) -> list[str]:
    return [m["kind"] for m in moments]


def assert_neutral(moments: list[dict]) -> None:
    """Слова шаблонов — только факты; названия каналов и объектов в «кавычках» не проверяются."""
    for m in moments:
        own = re.sub(r"«[^»]*»", "", f"{m['title']} {m['text']}")
        for name in (m["object"], m["subobject"]):
            own = own.replace(name or "\0", "")
        for word in FORBIDDEN:
            assert word not in own, (word, m["title"], m["text"])


def test_empty_slice_has_no_moments():
    assert key_moments(connect([])) == []


def test_smoke_group_and_single_alarm():
    events = [(100 + i, at(i), "Обнаружен дым") for i in range(1, 13)]  # 12 датчиков объекта Альфа за 11 мин
    events.append((201, at(120), "Обнаружен дым"))  # один датчик объекта Бета через 2 ч
    ms = key_moments(connect(events))
    group = next(m for m in ms if m["kind"] == "smoke_group")
    assert len(group["channel_ids"]) == 12 and group["object"] == "объект Альфа" and group["count"] == 12
    assert group["title"] == "10:01–10:12 — объект Альфа: 12 дымовых датчиков, 12 срабатываний за 11 мин"
    single = next(m for m in ms if m["kind"] == "smoke_single")
    assert single["channel_ids"] == [201] and single["title"] == "12:00 — объект Бета: дымовой датчик «ДД ПК1»"
    assert kinds(ms)[0] == "first_event" and kinds(ms)[-1] == "last_event"
    assert_neutral(ms)


def test_levers_and_state_wave_with_return():
    events = [(301, at(0), "Рычаг сдернут"), (301, at(3), "Рычаг сдернут")]
    events += [(400 + i, at(10) + timedelta(seconds=i), "Обесточен") for i in range(1, 9)]
    events += [(400 + i, at(30) + timedelta(seconds=i), "Включен") for i in range(1, 9)]
    ms = key_moments(connect(events))
    lever = next(m for m in ms if m["kind"] == "lever")
    assert lever["count"] == 2 and "2 срыва рычага «УСРСБ ПК5»" in lever["title"]
    wave = next(m for m in ms if m["kind"] == "state_wave")
    assert wave["state"] == "Обесточен" and len(wave["channel_ids"]) == 8
    assert "8 каналов сообщили «Обесточен» за 7 с" in wave["title"]
    ret = next(m for m in ms if m["kind"] == "state_return")
    assert ret["state"] == "Обесточен" and "«Включен» — 8" in ret["text"]
    assert_neutral(ms)


def test_few_channels_are_not_a_wave():
    events = [(400 + i, at(i), "Неисправен") for i in range(1, 5)]  # 4 канала — меньше порога
    assert "state_wave" not in kinds(key_moments(connect(events)))


def test_flapping_ignores_motion_sensors():
    values = ["Норма", "Не замкнут"]
    events = [(601, at(i * 2), values[i % 2]) for i in range(14)]  # 13 смен за 26 мин
    events += [(701, at(i * 0.2), ["Движения нет", "Обнаружено движение"][i % 2]) for i in range(100)]
    ms = key_moments(connect(sorted(events, key=lambda e: e[1])))
    flap = [m for m in ms if m["kind"] == "flapping"]
    assert len(flap) == 1 and flap[0]["channel_ids"] == [601]
    assert "«КД дверь» сменил состояние 13 раз за 24 мин" in flap[0]["title"]


def test_flapping_counts_states_not_passes_or_codes():
    # проезды: значение — билет машины, каждый раз новый; метки времени — тоже каждый раз новые
    events = [(801, at(i * 2), f"2015{i:04d}-{i}") for i in range(20)]
    events += [(903, at(i * 2 + 1), f"01.01.1970 03:00:{i:02d}") for i in range(20)]
    events += [(601, at(i * 3), ["Норма", "Не замкнут"][i % 2]) for i in range(12)]  # 11 смен — состояние
    ms = key_moments(connect(sorted(events, key=lambda e: e[1])))
    flap = [m for m in ms if m["kind"] == "flapping"]
    assert [m["channel_ids"] for m in flap] == [[601]]


def test_access_peak_busiest_hours():
    events = [(801, at(i * 3), f"car-{i % 5}") for i in range(15)]  # 15 проездов за 42 мин
    events += [(802, at(200 + i * 10), f"car-{i}") for i in range(5)]  # 5 за 40 мин — меньше порога
    ms = key_moments(connect(events))
    peaks = [m for m in ms if m["kind"] == "access_peak"]
    assert len(peaks) == 1 and peaks[0]["count"] == 15
    assert "объект Альфа: 15 проездов за 42 мин" in peaks[0]["title"]
    assert "ПС объект Альфа — 15" in peaks[0]["text"] and "Разных пропусков — 5" in peaks[0]["text"]
    assert_neutral(ms)


def test_code_series_and_single_codes():
    # у канала кодов нет спокойных значений: каждое событие — тревога
    events = [(901, at(0), "error1", True), (901, at(120), "error3", True), (901, at(360), "error1", True)]
    events += [(902, at(60), "error2", True), (902, at(60 * 30), "error5", True)]  # разрыв больше суток
    # канал состояний с тревогой и возвратом — не коды, у него свои детекторы
    events += [(601, at(10), "Не замкнут", True), (601, at(11), "Норма")]
    ms = key_moments(connect(sorted(events, key=lambda e: e[1])))
    codes = [m for m in ms if m["kind"] == "code_series"]
    assert len(codes) == 2
    series_, single = codes
    assert series_["title"] == "28.03 10:00–16:00 — ПС объект Альфа: 3 тревоги типа «Код ошибки» за 6 ч"
    assert "«error1» — 2, «error3» — 1" in series_["text"] and series_["subobject"] == "ПС объект Альфа"
    assert single["count"] == 2 and "одиночные тревоги типа «Код ошибки»: 2 на 1 подобъекте" in single["title"]
    assert 601 not in {c for m in codes for c in m["channel_ids"]}
    assert_neutral(ms)


def test_failure_records_grouped_by_time():
    # записи службы без тревоги: одна минута — один момент, через сутки — другой
    events = [(904, at(0), "comp2"), (905, at(0), "comp4"), (904, at(60 * 24), "comp1")]
    ms = key_moments(connect(events))
    rec = [m for m in ms if m["kind"] == "failure_record"]
    assert [m["count"] for m in rec] == [2, 1]
    assert rec[0]["title"] == "28.03 10:00 — Отказы и ремонты: 2 записи, 2 подобъекта"
    assert rec[0]["text"] == "ПС объект Альфа — «comp2»; ПС объект Бета — «comp4»."
    assert rec[1]["title"].endswith("1 запись, ПС объект Альфа — «comp1»")
    assert_neutral(ms)


def test_gas_peak_above_threshold_only():
    events = [(501, at(i), v) for i, v in enumerate(["0.01", "0.1", "0.25", "0.31", "0.02"])]
    ms = key_moments(connect(events))
    gas = [m for m in ms if m["kind"] == "gas_peak"]
    assert len(gas) == 1 and gas[0]["count"] == 2 and "максимум 0.31" in gas[0]["title"]


def test_silence_between_events():
    events = [(601, at(0), "Норма"), (601, at(125), "Не замкнут")]
    silence = [m for m in key_moments(connect(events)) if m["kind"] == "silence"]
    assert len(silence) == 1 and silence[0]["title"] == "10:00–12:05 — нет событий 2 ч 5 мин"
    assert silence[0]["until"] == at(125) and silence[0]["channel_ids"] == []


def test_silence_counts_from_stream_step():
    # телеметрия пачкой раз в час: часовой интервал — обычный шаг, не пауза; пауза — от трёх шагов
    hours = [*range(10), *range(15, 18)]
    events = [(401 + k, at(h * 60), "Норма") for h in hours for k in range(5)]
    silence = [m for m in key_moments(connect(events)) if m["kind"] == "silence"]
    assert [m["title"] for m in silence] == ["28.03 19:00–29.03 01:00 — нет событий 6 ч"]
    assert "Обычный шаг потока — 1 ч." in silence[0]["text"]


def test_limit_folds_extra_single_alarms():
    # 40 одиночных срабатываний с разрывом больше 25 мин — не влезают в LIMIT
    events = [(100 + (i % 20) + 1, at(i * 30), "Обнаружен дым") for i in range(40)]
    ms = key_moments(connect(events))
    assert len(ms) == LIMIT and kinds(ms)[0] == "first_event" and kinds(ms)[-1] == "last_event"
    folded = [m for m in ms if m["title"].startswith("Ещё")]
    assert len(folded) == 1 and folded[0]["kind"] == "smoke_single"
    singles = [m for m in ms if m["kind"] == "smoke_single"]
    assert sum(m["count"] for m in singles) == 40
    assert [m["at"] for m in ms] == sorted(m["at"] for m in ms)


def test_plural_and_verb_agreement():
    assert plural(1, "канал", "канала", "каналов") == "1 канал"
    assert plural(22, "канал", "канала", "каналов") == "22 канала"
    assert plural(11, "канал", "канала", "каналов") == "11 каналов"
    assert plural(1204, "событие", "события", "событий") == "1 204 события"
    assert verb(21, "сообщил", "сообщили") == "сообщил" and verb(11, "сообщил", "сообщили") == "сообщили"


def test_brief_names_what_was_found():
    events = [(100 + i, at(i), "Обнаружен дым") for i in range(1, 6)] + [(201, at(10), "Обнаружен дым")]
    assert brief(key_moments(connect(events))) == "1 группа дымовых срабатываний; 1 одиночное срабатывание дыма"


# ---------- срезы встроенных сценариев: data/processed/emulator, собираются `make emulator-slices`

SLICES = Path(settings.slices_dir)
SCENARIOS = [
    "new-object",
    "ordinary-day",
    "power-outage",
    "ppr-failures",
    "ppr",
    "sample-2026-08-01",
    "solitary-fault",
    "subobject-week",
]


@pytest.fixture(scope="module")
def catalog_db(tmp_path_factory) -> Path:
    """Справочник в файле DuckDB — один раз на модуль: таблицы ch и obj, как их регистрирует эмулятор."""
    if not (settings.objects_csv.is_file() and settings.channels_csv.is_file()):
        pytest.skip("нет справочников в data/raw")
    catalog: Catalog = load_catalog(settings.objects_csv, settings.channels_csv)
    add_external_catalogs(catalog, SLICES / "external")  # справочники наборов, если срезы наборов собраны
    tmp = tmp_path_factory.mktemp("catalog")
    with (tmp / "ch.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel_id", "name", "sensor_type", "subsystem", "tag", "sub_object_id", "object_id"])
        for c in catalog.channels.values():
            parent = catalog.objects[c.object_id].parent_id if c.object_id in catalog.objects else None
            w.writerow([c.id, c.name, c.sensor_type, c.subsystem, c.tag, c.object_id, parent])
    with (tmp / "obj.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "level", "parent_id", "name"])
        w.writerows([o.id, o.level, o.parent_id, o.name] for o in catalog.objects.values())
    db = tmp / "catalog.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute(f"CREATE TABLE ch AS SELECT * FROM read_csv('{tmp / 'ch.csv'}', header = true)")
        con.execute(f"CREATE TABLE obj AS SELECT * FROM read_csv('{tmp / 'obj.csv'}', header = true)")
    return db


def slice_con(name: str, db: Path) -> duckdb.DuckDBPyConnection:
    path = SLICES / f"{name}.parquet"
    if not path.is_file():
        pytest.skip(f"срез {name} не подготовлен: make emulator-slices")
    con = duckdb.connect(str(db), read_only=True)
    con.execute(f"CREATE TEMP VIEW s AS SELECT * FROM read_parquet('{path}')")
    return con


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_slices(name, catalog_db):
    con = slice_con(name, catalog_db)
    started = time.perf_counter()
    ms = key_moments(con)
    elapsed = time.perf_counter() - started
    assert 2 <= len(ms) <= LIMIT and elapsed < 2
    assert kinds(ms)[0] == "first_event" and kinds(ms)[-1] == "last_event"
    assert [m["at"] for m in ms] == sorted(m["at"] for m in ms)
    assert all(m["title"] and m["text"] for m in ms)
    assert_neutral(ms)


EXPECTED = {
    "new-object": ("smoke_group", "объект Гамма: 358 дымовых датчиков"),
    "power-outage": ("state_wave", "122 канала сообщили «Обесточен»"),
    "solitary-fault": ("smoke_single", "дымовой датчик «ДД ПК432 ур.4», 5 срабатываний"),
    "ppr-failures": ("state_wave", "сообщили «Неисправен»"),
    "ppr": ("lever", "51 срыв рычага"),
    "sample-2026-08-01": ("gas_peak", "газ выше 0.2"),
    "subobject-week": ("silence", "нет событий 18 ч 7 мин"),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_scenario_facts(name, catalog_db):
    kind, fragment = EXPECTED[name]
    ms = key_moments(slice_con(name, catalog_db))
    assert any(m["kind"] == kind and fragment in m["title"] for m in ms), [m["title"] for m in ms]


# срезы сценариев внешних наборов (docs/architecture/external-data.md §5.2): что в них видно и чего не должно быть
EXTERNAL = {
    "ext-vast2017-week": ("access_peak", "проездов за", "flapping"),
    "ext-pdm-week": ("failure_record", "Отказы и ремонты", None),
    "ext-magdeburg-night": ("first_event", "первое событие", "silence"),
}


@pytest.mark.parametrize("name", sorted(EXTERNAL))
def test_external_scenario_facts(name, catalog_db):
    kind, fragment, absent = EXTERNAL[name]
    ms = key_moments(slice_con(name, catalog_db))
    assert any(m["kind"] == kind and fragment in m["title"] for m in ms), [m["title"] for m in ms]
    assert absent is None or absent not in kinds(ms)
    assert not any(m["kind"] == "silence" and m["title"].endswith("нет событий 1 ч") for m in ms)
    assert_neutral(ms)
