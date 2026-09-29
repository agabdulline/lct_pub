"""Заявки на ТО (docs/api.md, «Заявки на ТО»). Правила создания — `app/domain/work_orders.py` (8.5); здесь — список,
ручное создание, карточка с историей, смена статуса и выгрузка CSV. Время — по часам выбранного прогона."""

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response
from sqlalchemy import text

from app.api.deps import RunCtx, Session
from app.api.errors import ApiError
from app.api.params import csv_list, page_params
from app.domain.notify import Notification, publish
from app.domain.records import prediction
from app.domain.views import object_tree
from app.domain.work_orders import WORK_ORDER_SELECT, rule_description, work_order_to_dict, work_orders_by_ids
from app.schemas.api import WorkOrder, WorkOrderCreate, WorkOrderDetail, WorkOrderPage, WorkOrderPatch

router = APIRouter(tags=["заявки"])

OPEN = ("new", "in_progress")
TRANSITIONS = {
    "new": {"in_progress", "done", "rejected"},
    "in_progress": {"done", "rejected"},
    "done": set(),
    "rejected": set(),
}
MANUAL_DESCRIPTION = "Создана вручную диспетчером"
CSV_COLUMNS = (
    "id",
    "created_at",
    "status",
    "priority",
    "rule",
    "object_name",
    "sub_object_name",
    "channel_id",
    "channel_name",
    "sensor_type",
    "title",
    "due_date",
    "assignee",
    "comment",
    "prediction_id",
)
_RUN = "w.run_id IS NOT DISTINCT FROM :run"


async def _filters(session, run_id, status, priority, object_id, channel_id, rule) -> tuple[str, dict]:
    where, params = [_RUN], {"run": run_id}
    for column, value in (("w.status", status), ("w.priority", priority), ("w.rule", rule)):
        if values := csv_list(value):
            key = column.split(".")[1]
            where.append(f"{column} = ANY(:{key})")
            params[key] = values
    if channel_id is not None:
        where.append("w.channel_id = :channel")
        params["channel"] = channel_id
    if object_id is not None:
        tree = await object_tree(session)
        if object_id not in tree.objects:
            raise ApiError(404, "object_not_found", f"объект {object_id} не найден")
        where.append("c.object_id = ANY(:subs)")
        params["subs"] = sorted(tree.sub_objects_under(object_id))
    return " AND ".join(where), params


Filter = Annotated[str | None, Query(description="через запятую")]


