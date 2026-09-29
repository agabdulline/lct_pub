"""Правила заявок без ML — `solitary_repeat`, `fault_persisting` (docs/architecture/backend.md §4, §5).

`beat` ставит задачу каждые 30 с: правило смотрит на системные часы прогона (`runs.sim_now`), а при ускорении
×60 час симуляционного времени проходит за минуту. Повторный запуск безопасен — одна открытая заявка на канал
и правило, закрытая не создаётся заново на тот же случай. Правила по прогнозам вызывают скоринг и ежедневный
прогноз.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import runs
from app.domain.notify import publish
from app.domain.work_orders import apply_periodic_rules
from app.tasks.aio import run_with_session
from app.tasks.celery_app import celery_app

log = logging.getLogger("rules")


async def apply(session: AsyncSession, run_id: int | None = None) -> int:
    """Правила без ML для прогона (по умолчанию — активного) на его симуляционное время; коммит и публикация."""
    run = await (runs.get_run(session, run_id) if run_id is not None else runs.active_run(session))
    if run is None:
        return 0
    now = run.sim_now or run.sim_start
    notifications = await apply_periodic_rules(session, run.id, now)
    await session.commit()
    await publish(notifications)
    if notifications:
        log.info("прогон %s: %d заявок по правилам без ML на %s", run.id, len(notifications), now.isoformat())
    return len(notifications)


@celery_app.task(name="rules.apply_rules")
def apply_rules(run_id: int | None = None) -> int:
    return run_with_session(lambda session: apply(session, run_id))
