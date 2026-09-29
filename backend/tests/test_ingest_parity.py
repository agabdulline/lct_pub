"""Поток и загрузчик дают одну и ту же рабочую базу: те же журналы, что в test_loader, прогоняются через
`ingest_events` пакетами по 3 события и сравниваются с результатом `load-db`."""

import csv
import datetime as dt
import io

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.archive import build_archive
from app.domain.ingest import RawEvent, ingest_events
from app.loader import Loader, pg_conninfo
from tests.test_loader import CHANNELS, JOURNAL_2025, JOURNAL_2026, OBJECTS

TABLES = (
    "work_orders, alarms, predictions, alarm_episodes, fault_episodes, channel_state, channel_daily, "
    "channel_hourly, events_raw, events, model_versions, channels, objects, runs"
)
DATA_TABLES = "alarms, alarm_episodes, fault_episodes, channel_state, channel_daily, channel_hourly, events_raw, events"
SNAPSHOT = {
    "events": "SELECT channel_id, event_id, ts, is_alarm, value_raw, value_num FROM events ORDER BY 1, 3, 2",
    "channel_daily": "SELECT channel_id, date, n_events, n_alarm, n_fault, n_num, min, max, round(mean::numeric, 9) "
    "FROM channel_daily ORDER BY 1, 2",
    "fault_episodes": "SELECT channel_id, start_ts, end_ts, state FROM fault_episodes ORDER BY 1, 2",
    "channel_state": "SELECT channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at, fault_since "
    "FROM channel_state ORDER BY 1",
    "alarms": "SELECT channel_id, ts FROM alarms ORDER BY 1, 2",
    "alarm_episodes": "SELECT object_id, start_ts, end_ts, n_channels, n_alarms, kind, has_lever, channel_ids "
    "FROM alarm_episodes ORDER BY 1, 2",
}


def journal_events(*journals: str) -> list[RawEvent]:
    out = []
    for journal in journals:
        for r in csv.DictReader(io.StringIO(journal)):
            ts = dt.datetime.fromisoformat(f"{r['дата']}T{r['время']}")  # без пояса — Москва
            out.append(
                RawEvent(
                    int(r["ид_канала_данных"]), ts, r["значение_датчика"], r["тревожное"] == "t", int(r["ид_события"])
                )
            )
    return sorted(out, key=lambda e: (e.ts, e.channel_id))


@pytest.mark.anyio
async def test_stream_equals_loader(db_url, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "справочник_объектов_диспетчер.csv").write_text(OBJECTS, encoding="utf-8")
    (raw / "справочник_каналов_датчиков.csv").write_text(CHANNELS, encoding="utf-8")
    (raw / "ext-journal-2025.csv").write_text(JOURNAL_2025, encoding="utf-8")
    (raw / "ext-journal-2026.csv").write_text(JOURNAL_2026, encoding="utf-8")
    build_archive(raw, tmp_path / "archive", memory_limit="1GB", threads=2)

    url = db_url.render_as_string(hide_password=False)
    with psycopg.connect(pg_conninfo(url), autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        Loader(
            tmp_path / "archive", url, tmp_path / "work", workers=1, memory_limit="1GB", threads=2, compress_before=None
        ).run()
        loaded = {name: conn.execute(sql).fetchall() for name, sql in SNAPSHOT.items()}
        conn.execute(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE")

    engine = create_async_engine(db_url, poolclass=NullPool)
    async with async_sessionmaker(engine)() as session:
        events = journal_events(JOURNAL_2025, JOURNAL_2026)
        for start in range(0, len(events), 3):
            await ingest_events(None, events[start : start + 3], session=session)
        await session.commit()
        streamed = {name: [tuple(r) for r in await session.execute(text(sql))] for name, sql in SNAPSHOT.items()}
    await engine.dispose()

    for name in SNAPSHOT:
        assert streamed[name] == loaded[name], name
