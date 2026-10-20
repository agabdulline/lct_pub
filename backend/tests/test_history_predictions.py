"""Решения моделей на истории (`app.history_predictions`): строки `run_id = NULL` из файла ML-кода, ссылка связки на
базовой тревоге, повторная запись заменяет, метрики базы считают версии и связку."""

import datetime as dt
from zoneinfo import ZoneInfo

import duckdb
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.metrics import model_metrics
from app.history_predictions import write
from app.loader import pg_conninfo

MSK = ZoneInfo("Europe/Moscow")
T1 = dt.datetime(2026, 3, 2, 10, 0, tzinfo=MSK)
T2 = dt.datetime(2026, 6, 3, 11, 0, tzinfo=MSK)  # неделя после дня без журнала: v3 — coverage_gap, решает v0


def _parquet(path, rows: list[tuple]) -> None:
    """Файл в раскладке ml.history.smoke: на тревогу строки baseline-v0 и v3, у решающей — decides."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE f (model VARCHAR, model_version VARCHAR, channel_id INTEGER, alarm_ts TIMESTAMPTZ, "
        "decision_time TIMESTAMPTZ, horizon_start TIMESTAMPTZ, horizon_end TIMESTAMPTZ, status VARCHAR, "
        "skip_reason VARCHAR, score DOUBLE, threshold DOUBLE, alert BOOLEAN, outcome VARCHAR, outcome_at TIMESTAMPTZ, "
        "outcome_source VARCHAR, decides BOOLEAN)"
    )
    for ts, version, status, reason, score, threshold, outcome, decides in rows:
        t = ts + dt.timedelta(seconds=1)
        alert = None if score is None else score >= threshold
        con.execute(
            "INSERT INTO f VALUES ('smoke_alarm', ?, 1001, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'history', ?)",
            [version, ts, t, t, t + dt.timedelta(hours=24), status, reason, score, threshold, alert, outcome,
             t + dt.timedelta(hours=2) if outcome == "fault" else t + dt.timedelta(hours=24), decides],
        )  # fmt: skip
    con.execute(f"COPY f TO '{path}' (FORMAT parquet)")


@pytest.mark.anyio
async def test_history_rows_chain_link_and_metrics(session: AsyncSession, seeded, tmp_path):
    for ts in (T1, T2):
        await session.execute(
            text(
                "INSERT INTO alarms (run_id, channel_id, object_id, ts, verdict) "
                "VALUES (NULL, 1001, 100, :ts, 'no_score')"
            ),
            {"ts": ts},
        )
    await session.commit()
    path = tmp_path / "smoke_alarm.parquet"
    _parquet(
        path,
        [
            (T1, "baseline-v0", "scored", None, 0.40, 0.431, "fault", False),
            (T1, "v3", "scored", None, 0.60, 0.489, "fault", True),  # v3 оценила — решает она: верная тревога
            (T2, "baseline-v0", "scored", None, 0.20, 0.431, "no_fault", True),  # v3 пропустила — решает v0
            (T2, "v3", "skipped", "coverage_gap", None, 0.489, "no_fault", False),
        ],
    )
    conninfo = pg_conninfo(seeded.render_as_string(hide_password=False))
    first = write(conninfo, [path])
    assert first["inserted"] == 4 and first["decided_alarms"] == 2
    again = write(conninfo, [path])  # повтор заменяет, а не дублирует
    assert again["replaced"] == {"smoke_alarm": 4} and again["inserted"] == 4

    engine = create_engine(seeded)
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT a.ts, p.model_version FROM alarms a JOIN predictions p ON p.id = a.prediction_id "
                "WHERE a.run_id IS NULL ORDER BY a.ts"
            )
        ).all()
        assert [(ts, v) for ts, v in rows] == [(T1, "v3"), (T2, "baseline-v0")]
        assert c.scalar(text("SELECT count(*) FROM predictions WHERE run_id IS NULL AND alarm_id IS NULL")) == 0
    engine.dispose()

    now = dt.datetime(2026, 7, 1, tzinfo=MSK)
    chain = (await model_metrics(session, None, "smoke_alarm", now, model_version="v3>baseline-v0"))["all"]
    v0 = (await model_metrics(session, None, "smoke_alarm", now, model_version="baseline-v0"))["all"]
    v3 = (await model_metrics(session, None, "smoke_alarm", now, model_version="v3"))["all"]
    assert (chain["decisions"], chain["tp"], chain["fp"], chain["fn"]) == (2, 1, 0, 0)
    assert (v0["decisions"], v0["tp"], v0["fn"]) == (2, 0, 1)
    assert (v3["decisions"], v3["tp"]) == (1, 1)
