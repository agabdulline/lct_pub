"""Вскрытие под охраной (docs/architecture/access.md §4): пересчёт правила по прогону — `access.sync(run_id, final)`.

Ставят приём событий (с дебаунсом 2 с, когда в пачке двери, люки или охрана либо у прогона идут кандидаты и
инциденты) и такт `runs.tick` — страховкой по идущему прогону и окончательно по закрытым. Один пересчёт прогона за раз —
блокировка Redis до коммита: пересчёты из приёма и такта не вставляют один инцидент дважды.
"""

import logging

from app.core.redis import try_lock, unlock
from app.domain.access_runs import LOCK_SECONDS, TASK, clear_pending, enqueue_sync, lock_key, sync_run
from app.domain.notify import publish
from app.domain.runs import get_run
from app.tasks.aio import run_with_session
from app.tasks.celery_app import celery_app

log = logging.getLogger("tasks.access")


@celery_app.task(name=TASK)
def sync(run_id: int, final: bool = False) -> dict | None:
    async def work(session):
        # pending снимаем до чтения данных: события во время пересчёта поставят ровно одну следующую задачу
        await clear_pending(run_id, final)
        if not await try_lock(lock_key(run_id), LOCK_SECONDS):
            # идёт другой пересчёт: этот может не увидеть свежий водяной знак — повторить, но через pending-ключ
            await enqueue_sync(run_id, final)
            return {"run_id": run_id, "skipped": "locked"}
        try:
            run = await get_run(session, run_id)
            if run is None:
                return None
            summary, notifications = await sync_run(session, run, final=final)
            await session.commit()
        finally:
            await unlock(lock_key(run_id))
        await publish(notifications)
        return {"run_id": run_id, "final": final, **summary}

    summary = run_with_session(work)
    if summary and (summary.get("created") or summary.get("updated")):
        log.info("вскрытие под охраной: %s", summary)
    return summary
