"""Каналы (docs/api.md, «Объекты и каналы», «Схемы для интерфейса»)."""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.api.deps import RunCtx, Session, for_channel
from app.api.errors import ApiError
from app.api.params import csv_list, page_params
from app.domain.model_versions import primary_only
from app.domain.records import RUN, predictions_where
from app.domain.runs import data_gap
from app.domain.status import event_status, in_alarm
from app.domain.values import EVENT_SENSOR_TYPES, FAULT_STATES
from app.domain.views import channel_views, object_tree
from app.domain.work_orders import WORK_ORDER_SELECT, work_order_to_dict
from app.domain.work_orders import config as work_order_config
from app.schemas.api import ChannelDetail, ChannelPage, ChannelTimeline, PredictionPage

router = APIRouter(tags=["каналы"])

TIMELINE_DAYS = 30
TIMELINE_MAX_EVENTS = 5000


@router.get("/channels", response_model=ChannelPage)
async def channels(
    ctx: RunCtx,
    session: Session,
    object_id: int | None = None,
    sensor_type: Annotated[str | None, Query(description="через запятую")] = None,
    state: Annotated[
        str | None, Query(description="статусы через запятую: ok, risk, fault, alarm, ppr, unknown")
    ] = None,
    risk_min: float | None = None,
    q: Annotated[str | None, Query(description="название, тег или id")] = None,
    sort: Annotated[str | None, Query(description="-risk — сначала рискованные")] = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    page, size = page_params(page, size)
    items = list((await channel_views(session, ctx.run_id, ctx.now, ctx.source)).values())
    if object_id is not None:
        tree = await object_tree(session, ctx.source)
        if object_id not in tree.objects:
            raise ApiError(404, "object_not_found", f"объект {object_id} не найден")
        subs = tree.sub_objects_under(object_id)
        items = [c for c in items if c.sub_object_id in subs]
    if types := csv_list(sensor_type):
        items = [c for c in items if c.sensor_type in types]
    if statuses := csv_list(state):
        items = [c for c in items if c.status in statuses]
    if risk_min is not None:
        items = [c for c in items if c.risk is not None and c.risk >= risk_min]
    if q:
        needle = q.strip().lower()
        items = [c for c in items if needle in c.name.lower() or needle in c.tag.lower() or needle == str(c.id)]
    if sort == "-risk":
        items.sort(key=lambda c: (c.risk is None, -(c.risk or 0), c.id))
    else:
        items.sort(key=lambda c: (c.sub_object_name, c.picket_number is None, c.picket_number or 0, c.name))
    return {
        "items": [c.as_dict() for c in items[(page - 1) * size : page * size]],
        "total": len(items),
        "page": page,
        "size": size,
    }


async def _counters(session, run_id, channel_id: int, now: dt.datetime) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM run_alarms(:run) a WHERE a.channel_id = :ch AND a.ts >= :d30 AND a.ts <= :now)
                    AS alarms_30d,
                  (SELECT count(*) FROM run_alarms(:run) a JOIN alarm_episodes e ON e.id = a.episode_id
                   WHERE a.channel_id = :ch AND a.ts >= :d30 AND a.ts <= :now AND e.kind = 'solitary')
                    AS solitary_alarms_30d,
                  (SELECT count(*) FROM run_fault_episodes(:run) f
                   WHERE f.channel_id = :ch AND f.start_ts >= :d365 AND f.start_ts <= :now) AS faults_365d,
                  (SELECT coalesce(sum(extract(epoch FROM least(coalesce(f.end_ts, :now), :now)
                                                           - greatest(f.start_ts, :d30))), 0) / 3600
                   FROM run_fault_episodes(:run) f
                   WHERE f.channel_id = :ch AND f.start_ts <= :now AND coalesce(f.end_ts, :now) >= :d30)
                    AS fault_hours_30d,
                  (SELECT max(f.start_ts) FROM run_fault_episodes(:run) f
                   WHERE f.channel_id = :ch AND f.start_ts <= :now) AS last_fault_at
                """
            ),
            {
                "run": run_id,
                "ch": channel_id,
                "now": now,
                "d30": now - dt.timedelta(days=30),
                "d365": now - dt.timedelta(days=365),
            },
        )
    ).one()
    return {**row._mapping, "fault_hours_30d": round(float(row.fault_hours_30d), 2)}


async def _last_prediction(session, run_id, channel_id: int, model: str, now: dt.datetime) -> dict | None:
    found = await predictions_where(
        session,
        f"p.{RUN} AND p.channel_id = :ch AND p.model = :model AND p.decision_time <= :now AND {primary_only('p.')}",
        {"run": run_id, "ch": channel_id, "model": model, "now": now},
        "ORDER BY p.decision_time DESC LIMIT 1",
    )
    return found[0] if found else None


@router.get("/channels/{channel_id}", response_model=ChannelDetail)
async def channel_detail(channel_id: int, ctx: RunCtx, session: Session) -> dict:
    """Карточка канала: метаданные, состояние, последние прогнозы, счётчики, открытые заявки, соседи по группе."""
    ctx = await for_channel(session, ctx, channel_id)
    views = await channel_views(session, ctx.run_id, ctx.now, ctx.source)
    channel = views.get(channel_id)
    if channel is None:
        raise ApiError(404, "channel_not_found", f"канал {channel_id} не найден")
    neighbors = [
        {"id": c.id, "name": c.name, "sensor_type": c.sensor_type, "status": c.status, "picket_number": c.picket_number}
        for c in views.values()
        if c.sub_object_id == channel.sub_object_id and c.tag_group == channel.tag_group and c.id != channel.id
    ]
    neighbors.sort(
        key=lambda n: (n["picket_number"] is None, abs((n["picket_number"] or 0) - (channel.picket_number or 0)))
    )
    return {
        **channel.as_dict(),
        "counters": await _counters(session, ctx.run_id, channel_id, ctx.now),
        "last_alarm_prediction": await _last_prediction(session, ctx.run_id, channel_id, "smoke_alarm", ctx.now),
        "last_daily_prediction": await _last_prediction(session, ctx.run_id, channel_id, "daily", ctx.now),
        "open_work_orders": [
            work_order_to_dict(r)
            for r in await session.execute(
                text(
                    f"{WORK_ORDER_SELECT} WHERE w.{RUN} AND w.channel_id = :ch AND w.status IN ('new', 'in_progress') "
                    "ORDER BY w.created_at DESC"
                ),
                {"run": ctx.run_id, "ch": channel_id},
            )
        ],
        "neighbors": neighbors[:30],
        "fault_since": await session.scalar(
            text("SELECT fault_since FROM run_channel_state(:run) WHERE channel_id = :ch"),
            {"run": ctx.run_id, "ch": channel_id},
        ),
        "data_gap": await _data_gap(session, ctx.run_id),
    }


async def _data_gap(session, run_id: int | None) -> dict | None:
    gap = await data_gap(session, run_id, dt.timedelta(hours=work_order_config()["fault_persisting"]["data_gap_hours"]))
    return {"start_ts": gap[0], "end_ts": gap[1]} if gap else None


def _alarm_episodes(events: list[dict], sensor_type: str) -> list[dict]:
    """Интервалы тревоги из ленты событий: от события в тревоге до первого без неё. У событийного канала (коды
    ошибок, проезды) интервалов нет: тревожное событие — факт в свой момент, а не состояние до следующего."""
    if sensor_type in EVENT_SENSOR_TYPES:
        return []
    episodes, start = [], None
    for e in events:
        alarm = in_alarm(sensor_type, e["value"], e["is_alarm"]) and e["value"] not in FAULT_STATES
        if alarm and start is None:
            start = e["ts"]
        elif not alarm and start is not None:
            episodes.append({"kind": "alarm", "start_ts": start, "end_ts": e["ts"]})
            start = None
    if start is not None:
        episodes.append({"kind": "alarm", "start_ts": start, "end_ts": None})
    return episodes


@router.get("/channels/{channel_id}/timeline", response_model=ChannelTimeline, response_model_by_alias=True)
async def channel_timeline(
    channel_id: int,
    ctx: RunCtx,
    session: Session,
    from_: Annotated[dt.datetime | None, Query(alias="from")] = None,
    to: dt.datetime | None = None,
) -> dict:
    """События, эпизоды состояний и суточные итоги за период; по умолчанию — 30 суток до «сейчас» прогона.
    Газ — значимые изменения после зоны нечувствительности, движение — из окна сырого потока."""
    ctx = await for_channel(session, ctx, channel_id)
    info = (
        await session.execute(
            text("SELECT sensor_type, stream, deadband FROM channels WHERE id = :id"), {"id": channel_id}
        )
    ).first()
    if info is None:
        raise ApiError(404, "channel_not_found", f"канал {channel_id} не найден")
    to = to or ctx.now + dt.timedelta(seconds=1)  # событие в самый момент «сейчас» — тоже в окне
    frm = from_ or to - dt.timedelta(days=TIMELINE_DAYS)
    # сырой канал без зоны (движение) есть только в сыром потоке; с зоной (газ) — значимыми изменениями в events
    source = "run_events_raw" if info.stream == "raw" and info.deadband is None else "run_events"
    params = {"run": ctx.run_id, "ch": channel_id, "from": frm, "to": to}
    rows = await session.execute(
        text(
            f"SELECT ts, value_raw, value_num, is_alarm FROM {source}(:run) "
            "WHERE channel_id = :ch AND ts >= :from AND ts < :to ORDER BY ts DESC, event_id DESC NULLS LAST "
            f"LIMIT {TIMELINE_MAX_EVENTS}"
        ),
        params,
    )
    events = [
        {
            "ts": r.ts,
            "value": r.value_raw,
            "value_num": r.value_num,
            "is_alarm": r.is_alarm,
            "status": event_status(info.sensor_type, r.value_raw, r.is_alarm),
        }
        for r in reversed(rows.all())
    ]
    faults = await session.execute(
        text(
            "SELECT start_ts, end_ts, state FROM run_fault_episodes(:run) WHERE channel_id = :ch "
            "AND start_ts < :to AND (end_ts IS NULL OR end_ts >= :from) ORDER BY start_ts"
        ),
        params,
    )
    episodes = [
        {"kind": "off" if f.state == "Отключено устройство" else "fault", "start_ts": f.start_ts, "end_ts": f.end_ts}
        for f in faults
    ]
    episodes += _alarm_episodes(events, info.sensor_type)
    daily = await session.execute(
        text(
            "SELECT date, n_events, n_alarm, n_fault, min, max, mean FROM run_channel_daily(:run) "
            "WHERE channel_id = :ch AND date >= CAST(:from_date AS date) AND date <= CAST(:to_date AS date) "
            "ORDER BY date"
        ),
        {**params, "from_date": frm.date(), "to_date": to.date()},
    )
    return {
        "from": frm,
        "to": to,
        "events": events,
        "episodes": sorted(episodes, key=lambda e: e["start_ts"]),
        "daily": [dict(r._mapping) for r in daily],
    }


@router.get("/channels/{channel_id}/predictions", response_model=PredictionPage)
async def channel_predictions(
    channel_id: int, ctx: RunCtx, session: Session, model: str | None = None, page: int = 1, size: int = 50
) -> dict:
    """Прогнозы канала с исходами, новые сверху."""
    ctx = await for_channel(session, ctx, channel_id)
    page, size = page_params(page, size)
    where = f"p.{RUN} AND p.channel_id = :ch AND (CAST(:model AS text) IS NULL OR p.model = :model)"
    params = {"run": ctx.run_id, "ch": channel_id, "model": model}
    total = await session.scalar(text(f"SELECT count(*) FROM predictions p WHERE {where}"), params)
    items = await predictions_where(
        session, where, params, f"ORDER BY p.decision_time DESC, p.id DESC LIMIT {size} OFFSET {(page - 1) * size}"
    )
    return {"items": items, "total": total, "page": page, "size": size}
