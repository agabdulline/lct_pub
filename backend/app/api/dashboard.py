"""Дашборд (docs/api.md, «Дашборд»): окна — 24 часа до «сейчас» выбранного прогона."""

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import RunCtx, Session
from app.domain.metrics import live, model_metrics
from app.domain.views import aggregate, by_sensor_type, by_sub_object, channel_views, object_tree
from app.schemas.api import DashboardRisk, DashboardSummary

router = APIRouter(tags=["дашборд"])

TOP_RISK = 20
_MSK = dt.timezone(dt.timedelta(hours=3))


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def summary(ctx: RunCtx, session: Session) -> dict:
    """KPI: активные тревоги, тревоги и доля ППР за сутки, прогнозы с тревогой, заявки, каналы, живые P/R,
    тревоги по часам и вердиктам."""
    now = ctx.now
    day_ago = now - dt.timedelta(hours=24)
    params = {"run": ctx.run_id, "from": day_ago, "now": now}
    views = await channel_views(session, ctx.run_id, now, ctx.source)
    all_channels = aggregate(views.values())
    hours = (
        await session.execute(
            text(
                """
                SELECT date_trunc('hour', ts) AS hour,
                    count(*) FILTER (WHERE verdict = 'ppr') AS ppr,
                    count(*) FILTER (WHERE verdict = 'sensor_fault') AS sensor_fault,
                    count(*) FILTER (WHERE verdict = 'reaction') AS reaction,
                    count(*) FILTER (WHERE verdict IN ('no_score', 'pending')) AS no_score
                FROM run_alarms(:run) WHERE ts > :from AND ts <= :now GROUP BY 1
                """
            ),
            params,
        )
    ).all()
    by_hour = {r.hour: r for r in hours}
    last_hour = now.astimezone(_MSK).replace(minute=0, second=0, microsecond=0)
    alarms_by_hour = []
    for h in range(23, -1, -1):
        start = last_hour - dt.timedelta(hours=h)
        r = by_hour.get(start)
        alarms_by_hour.append(
            {
                "hour": start,
                "ppr": r.ppr if r else 0,
                "sensor_fault": r.sensor_fault if r else 0,
                "reaction": r.reaction if r else 0,
                "no_score": r.no_score if r else 0,
            }
        )
    alarms_24h = sum(r.ppr + r.sensor_fault + r.reaction + r.no_score for r in hours)
    ppr_24h = sum(r.ppr for r in hours)
    counts = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM predictions WHERE run_id IS NOT DISTINCT FROM :run AND alert
                   AND decision_time > :from AND decision_time <= :now) AS alert_predictions_24h,
                  (SELECT count(*) FROM work_orders WHERE run_id IS NOT DISTINCT FROM :run
                   AND status IN ('new', 'in_progress')) AS open_work_orders,
                  (SELECT count(*) FROM work_orders WHERE run_id IS NOT DISTINCT FROM :run
                   AND status IN ('new', 'in_progress') AND due_date < CAST(:today AS date)) AS overdue_work_orders
                """
            ),
            {**params, "today": now.astimezone(_MSK).date()},
        )
    ).one()
    return {
        "now": now,
        "active_alarms": all_channels.alarm + all_channels.ppr,
        "alarms_24h": alarms_24h,
        "ppr_share_24h": ppr_24h / alarms_24h if alarms_24h else None,
        "alert_predictions_24h": counts.alert_predictions_24h,
        "open_work_orders": counts.open_work_orders,
        "overdue_work_orders": counts.overdue_work_orders,
        "channels_total": all_channels.channels,
        "channels_in_fault": all_channels.in_fault,
        "channels_at_risk": all_channels.at_risk,
        "live": {m: live(await model_metrics(session, ctx.run_id, m, now)) for m in ("smoke_alarm", "daily")},
        "alarms_by_hour": alarms_by_hour,
    }


@router.get("/dashboard/risk", response_model=DashboardRisk)
async def risk(ctx: RunCtx, session: Session) -> dict:
    """Топ каналов по риску ежедневной модели (без неисправных) и риск по объектам с разбивкой по типам датчиков."""
    views = await channel_views(session, ctx.run_id, ctx.now, ctx.source)
    top = sorted(
        (c for c in views.values() if c.risk is not None and c.status != "fault"), key=lambda c: -(c.risk or 0)
    )[:TOP_RISK]
    tree = await object_tree(session, ctx.source)
    groups = by_sub_object(views)
    by_object = []
    for object_id in tree.children.get(tree.district_id, []):
        channels = [c for sub in tree.sub_objects_under(object_id) for c in groups.get(sub, [])]
        a = aggregate(channels)
        by_object.append(
            {
                "object_id": object_id,
                "object_name": tree.objects[object_id]["name"],
                "channels": a.channels,
                "at_risk": a.at_risk,
                "in_fault": a.in_fault,
                "active_alarms": a.alarm + a.ppr,
                "max_risk": a.max_risk,
                "by_sensor_type": by_sensor_type(channels),
            }
        )
    return {"top": [c.as_dict() for c in top], "by_object": by_object}
