"""Правила заявок на ТО (docs/product/v1-scope.md «Правила заявок v1», docs/architecture/backend.md §5).

- `alarm_model_alert`, `daily_model_alert` — по прогнозам с тревогой: `apply_prediction_rules` вызывает скоринг
  после записи прогнозов в той же транзакции.
- `solitary_repeat`, `fault_persisting` — без ML, по состоянию прогона: `apply_periodic_rules` вызывает задача
  `rules.apply_rules`.

Одна открытая заявка на канал и правило — индекс `work_orders_open_uq`, вставка `ON CONFLICT DO NOTHING`.
Правила без ML не повторяют заявку на тот же случай: закрытая или отклонённая заявка не создаётся заново, пока
длится та же неисправность или то же окно одиночных тревог. Конфиг — `backend/config/work_order_rules.yaml`.
Уведомления `work_order.created` публикует вызывающий — после коммита.
"""

import datetime as dt
from bisect import bisect_left, bisect_right
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Prediction, WorkOrder
from app.domain.notify import Notification
from app.domain.runs import data_gap

CONFIG = Path(__file__).resolve().parents[2] / "config" / "work_order_rules.yaml"
MSK = ZoneInfo("Europe/Moscow")
# Правила по прогнозам с тревогой — у них в конфиге `models`
PREDICTION_RULES = (
    "alarm_model_alert",
    "daily_model_alert",
    "ext_fire_alert",
    "ext_access_violation",
    "ext_pdm_failure",
)
# `access_open_armed` — вскрытие под охраной, ставит такт правила доступа (`app/domain/access_runs.py`)
RULES = (*PREDICTION_RULES, "solitary_repeat", "fault_persisting", "access_open_armed")


@cache
def config(path: Path = CONFIG) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    unknown = set(data) - set(RULES)
    if unknown:
        raise ValueError(f"work_order_rules.yaml: неизвестные правила {sorted(unknown)}")
    return data


def rule_description(rule: str) -> str | None:
    """Текст правила для карточки заявки; ручные заявки — без описания."""
    return config().get(rule, {}).get("description")


def _due(created: dt.datetime, hours: float) -> dt.date:
    return (created + dt.timedelta(hours=hours)).astimezone(MSK).date()


# --- заявка в виде контракта API --------------------------------------------------------------------------------

WORK_ORDER_SELECT = """
SELECT w.id, w.run_id, w.channel_id, c.name AS channel_name, c.sensor_type, so.parent_id AS object_id,
    o.name AS object_name, so.id AS sub_object_id, so.name AS sub_object_name, w.prediction_id, w.rule, w.priority,
    w.status, w.title, w.due_date, w.created_at,
    w.updated_at, w.closed_at, w.assignee, w.comment
FROM work_orders w
JOIN channels c ON c.id = w.channel_id
JOIN objects so ON so.id = c.object_id
LEFT JOIN objects o ON o.id = so.parent_id
"""


def work_order_to_dict(row) -> dict:
    """Строка WORK_ORDER_SELECT → заявка API (`docs/api.md`, «Заявка»)."""
    return {
        "id": row.id,
        "run_id": row.run_id,
        "channel_id": row.channel_id,
        "channel_name": row.channel_name,
        "sensor_type": row.sensor_type,
        "object_id": row.object_id,
        "object_name": row.object_name,
        "sub_object_id": row.sub_object_id,
        "sub_object_name": row.sub_object_name,
        "prediction_id": row.prediction_id,
        "rule": row.rule,
        "priority": row.priority,
        "status": row.status,
        "title": row.title,
        "due_date": row.due_date,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "closed_at": row.closed_at,
        "assignee": row.assignee,
        "comment": row.comment,
    }


