"""VAST Challenge 2017 MC1 — проезды по заповеднику Lekagul (docs/architecture/external-data.md §3.2, §3.3, §4.1).

Исходник — архив с одним CSV: момент, билет машины, её класс, датчик. Справочник: корень набора → заповедник →
6 групп датчиков (уровень 3), 40 датчиков проезда. Журнал — событие на каждый проезд, значение — идентификатор
билета (RF-метки), как пропуск в СКУД; класс машины — не в событии, а в справочнике пропусков.

Допуски. По описанию набора закрытые ворота пропускают только машины рейнджеров — класс `2P`. За ними — посты и
база рейнджеров. Какие посты за воротами, в тексте нет: берём посты, где не-рейнджеры почти не бывают
(доля меньше `RESTRICTED_SHARE`); посты 0 и 2 стоят на общих дорогах. Допуск — билет `2P` → группы
«Закрытые ворота» и «Посты и база рейнджеров за воротами»; у остальных групп допусков нет, они открыты.

Разметка:
- `violation` — проезд машины не-`2P` через объект с допуском, по событию: грузовик, ночами проезжающий закрытые
  ворота к посту 3 (эталон набора), и мотоциклы на посту 1 в обход ворот;
- `suspicious` — поездки из эталона набора, которые нарушением доступа не являются: грузовики, ночующие вне
  кемпингов в названные даты, и две машины с превышением скорости.
"""

import json
import re
import zipfile
from pathlib import Path

import duckdb

from app.external.registry import Dataset, channel_id, object_id

SOURCE_FILE = "MC1Data.zip"
CSV_NAME = "Lekagul Sensor Data.csv"
SYSTEM_TYPE = "Контроль доступа (внешн.)"
SENSOR_TYPE = "Датчик проезда"
RANGER_CLASS = "2P"
RESTRICTED_SHARE = 0.10  # пост рейнджеров за воротами — доля проездов не-рейнджеров меньше этой

ROOT_N, PRESERVE_N = 1, 2
# Группа: номер объекта, ключ tag_group, название, закрыта ли допуском
GROUPS = {
    "entrances": (11, "Въезды", False),
    "general_gates": (12, "Общие ворота", False),
    "campings": (13, "Кемпинги", False),
    "ranger_stops_open": (14, "Посты рейнджеров на общих дорогах", False),
    "closed_gates": (15, "Закрытые ворота", True),
    "ranger_zone": (16, "Посты и база рейнджеров за воротами", True),
}
CLASS_NAMES = {
    "1": "2-осная легковая или мотоцикл",
    "2": "2-осный грузовик",
    "3": "3-осный грузовик",
    "4": "грузовик от 4 осей",
    "5": "2-осный автобус",
    "6": "3-осный автобус",
    "2P": "машина рейнджеров (2-осный грузовик)",
}
# Эталон набора (Reviewer Guide MC1): даты ночёвок грузовиков вне кемпингов и машины с превышением скорости
SOLUTION_OVERNIGHT_DATES = ["2015-06-27", "2015-07-04", "2015-09-01", "2015-10-22", "2016-03-23", "2016-05-08"]
SOLUTION_SPEEDING = ["20151320031302-558", "20151320031302-782"]


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _write(con: duckdb.DuckDBPyConnection, sql: str, path: Path) -> None:
    con.execute(f"COPY ({sql}) TO {_q(path)} (FORMAT parquet, COMPRESSION zstd)")


def _sensor(gate: str, open_stops: set[str]) -> tuple[str, int, str]:
    """Датчик → (группа, номер в группе, название)."""
    if gate == "ranger-base":
        return "ranger_zone", 99, "База рейнджеров"
    m = re.fullmatch(r"(entrance|general-gate|camping|gate|ranger-stop)(\d+)", gate)
    if not m:
        raise SystemExit(f"{CSV_NAME}: неизвестный датчик «{gate}»")
    kind, i = m.group(1), int(m.group(2))
    if kind == "ranger-stop":
        group = "ranger_stops_open" if gate in open_stops else "ranger_zone"
        return group, i, f"Пост рейнджеров {i}"
    group, label = {
        "entrance": ("entrances", "Въезд"),
        "general-gate": ("general_gates", "Общие ворота"),
        "camping": ("campings", "Кемпинг"),
        "gate": ("closed_gates", "Закрытые ворота"),
    }[kind]
    return group, i, f"{label} {i}"


def _extract(zip_path: Path, tmp: Path) -> Path:
    with zipfile.ZipFile(zip_path) as z:
        data = z.read(CSV_NAME)
    path = tmp / "_source.csv"
    path.write_bytes(data)
    return path


