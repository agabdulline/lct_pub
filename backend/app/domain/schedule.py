"""Работа по часам прогона (D10): в режиме потока «сейчас» — симуляционное время, и дневной прогноз «в 00:10»
наступает, когда до него доходит эмулятор, а не реальные часы. `beat` раз в 15 с запускает `tick`: он смотрит
на активный прогон, закрывает исходы и запускает ежедневный прогноз, если наступил новый день. Прогоны, закрытые
за последний час, дозакрывают исходы по своим часам и по журналу после конца (`close_recent_runs`)."""

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HACKATHON, Run
from app.domain.access_runs import enqueue_sync as enqueue_access_sync
from app.domain.daily import RUN_AT, already_done, daily_forecast
from app.domain.external import close_external_outcomes, external_models, external_tick, score_model
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.notify import Notification
from app.domain.outcomes import close_outcomes, close_outcomes_history
from app.domain.runs import get_run, recently_closed_runs, run_now, run_source

_MSK = dt.timezone(dt.timedelta(hours=3))


def due_daily_date(run: Run) -> dt.date | None:
    """Дата ежедневного прогноза по часам прогона. Дата старта — сразу, даже если прогон начался посреди дня:
    признаки берут дневные итоги строго до этой даты, а они целиком из базовой истории. Следующие дни — когда
    наступило 00:10: итоги предыдущего дня прогона уже досчитаны."""
    now = (run.sim_now or run.sim_start).astimezone(_MSK)
    if now.date() == run.sim_start.astimezone(_MSK).date():
        return now.date()
    return now.date() if now >= dt.datetime.combine(now.date(), RUN_AT, _MSK) else None


async def tick(session: AsyncSession, run: Run, ml: MlClient | None = None) -> tuple[dict, list[Notification]]:
    now = run.sim_now or run.sim_start
    summary: dict = {"run_id": run.id, "now": now.isoformat()}
    source = await run_source(session, run)
    if source != HACKATHON:
        # Прогон внешнего набора: модели набора и исходы по его разметке; ежедневный прогноз и исходы по
        # неисправностям хакатона здесь не работают (external-data.md §4.3)
        summary["external"], notifications = await external_tick(session, run, source, ml)
        return summary, notifications
    notifications: list[Notification] = []
    date = due_daily_date(run)
    if date is not None and not await already_done(session, run.id, date):
        summary["daily"], notifications = await daily_forecast(session, run.id, date, ml)
    summary["outcomes"], closed = await close_outcomes(session, run.id, now)
    notifications += closed
    # вскрытие под охраной (access.md §4): пересчёт ставит приём событий, такт — страховкой
    await enqueue_access_sync(run.id)
    return summary, notifications


async def close_recent_runs(session: AsyncSession) -> tuple[dict[int, dict], list[Notification]]:
    """Исходы прогонов, закрытых за последний час: по их часам на момент завершения, остальное — по журналу
    (`close_outcomes_history`). Неисправность между последним тактом активного прогона и его концом иначе осталась
    бы без исхода, а горизонт короткого прогона по его часам не истекает. Такт повторяет досчёт час — так закрываются
    и решения, оценённые уже после конца прогона. Повтор ничего не меняет — закрываются только прогнозы без исхода."""
    counts: dict[int, dict] = {}
    notifications: list[Notification] = []
    for run in await recently_closed_runs(session):
        source = await run_source(session, run)
        if source != HACKATHON:
            closed, updates = await _close_external_history(session, run, source)
            if any(closed.values()):
                counts[run.id] = closed
            notifications += updates
            continue
        closed, updates = await close_outcomes_history(session, run)
        if any(closed.values()):
            counts[run.id] = closed
        notifications += updates
        # все события закрытого прогона приняты: волна на конце видна, открытые инциденты — `run_end`
        await enqueue_access_sync(run.id, final=True)
    return counts, notifications


async def _close_external_history(session: AsyncSession, run: Run, dataset: str) -> tuple[dict, list[Notification]]:
    """Прогон внешнего набора после его конца: решения моделей от последнего такта до конца прогона (хвост, который
    такт активного прогона не застал), исходы по часам прогона, затем по разметке до конца журнала набора
    (`sim_end` прогона-основы) — горизонт короткого прогона по его часам не истекает."""
    ml = MlClient()
    try:
        models = await external_models(dataset, ml)
    except MlUnavailable:
        return {}, []
    notifications: list[Notification] = []
    for model in models:
        _, created = await score_model(session, run, dataset, model, ml)
        notifications += created
    live, closed = await close_external_outcomes(session, run.id, dataset, models, run_now(run))
    notifications += closed
    base = await get_run(session, run.base_run_id) if run.base_run_id else None
    end = base.sim_end if base and base.sim_end else run_now(run)
    history, updates = await close_external_outcomes(session, run.id, dataset, models, end, source="history")
    counts = {f"live_{k}": v for k, v in live.items()} | {f"history_{k}": v for k, v in history.items()}
    return counts, notifications + updates
