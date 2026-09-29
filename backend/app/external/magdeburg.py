"""Magdeburg, Industrial Hall — пожар и помехи в цехе (docs/architecture/external-data.md §3.2, §4.1).

Исходник — одна таблица: строка = узел × момент, 13 показаний, метки опыта. Справочник: корень набора → цех →
9 узлов (уровень 3), на узле 13 каналов показаний и служебный канал цели «Узел» (D32). Журнал — каждое показание
отдельным событием с меткой строки, время — московское без пояса (в данных +02:00). Разметка — интервалы опытов
на цех: `fire` или `nuisance` по флагу набора `fire`, вид — `scenario_label`.

Особенности исходника, которые правит подготовка:
- опыт из одной строки вплотную к опыту того же вида (13-й перед 14-м) склеивается с ним;
- в одном номере опыта бывает два вида подряд (21: садовый мусор, затем этанол) — две метки;
- строки с флагом аномалии без номера опыта — метка `unknown`: это не фон, оценка модели их исключает.
"""

import json
from collections import Counter
from pathlib import Path

import duckdb

from app.external.registry import Dataset, channel_id, object_id

SOURCE_FILE = "industrial_hall.csv"
STEP_S = 10  # шаг записи узла, с
SYSTEM_TYPE = "Пожарный мониторинг (внешн.)"
TARGET_TYPE = "Узел"

# Исходная колонка → номер канала на узле, тип датчика, подпись, единицы. Единиц в описании набора нет:
# у CO₂, температуры и влажности они однозначны по диапазону значений, у остальных — не указываем.
MEASURES = [
    ("CO2_Room", 1, "Датчик CO₂", "CO₂", "ppm"),
    ("CO_Room", 2, "Датчик CO", "CO", None),
    ("H2_Room", 3, "Датчик H₂", "H₂", None),
    ("VOC_Room_RAW", 4, "Датчик ЛОС", "ЛОС, сырой сигнал", None),
    ("PM05_Room", 5, "Счётчик частиц", "Частицы PM0.5", None),
    ("PM10_Room", 6, "Счётчик частиц", "Частицы PM1", None),
    ("PM25_Room", 7, "Счётчик частиц", "Частицы PM2.5", None),
    ("PM40_Room", 8, "Счётчик частиц", "Частицы PM4", None),
    ("PM100_Room", 9, "Счётчик частиц", "Частицы PM10", None),
    ("PM_Total_Room", 10, "Счётчик частиц", "Частицы, всего", None),
    ("PM_Room_Typical_Size", 11, "Счётчик частиц", "Типичный размер частиц", None),
    ("Temperature_Room", 12, "Термометр", "Температура", "°C"),
    ("Humidity_Room", 13, "Гигрометр", "Влажность", "%"),
]

UNITS_NOTE = "единиц в описании набора нет; у CO₂ (ppm), температуры (°C) и влажности (%) — по диапазону значений"

ROOT_N, HALL_N, NODE_N0 = 1, 2, 100
MERGE_MAX_ROWS = 10  # опыт меньше стольких строк — кандидат на склейку с соседним
MERGE_GAP_S = 60  # … если до опыта того же вида не больше минуты
UNKNOWN_GAP_S = 60  # строки аномалии без номера опыта с разрывом до минуты — один интервал


def node_number(sensor_id: str) -> int:
    return int(sensor_id.removeprefix("sensornode"))


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _write(con: duckdb.DuckDBPyConnection, sql: str, path: Path) -> None:
    con.execute(f"COPY ({sql}) TO {_q(path)} (FORMAT parquet, COMPRESSION zstd)")


def _catalog(con: duckdb.DuckDBPyConnection, k: int, nodes: list[str], out: Path) -> None:
    root, hall = object_id(k, ROOT_N), object_id(k, HALL_N)
    objects = [
        (root, None, 1, "external_site", "Magdeburg · цех (внешние данные)", json.dumps({"dataset": "magdeburg"})),
        (hall, root, 2, "hall", "Промышленный цех 10×22×8 м", None),
    ]
    channels = []
    for sid in nodes:
        num = node_number(sid)
        node = object_id(k, NODE_N0 + num)
        objects.append((node, hall, 3, "sensor_node", f"Узел {num:02d}", json.dumps({"sensor_id": sid})))
        channels.append((channel_id(k, num * 100), node, TARGET_TYPE, "node", f"Узел {num:02d} · оценка", sid, None))
        for column, m, sensor_type, label, unit in MEASURES:
            channels.append(
                (channel_id(k, num * 100 + m), node, sensor_type, column, f"{label} · узел {num:02d}", sid, unit)
            )
    con.execute(
        "CREATE OR REPLACE TABLE p_objects (id INTEGER, parent_id INTEGER, level SMALLINT, kind VARCHAR, "
        "name VARCHAR, meta VARCHAR)"
    )
    con.executemany("INSERT INTO p_objects VALUES (?, ?, ?, ?, ?, ?)", objects)
    con.execute(
        "CREATE OR REPLACE TABLE p_channels_raw (id INTEGER, object_id INTEGER, sensor_type VARCHAR, tag VARCHAR, "
        "name VARCHAR, tag_group VARCHAR, unit VARCHAR)"
    )
    con.executemany("INSERT INTO p_channels_raw VALUES (?, ?, ?, ?, ?, ?, ?)", channels)
    _write(con, "SELECT * FROM p_objects ORDER BY id", out / "objects.parquet")
    _write(
        con,
        f"""SELECT id, object_id, '{SYSTEM_TYPE}' AS system_type, sensor_type, tag, name,
                   NULL::INTEGER AS picket_number, NULL::DOUBLE AS picket_offset, tag_group,
                   'significant' AS stream, NULL::DOUBLE AS deadband, unit
            FROM p_channels_raw ORDER BY id""",
        out / "channels.parquet",
    )


