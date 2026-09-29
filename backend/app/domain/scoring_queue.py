"""Постановка скоринга тревог с дебаунсом по объекту (docs/architecture/backend.md §2.1).

При плановой проверке за секунды приходят десятки тревог объекта — их скорим одним запросом: первая тревога
ставит задачу `score_alarms` с задержкой 5 с, следующие в это окно ничего не ставят. Задача берёт все тревоги
объекта со статусом `pending`.
"""

import logging

from app.core.config import settings
from app.core.redis import async_client, try_lock, unlock

log = logging.getLogger("scoring_queue")

DEBOUNCE_SECONDS = 5
TASK = "scoring.score_alarms"


def _key(run_id: int | None, object_id: int) -> str:
    return f"score:debounce:{run_id if run_id is not None else 'base'}:{object_id}"


async def enqueue_alarm_scoring(run_id: int | None, object_ids: set[int]) -> int:
    """Поставить скоринг по объектам. Возвращает число поставленных задач; без Redis — 0."""
    if not object_ids or not settings.enqueue_tasks:
        return 0
    from app.tasks.celery_app import celery_app

    queued = 0
    try:
        redis = async_client()
        for object_id in sorted(object_ids):
            if await redis.set(_key(run_id, object_id), "1", nx=True, ex=DEBOUNCE_SECONDS):
                celery_app.send_task(TASK, args=[run_id, object_id], countdown=DEBOUNCE_SECONDS)
                queued += 1
        await redis.aclose()
    except Exception as e:  # noqa: BLE001 — приём не должен падать из-за очереди
        log.warning("скоринг не поставлен для %s: %s", sorted(object_ids), e)
    return queued


EXTERNAL_DEBOUNCE_SECONDS = 2
EXTERNAL_TASK = "scoring.score_external_events"


async def enqueue_external_scoring(run_id: int, datasets: set[str]) -> int:
    """Событийные модели внешних наборов (этап 14): задача на (прогон, набор) с дебаунсом — события за окно
    скорятся одной пачкой. Какие модели событийные, решает задача; потерянную задачу подхватит такт."""
    if not datasets or not settings.enqueue_tasks:
        return 0
    from app.tasks.celery_app import celery_app

    queued = 0
    try:
        redis = async_client()
        for dataset in sorted(datasets):
            key = f"score:external:debounce:{run_id}:{dataset}"
            if await redis.set(key, "1", nx=True, ex=EXTERNAL_DEBOUNCE_SECONDS):
                celery_app.send_task(EXTERNAL_TASK, args=[run_id, dataset], countdown=EXTERNAL_DEBOUNCE_SECONDS)
                queued += 1
        await redis.aclose()
    except Exception as e:  # noqa: BLE001 — приём не должен падать из-за очереди
        log.warning("скоринг событий набора не поставлен (%s, %s): %s", run_id, sorted(datasets), e)
    return queued


LOCK_SECONDS = 120


def _lock_key(run_id: int | None, object_id: int) -> str:
    return f"score:lock:{run_id if run_id is not None else 'base'}:{object_id}"


async def acquire_lock(run_id: int | None, object_id: int) -> bool:
    """Один скоринг объекта за раз: повтор задачи (досчёт после перезапуска, повторная постановка) не оценивает
    те же тревоги параллельно."""
    return await try_lock(_lock_key(run_id, object_id), LOCK_SECONDS)


async def release_lock(run_id: int | None, object_id: int) -> None:
    await unlock(_lock_key(run_id, object_id))


# Тревога недооценена, если нет прогноза хотя бы одной версии прогона (основной или теневой, 15.2); у прогона без
# выбора версий — если нет ни одного прогноза
_PENDING = """
SELECT DISTINCT a.run_id, a.object_id FROM alarms a JOIN runs r ON r.id = a.run_id
WHERE a.run_id = ANY(:runs) AND EXISTS (
    SELECT 1 FROM unnest(CASE WHEN r.primary_model IS NULL THEN ARRAY[NULL::text]
                              ELSE string_to_array(r.primary_model, '>') || r.shadow_models END) AS v(version)
    WHERE NOT EXISTS (
        SELECT 1 FROM predictions p WHERE p.alarm_id = a.id AND (v.version IS NULL OR p.model_version = v.version)
    )
)
"""

# Досчитываются активные прогоны и закрытые за последний час: задача, поставленная прямо перед остановкой,
# могла потеряться при пересоздании контейнеров или не успеть до конца дебаунса
_RUNS_TO_SWEEP = """
SELECT id FROM runs WHERE status IN ('running', 'paused') OR finished_at > now() - interval '1 hour'
"""


async def sweep_pending(session) -> dict[int, set[int]]:
    """Тревоги без решения модели — объекты по прогонам; их скоринг ставится заново."""
    from sqlalchemy import text

    runs = [r[0] for r in await session.execute(text(_RUNS_TO_SWEEP))]
    if not runs:
        return {}
    found: dict[int, set[int]] = {}
    for run_id, object_id in await session.execute(text(_PENDING), {"runs": runs}):
        found.setdefault(run_id, set()).add(object_id)
    return found
