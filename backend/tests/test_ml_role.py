"""Роль ml-service только на чтение (D2, миграция 0006): чтения ml-service проходят, запись — нет."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

# те же чтения, что делает ml-service/app/history.py: справочник и представление прогона
ML_READS = [
    "SELECT id AS channel_id, object_id, sensor_type, tag, picket_number FROM channels",
    "SELECT channel_id, coalesce(event_id, -1) AS event_id, ts, value_raw FROM run_events(NULL) LIMIT 1",
    "SELECT channel_id, date, n_events, n_alarm, n_fault, n_num, mean, max FROM run_channel_daily(NULL) LIMIT 1",
    "SELECT channel_id, start_ts, end_ts FROM run_fault_episodes(NULL) LIMIT 1",
]
WRITES = [
    "INSERT INTO runs (scenario, status, sim_start) VALUES ('x', 'running', now())",
    "UPDATE channels SET name = name",
    "DELETE FROM predictions",
    "CREATE TABLE ml_scratch (id int)",
]


@pytest.mark.anyio
async def test_ml_role_reads_but_cannot_write(session):
    # таблица следующей миграции: права по умолчанию дают роли чтение и её
    await session.execute(text("CREATE TABLE later_table (id int)"))
    await session.execute(text("SET LOCAL ROLE lct_ml"))
    for sql in [*ML_READS, "SELECT * FROM later_table"]:
        await session.execute(text(sql))
    for sql in WRITES:
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with session.begin_nested():
                await session.execute(text(sql))