def _events(con: duckdb.DuckDBPyConnection, k: int, out: Path) -> None:
    columns = ", ".join(c for c, *_ in MEASURES)
    con.execute("CREATE OR REPLACE TABLE p_measure (measure VARCHAR, m INTEGER)")
    con.executemany("INSERT INTO p_measure VALUES (?, ?)", [(c, m) for c, m, *_ in MEASURES])
    base = channel_id(k, 0)
    _write(
        con,
        f"""WITH u AS (
                UNPIVOT (SELECT ts, node, {columns} FROM src) ON {columns} INTO NAME measure VALUE value
            )
            SELECT row_number() OVER (ORDER BY u.ts, u.node, p.m)::BIGINT AS event_id,
                   ({base} + u.node * 100 + p.m)::INTEGER AS channel_id,
                   u.ts, false AS is_alarm, u.value
            FROM u JOIN p_measure p USING (measure)
            WHERE u.value IS NOT NULL AND u.value <> ''
            ORDER BY event_id""",
        out / "events.parquet",
    )


def _experiments(con: duckdb.DuckDBPyConnection) -> tuple[list[dict], list[dict]]:
    """Опыты по (номер, вид) с фазами; мелкие склеиваются с соседом того же вида."""
    rows = con.execute(
        """SELECT CAST(CAST(anomaly_scenario AS DOUBLE) AS INTEGER) AS exp, scenario_label AS subtype,
                  min(ts), max(ts), count(*), max(fire), max(nuisance), min(fire),
                  list(DISTINCT experiment_number ORDER BY experiment_number)
           FROM src WHERE anomaly_scenario IS NOT NULL AND anomaly_scenario <> ''
           GROUP BY ALL ORDER BY 3"""
    ).fetchall()
    phases = {}
    for exp, subtype, phase, start, last in con.execute(
        """SELECT CAST(CAST(anomaly_scenario AS DOUBLE) AS INTEGER), scenario_label, progress_label, min(ts), max(ts)
           FROM src WHERE anomaly_scenario IS NOT NULL AND anomaly_scenario <> ''
             AND progress_label IS NOT NULL AND progress_label <> ''
           GROUP BY ALL ORDER BY 4"""
    ).fetchall():
        phases.setdefault((exp, subtype), []).append({"phase": phase, "from": str(start), "to": str(last)})

    items = []
    for exp, subtype, start, last, n, fire_max, nuis_max, fire_min, numbers in rows:
        if fire_max != fire_min:
            raise SystemExit(f"опыт {exp} ({subtype}): флаг fire меняется внутри опыта")
        items.append(
            {
                "experiments": [exp],
                "subtype": subtype,
                "start": start,
                "last": last,
                "rows": n,
                "fire": fire_max == "1.0",
                "nuisance_flag": nuis_max == "1.0",
                "experiment_number": [x for x in numbers if x],
                "phases": phases.get((exp, subtype), []),
            }
        )

    merged = []
    for it in items:
        if it["rows"] < MERGE_MAX_ROWS:
            near = [
                o
                for o in items
                if o is not it
                and o["subtype"] == it["subtype"]
                and o["rows"] >= MERGE_MAX_ROWS
                and min(abs((o["start"] - it["last"]).total_seconds()), abs((it["start"] - o["last"]).total_seconds()))
                <= MERGE_GAP_S
            ]
            if near:
                target = near[0]
                target["start"], target["last"] = min(target["start"], it["start"]), max(target["last"], it["last"])
                target["rows"] += it["rows"]
                target["phases"] = sorted(target["phases"] + it["phases"], key=lambda p: p["from"])
                target["merged"] = target.get("merged", []) + it["experiments"]
                merged.append(
                    {"experiment": it["experiments"][0], "into": target["experiments"][0], "rows": it["rows"]}
                )
                it["drop"] = True
    return [it for it in items if not it.get("drop")], merged


