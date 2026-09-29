"""Эпизоды оборудования в ленте (docs/api.md, «Эпизоды оборудования»; backend.md §6.2)."""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.api.deps import RunCtx, Session
from app.api.errors import ApiError
from app.api.params import csv_list, page_params
from app.domain.equipment import EQUIPMENT_FAULT, POWER_OUTAGE, episode_channels, episodes_where
from app.domain.status import channel_status
from app.schemas.api import EquipmentEpisodeDetail, EquipmentEpisodePage

router = APIRouter(tags=["тревоги"])

# Представление прогона: эпизоды прогона и базовые, закончившиеся до его старта; кандидаты без порога — не в ленте
_VIEW = (
    "e.qualified AND (e.run_id IS NOT DISTINCT FROM :run OR (CAST(:run AS integer) IS NOT NULL "
    "AND e.run_id IS NULL AND e.end_ts < run_sim_start(:run)))"
)


def _filters(
    run_id: int | None,
    now: dt.datetime,
    from_: dt.datetime | None,
    to: dt.datetime | None,
    object_id: int | None,
    kinds: list[str] | None,
    active: bool | None,
) -> tuple[str, dict]:
    where = [_VIEW, "e.start_ts <= :now"]
    params: dict = {"run": run_id, "now": now}
    if from_ is not None:
        where.append("(e.end_ts IS NULL OR e.end_ts >= :from)")
        params["from"] = from_
    if to is not None:
        where.append("e.start_ts < :to")
        params["to"] = to
    if object_id is not None:
        where.append(
            "(e.object_id = :object OR EXISTS (SELECT 1 FROM equipment_episode_channels ec "
            "JOIN channels c ON c.id = ec.channel_id WHERE ec.episode_id = e.id AND c.object_id = :object))"
        )
        params["object"] = object_id
    if kinds:
        where.append("e.kind = ANY(:kinds)")
        params["kinds"] = kinds
    if active is not None:
        where.append("e.end_ts IS NULL" if active else "e.end_ts IS NOT NULL")
    return " AND ".join(where), params


@router.get("/equipment-episodes", response_model=EquipmentEpisodePage)
async def equipment_episodes(
    ctx: RunCtx,
    session: Session,
    from_: Annotated[dt.datetime | None, Query(alias="from", description="эпизод пересекает [from, to)")] = None,
    to: dt.datetime | None = None,
    object_id: Annotated[int | None, Query(description="объект или подобъект")] = None,
    kind: Annotated[str | None, Query(description="power_outage, equipment_fault через запятую")] = None,
    active: Annotated[bool | None, Query(description="true — открытые, false — закрытые")] = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    """Эпизоды оборудования, новые сверху по началу; `counts` — по тем же фильтрам без `kind`."""
    page, size = page_params(page, size)
    where, params = _filters(ctx.run_id, ctx.now, from_, to, object_id, csv_list(kind), active)
    scope, scope_params = _filters(ctx.run_id, ctx.now, from_, to, object_id, None, active)
    total = await session.scalar(text(f"SELECT count(*) FROM equipment_episodes e WHERE {where}"), params)
    items = await episodes_where(
        session,
        ctx.run_id,
        where,
        params,
        f"ORDER BY e.start_ts DESC, e.id DESC LIMIT {size} OFFSET {(page - 1) * size}",
    )
    counts = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE e.end_ts IS NULL) AS active, "
                f"count(*) FILTER (WHERE e.kind = '{POWER_OUTAGE}') AS power_outage, "
                f"count(*) FILTER (WHERE e.kind = '{EQUIPMENT_FAULT}') AS equipment_fault "
                f"FROM equipment_episodes e WHERE {scope}"
            ),
            scope_params,
        )
    ).one()
    return {"items": items, "total": total, "page": page, "size": size, "counts": dict(counts._mapping)}


@router.get("/equipment-episodes/{episode_id}", response_model=EquipmentEpisodeDetail)
async def equipment_episode(episode_id: int, ctx: RunCtx, session: Session) -> dict:
    """Эпизод и его каналы: состояние на входе, когда вернулись в норму, статус сейчас."""
    found = await episodes_where(session, ctx.run_id, f"{_VIEW} AND e.id = :id", {"run": ctx.run_id, "id": episode_id})
    if not found:
        raise ApiError(404, "equipment_episode_not_found", f"эпизод оборудования {episode_id} не найден в прогоне")
    members = (await episode_channels(session, ctx.run_id, [episode_id])).get(episode_id, [])
    return {
        **found[0],
        "members": [
            {
                **{k: m[k] for k in ("channel_id", "channel_name", "sensor_type", "sub_object_id", "sub_object_name")},
                **{k: m[k] for k in ("picket_number", "value", "start_ts", "end_ts")},
                "status": channel_status(m["sensor_type"], m["current_value"], m["current_alarm"], None),
            }
            for m in members
        ],
    }