async def work_orders_by_ids(session: AsyncSession, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = await session.execute(text(WORK_ORDER_SELECT + " WHERE w.id = ANY(:ids) ORDER BY w.id"), {"ids": ids})
    return [work_order_to_dict(r) for r in rows]


async def _channels(session: AsyncSession, ids: set[int]) -> dict[int, tuple[str, str, str]]:
    """Канал → (название, тип датчика, название его объекта)."""
    rows = await session.execute(
        text(
            "SELECT c.id, c.name, c.sensor_type, o.name AS object_name FROM channels c "
            "JOIN objects o ON o.id = c.object_id WHERE c.id = ANY(:ids)"
        ),
        {"ids": sorted(ids)},
    )
    return {r.id: (r.name, r.sensor_type, r.object_name) for r in rows}


def _title(template: str, name: str, sensor_type: str, object_name: str = "", **values) -> str:
    # в названиях каналов бывают двойные пробелы и переносы строк
    text_ = template.format(channel=name, sensor=sensor_type.lower(), object=object_name or name, **values)
    return " ".join(text_.split())


async def _create(session: AsyncSession, run_id: int | None, orders: list[dict]) -> list[Notification]:
    """Вставить заявки; уже открытые по тому же каналу и правилу пропускаются."""
    if not orders:
        return []
    stmt = insert(WorkOrder).values(orders).on_conflict_do_nothing().returning(WorkOrder.id)
    ids = list((await session.scalars(stmt)).all())
    return [Notification(run_id, "work_order.created", order) for order in await work_orders_by_ids(session, ids)]


def _order(run_id, channel_id, rule, priority, title, created, due_hours, prediction_id=None) -> dict:
    return {
        "run_id": run_id,
        "channel_id": channel_id,
        "prediction_id": prediction_id,
        "rule": rule,
        "priority": priority,
        "status": "new",
        "title": title,
        "due_date": _due(created, due_hours),
        "created_at": created,
        "updated_at": created,
    }


# --- правила по прогнозам ---------------------------------------------------------------------------------------


async def apply_prediction_rules(
    session: AsyncSession, run_id: int | None, prediction_ids: list[int]
) -> list[Notification]:
    """Заявки по прогнозам с тревогой: `alarm_model_alert` — событийная модель, `daily_model_alert` —
    ежедневная, `ext_*` — модели внешних наборов. Время заявки — время решения модели."""
    cfg = config()
    rule_by_model = {
        model: rule
        for rule in PREDICTION_RULES
        if cfg.get(rule, {}).get("enabled", True)
        for model in cfg.get(rule, {}).get("models", [])
    }
    if not prediction_ids or not rule_by_model:
        return []
    preds = (
        await session.scalars(
            select(Prediction).where(
                Prediction.id.in_(prediction_ids),
                Prediction.status == "scored",
                Prediction.alert.is_(True),
                Prediction.model.in_(list(rule_by_model)),
            )
        )
    ).all()
    channels = await _channels(session, {p.channel_id for p in preds})
    shares = await _daily_shares(session, run_id, [p for p in preds if rule_by_model[p.model] == "daily_model_alert"])
    orders, daily = [], []
    for p in preds:
        rule = rule_by_model[p.model]
        rc = cfg[rule]
        name, sensor_type, object_name = channels.get(p.channel_id, (str(p.channel_id), "", ""))
        priority = rc["priority"]
        above = rc.get("priority_medium_above")
        if above is not None and p.score is not None and p.threshold is not None:
            if p.score >= p.threshold + above * (1 - p.threshold):
                priority = "medium"
        value = (p.context or {}).get("value") or (p.context or {}).get("target") or ""
        title = _title(rc["title"], name, sensor_type, object_name, score=p.score or 0.0, value=value)
        order = _order(p.run_id, p.channel_id, rule, priority, title, p.decision_time, rc["due_hours"], p.id)
        if rule == "daily_model_alert":
            margin = (p.score - p.threshold) / (1 - p.threshold) if p.threshold is not None and p.threshold < 1 else 0.0
            daily.append(((shares.get(p.id, 0.0), margin, -p.channel_id), order))
        else:
            orders.append((p.score or 0.0, order))
    cap = cfg.get("daily_model_alert", {}).get("max_per_day")
    kept = [o for _, o in daily] if cap is None else await _top_daily(session, run_id, daily, cap)
    return await _create(session, run_id, [o for _, o in orders] + kept)


async def _daily_shares(session: AsyncSession, run_id: int | None, preds: list[Prediction]) -> dict[int, float]:
    """Доля оценки среди оценённых решений той же ежедневной модели за те же сутки: место по возрастанию (равные —
    средним местом), делённое на число решений, 0–1. У моделей таблицы маршрутизации (задача C) пороги от 0.01 до
    0.72 — оценки разных моделей несравнимы, доля — сравнима: у каждой лучшие решения дня ближе к 1."""
    out: dict[int, float] = {}
    for version, decision in {(p.model_version, p.decision_time) for p in preds}:
        scores = sorted(
            (
                await session.scalars(
                    text(
                        "SELECT score FROM predictions WHERE run_id IS NOT DISTINCT FROM :run AND model = :model "
                        "AND model_version = :version AND decision_time = :decision AND status = 'scored'"
                    ),
                    {"run": run_id, "model": "daily", "version": version, "decision": decision},
                )
            ).all()
        )
        for p in preds:
            if (p.model_version, p.decision_time) == (version, decision) and scores and p.score is not None:
                less, equal = bisect_left(scores, p.score), bisect_right(scores, p.score)
                out[p.id] = (less + equal + 1) / 2 / len(scores)  # средний номер места равных, с 1
    return out


async def _top_daily(
    session: AsyncSession, run_id: int | None, daily: list[tuple[tuple, dict]], cap: int
) -> list[dict]:
    """Ежедневные модели дают сотни тревог в сутки по всей сети — заявками становятся `cap` лучших за день прогноза
    по доле оценки внутри своей модели (`_daily_shares`), затем по запасу над порогом и номеру канала; остальные
    видны в журнале и на дашборде. Каналы с открытой заявкой места не занимают."""
    if not daily:
        return []
    params = {"run": run_id, "rule": "daily_model_alert"}
    open_channels = set(
        (
            await session.scalars(
                text(
                    "SELECT channel_id FROM work_orders WHERE run_id IS NOT DISTINCT FROM :run AND rule = :rule "
                    "AND status IN ('new', 'in_progress')"
                ),
                params,
            )
        ).all()
    )
    by_day: dict[dt.date, list[tuple[float, dict]]] = {}
    for score, order in daily:
        if order["channel_id"] not in open_channels:
            by_day.setdefault(order["created_at"].astimezone(MSK).date(), []).append((score, order))
    kept = []
    for day, candidates in by_day.items():
        done = await session.scalar(
            text(
                "SELECT count(*) FROM work_orders WHERE run_id IS NOT DISTINCT FROM :run AND rule = :rule "
                "AND (created_at AT TIME ZONE 'Europe/Moscow')::date = :day"
            ),
            {**params, "day": day},
        )
        room = max(cap - done, 0)
        kept += [o for _, o in sorted(candidates, key=lambda c: c[0], reverse=True)[:room]]
    return kept


# --- правила без ML ---------------------------------------------------------------------------------------------

# Длительность — от начала неисправности, а при разрыве данных перед прогоном (`data_gap`) — не раньше начала журнала:
# месяц без данных за неисправность не считается (A18)
_FAULT_PERSISTING = """
SELECT cs.channel_id, c.name, c.sensor_type, x.counted_since
FROM run_channel_state(:run) cs
JOIN channels c ON c.id = cs.channel_id
CROSS JOIN LATERAL (
  SELECT greatest(cs.fault_since, coalesce(CAST(:counted_from AS timestamptz), cs.fault_since)) AS counted_since
) x
WHERE cs.fault_since IS NOT NULL AND x.counted_since <= :since AND cs.value_raw = ANY(:states)
  AND NOT EXISTS (  -- на эту неисправность заявка уже была — открытая, закрытая или отклонённая
    SELECT 1 FROM work_orders w
    WHERE w.run_id IS NOT DISTINCT FROM :run AND w.channel_id = cs.channel_id AND w.rule = 'fault_persisting'
      AND w.created_at >= cs.fault_since)
"""

_SOLITARY_REPEAT = """
SELECT e.channel_id, c.name, c.sensor_type, count(*) AS n
FROM (
  SELECT unnest(channel_ids) AS channel_id FROM run_alarm_episodes(:run)
  WHERE kind = 'solitary' AND start_ts > :since AND start_ts <= :now
) e
JOIN channels c ON c.id = e.channel_id
WHERE NOT EXISTS (  -- в этом окне заявка уже была
  SELECT 1 FROM work_orders w
  WHERE w.run_id IS NOT DISTINCT FROM :run AND w.channel_id = e.channel_id AND w.rule = 'solitary_repeat'
    AND w.created_at > :since)
GROUP BY e.channel_id, c.name, c.sensor_type
HAVING count(*) >= :min_episodes
"""


async def apply_periodic_rules(session: AsyncSession, run_id: int | None, now: dt.datetime) -> list[Notification]:
    """`fault_persisting` и `solitary_repeat` на момент `now` по системным часам прогона."""
    cfg = config()
    orders = []
    rc = cfg.get("fault_persisting")
    if rc and rc.get("enabled", True):
        gap = await data_gap(session, run_id, dt.timedelta(hours=rc["data_gap_hours"]))
        rows = await session.execute(
            text(_FAULT_PERSISTING),
            {
                "run": run_id,
                "since": now - dt.timedelta(hours=rc["min_hours"]),
                "states": rc["states"],
                "counted_from": gap[1] if gap else None,
            },
        )
        for r in rows:
            hours = int((now - r.counted_since).total_seconds() // 3600)
            title = _title(rc["title"], r.name, r.sensor_type, hours=hours)
            orders.append(_order(run_id, r.channel_id, "fault_persisting", rc["priority"], title, now, rc["due_hours"]))
    rc = cfg.get("solitary_repeat")
    if rc and rc.get("enabled", True):
        rows = await session.execute(
            text(_SOLITARY_REPEAT),
            {
                "run": run_id,
                "now": now,
                "since": now - dt.timedelta(days=rc["window_days"]),
                "min_episodes": rc["min_episodes"],
            },
        )
        for r in rows:
            title = _title(rc["title"], r.name, r.sensor_type, count=r.n)
            orders.append(_order(run_id, r.channel_id, "solitary_repeat", rc["priority"], title, now, rc["due_hours"]))
    return await _create(session, run_id, orders)