def build(con: duckdb.DuckDBPyConnection, dataset: Dataset, inputs: dict[str, Path], out: Path) -> dict:
    k = dataset.k
    csv = _extract(inputs[SOURCE_FILE], out)
    con.execute(
        f"""CREATE OR REPLACE TABLE src AS
            SELECT CAST("Timestamp" AS TIMESTAMP) AS ts, "car-id" AS car, "car-type" AS class, "gate-name" AS gate
            FROM read_csv({_q(csv)}, all_varchar = true, header = true)"""
    )
    csv.unlink()
    rows = con.execute("SELECT count(*) FROM src").fetchone()[0]
    if n := con.execute(
        "SELECT count(*) FROM (SELECT car FROM src GROUP BY car HAVING count(DISTINCT class) > 1)"
    ).fetchone()[0]:
        raise SystemExit(f"{CSV_NAME}: у {n} билетов меняется класс машины")

    # Посты рейнджеров: за воротами — где не-рейнджеры почти не бывают
    stops = con.execute(
        f"""SELECT gate, count(*) FILTER (WHERE class <> '{RANGER_CLASS}') / count(*) AS share
            FROM src WHERE gate LIKE 'ranger-stop%' GROUP BY gate ORDER BY gate"""
    ).fetchall()
    open_stops = {g for g, share in stops if share >= RESTRICTED_SHARE}

    # Справочник
    root, preserve = object_id(k, ROOT_N), object_id(k, PRESERVE_N)
    objects = [
        (
            root,
            None,
            1,
            "external_site",
            "Заповедник Lekagul · VAST 2017 (синтетика)",
            json.dumps({"dataset": dataset.key}),
        ),
        (preserve, root, 2, "preserve", "Заповедник", None),
    ]
    for n, name, restricted in GROUPS.values():
        objects.append((object_id(k, n), preserve, 3, "sensor_group", name, json.dumps({"restricted": restricted})))
    gates = [r[0] for r in con.execute("SELECT DISTINCT gate FROM src ORDER BY gate").fetchall()]
    channels, gate_channel = [], {}
    for gate in gates:
        group, i, name = _sensor(gate, open_stops)
        n = GROUPS[group][0]
        cid = channel_id(k, n * 100 + i)
        gate_channel[gate] = cid
        channels.append((cid, object_id(k, n), gate, name, group))
    con.execute(
        "CREATE OR REPLACE TABLE p_objects "
        "(id INTEGER, parent_id INTEGER, level SMALLINT, kind VARCHAR, name VARCHAR, meta VARCHAR)"
    )
    con.executemany("INSERT INTO p_objects VALUES (?, ?, ?, ?, ?, ?)", objects)
    con.execute(
        "CREATE OR REPLACE TABLE p_channels "
        "(id INTEGER, object_id INTEGER, tag VARCHAR, name VARCHAR, tag_group VARCHAR)"
    )
    con.executemany("INSERT INTO p_channels VALUES (?, ?, ?, ?, ?)", channels)
    _write(con, "SELECT * FROM p_objects ORDER BY id", out / "objects.parquet")
    _write(
        con,
        f"""SELECT id, object_id, '{SYSTEM_TYPE}' AS system_type, '{SENSOR_TYPE}' AS sensor_type, tag, name,
                   NULL::INTEGER AS picket_number, NULL::DOUBLE AS picket_offset, tag_group,
                   'significant' AS stream, NULL::DOUBLE AS deadband, NULL::VARCHAR AS unit
            FROM p_channels ORDER BY id""",
        out / "channels.parquet",
    )

    # Журнал: значение — билет машины
    con.execute(
        """CREATE OR REPLACE TABLE ev AS
           SELECT row_number() OVER (ORDER BY s.ts, s.car, s.gate)::BIGINT AS event_id, c.id AS channel_id,
                  s.ts, false AS is_alarm, s.car AS value, s.class, s.gate, c.object_id
           FROM src s JOIN p_channels c ON c.tag = s.gate"""
    )
    _write(con, "SELECT event_id, channel_id, ts, is_alarm, value FROM ev ORDER BY event_id", out / "events.parquet")

    # Пропуска и допуски
    con.execute("CREATE OR REPLACE TABLE p_class (class VARCHAR, class_name VARCHAR)")
    con.executemany("INSERT INTO p_class VALUES (?, ?)", list(CLASS_NAMES.items()))
    unknown_classes = con.execute("SELECT DISTINCT class FROM src ANTI JOIN p_class USING (class)").fetchall()
    if unknown_classes:
        raise SystemExit(f"{CSV_NAME}: неизвестные классы машин {unknown_classes}")
    _write(
        con,
        """SELECT car AS credential_id, 'vehicle_ticket' AS kind,
                  json_object('class', class, 'class_name', class_name, 'first_seen', min(ts)::VARCHAR,
                              'last_seen', max(ts)::VARCHAR)::VARCHAR AS meta
           FROM src JOIN p_class USING (class) GROUP BY car, class, class_name ORDER BY car""",
        out / "credentials.parquet",
    )
    restricted_objects = [object_id(k, n) for n, _, restricted in GROUPS.values() if restricted]
    _write(
        con,
        f"""SELECT DISTINCT s.car AS credential_id, o.object_id::INTEGER AS object_id,
                   NULL::TIMESTAMP AS valid_from, NULL::TIMESTAMP AS valid_to
            FROM src s, (SELECT unnest({restricted_objects}) AS object_id) o
            WHERE s.class = '{RANGER_CLASS}' ORDER BY 1, 2""",
        out / "permits.parquet",
    )

    labels = _labels(con, k, restricted_objects, out)
    return {
        "source_rows": rows,
        "sensors": len(gates),
        "ranger_stops_non_ranger_share": {g: round(share, 4) for g, share in stops},
        "open_ranger_stops": sorted(open_stops),
        "credentials": con.execute("SELECT count(DISTINCT car) FROM src").fetchone()[0],
        "credentials_by_class": dict(
            con.execute("SELECT class, count(DISTINCT car) FROM src GROUP BY 1 ORDER BY 1").fetchall()
        ),
        "labels": labels,
    }


