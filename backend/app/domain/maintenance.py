"""Обслуживание слоёв: архив событий прогона и окно сырого потока (docs/architecture/data.md §2)."""

import logging
import time
from pathlib import Path

import duckdb
import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.values import sql_value_state

log = logging.getLogger("maintenance")

RAW_WINDOW_DAYS = 90

# Полный поток прогона: сырые каналы (у хакатона газ и движение) — из сырого слоя, остальное значимо целиком
_RUN_EVENTS = """
COPY (
    SELECT e.event_id, e.channel_id, e.ts, e.is_alarm, e.value_raw, e.value_num, c.sensor_type
    FROM events_raw e JOIN channels c ON c.id = e.channel_id WHERE e.run_id = %(run)s
    UNION ALL
    SELECT e.event_id, e.channel_id, e.ts, e.is_alarm, e.value_raw, e.value_num, c.sensor_type
    FROM events e JOIN channels c ON c.id = e.channel_id
    WHERE e.run_id = %(run)s AND c.stream = 'significant'
) TO STDOUT (FORMAT csv)
"""


def archive_run(conninfo: str, archive_dir: Path, run_id: int) -> dict:
    """Сырые события прогона — в `archive/runs/run=<id>/events.parquet`, колонки как у базы. Повторный вызов
    перезаписывает файл."""
    started = time.monotonic()
    target = archive_dir / "runs" / f"run={run_id}"
    target.mkdir(parents=True, exist_ok=True)
    csv_path = target / "events.csv.tmp"
    with psycopg.connect(conninfo) as pg, pg.cursor() as cur, open(csv_path, "wb") as f:
        with cur.copy(_RUN_EVENTS, {"run": run_id}) as copy:
            for block in copy:
                f.write(block)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    n = con.execute(
        f"""
        COPY (
            SELECT event_id, channel_id, ts, is_alarm, value_raw, value_num,
                {sql_value_state("value_raw", "sensor_type")} AS state, true AS in_catalog
            FROM read_csv('{csv_path}', header = false, columns = {{
                'event_id': 'BIGINT', 'channel_id': 'INTEGER', 'ts': 'TIMESTAMPTZ', 'is_alarm': 'BOOLEAN',
                'value_raw': 'VARCHAR', 'value_num': 'DOUBLE', 'sensor_type': 'VARCHAR'}})
            ORDER BY channel_id, ts, event_id
        ) TO '{target / "events.parquet"}' (FORMAT parquet, COMPRESSION zstd)
        """
    ).fetchone()[0]
    csv_path.unlink()
    report = {
        "run_id": run_id,
        "events": n,
        "path": str(target / "events.parquet"),
        "seconds": round(time.monotonic() - started, 1),
    }
    log.info("архив прогона: %s", report)
    return report


async def trim_raw_window(session: AsyncSession, days: int = RAW_WINDOW_DAYS) -> dict[str, int]:
    """Окно `events_raw` и `channel_hourly` базы — 90 дней от последнего события данных, а не от текущей даты.
    Строки прогонов не трогаются: их сырой поток нужен представлению прогона и уходит в архив при завершении."""
    anchor = await session.scalar(text("SELECT max(ts) FROM channel_state WHERE run_id IS NULL"))
    if anchor is None:
        return {"events_raw": 0, "channel_hourly": 0}
    params = {"cutoff": anchor, "days": days}
    raw = await session.execute(
        text("DELETE FROM events_raw WHERE run_id IS NULL AND ts < :cutoff - make_interval(days => :days)"), params
    )
    hourly = await session.execute(
        text("DELETE FROM channel_hourly WHERE run_id IS NULL AND hour < :cutoff - make_interval(days => :days)"),
        params,
    )
    return {"events_raw": raw.rowcount, "channel_hourly": hourly.rowcount}
