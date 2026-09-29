"""Скоринг тревог (docs/architecture/backend.md §4): `score_alarms(run_id, object_id)` ставит приём событий с
дебаунсом 5 с по объекту (`app/domain/scoring_queue.py`)."""

import logging

from app.domain.external import close_external_outcomes, external_models, score_events
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.notify import publish
from app.domain.runs import get_run, run_now
from app.domain.scoring_queue import acquire_lock, release_lock
from app.domain.triage import score_primary, score_shadows
from app.domain.work_orders import apply_prediction_rules
from app.tasks.aio import run_with_session
from app.tasks.celery_app import celery_app

log = logging.getLogger("tasks.scoring")


@celery_app.task(name="scoring.score_alarms")
def score_alarms(run_id: int | None, object_id: int) -> dict:
    async def work(session):
        if not await acquire_lock(run_id, object_id):
            return {"run_id": run_id, "object_id": object_id, "skipped": "locked"}
        try:
            # вердикт и заявки основной версии — сразу, теневые версии (15.2) — следующим шагом: вердикт их не ждёт.
            # Упал теневой шаг — вердикт уже записан, недостающие прогнозы досчитает runs.tick (sweep_pending)
            result = await score_primary(session, run_id, object_id)
            orders = await apply_prediction_rules(session, run_id, result.prediction_ids)
            await session.commit()
            await publish(result.notifications + orders)
            shadow = await score_shadows(session, run_id, object_id)
            await session.commit()
            await publish(shadow.notifications)
        finally:
            await release_lock(run_id, object_id)
        return {
            "run_id": run_id,
            "object_id": object_id,
            "alarms": result.alarms,
            "scored": result.scored,
            "skipped": result.skipped,
            "model_version": result.model_version,
            "shadow": shadow.shadow,
        }

    summary = run_with_session(work)
    log.info("скоринг: %s", summary)
    return summary


@celery_app.task(name="scoring.score_external_events")
def score_external_events(run_id: int, dataset: str) -> dict | None:
    """Событийные модели внешнего набора (`trigger = on_event`) по новым событиям прогона — ставит приём потока."""

    async def work(session):
        run = await get_run(session, run_id)
        if run is None:
            return None
        ml = MlClient()
        try:
            models = [m for m in await external_models(dataset, ml) if m.get("trigger") == "on_event"]
        except MlUnavailable as e:
            return {"run_id": run_id, "dataset": dataset, "error": e.reason}
        if not models:
            return None
        summary, notifications = {}, []
        for model in models:
            summary[model["name"]], created = await score_events(session, run, dataset, model, ml)
            notifications += created
        summary["outcomes"], closed = await close_external_outcomes(session, run.id, dataset, models, run_now(run))
        await session.commit()
        await publish(notifications + closed)
        return {"run_id": run_id, "dataset": dataset, **summary}

    summary = run_with_session(work)
    if summary:
        log.info("скоринг событий набора: %s", summary)
    return summary
