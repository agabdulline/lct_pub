"""Задачи прогонов (docs/architecture/backend.md §4): такт по часам прогона, архив завершённого прогона,
окно сырого потока."""

import logging

from app.core.config import settings
from app.core.redis import try_lock, unlock
from app.domain.maintenance import archive_run as archive_run_events
from app.domain.maintenance import trim_raw_window as trim
from app.domain.notify import publish
from app.domain.runs import active_run
from app.domain.schedule import close_recent_runs
from app.domain.schedule import tick as run_tick
from app.domain.scoring_queue import enqueue_alarm_scoring, sweep_pending
from app.loader import pg_conninfo
from app.tasks.aio import run_with_session
from app.tasks.celery_app import celery_app

log = logging.getLogger("tasks.runs")

# Один такт за раз: такт после старта прогона и такт `beat` не считают один ежедневный прогноз дважды
TICK_LOCK = "runs:tick:lock"
TICK_LOCK_SECONDS = 300


@celery_app.task(name="runs.tick")
def tick() -> dict | None:
    """Активный прогон: ежедневный прогноз на дату старта и в 00:10 следующих дней по его часам, закрытие исходов
    прогнозов. Прогоны, закрытые за последний час, дозакрывают исходы по часам на момент завершения. Плюс досчёт:
    тревоги без решения модели в активном и недавно закрытых прогонах снова ставятся в скоринг. Ставится `beat`
    раз в 15 с и сразу после создания прогона."""

    async def work(session):
        if not await try_lock(TICK_LOCK, TICK_LOCK_SECONDS):
            return None  # идёт другой такт; следующий `beat` — через 15 с
        try:
            summary: dict = {}
            for run_id, objects in (await sweep_pending(session)).items():
                if queued := await enqueue_alarm_scoring(run_id, objects):
                    summary.setdefault("swept", {})[run_id] = queued
            closed, notifications = await close_recent_runs(session)
            if closed:
                summary["closed_runs"] = closed
            if (run := await active_run(session)) is not None:
                active, updates = await run_tick(session, run)
                summary.update(active)
                notifications += updates
            await session.commit()
        finally:
            await unlock(TICK_LOCK)
        await publish(notifications)
        return summary or None

    summary = run_with_session(work)
    if summary and (
        summary.get("daily")
        or summary.get("swept")
        or summary.get("closed_runs")
        or any(summary.get("outcomes", {}).values())
    ):
        log.info("такт: %s", summary)
    return summary


@celery_app.task(name="runs.archive_run")
def archive_run(run_id: int) -> dict:
    return archive_run_events(pg_conninfo(settings.database_url), settings.archive_dir, run_id)


@celery_app.task(name="runs.trim_raw_window")
def trim_raw_window() -> dict:
    async def work(session):
        deleted = await trim(session)
        await session.commit()
        return deleted

    deleted = run_with_session(work)
    log.info("окно сырого потока: удалено %s", deleted)
    return deleted
