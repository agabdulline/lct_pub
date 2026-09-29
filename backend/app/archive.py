"""Архив Parquet — весь журнал без потерь (D23, D26; docs/architecture/data.md §2).

Сборка из `data/raw` по правилам `docs/data-format.md` §8: строки-заголовки внутри файла пропускаются, время —
Europe/Moscow, дедупликация по `(канал, ид_события, время, значение)`, каналы вне справочника остаются с пометкой.
Раскладка: `events/year=YYYY/month=MM/part-0.parquet` (zstd, сортировка канал + время), `catalog/*.parquet`,
отчёт — `report.json`. Год собирается во временную папку и подменяется целиком — повторный запуск безопасен.

Колонки событий: event_id, channel_id, ts (timestamptz), is_alarm, value_raw (как в источнике), value_num,
state (нормализованное состояние), in_catalog.
"""

import json
import logging
import re
import shutil
import time
from pathlib import Path

import duckdb

from app.domain.catalog import read_channels, read_objects
from app.domain.values import sql_value_num, sql_value_state

log = logging.getLogger("archive")

JOURNAL = re.compile(r"ext-journal-(\d{4})\.csv$")
EVENTS_DIR = "events"
CATALOG_DIR = "catalog"
REPORT_FILE = "report.json"
HEADER_EVENT_ID = "ид_события"


def events_glob(archive_dir: Path) -> str:
    """Шаблон файлов событий базовой истории для read_parquet(…, hive_partitioning=true)."""
    return str(archive_dir / EVENTS_DIR / "year=*" / "month=*" / "*.parquet")


def connect(memory_limit: str = "8GB", threads: int | None = None, temp_dir: Path | None = None):
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"SET memory_limit = '{memory_limit}'")
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if temp_dir:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{temp_dir}'")
    return con


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def journal_files(raw_dir: Path, years: list[int] | None = None) -> dict[int, Path]:
    files = {}
    for p in sorted(raw_dir.iterdir()):
        m = JOURNAL.match(p.name)
        if m and (years is None or int(m.group(1)) in years):
            files[int(m.group(1))] = p
    return files


def write_catalog(con, raw_dir: Path, out_dir: Path) -> dict:
    """Справочники с производными полями — в `catalog/`; таблица `channels` остаётся в соединении для join."""
    objects = read_objects(raw_dir)
    channels = read_channels(raw_dir)
    con.execute(
        "CREATE OR REPLACE TABLE objects (id INTEGER, parent_id INTEGER, level SMALLINT, kind VARCHAR, name VARCHAR)"
    )
    con.executemany(
        "INSERT INTO objects VALUES (?, ?, ?, ?, ?)", [(o.id, o.parent_id, o.level, o.kind, o.name) for o in objects]
    )
    con.execute(
        "CREATE OR REPLACE TABLE channels (id INTEGER, object_id INTEGER, system_type VARCHAR, sensor_type VARCHAR, "
        "tag VARCHAR, name VARCHAR, picket_number INTEGER, picket_offset DOUBLE, tag_group VARCHAR)"
    )
    con.executemany(
        "INSERT INTO channels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                c.id,
                c.object_id,
                c.system_type,
                c.sensor_type,
                c.tag,
                c.name,
                c.picket_number,
                c.picket_offset,
                c.tag_group,
            )
            for c in channels
        ],
    )
    target = out_dir / CATALOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    for table in ("objects", "channels"):
        con.execute(f"COPY {table} TO {_q(target / f'{table}.parquet')} (FORMAT parquet, COMPRESSION zstd)")
    return {"objects": len(objects), "channels": len(channels)}


def load_catalog(con, archive_dir: Path) -> None:
    """Справочники из уже собранного архива — в таблицы соединения."""
    for table in ("objects", "channels"):
        path = archive_dir / CATALOG_DIR / f"{table}.parquet"
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet({_q(path)})")