def _unknown(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Интервалы строк с флагом аномалии без номера опыта."""
    return con.execute(
        f"""WITH a AS (
                SELECT DISTINCT ts FROM src
                WHERE anomaly_label = '1.0' AND (anomaly_scenario IS NULL OR anomaly_scenario = '')
            ), g AS (
                SELECT ts, sum(CASE WHEN prev IS NULL OR epoch(ts) - epoch(prev) > {UNKNOWN_GAP_S} THEN 1 ELSE 0 END)
                       OVER (ORDER BY ts) AS grp
                FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev FROM a)
            )
            SELECT min(ts), max(ts), count(*) FROM g GROUP BY grp ORDER BY 1"""
    ).fetchall()


def _labels(con: duckdb.DuckDBPyConnection, k: int, out: Path) -> dict:
    items, merged = _experiments(con)
    unknown = _unknown(con)
    unknown_rows = con.execute(
        "SELECT count(*) FROM src WHERE anomaly_label = '1.0' AND (anomaly_scenario IS NULL OR anomaly_scenario = '')"
    ).fetchone()[0]
    hall = object_id(k, HALL_N)
    labels = []
    for it in items:
        meta = {
            "subtype": it["subtype"],
            "experiment": it["experiments"][0],
            "experiment_number": it["experiment_number"],
            "phases": it["phases"],
            "rows": it["rows"],
            "fire_flag": it["fire"],
            "nuisance_flag": it["nuisance_flag"],
        }
        if it.get("merged"):
            meta["merged_experiments"] = it["merged"]
        labels.append((hall, None, it["start"], it["last"], "fire" if it["fire"] else "nuisance", meta))
    for start, last, n in unknown:
        meta = {"note": "флаг аномалии без номера опыта и вида: не фон, из оценки исключается", "timestamps": n}
        labels.append((hall, None, start, last, "unknown", meta))
    con.execute(
        "CREATE OR REPLACE TABLE p_labels (object_id INTEGER, channel_id INTEGER, start_ts TIMESTAMP, "
        "last_ts TIMESTAMP, label VARCHAR, meta VARCHAR)"
    )
    con.executemany(
        "INSERT INTO p_labels VALUES (?, ?, ?, ?, ?, ?)",
        [(o, c, s, e, lab, json.dumps(m, ensure_ascii=False)) for o, c, s, e, lab, m in labels],
    )
    # Интервал — [первая запись, последняя запись + шаг): последняя строка опыта описывает следующие 10 с
    _write(
        con,
        f"""SELECT object_id, channel_id, start_ts, last_ts + INTERVAL {STEP_S} SECOND AS end_ts, label, meta
            FROM p_labels ORDER BY start_ts""",
        out / "labels.parquet",
    )
    return {
        "experiments": len(items),
        "by_subtype": dict(sorted(Counter(it["subtype"] for it in items).items())),
        "merged": merged,
        "unknown_intervals": len(unknown),
        "unknown_rows": unknown_rows,
    }


def build(con: duckdb.DuckDBPyConnection, dataset: Dataset, inputs: dict[str, Path], out: Path) -> dict:
    src = inputs[SOURCE_FILE]
    # Все поля строкой: значения идут в журнал как в источнике; Date с поясом → московское время без пояса
    con.execute(
        f"""CREATE OR REPLACE TABLE src AS
            SELECT CAST(CAST("Date" AS TIMESTAMPTZ) AS TIMESTAMP) AS ts,
                   Sensor_ID AS sensor_id,
                   CAST(regexp_extract(Sensor_ID, '(\\d+)$', 1) AS INTEGER) AS node,
                   * EXCLUDE ("Date", Sensor_ID)
            FROM read_csv({_q(src)}, all_varchar = true, header = true)"""
    )
    rows = con.execute("SELECT count(*) FROM src").fetchone()[0]
    nodes = [r[0] for r in con.execute("SELECT DISTINCT sensor_id FROM src ORDER BY 1").fetchall()]
    missing = [c for c, *_ in MEASURES if c not in {r[0] for r in con.execute("DESCRIBE src").fetchall()}]
    if missing:
        raise SystemExit(f"{SOURCE_FILE}: нет колонок {', '.join(missing)}")
    duplicates = con.execute(
        "SELECT count(*) FROM (SELECT ts, sensor_id FROM src GROUP BY ALL HAVING count(*) > 1)"
    ).fetchone()[0]
    if duplicates:
        raise SystemExit(f"{SOURCE_FILE}: {duplicates} повторов (узел, время)")

    _catalog(con, dataset.k, nodes, out)
    _events(con, dataset.k, out)
    labels = _labels(con, dataset.k, out)
    return {
        "source_rows": rows,
        "nodes": nodes,
        "measures_per_node": len(MEASURES),
        "step_s": STEP_S,
        "units_note": UNITS_NOTE,
        "labels": labels,
    }
