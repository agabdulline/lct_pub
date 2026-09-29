"""Azure Predictive Maintenance — износ агрегатов (docs/architecture/external-data.md §3.2, §4.1).

Синтетический набор Microsoft: 100 агрегатов 4 моделей, телеметрия по часам за 2015 год, журнал ошибок, записи ТО
(замена компонента, с июня 2014 года), отказы компонентов, возраст агрегата. Справочник: корень набора → модель →
агрегат (уровень 3); на агрегате канал цели «Агрегат» (D32), 4 показания, «Код ошибки», «Журнал ТО» и «Отказы и
ремонты». Возраст и модель — в `objects.meta`.

Журнал — то, что система получала бы потоком: показания, ошибки (их сообщает устройство — `is_alarm = true`),
записи ТО и записи об отказах — история ремонтов, вход по ТЗ. Разметка — отказы компонентов на агрегат.
В исходнике 743 из 761 отказа совпадают по времени и компоненту с записью ТО — это замена после отказа; признаки
видят её только в окнах `[T−w, T)`, то есть после отказа.
"""

import json
from pathlib import Path

import duckdb

from app.external.registry import Dataset, channel_id, object_id

FILES = {
    "machines": "PdM_machines.csv",
    "telemetry": "PdM_telemetry.csv",
    "errors": "PdM_errors.csv",
    "maint": "PdM_maint.csv",
    "failures": "PdM_failures.csv",
}
SYSTEM_TYPE = "Телеметрия оборудования (внешн.)"
TARGET_TYPE = "Агрегат"
# Номер канала на агрегате, исходная колонка или файл, тип датчика, подпись, единицы (в описании набора их нет;
# у напряжения — по имени колонки volt)
TELEMETRY = [
    (1, "volt", "Напряжение", "Напряжение", "В"),
    (2, "rotate", "Скорость вращения", "Скорость вращения", None),
    (3, "pressure", "Давление", "Давление", None),
    (4, "vibration", "Вибрация", "Вибрация", None),
]
LOGS = [
    (5, "errors", "Код ошибки", "Коды ошибок"),
    (6, "maint", "Журнал ТО", "Журнал ТО"),
    (7, "failures", "Отказы и ремонты", "Отказы и ремонты"),
]
ROOT_N, MODEL_N0, MACHINE_N0 = 1, 10, 100
FAILURE_WINDOW_H = 1  # отказ в исходнике — момент; метка — [момент, момент + 1 ч), шаг телеметрии


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _write(con: duckdb.DuckDBPyConnection, sql: str, path: Path) -> None:
    con.execute(f"COPY ({sql}) TO {_q(path)} (FORMAT parquet, COMPRESSION zstd)")