@router.get("/work-orders", response_model=WorkOrderPage)
async def work_orders(
    ctx: RunCtx,
    session: Session,
    status: Filter = None,
    priority: Filter = None,
    object_id: int | None = None,
    channel_id: int | None = None,
    rule: Filter = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    """Заявки прогона, новые сверху."""
    page, size = page_params(page, size)
    where, params = await _filters(session, ctx.run_id, status, priority, object_id, channel_id, rule)
    total = await session.scalar(
        text(f"SELECT count(*) FROM work_orders w JOIN channels c ON c.id = w.channel_id WHERE {where}"), params
    )
    rows = await session.execute(
        text(
            f"{WORK_ORDER_SELECT} WHERE {where} ORDER BY w.created_at DESC, w.id DESC "
            f"LIMIT {size} OFFSET {(page - 1) * size}"
        ),
        params,
    )
    return {"items": [work_order_to_dict(r) for r in rows], "total": total, "page": page, "size": size}


@router.get("/work-orders/export.csv", response_class=Response)
async def export_csv(
    ctx: RunCtx,
    session: Session,
    status: Filter = None,
    priority: Filter = None,
    object_id: int | None = None,
    channel_id: int | None = None,
    rule: Filter = None,
) -> Response:
    """Выгрузка с теми же фильтрами: UTF-8 с BOM и разделитель `;` — файл открывается в Excel."""
    where, params = await _filters(session, ctx.run_id, status, priority, object_id, channel_id, rule)
    rows = await session.execute(
        text(f"{WORK_ORDER_SELECT} WHERE {where} ORDER BY w.created_at DESC, w.id DESC"), params
    )
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        order = work_order_to_dict(r)
        writer.writerow(["" if order[k] is None else order[k] for k in CSV_COLUMNS])
    run = f"run-{ctx.run_id}" if ctx.run_id is not None else "base"
    return Response(
        content="﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="work-orders-{run}.csv"'},
    )


async def _history(session, order: dict) -> list[dict]:
    rows = await session.execute(
        text("SELECT ts, status, comment, assignee FROM work_order_events WHERE work_order_id = :id ORDER BY ts, id"),
        {"id": order["id"]},
    )
    history = [dict(r._mapping) for r in rows]
    if not history or history[0]["status"] != "new":
        # заявки правил создаются без записи в истории — начало истории восстанавливается по самой заявке
        history.insert(0, {"ts": order["created_at"], "status": "new", "comment": None, "assignee": None})
    return history


async def _detail(session, order_id: int, run_id) -> dict:
    found = await work_orders_by_ids(session, [order_id])
    if not found or found[0]["run_id"] != run_id:
        raise ApiError(404, "work_order_not_found", f"заявка {order_id} не найдена в прогоне")
    order = found[0]
    return {
        **order,
        "prediction": await prediction(session, order["prediction_id"]) if order["prediction_id"] else None,
        "rule_description": rule_description(order["rule"]) or MANUAL_DESCRIPTION,
        "history": await _history(session, order),
    }


@router.get("/work-orders/{order_id}", response_model=WorkOrderDetail)
async def work_order(order_id: int, ctx: RunCtx, session: Session) -> dict:
    """Заявка с основанием (прогноз, правило) и историей."""
    return await _detail(session, order_id, ctx.run_id)


@router.post("/work-orders", status_code=201, response_model=WorkOrder)
async def create_work_order(body: WorkOrderCreate, ctx: RunCtx, session: Session) -> dict:
    """Ручная заявка. Одна открытая ручная заявка на канал в прогоне."""
    if await session.scalar(text("SELECT id FROM channels WHERE id = :id"), {"id": body.channel_id}) is None:
        raise ApiError(422, "channel_not_found", f"канал {body.channel_id} не найден")
    duplicate = await session.scalar(
        text(
            f"SELECT id FROM work_orders w WHERE {_RUN} AND w.channel_id = :ch AND w.rule = 'manual' "
            "AND w.status IN ('new', 'in_progress')"
        ),
        {"run": ctx.run_id, "ch": body.channel_id},
    )
    if duplicate is not None:
        raise ApiError(409, "duplicate", f"по каналу уже есть открытая ручная заявка {duplicate}")
    params = {
        "run": ctx.run_id,
        "ch": body.channel_id,
        "priority": body.priority,
        "title": body.title,
        "due": body.due_date,
        "comment": body.comment,
        "assignee": body.assignee,
        "now": ctx.now,
    }
    order_id = await session.scalar(
        text(
            "INSERT INTO work_orders (run_id, channel_id, rule, priority, status, title, due_date, comment, assignee, "
            "created_at, updated_at) VALUES (:run, :ch, 'manual', :priority, 'new', :title, :due, :comment, "
            ":assignee, :now, :now) RETURNING id"
        ),
        params,
    )
    await session.execute(
        text(
            "INSERT INTO work_order_events (work_order_id, ts, status, comment, assignee) "
            "VALUES (:id, :now, 'new', :comment, :assignee)"
        ),
        {**params, "id": order_id},
    )
    order = (await work_orders_by_ids(session, [order_id]))[0]
    await session.commit()
    await publish([Notification(ctx.run_id, "work_order.created", order)])
    return order


@router.patch("/work-orders/{order_id}", response_model=WorkOrderDetail)
async def update_work_order(order_id: int, body: WorkOrderPatch, ctx: RunCtx, session: Session) -> dict:
    """Статус (`new` → `in_progress` → `done` | `rejected`), комментарий, исполнитель; всё — в историю."""
    current = await _detail(session, order_id, ctx.run_id)
    status = body.status or current["status"]
    if status != current["status"] and status not in TRANSITIONS[current["status"]]:
        raise ApiError(409, "invalid_transition", f"заявка {order_id}: {current['status']} → {status} нельзя")
    fields = body.model_dump(exclude_unset=True)
    assignee = fields.get("assignee", current["assignee"])
    comment = fields.get("comment", current["comment"])
    closed = ctx.now if status in ("done", "rejected") and current["status"] in OPEN else current["closed_at"]
    await session.execute(
        text(
            "UPDATE work_orders SET status = :status, comment = :comment, assignee = :assignee, updated_at = :now, "
            "closed_at = :closed WHERE id = :id"
        ),
        {"status": status, "comment": comment, "assignee": assignee, "now": ctx.now, "closed": closed, "id": order_id},
    )
    await session.execute(
        text(
            "INSERT INTO work_order_events (work_order_id, ts, status, comment, assignee) "
            "VALUES (:id, :now, :status, :comment, :assignee)"
        ),
        {"id": order_id, "now": ctx.now, "status": status, "comment": fields.get("comment"), "assignee": assignee},
    )
    await session.commit()
    return await _detail(session, order_id, ctx.run_id)
