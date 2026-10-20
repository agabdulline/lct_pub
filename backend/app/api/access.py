"""Вскрытие под охраной в ленте (docs/api.md, «Вскрытие под охраной»; docs/architecture/access.md §5)."""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.api.deps import RunCtx, Session
from app.api.errors import ApiError
from app.api.params import csv_list, page_params
from app.domain.access import rules
from app.domain.access_feed import incident_openings, incidents_by_ids, incidents_where
from app.domain.notify import Notification, publish
from app.schemas.api import AccessIncident, AccessIncidentDetail, AccessIncidentPage, AccessIncidentPatch

router = APIRouter(tags=["доступ"])

# Представление прогона: инциденты прогона и базовые, закончившиеся до его старта. Кандидаты не видны, кроме
# оценённых моделью доступа «вероятно свой»: строка ленты без заявки, пока не сняли охрану или не решило правило
_VIEW = (
    "(i.status <> 'candidate' OR i.verdict = 'own') AND (i.run_id IS NOT DISTINCT FROM :run OR "
    "(CAST(:run AS integer) IS NOT NULL AND i.run_id IS NULL AND i.end_ts < run_sim_start(:run)))"
)
STATUSES = ("candidate", "open", "closed", "dismissed")


def _filters(
    run_id: int | None,
    now: dt.datetime,
    from_: dt.datetime | None,
    to: dt.datetime | None,
    object_id: int | None,
    priorities: list[str] | None,
) -> tuple[list[str], dict]:
    where = [_VIEW, "i.start_ts <= :now"]
    params: dict = {"run": run_id, "now": now}
    if from_ is not None:
        where.append("(i.end_ts IS NULL OR i.end_ts >= :from)")
        params["from"] = from_
    if to is not None:
        where.append("i.start_ts < :to")
        params["to"] = to
    if object_id is not None:
        where.append("(i.object_id = :object OR i.sub_object_id = :object)")
        params["object"] = object_id
    if priorities:
        where.append("i.priority = ANY(:priorities)")
        params["priorities"] = priorities
    return where, params


@router.get("/access-incidents", response_model=AccessIncidentPage)
async def access_incidents(
    ctx: RunCtx,
    session: Session,
    from_: Annotated[dt.datetime | None, Query(alias="from", description="инцидент пересекает [from, to)")] = None,
    to: dt.datetime | None = None,
    object_id: Annotated[int | None, Query(description="объект или подобъект")] = None,
    status: Annotated[
        str | None,
        Query(
            description="candidate («вероятно свой» по модели), open, closed, dismissed через запятую; "
            "по умолчанию candidate, open и closed"
        ),
    ] = None,
    priority: Annotated[str | None, Query(description="high, medium через запятую")] = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    """Инциденты «вскрытие под охраной», новые сверху по началу; `counts` — по тем же фильтрам без `status`."""
    page, size = page_params(page, size)
    scope, params = _filters(ctx.run_id, ctx.now, from_, to, object_id, csv_list(priority))
    statuses = [s for s in csv_list(status) or ["candidate", "open", "closed"] if s in STATUSES]
    where = " AND ".join([*scope, "i.status = ANY(:statuses)"])
    params["statuses"] = statuses
    total = await session.scalar(text(f"SELECT count(*) FROM access_incidents i WHERE {where}"), params)
    items = await incidents_where(
        session, where, params, f"ORDER BY i.start_ts DESC, i.id DESC LIMIT {size} OFFSET {(page - 1) * size}"
    )
    counts = (
        await session.execute(
            text(
                "SELECT "
                + ", ".join(f"count(*) FILTER (WHERE i.status = '{s}') AS {s}" for s in STATUSES)
                + f" FROM access_incidents i WHERE {' AND '.join(scope)}"
            ),
            params,
        )
    ).one()
    return {"items": items, "total": total, "page": page, "size": size, "counts": dict(counts._mapping)}


async def _incident(session, ctx, incident_id: int) -> dict:
    found = await incidents_where(session, f"i.id = :id AND {_VIEW}", {"id": incident_id, "run": ctx.run_id})
    if not found:
        raise ApiError(404, "access_incident_not_found", f"инцидент {incident_id} не найден в прогоне")
    return found[0]


@router.get("/access-incidents/{incident_id}", response_model=AccessIncidentDetail)
async def access_incident(incident_id: int, ctx: RunCtx, session: Session) -> dict:
    """Инцидент, его открытия по времени и переходы охраны подобъекта от начала охраны до конца инцидента."""
    item = await _incident(session, ctx, incident_id)
    r = rules()
    guard = await session.execute(
        text(
            "SELECT ts, value_raw AS value FROM events WHERE channel_id = :ch AND value_raw = ANY(:values) "
            "AND (run_id IS NOT DISTINCT FROM :run OR (run_id IS NULL AND ts < run_sim_start(:run))) "
            "AND ts >= :from AND ts <= :to ORDER BY ts LIMIT 200"
        ),
        {
            "ch": item["guard_channel_id"],
            "values": [r.armed, r.disarmed, r.faulty, r.healthy],
            "run": item["run_id"],
            "from": item["armed_since"] or item["start_ts"],
            "to": item["end_ts"] or ctx.now,
        },
    )
    return item | {
        "opening_events": await incident_openings(session, incident_id),
        "guard_events": [dict(g._mapping) for g in guard],
    }


@router.patch("/access-incidents/{incident_id}", response_model=AccessIncident)
async def patch_access_incident(incident_id: int, body: AccessIncidentPatch, ctx: RunCtx, session: Session) -> dict:
    """Разбор диспетчером: «свои работы», «проникновение», «ложное» и комментарий. Заявку не трогает."""
    await _incident(session, ctx, incident_id)
    changes = body.model_dump(exclude_unset=True)
    if changes:
        await session.execute(
            text(
                "UPDATE access_incidents SET "
                + ", ".join(f"{k} = :{k}" for k in changes)
                + ", updated_at = now() WHERE id = :id"
            ),
            changes | {"id": incident_id},
        )
        await session.commit()
    item = (await incidents_by_ids(session, ctx.run_id, [incident_id]))[0]
    await publish([Notification(item["run_id"], "access_incident.updated", item)])
    return item
