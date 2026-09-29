"""Прогоны: `GET /runs`, `GET /runs/{id}` (docs/api.md, «Прогон»). Создание — `POST /emulator/start`, удаление —
только командой на сервере (D22)."""

from fastapi import APIRouter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Session
from app.api.errors import ApiError
from app.db.models import HACKATHON, Run
from app.domain.metrics import live_by_run
from app.domain.ml_client import MlUnavailable
from app.domain.model_versions import alarm_versions, chain_of, shadows_of
from app.domain.notify import Notification, publish
from app.domain.reverdict import audit, reverdict
from app.domain.runs import FINAL_STATUSES, run_source
from app.schemas.runs import RunAuditList, RunList, RunOut, RunPatch, RunPatchOut

router = APIRouter(tags=["прогоны"])

# Счётчики и живые P/R всех прогонов — запросами с группировкой по run_id, а не по запросу на прогон:
# таблица прогонов в интерфейсе получает всё одним ответом. Число событий — из runs.events: подсчёт по таблицам
# событий распаковывал сжатые чанки и занимал секунды (миграция 0014)
_COUNTS = """
SELECT r.id AS run_id, coalesce(a.n, 0) AS alarms, coalesce(p.n, 0) AS predictions, coalesce(w.n, 0) AS work_orders
FROM unnest(CAST(:runs AS integer[])) AS r(id)
LEFT JOIN (SELECT run_id, count(*) AS n FROM alarms WHERE run_id = ANY(:runs) GROUP BY run_id) a ON a.run_id = r.id
LEFT JOIN (SELECT run_id, count(*) AS n FROM predictions WHERE run_id = ANY(:runs) GROUP BY run_id) p
    ON p.run_id = r.id
LEFT JOIN (SELECT run_id, count(*) AS n FROM work_orders WHERE run_id = ANY(:runs) GROUP BY run_id) w
    ON w.run_id = r.id
"""


async def runs_out(session: AsyncSession, runs: list[Run]) -> list[RunOut]:
    ids = [r.id for r in runs]
    if not ids:
        return []
    counts = {row.run_id: row for row in await session.execute(text(_COUNTS), {"runs": ids})}
    lives = await live_by_run(session, ids)
    sources = {run.id: await run_source(session, run) for run in runs}
    return [
        RunOut(
            id=run.id,
            scenario=run.scenario,
            scenario_title=(run.params or {}).get("title") or run.scenario,
            status=run.status,
            sim_start=run.sim_start,
            sim_end=run.sim_end,
            sim_now=run.sim_now,
            speed=run.speed,
            model_version=run.model_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            events=run.events,
            events_lost=run.events_lost,
            loss_windows=run.loss_windows or [],
            alarms=counts[run.id].alarms,
            predictions=counts[run.id].predictions,
            work_orders=counts[run.id].work_orders,
            live=lives[run.id] if counts[run.id].predictions else None,
            kind=run.kind,
            base_run_id=run.base_run_id,
            dataset=None if sources[run.id] == HACKATHON else sources[run.id],
            primary_model=run.primary_model,
            shadow_models=list(run.shadow_models or []),
        )
        for run in runs
    ]


@router.get("/runs", response_model=RunList)
async def list_runs(session: Session) -> RunList:
    """Прогоны, новые сверху: сценарий, статус, период, число событий, тревог, прогнозов, заявок, живые P/R."""
    runs = (await session.scalars(select(Run).order_by(Run.id.desc()))).all()
    return RunList(items=await runs_out(session, list(runs)))


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int, session: Session) -> RunOut:
    run = await session.get(Run, run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"прогон {run_id} не найден")
    return (await runs_out(session, [run]))[0]


@router.patch("/runs/{run_id}", response_model=RunPatchOut)
async def patch_run(run_id: int, body: RunPatch, session: Session) -> dict:
    """Сменить основную версию событийной модели прогона (15.2). Действует на тревоги, оценённые после смены;
    `apply_to_past` — пересчитать и вердикты прошлых тревог по новой основной (заявки не отзываются). Набор версий
    прогона сохраняется: прежняя основная становится теневой. Смена и пересчёт — в журнале `/runs/{id}/audit`."""
    run = await session.get(Run, run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"прогон {run_id} не найден")
    if run.status in FINAL_STATUSES:
        raise ApiError(409, "run_finished", f"прогон {run_id} завершён: основную модель менять не для чего")
    if run.kind != "scenario" or run.base_run_id is not None:
        raise ApiError(409, "no_alarm_model", f"прогон {run_id} — внешний набор: дымовых тревог хакатона в нём нет")
    try:
        ready, _ = await alarm_versions()
    except MlUnavailable as e:
        raise ApiError(503, "ml_unavailable", f"версию {body.primary_model} не проверить: {e.reason}") from e
    if body.primary_model not in ready:
        message = f"версии событийной модели {body.primary_model} нет среди готовых: {', '.join(ready) or 'нет'}"
        raise ApiError(422, "unknown_version", message)
    before = run.primary_model
    versions = [*chain_of(run.primary_model), *(run.shadow_models or [])]
    run.primary_model = body.primary_model
    run.shadow_models = shadows_of(body.primary_model, [v for v in versions if v in ready])
    change = {"from": before, "to": run.primary_model, "shadow_models": run.shadow_models}
    await audit(session, run.id, "primary_model", change | {"apply_to_past": body.apply_to_past})
    summary, notifications = None, []
    if body.apply_to_past:
        summary, notifications = await reverdict(session, run.id)
    await session.commit()
    await publish([Notification(run.id, "run.updated", {"id": run.id, "primary_model": run.primary_model})])
    await publish(notifications)
    out = (await runs_out(session, [run]))[0].model_dump()
    return out | {"reverdict": summary}


@router.get("/runs/{run_id}/audit", response_model=RunAuditList)
async def run_audit(run_id: int, session: Session) -> dict:
    """Журнал действий с прогоном, новые сверху: смены основной версии событийной модели и пересчёты вердиктов."""
    if await session.get(Run, run_id) is None:
        raise ApiError(404, "run_not_found", f"прогон {run_id} не найден")
    rows = await session.execute(
        text("SELECT id, ts, sim_ts, action, details FROM run_audit WHERE run_id = :r ORDER BY id DESC"),
        {"r": run_id},
    )
    return {"items": [dict(r._mapping) for r in rows]}