def build_year(con, path: Path, year: int, out_dir: Path) -> dict:
    """Один журнал → `events/year=YYYY/`. Возвращает счётчики для отчёта."""
    started = time.monotonic()
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE parsed AS
        SELECT
            event_id = '{HEADER_EVENT_ID}' AS is_header,
            TRY_CAST(event_id AS BIGINT) AS event_id,
            TRY_CAST(channel_id AS INTEGER) AS channel_id,
            timezone('Europe/Moscow', TRY_CAST(d || ' ' || t AS TIMESTAMP)) AS ts,
            CASE WHEN alarm IN ('t', 'true') THEN true WHEN alarm IN ('f', 'false') THEN false END AS is_alarm,
            value_raw
        FROM read_csv({_q(path)}, header = true, all_varchar = true, delim = ',', quote = '"',
            columns = {{'event_id': 'VARCHAR', 'channel_id': 'VARCHAR', 'd': 'VARCHAR', 't': 'VARCHAR',
                        'alarm': 'VARCHAR', 'value_raw': 'VARCHAR'}})
        """
    )
    valid = (
        "NOT is_header AND event_id IS NOT NULL AND channel_id IS NOT NULL AND ts IS NOT NULL "
        "AND is_alarm IS NOT NULL AND value_raw IS NOT NULL"
    )
    n_rows, n_header, n_invalid = con.execute(
        f"SELECT count(*), count(*) FILTER (is_header), count(*) FILTER (NOT is_header AND NOT ({valid})) FROM parsed"
    ).fetchone()

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE year_events AS
        SELECT g.event_id, g.channel_id, g.ts, g.is_alarm, g.value_raw,
            {sql_value_num("g.value_raw", "c.sensor_type")} AS value_num,
            {sql_value_state("g.value_raw", "c.sensor_type")} AS state,
            c.id IS NOT NULL AS in_catalog,
            g.n_copies, g.alarm_conflict,
            CAST(month(timezone('Europe/Moscow', g.ts)) AS INTEGER) AS month,
            CAST(year(timezone('Europe/Moscow', g.ts)) AS INTEGER) AS local_year
        FROM (
            SELECT channel_id, event_id, ts, value_raw, bool_or(is_alarm) AS is_alarm, count(*) AS n_copies,
                bool_or(is_alarm) <> bool_and(is_alarm) AS alarm_conflict
            FROM parsed WHERE {valid}
            GROUP BY channel_id, event_id, ts, value_raw
        ) g
        LEFT JOIN channels c ON c.id = g.channel_id
        """
    )
    con.execute("DROP TABLE parsed")

    stats = con.execute(
        """
        SELECT count(*), sum(n_copies - 1), count(*) FILTER (alarm_conflict),
            count(*) FILTER (NOT in_catalog), count(DISTINCT channel_id) FILTER (NOT in_catalog),
            count(DISTINCT channel_id), count(*) FILTER (is_alarm), count(*) FILTER (local_year <> ?),
            strftime(timezone('Europe/Moscow', min(ts)), '%Y-%m-%d %H:%M:%S'),
            strftime(timezone('Europe/Moscow', max(ts)), '%Y-%m-%d %H:%M:%S')
        FROM year_events
        """,
        [year],
    ).fetchone()
    months = dict(con.execute("SELECT month, count(*) FROM year_events GROUP BY 1 ORDER BY 1").fetchall())

    events_dir = out_dir / EVENTS_DIR
    staging = events_dir / f".staging-year={year}"
    shutil.rmtree(staging, ignore_errors=True)
    for month in months:
        target = staging / f"month={month:02d}"
        target.mkdir(parents=True)
        con.execute(
            f"""
            COPY (
                SELECT event_id, channel_id, ts, is_alarm, value_raw, value_num, state, in_catalog
                FROM year_events WHERE month = {month} ORDER BY channel_id, ts, event_id
            ) TO {_q(target / "part-0.parquet")} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 1000000)
            """
        )
    con.execute("DROP TABLE year_events")
    final = events_dir / f"year={year}"
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)

    return {
        "source": path.name,
        "source_bytes": path.stat().st_size,
        "rows_read": n_rows,
        "header_rows": n_header,
        "invalid_rows": n_invalid,
        "events": stats[0],
        "duplicates_dropped": int(stats[1] or 0),
        "duplicate_alarm_conflicts": stats[2],
        "out_of_catalog_events": stats[3],
        "out_of_catalog_channels": stats[4],
        "channels": stats[5],
        "alarm_events": stats[6],
        "events_outside_year": stats[7],
        "first_ts": stats[8],
        "last_ts": stats[9],
        "events_by_month": {f"{m:02d}": n for m, n in months.items()},
        "parquet_bytes": sum(p.stat().st_size for p in final.rglob("*.parquet")),
        "seconds": round(time.monotonic() - started, 1),
    }


def build_archive(
    raw_dir: Path,
    out_dir: Path,
    years: list[int] | None = None,
    memory_limit: str = "8GB",
    threads: int | None = None,
    temp_dir: Path | None = None,
) -> dict:
    """Собрать архив (все годы или выбранные) и обновить отчёт `report.json`."""
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    files = journal_files(raw_dir, years)
    if not files:
        raise FileNotFoundError(f"нет журналов ext-journal-YYYY.csv в {raw_dir}")

    report_path = out_dir / REPORT_FILE
    report = json.loads(report_path.read_text()) if report_path.exists() else {"years": {}}
    con = connect(memory_limit, threads, temp_dir)
    report["catalog"] = write_catalog(con, raw_dir, out_dir)
    for year, path in files.items():
        log.info("год %s: %s", year, path.name)
        report["years"][str(year)] = build_year(con, path, year, out_dir)
        log.info("год %s готов: %s", year, report["years"][str(year)])
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    per_year = report["years"].values()
    report["total"] = {
        key: sum(y[key] for y in per_year)
        for key in (
            "rows_read",
            "events",
            "duplicates_dropped",
            "out_of_catalog_events",
            "parquet_bytes",
            "source_bytes",
        )
    }
    report["last_build_seconds"] = round(time.monotonic() - started, 1)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report
