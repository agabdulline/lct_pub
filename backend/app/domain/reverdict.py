"""Пересчёт прошлых вердиктов прогона по основной версии событийной модели (этап 15.2, backend.md §2.1).

Смена основной версии (`PATCH /runs/{id}`) действует на тревоги, оценённые после неё. «Применить к прошлым» —
вердикты тревог, решённых другой версией, пересчитываются по решению основной: тем же правилом, что при скоринге
(`triage.verdict`, наследование решения за сутки, плановая проверка важнее модели). Тревоги без решения основной
версии (она была недоступна) не меняются. Заявки не отзываются — по ним уже могли начать работу; по тревогам
основной версии без открытой заявки она создаётся, как при скоринге. Всё записывается в журнал прогона
(`run_audit`). У связки (`v3>baseline-v0`) — решение первой её версии, у которой оно есть (`triage.decide`).
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RunAudit
from app.domain.feed import alarms_by_ids
from app.domain.model_versions import chain_of
from app.domain.notify import Notification
from app.domain.triage import decide
from app.domain.work_orders import apply_prediction_rules

MAX_AUDIT_CHANGES = 500  # тревог с изменением в записи журнала; остальные — числом
MAX_EVENTS = 500  # `alarm.scored` за пересчёт; остальное интерфейс перечитает


async def audit(session: AsyncSession, run_id: int, action: str, details: dict) -> None:
    sim_ts = await session.scalar(text("SELECT coalesce(sim_now, sim_start) FROM runs WHERE id = :r"), {"r": run_id})
    session.add(RunAudit(run_id=run_id, sim_ts=sim_ts, action=action, details=details))
    await session.flush()


async def reverdict(session: AsyncSession, run_id: int) -> tuple[dict, list[Notification]]:
    """Вердикты всех тревог прогона — по решению его основной версии. Коммит — на вызывающем."""
    version = await session.scalar(text("SELECT primary_model FROM runs WHERE id = :r"), {"r": run_id})
    if version is None:
        return {"version": None, "alarms": 0, "changed": 0}, []
    alarms = await session.scalars(text("SELECT id FROM alarms WHERE run_id = :r ORDER BY ts, id"), {"r": run_id})
    ids = list(alarms.all())
    decisions = await decide(session, run_id, ids, chain_of(version))
    changes, changed_ids = decisions.changes, [c["alarm_id"] for c in decisions.changes]
    orders = await apply_prediction_rules(session, run_id, decisions.decided)
    summary = {
        "version": version,
        "alarms": len(ids),
        "changed": len(changes),
        "verdicts": dict(decisions.transitions),
        "no_decision": decisions.no_decision,
        "orders_created": len(orders),
    }
    await audit(session, run_id, "reverdict", summary | {"changes": changes[:MAX_AUDIT_CHANGES]})
    notifications = list(orders)
    if changed_ids:
        now = await session.scalar(text("SELECT coalesce(sim_now, sim_start) FROM runs WHERE id = :r"), {"r": run_id})
        for alarm in await alarms_by_ids(session, run_id, changed_ids[:MAX_EVENTS], now):
            notifications.append(Notification(run_id, "alarm.scored", alarm))
    return summary, notifications
