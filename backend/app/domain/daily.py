"""Ежедневный прогноз (docs/architecture/backend.md §2.2): в 00:10 по часам прогона — скоринг на дату D,
окно цели `[D + 24 ч, D + 30 сут)`. Пока ML-сервис отвечает `503 daily_model_missing`, прогнозы пишутся
пропущенными с причиной от сервиса — в журнале видно, почему оценки нет.

Модели по типам (15.4): канал решает модель своего типа по таблице маршрутизации ML-сервиса — версия прогноза
(`model_version`) берётся из строки ответа (`results[].model`: baseline-v0, ups_ensemble, manual_alarm), порог — тоже.
"""

import datetime as dt
import logging

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import bindparam

from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.notify import Notification
from app.domain.work_orders import apply_prediction_rules

log = logging.getLogger("daily")

MODEL = "daily"
RUN_AT = dt.time(0, 10)  # по часам прогона: дневные итоги D−1 уже досчитаны
TARGET_START = dt.timedelta(hours=24)
TARGET_END = dt.timedelta(days=30)
_MSK = dt.timezone(dt.timedelta(hours=3))

_INSERT = text(
    f"""
    INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, horizon_end,
        status, skip_reason, score, threshold, alert, seen_channel, object_history, factors)
    SELECT :run, '{MODEL}', coalesce(r.model, :version), r.channel_id, :decision, coalesce(r.target_start, :h_start),
        coalesce(r.target_end, :h_end), r.status, r.skip_reason, r.score, r.threshold, r.alert, r.seen_channel,
        r.object_history, r.factors
    FROM jsonb_to_recordset(:rows) AS r(channel_id int, status text, skip_reason text, score float8,
        threshold float8, alert bool, seen_channel bool, object_history bool, factors jsonb, target_start timestamptz,
        target_end timestamptz, model text)
    ON CONFLICT (run_id, model, model_version, channel_id, decision_time) DO NOTHING
    RETURNING id, alert
    """
).bindparams(bindparam("rows", type_=JSONB))


def decision_time(date: dt.date) -> dt.datetime:
    return dt.datetime.combine(date, dt.time(), _MSK)


async def already_done(session: AsyncSession, run_id: int | None, date: dt.date) -> bool:
    return bool(
        await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM predictions WHERE run_id IS NOT DISTINCT FROM :run "
                f"AND model = '{MODEL}' AND decision_time = :t)"
            ),
            {"run": run_id, "t": decision_time(date)},
        )
    )


async def daily_forecast(
    session: AsyncSession, run_id: int | None, date: dt.date, ml: MlClient | None = None
) -> tuple[dict, list[Notification]]:
    """Прогноз на дату по всем каналам с состоянием в прогоне. Повторный вызов на ту же дату ничего не пишет."""
    ml = ml or MlClient()
    t = decision_time(date)
    try:
        response = await ml.score_daily(run_id, date)
        version = response.get("model_version") or response.get("model") or "unknown"
        rows = [
            {
                k: r.get(k)
                for k in (
                    "channel_id",
                    "status",
                    "skip_reason",
                    "score",
                    "threshold",
                    "alert",
                    "seen_channel",
                    "object_history",
                    "factors",
                    "target_start",
                    "target_end",
                    "model",
                )
            }
            for r in response["results"]
        ]
    except MlUnavailable as e:
        log.info("ежедневный прогноз %s: %s — пишем пропуски", date, e.reason)
        version = "unavailable"
        channels = await session.scalars(
            text("SELECT channel_id FROM run_channel_state(:run) ORDER BY channel_id"), {"run": run_id}
        )
        rows = [{"channel_id": c, "status": "skipped", "skip_reason": e.reason} for c in channels]
    inserted = (
        await session.execute(
            _INSERT,
            {
                "run": run_id,
                "version": version,
                "decision": t,
                "h_start": t + TARGET_START,
                "h_end": t + TARGET_END,
                "rows": rows,
            },
        )
    ).all()
    summary = {
        "run_id": run_id,
        "date": date.isoformat(),
        "decision_time": t.isoformat(),
        "predictions": len(inserted),
        "alerts": sum(1 for _, alert in inserted if alert),
    }
    notifications = [Notification(run_id, "daily_forecast.completed", summary)] if inserted else []
    notifications += await apply_prediction_rules(session, run_id, [pid for pid, _ in inserted])
    return summary, notifications
