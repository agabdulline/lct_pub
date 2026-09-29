"""Лента тревог с триажем (docs/api.md, «Тревоги и живая лента»)."""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.api.deps import RunCtx, Session
from app.api.errors import ApiError
from app.api.params import csv_list, page_params
from app.domain.feed import ALARM_SELECT, alarm_to_dict
from app.domain.model_versions import disagree_sql
from app.domain.records import prediction
from app.domain.views import channel_views, object_tree
from app.schemas.api import AlarmDetail, AlarmPage

router = APIRouter(tags=["тревоги"])

VERDICTS = ("ppr", "sensor_fault", "reaction", "no_score")
NEIGHBORS = 8
DISAGREE = disagree_sql("a.id")  # версии событийной модели разошлись (15.2)


async def _filters(
    session,
    run_id,
    frm,
    to,
    object_id,
    channel_id,
    kind,
    verdict: list[str] | None,
    alert: bool | None = None,
    disagree: bool | None = None,
) -> tuple[str, dict]:
    where, params = ["true"], {"run": run_id}
    if frm is not None:
        where.append("a.ts >= :from")
        params["from"] = frm
    if to is not None:
        where.append("a.ts < :to")
        params["to"] = to
    if object_id is not None:
        tree = await object_tree(session)
        o = tree.objects.get(object_id)
        if o is None:
            raise ApiError(404, "object_not_found", f"объект {object_id} не найден")
        if o["level"] == "object":
            where.append("a.object_id = :object")
            params["object"] = object_id
        elif o["level"] == "sub_object":
            where.append("c.object_id = :object")
            params["object"] = object_id
    if channel_id is not None:
        where.append("a.channel_id = :channel")
        params["channel"] = channel_id
    if kinds := csv_list(kind):
        where.append("coalesce(e.kind, 'solitary') = ANY(:kinds)")
        params["kinds"] = kinds
    if verdict:
        where.append("a.verdict = ANY(:verdicts)")
        params["verdicts"] = verdict
    if alert is not None:
        where.append("p.alert IS NOT DISTINCT FROM :alert" if alert else "p.alert IS NOT TRUE")
        params["alert"] = alert
    if disagree is not None:
        where.append(DISAGREE if disagree else f"NOT {DISAGREE}")
    return " AND ".join(where), params


@router.get("/alarms", response_model=AlarmPage)
async def alarms(
    ctx: RunCtx,
    session: Session,
    from_: Annotated[dt.datetime | None, Query(alias="from")] = None,
    to: dt.datetime | None = None,
    object_id: Annotated[int | None, Query(description="объект или подобъект")] = None,
    channel_id: int | None = None,
    kind: Annotated[str | None, Query(description="ppr, group, solitary через запятую")] = None,
    verdict: Annotated[
        str | None, Query(description="ppr, sensor_fault, reaction, no_score, pending через запятую")
    ] = None,
    alert: Annotated[
        bool | None, Query(description="решение модели (своё или унаследованное) выше порога — prediction.alert")
    ] = None,
    disagree: Annotated[
        bool | None,
        Query(description="версии событийной модели разошлись: обе оценили, тревога модели у одной (15.2)"),
    ] = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    """Лента тревог, новые сверху; `counts` — по тем же фильтрам без `verdict` и `disagree`."""
    page, size = page_params(page, size)
    to = to or ctx.now + dt.timedelta(seconds=1)  # будущее прогона не показываем
    where, params = await _filters(
        session, ctx.run_id, from_, to, object_id, channel_id, kind, csv_list(verdict), alert, disagree
    )
    scope_where, scope_params = await _filters(session, ctx.run_id, from_, to, object_id, channel_id, kind, None, alert)
    base = (
        "FROM run_alarms(:run) a JOIN channels c ON c.id = a.channel_id "
        "LEFT JOIN alarm_episodes e ON e.id = a.episode_id LEFT JOIN predictions p ON p.id = a.prediction_id"
    )
    total = await session.scalar(text(f"SELECT count(*) {base} WHERE {where}"), params)
    rows = await session.execute(
        text(f"{ALARM_SELECT} WHERE {where} ORDER BY a.ts DESC, a.id DESC LIMIT {size} OFFSET {(page - 1) * size}"),
        params,
    )
    views = await channel_views(session, ctx.run_id, ctx.now, ctx.source)
    alarm_channels = [c.id for c in views.values() if c.status in ("alarm", "ppr")]
    counts = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE a.channel_id = ANY(:active)) AS active, "
                + ", ".join(f"count(*) FILTER (WHERE a.verdict = '{v}') AS {v}" for v in VERDICTS)
                + ", count(*) FILTER (WHERE a.verdict = 'ppr' AND p.alert) AS ppr_alert"
                + f", count(*) FILTER (WHERE {DISAGREE}) AS disagree"
                + f" {base} WHERE {scope_where}"
            ),
            {**scope_params, "active": alarm_channels},
        )
    ).one()
    return {
        "items": [alarm_to_dict(r, ctx.now) for r in rows],
        "total": total,
        "page": page,
        "size": size,
        "counts": dict(counts._mapping),
    }


@router.get("/alarms/{alarm_id}", response_model=AlarmDetail)
async def alarm_detail(alarm_id: int, ctx: RunCtx, session: Session) -> dict:
    """Тревога: контекст эпизода, ближайшие по пикету датчики того же типа на подобъекте, полный прогноз."""
    row = (await session.execute(text(f"{ALARM_SELECT} WHERE a.id = :id"), {"run": ctx.run_id, "id": alarm_id})).first()
    if row is None:
        raise ApiError(404, "alarm_not_found", f"тревога {alarm_id} не найдена в прогоне")
    alarm = alarm_to_dict(row, ctx.now)
    views = await channel_views(session, ctx.run_id, ctx.now, ctx.source)
    me = views[alarm["channel_id"]]
    peers = [
        c
        for c in views.values()
        if c.sub_object_id == me.sub_object_id and c.sensor_type == me.sensor_type and c.id != me.id
    ]
    peers.sort(key=lambda c: (c.picket_number is None, abs((c.picket_number or 0) - (me.picket_number or 0))))
    peers = peers[:NEIGHBORS]
    last = {}
    if peers:
        last = dict(
            (
                await session.execute(
                    text(
                        "SELECT channel_id, max(ts) FROM run_alarms(:run) WHERE channel_id = ANY(:ids) "
                        "AND ts <= :ts AND ts > :ts - interval '1 hour' GROUP BY channel_id"
                    ),
                    {"run": ctx.run_id, "ids": [p.id for p in peers], "ts": alarm["ts"]},
                )
            ).all()
        )
    alarm["neighbors"] = [
        {
            "channel_id": p.id,
            "channel_name": p.name,
            "picket_number": p.picket_number,
            "ts": last.get(p.id),
            "status": p.status,
        }
        for p in peers
    ]
    alarm["prediction_detail"] = await prediction(session, alarm["prediction"]["id"]) if alarm["prediction"] else None
    return alarm