def build(con: duckdb.DuckDBPyConnection, dataset: Dataset, inputs: dict[str, Path], out: Path) -> dict:
    k = dataset.k
    for table, name in FILES.items():
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_csv({_q(inputs[name])}, all_varchar = true)"
        )
    duplicates = con.execute("SELECT count(*) - count(DISTINCT (datetime, machineID)) FROM telemetry").fetchone()[0]
    if duplicates:
        raise SystemExit(f"{FILES['telemetry']}: {duplicates} повторов (агрегат, час)")

    machines = con.execute(
        "SELECT CAST(machineID AS INTEGER), model, CAST(age AS INTEGER) FROM machines ORDER BY 1"
    ).fetchall()
    models = sorted({m for _, m, _ in machines})
    root = object_id(k, ROOT_N)
    objects = [
        (root, None, 1, "external_site", "Парк агрегатов · Azure PdM (синтетика)", json.dumps({"dataset": dataset.key}))
    ]
    model_obj = {}
    for model in models:
        n = MODEL_N0 + int(model.removeprefix("model"))
        model_obj[model] = object_id(k, n)
        objects.append((model_obj[model], root, 2, "machine_model", f"Модель {model.removeprefix('model')}", None))
    channels = []
    for mid, model, age in machines:
        node = object_id(k, MACHINE_N0 + mid)
        meta = json.dumps({"machine_id": mid, "model": model, "age_years": age})
        objects.append((node, model_obj[model], 3, "machine", f"Агрегат {mid:03d}", meta))
        tag_group = f"machine{mid:03d}"
        channels.append(
            (channel_id(k, mid * 100), node, TARGET_TYPE, "machine", f"Агрегат {mid:03d} · оценка", tag_group, None)
        )
        for m, column, sensor_type, label, unit in TELEMETRY:
            channels.append(
                (
                    channel_id(k, mid * 100 + m),
                    node,
                    sensor_type,
                    column,
                    f"{label} · агрегат {mid:03d}",
                    tag_group,
                    unit,
                )
            )
        for m, table, sensor_type, label in LOGS:
            channels.append(
                (
                    channel_id(k, mid * 100 + m),
                    node,
                    sensor_type,
                    table,
                    f"{label} · агрегат {mid:03d}",
                    tag_group,
                    None,
                )
            )

    con.execute(
        "CREATE OR REPLACE TABLE p_objects "
        "(id INTEGER, parent_id INTEGER, level SMALLINT, kind VARCHAR, name VARCHAR, meta VARCHAR)"
    )
    con.executemany("INSERT INTO p_objects VALUES (?, ?, ?, ?, ?, ?)", objects)
    con.execute(
        "CREATE OR REPLACE TABLE p_channels "
        "(id INTEGER, object_id INTEGER, sensor_type VARCHAR, tag VARCHAR, name VARCHAR, tag_group VARCHAR, "
        "unit VARCHAR)"
    )
    con.executemany("INSERT INTO p_channels VALUES (?, ?, ?, ?, ?, ?, ?)", channels)
    _write(con, "SELECT * FROM p_objects ORDER BY id", out / "objects.parquet")
    _write(
        con,
        f"""SELECT id, object_id, '{SYSTEM_TYPE}' AS system_type, sensor_type, tag, name,
                   NULL::INTEGER AS picket_number, NULL::DOUBLE AS picket_offset, tag_group,
                   'significant' AS stream, NULL::DOUBLE AS deadband, unit
            FROM p_channels ORDER BY id""",
        out / "channels.parquet",
    )

    base = channel_id(k, 0)
    columns = ", ".join(c for _, c, *_ in TELEMETRY)
    con.execute("CREATE OR REPLACE TABLE p_measure (measure VARCHAR, m INTEGER)")
    con.executemany("INSERT INTO p_measure VALUES (?, ?)", [(c, m) for m, c, *_ in TELEMETRY])
    logs = {table: m for m, table, *_ in LOGS}
    con.execute(
        f"""CREATE OR REPLACE TABLE ev AS
            WITH t AS (
                UNPIVOT (SELECT CAST(datetime AS TIMESTAMP) AS ts, CAST(machineID AS INTEGER) AS mid, {columns}
                         FROM telemetry)
                ON {columns} INTO NAME measure VALUE value
            ), all_events AS (
                SELECT t.ts, t.mid, p.m, t.value, false AS is_alarm FROM t JOIN p_measure p USING (measure)
                UNION ALL
                SELECT CAST(datetime AS TIMESTAMP), CAST(machineID AS INTEGER), {logs["errors"]}, errorID, true
                FROM errors
                UNION ALL
                SELECT CAST(datetime AS TIMESTAMP), CAST(machineID AS INTEGER), {logs["maint"]}, comp, false FROM maint
                UNION ALL
                SELECT CAST(datetime AS TIMESTAMP), CAST(machineID AS INTEGER), {logs["failures"]}, failure, false
                FROM failures
            )
            SELECT row_number() OVER (ORDER BY ts, mid, m, value)::BIGINT AS event_id,
                   ({base} + mid * 100 + m)::INTEGER AS channel_id, ts, is_alarm, value
            FROM all_events WHERE value IS NOT NULL AND value <> ''"""
    )
    _write(con, "SELECT * FROM ev ORDER BY event_id", out / "events.parquet")

    _write(
        con,
        f"""SELECT ({object_id(k, MACHINE_N0)} + CAST(machineID AS INTEGER))::INTEGER AS object_id,
                   NULL::INTEGER AS channel_id, CAST(datetime AS TIMESTAMP) AS start_ts,
                   CAST(datetime AS TIMESTAMP) + INTERVAL {FAILURE_WINDOW_H} HOUR AS end_ts,
                   'failure' AS label,
                   json_object('subtype', failure, 'machine_id', CAST(machineID AS INTEGER))::VARCHAR AS meta
            FROM failures ORDER BY start_ts, object_id, failure""",
        out / "labels.parquet",
    )
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "machines": len(machines),
        "machines_by_model": dict(con.execute("SELECT model, count(*) FROM machines GROUP BY 1 ORDER BY 1").fetchall()),
        "telemetry_rows": one("SELECT count(*) FROM telemetry"),
        "errors": one("SELECT count(*) FROM errors"),
        "maintenance": one("SELECT count(*) FROM maint"),
        "failures": one("SELECT count(*) FROM failures"),
        "failures_by_component": dict(
            con.execute("SELECT failure, count(*) FROM failures GROUP BY 1 ORDER BY 1").fetchall()
        ),
        "failures_with_maintenance_record": one(
            "SELECT count(*) FROM failures f JOIN maint m ON m.datetime = f.datetime AND m.machineID = f.machineID "
            "AND m.comp = f.failure"
        ),
        "units_note": "единиц в описании набора нет; у напряжения — вольты по имени колонки volt",
    }