def _labels(con: duckdb.DuckDBPyConnection, k: int, restricted_objects: list[int], out: Path) -> dict:
    preserve = object_id(k, PRESERVE_N)
    # Нарушение: проезд не-рейнджера через объект с допуском; в обход ворот — если у билета нет записи на воротах
    con.execute(
        f"""CREATE OR REPLACE TABLE p_labels AS
            WITH v AS (
                SELECT e.*, bool_or(e.gate LIKE 'gate%') OVER (PARTITION BY e.value) AS passed_gate
                FROM ev e
                WHERE e.class <> '{RANGER_CLASS}' AND e.object_id IN ({",".join(map(str, restricted_objects))})
            )
            SELECT object_id::INTEGER AS object_id, channel_id, ts AS start_ts, ts + INTERVAL 1 SECOND AS end_ts,
                   'violation' AS label,
                   json_object('subtype', CASE WHEN passed_gate THEN 'closed_gate_pass' ELSE 'gate_bypass' END,
                               'event_id', event_id, 'credential_id', value, 'class', class,
                               'source', 'правило набора: закрытые ворота — только машины 2P')::VARCHAR AS meta
            FROM v"""
    )
    dates = ", ".join(f"DATE '{d}'" for d in SOLUTION_OVERNIGHT_DATES)
    speeding = ", ".join(f"'{c}'" for c in SOLUTION_SPEEDING)
    con.execute(
        f"""INSERT INTO p_labels
            WITH trips AS (
                SELECT value AS car, any_value(class) AS class, min(ts) AS first_ts, max(ts) AS last_ts,
                       count(*) FILTER (WHERE gate LIKE 'camping%') AS camping
                FROM ev GROUP BY value
            )
            SELECT {preserve}, NULL, first_ts, last_ts + INTERVAL 1 SECOND, 'suspicious',
                   json_object('subtype', 'overnight_truck', 'credential_id', car, 'class', class,
                               'source', 'эталон набора: грузовик ночует вне кемпингов')::VARCHAR
            FROM trips
            WHERE class = '4' AND CAST(first_ts AS DATE) IN ({dates})
              AND CAST(last_ts AS DATE) > CAST(first_ts AS DATE) AND camping = 0
            UNION ALL
            SELECT {preserve}, NULL, first_ts, last_ts + INTERVAL 1 SECOND, 'suspicious',
                   json_object('subtype', 'speeding', 'credential_id', car, 'class', class,
                               'source', 'эталон набора: превышение скорости')::VARCHAR
            FROM trips WHERE car IN ({speeding})"""
    )
    _write(con, "SELECT * FROM p_labels ORDER BY start_ts, channel_id", out / "labels.parquet")
    by = con.execute(
        "SELECT label, json_extract_string(meta, '$.subtype') AS subtype, count(*), "
        "count(DISTINCT json_extract_string(meta, '$.credential_id')) "
        "FROM p_labels GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    return {f"{label}/{subtype}": {"labels": n, "credentials": cars} for label, subtype, n, cars in by}
