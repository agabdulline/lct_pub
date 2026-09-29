"""Прогнозы внешних моделей (этап 14, docs/architecture/external-data.md §6.2–6.3).

Такт прогона внешнего набора (`schedule.tick`) вызывает модели его набора по `trigger` из `/v1/models`: `every_60s` —
решения на границах минут по часам прогона, `hourly` — на границах часов. За такт — не больше `MAX_DECISIONS`
решений, остальное догоняют следующие такты. Цель модели — объект набора (узел, агрегат); в `predictions` прогноз
пишется на его служебный канал цели (D32): `target_channel_id` из ответа модели или канал объекта с `target_tag`.

Исход — по разметке набора `external_labels`, а не по эпизодам «Неисправен». Метки ищутся на канале цели, на
объекте цели и его предках (у Magdeburg метка опыта — на цехе, узлы — его дети):

- `label_at_decision_time` — класс в момент решения: `T ∈ [start_ts, end_ts)` метки из `positive_labels` — `fault`,
  из `ignore_labels` — без исхода, иначе `no_fault`. Закрывается сразу: `T` не позже «сейчас» прогона.
- `label_in_horizon` — событие в горизонте `[T, T + horizon)`: начало метки из `positive_labels` в горизонте и не
  позже «сейчас» — `fault`, горизонт истёк — `no_fault`; горизонт задевает метку из `ignore_labels` — без исхода;
  в момент `T` цель уже внутри положительной метки (агрегат в отказе) — без исхода, как в обучении (AGENTS.md §5).
- `label_at_event` — событийные модели (`trigger = on_event`, `vast_gate`): решение на каждое событие потока, исход —
  метка из `positive_labels`, чей интервал `[start_ts, end_ts]` включает время события, а `meta[label_key]` совпадает
  со значением события (билет машины). Без совпадения ключа метка «ночёвки» пометила бы все проезды.

Событийные модели скорят события прогона после последнего оценённого (водяной знак — время события последнего
прогноза): задача `scoring.score_external_events` сразу после приёма (`scoring_queue`) и такт как страховка, если
задача потерялась. Одна и та же пачка дважды не пишется: момент решения у события детерминирован (ml-service), вставка —
`ON CONFLICT DO NOTHING`.
"""

import datetime as dt
import logging
import re
import time

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import bindparam

from app.core.redis import try_lock, unlock
from app.db.models import Run
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.notify import Notification
from app.domain.outcomes import MAX_UPDATE_EVENTS, outcome_updates
from app.domain.records import predictions_where
from app.domain.runs import run_now
from app.domain.work_orders import apply_prediction_rules

log = logging.getLogger("external")

STEPS = {"every_60s": dt.timedelta(seconds=60), "hourly": dt.timedelta(hours=1)}
MAX_DECISIONS = 30  # решений одной модели за такт: при ×60 такт `beat` в 15 с — это 15 минут прогона
MODELS_TTL = 60.0  # с: список моделей ML-сервиса меняется только с выкладкой модели
_models_cache: tuple[float, list[dict]] | None = None

_INSERT = text(
    """
    INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, horizon_end,
        status, skip_reason, score, threshold, alert, context, factors)
    SELECT :run, :model, r.model_version, r.channel_id, r.decision_time, r.horizon_start, r.horizon_end, r.status,
        r.skip_reason, r.score, r.threshold, r.alert, r.context, r.factors
    FROM jsonb_to_recordset(:rows) AS r(model_version text, channel_id int, decision_time timestamptz,
        horizon_start timestamptz, horizon_end timestamptz, status text, skip_reason text, score float8,
        threshold float8, alert bool, context jsonb, factors jsonb)
    ON CONFLICT (run_id, model, model_version, channel_id, decision_time) DO NOTHING
    RETURNING id, alert
    """
).bindparams(bindparam("rows", type_=JSONB))

# Метка относится к прогнозу: на его канале цели или на объекте цели и его предках (до уровня 1)
_LABEL_MATCH = """
    l.dataset = :dataset AND (l.channel_id = p.channel_id
        OR (l.channel_id IS NULL AND l.object_id IN (o.id, o.parent_id, op.parent_id)))
"""
_TARGETS = """
    FROM predictions p JOIN channels c ON c.id = p.channel_id JOIN objects o ON o.id = c.object_id
    LEFT JOIN objects op ON op.id = o.parent_id
"""
_RETURNING = "RETURNING p.id, p.run_id, p.channel_id, p.model, p.outcome, p.outcome_at, p.outcome_source"

# Класс в момент решения. Метка без конца — точка, момент решения она не покрывает
_AT_DECISION = f"""
UPDATE predictions p SET outcome = CASE WHEN x.pos THEN 'fault' ELSE 'no_fault' END,
    outcome_at = p.decision_time, outcome_source = :source
FROM (
    SELECT p.id,
        EXISTS (SELECT 1 FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:positive)
                AND l.start_ts <= p.decision_time AND p.decision_time < l.end_ts) AS pos,
        EXISTS (SELECT 1 FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:ignore)
                AND l.start_ts <= p.decision_time AND p.decision_time < l.end_ts) AS ign
    {_TARGETS}
    WHERE p.run_id = :run AND p.model = :model AND p.status = 'scored' AND p.outcome IS NULL
      AND p.decision_time <= :now
) x
WHERE p.id = x.id AND (x.pos OR NOT x.ign)
{_RETURNING}
"""

# Событие в горизонте: `fault` — как только начало метки видно по часам прогона, `no_fault` — горизонт истёк;
# решение, принятое внутри положительной метки `[start, end)` (цель уже в отказе), исхода не получает — и ровно в её
# начале: у Azure PdM отказ размечен [06:00, 07:00), в решении на 06:00 агрегат уже в отказе (в обучении lead 0
# не оценивается)
_IN_HORIZON = f"""
UPDATE predictions p SET outcome = CASE WHEN x.first IS NOT NULL THEN 'fault' ELSE 'no_fault' END,
    outcome_at = coalesce(x.first, p.horizon_end), outcome_source = :source
FROM (
    SELECT p.id,
        (SELECT min(l.start_ts) FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:positive)
            AND l.start_ts >= p.horizon_start AND l.start_ts < p.horizon_end AND l.start_ts <= :now) AS first,
        EXISTS (SELECT 1 FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:ignore)
            AND l.start_ts < p.horizon_end AND coalesce(l.end_ts, l.start_ts) >= p.horizon_start) AS ign,
        EXISTS (SELECT 1 FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:positive)
            AND l.start_ts <= p.horizon_start AND p.horizon_start < l.end_ts) AS busy
    {_TARGETS}
    WHERE p.run_id = :run AND p.model = :model AND p.status = 'scored' AND p.outcome IS NULL
) x
WHERE p.id = x.id AND NOT x.ign AND NOT x.busy AND (x.first IS NOT NULL OR p.horizon_end <= :now)
{_RETURNING}
"""
# Событие: время события — начало горизонта прогноза, значение события (билет) — в `context.value`
_AT_EVENT = f"""
UPDATE predictions p SET outcome = CASE WHEN x.pos THEN 'fault' ELSE 'no_fault' END,
    outcome_at = p.horizon_start, outcome_source = :source
FROM (
    SELECT p.id,
        EXISTS (SELECT 1 FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:positive)
                AND l.start_ts <= p.horizon_start AND p.horizon_start <= coalesce(l.end_ts, l.start_ts)
                AND (CAST(:label_key AS text) IS NULL
                     OR l.meta ->> CAST(:label_key AS text) = p.context ->> 'value')) AS pos,
        EXISTS (SELECT 1 FROM external_labels l WHERE {_LABEL_MATCH} AND l.label = ANY(:ignore)
                AND l.start_ts <= p.horizon_start AND p.horizon_start <= coalesce(l.end_ts, l.start_ts)) AS ign
    {_TARGETS}
    WHERE p.run_id = :run AND p.model = :model AND p.status = 'scored' AND p.outcome IS NULL
      AND p.horizon_start <= :now
) x
WHERE p.id = x.id AND (x.pos OR NOT x.ign)
{_RETURNING}
"""
_OUTCOME_SQL = {"label_at_decision_time": _AT_DECISION, "label_in_horizon": _IN_HORIZON, "label_at_event": _AT_EVENT}

MAX_ALERT_EVENTS = 100  # `prediction.created` за такт — только тревоги модели, остальное интерфейс перечитает
EVENT_BATCH = 500  # событий в одном запросе событийной модели
MAX_EVENTS = 5_000  # событий за один вызов `score_events`; остальное — следующий вызов
# Цель не из набора и событие позже момента решения не пишутся: у первой нет канала, второе оценится позже
_NOT_WRITTEN = frozenset({"unknown_target", "future_event"})

# События прогона по каналам набора с водяного знака; сырые каналы — из сырого слоя
_RUN_EVENTS = """
SELECT channel_id, ts, value_raw FROM (
    SELECT channel_id, ts, value_raw FROM events
    WHERE run_id = :run AND channel_id = ANY(:significant) AND ts >= :since
    UNION ALL
    SELECT channel_id, ts, value_raw FROM events_raw
    WHERE run_id = :run AND channel_id = ANY(:raw) AND ts >= :since
) e ORDER BY ts, channel_id LIMIT :limit
"""


def decision_class(result: dict) -> str | None:
    """Класс решения для интерфейса (`context.class`): из ответа модели, иначе самый вероятный из `classes`."""
    if result.get("class"):
        return result["class"]
    classes = result.get("classes") or {}
    return max(classes, key=classes.get) if classes else None


def horizon(model: dict) -> dt.timedelta:
    """`now` — момент решения, иначе длительность `24h`, `30m`, `60s`."""
    value = model.get("horizon") or "now"
    if value == "now":
        return dt.timedelta(0)
    m = re.fullmatch(r"(\d+)([smhd])", value)
    if m is None:
        raise ValueError(f"{model.get('name')}: горизонт {value!r} — ожидается now или 24h / 30m")
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    return dt.timedelta(**{units[m[2]]: int(m[1])})


async def external_models(dataset: str, ml: MlClient) -> list[dict]:
    """Внешние модели набора из `/v1/models`, готовые скорить. Список кешируется на `MODELS_TTL`."""
    global _models_cache
    if _models_cache is None or time.monotonic() - _models_cache[0] > MODELS_TTL:
        _models_cache = (time.monotonic(), await ml.models())
    return [
        m
        for m in _models_cache[1]
        if m.get("family") == "external" and m.get("dataset") == dataset and m.get("scoring") == "available"
    ]


def decision_times(start: dt.datetime, last: dt.datetime | None, now: dt.datetime, step: dt.timedelta) -> list:
    """Границы шага по часам прогона после последнего решения и не позже «сейчас»; первое — первая граница не раньше
    старта. Не больше `MAX_DECISIONS`."""
    epoch = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    if last is None:
        k = -((epoch - start) // step)  # ceil((start − epoch) / step)
        t = epoch + k * step
    else:
        t = last + step
    out = []
    while t <= now and len(out) < MAX_DECISIONS:
        out.append(t)
        t += step
    return out


async def _target_channels(session: AsyncSession, dataset: str, tag: str | None) -> dict[int, int]:
    """Объект цели → его служебный канал цели по `target_tag` модели."""
    if not tag:
        return {}
    rows = await session.execute(
        text("SELECT object_id, min(id) FROM channels WHERE source = :d AND tag = :tag GROUP BY object_id"),
        {"d": dataset, "tag": tag},
    )
    return {object_id: channel_id for object_id, channel_id in rows}


async def score_model(
    session: AsyncSession, run: Run, dataset: str, model: dict, ml: MlClient
) -> tuple[dict, list[Notification]]:
    """Решения одной внешней модели за такт: вызов `/v1/external/score` на каждую границу шага, запись прогнозов."""
    name = model["name"]
    if model.get("trigger") == "on_event":
        return await score_events(session, run, dataset, model, ml)
    step = STEPS.get(model.get("trigger") or "")
    if step is None:
        return {"skipped": f"trigger {model.get('trigger')!r} не поддержан"}, []
    last = await session.scalar(
        text("SELECT max(decision_time) FROM predictions WHERE run_id = :run AND model = :model"),
        {"run": run.id, "model": name},
    )
    times = decision_times(run.sim_start, last, run_now(run), step)
    if not times:
        return {"decisions": 0}, []
    targets = await _target_channels(session, dataset, model.get("target_tag"))
    width = horizon(model)
    rows, lost = [], 0
    for t in times:
        try:
            response = await ml.score_external(run.id, name, t)
        except MlUnavailable as e:
            # Решения не пишутся: следующий такт повторит с той же границы, когда сервис вернётся
            log.warning("внешняя модель %s, прогон %s, %s: %s", name, run.id, t.isoformat(), e.reason)
            break
        version = response.get("model") or model.get("label") or "unknown"
        for r in response["results"]:
            channel_id = r.get("target_channel_id") or targets.get(r["target"])
            if r.get("skip_reason") in _NOT_WRITTEN or channel_id is None:
                lost += 1
                continue
            rows.append(
                {
                    "model_version": r.get("model") or version,
                    "channel_id": channel_id,
                    "decision_time": t.isoformat(),
                    "horizon_start": t.isoformat(),
                    "horizon_end": (t + width).isoformat(),
                    "status": r["status"],
                    "skip_reason": r.get("skip_reason"),
                    "score": r.get("score"),
                    "threshold": r.get("threshold"),
                    "alert": r.get("alert"),
                    "context": {"target": r["target"], "classes": r.get("classes"), "class": decision_class(r)},
                    "factors": r.get("factors"),
                }
            )
    if lost:
        log.warning(
            "внешняя модель %s: %s результатов без канала цели (target_tag %r)", name, lost, model.get("target_tag")
        )
    inserted = (await session.execute(_INSERT, {"run": run.id, "model": name, "rows": rows})).all() if rows else []
    alerts = [pid for pid, alert in inserted if alert]
    notifications = [
        Notification(run.id, "prediction.created", p)
        for p in await predictions_where(
            session, "p.id = ANY(:ids)", {"ids": alerts[:MAX_ALERT_EVENTS]}, "ORDER BY p.id"
        )
    ]
    notifications += await apply_prediction_rules(session, run.id, alerts)
    summary = {"decisions": len(times), "predictions": len(inserted), "alerts": len(alerts)}
    return summary, notifications


async def score_events(
    session: AsyncSession, run: Run, dataset: str, model: dict, ml: MlClient
) -> tuple[dict, list[Notification]]:
    """Событийная модель: события прогона по каналам набора после последнего оценённого — пачками в
    `/v1/external/score` с «сейчас» прогона; прогноз — на канал события, горизонт — от события до решения.
    Задачу после приёма и такт разводит блокировка: одни и те же события не уходят в модель дважды."""
    name = model["name"]
    lock = f"score:external:lock:{run.id}:{name}"
    if not await try_lock(lock, 120):
        return {"skipped": "locked"}, []
    try:
        return await _score_events(session, run, dataset, model, ml)
    finally:
        await unlock(lock)


async def _score_events(
    session: AsyncSession, run: Run, dataset: str, model: dict, ml: MlClient
) -> tuple[dict, list[Notification]]:
    name = model["name"]
    since = await session.scalar(
        text("SELECT max(horizon_start) FROM predictions WHERE run_id = :run AND model = :model"),
        {"run": run.id, "model": name},
    )
    channels = (await session.execute(text("SELECT id, stream FROM channels WHERE source = :d"), {"d": dataset})).all()
    params = {
        "run": run.id,
        "since": since or run.sim_start,
        "significant": [c for c, stream in channels if stream != "raw"],
        "raw": [c for c, stream in channels if stream == "raw"],
        "limit": MAX_EVENTS,
    }
    events = (await session.execute(text(_RUN_EVENTS), params)).all()
    now = run_now(run)
    rows = []
    for start in range(0, len(events), EVENT_BATCH):
        batch = [
            {"channel_id": c, "ts": ts.isoformat(), "value": v} for c, ts, v in events[start : start + EVENT_BATCH]
        ]
        try:
            response = await ml.score_external_events(run.id, name, now, batch)
        except MlUnavailable as e:
            log.warning("событийная модель %s, прогон %s: %s", name, run.id, e.reason)
            break
        version = response.get("model") or model.get("label") or "unknown"
        for r in response["results"]:
            if r.get("skip_reason") in _NOT_WRITTEN or r.get("target_channel_id") is None:
                continue
            rows.append(
                {
                    "model_version": r.get("model") or version,
                    "channel_id": r["target_channel_id"],
                    "decision_time": r["decision_time"],
                    "horizon_start": r["event_ts"],
                    "horizon_end": r["decision_time"],
                    "status": r["status"],
                    "skip_reason": r.get("skip_reason"),
                    "score": r.get("score"),
                    "threshold": r.get("threshold"),
                    "alert": r.get("alert"),
                    "context": {
                        "event_ts": r["event_ts"],
                        "value": r.get("value"),
                        "classes": r.get("classes"),
                        "class": decision_class(r),
                    },
                    "factors": r.get("factors"),
                }
            )
    inserted = (await session.execute(_INSERT, {"run": run.id, "model": name, "rows": rows})).all() if rows else []
    alerts = [pid for pid, alert in inserted if alert]
    notifications = [
        Notification(run.id, "prediction.created", p)
        for p in await predictions_where(
            session, "p.id = ANY(:ids)", {"ids": alerts[:MAX_ALERT_EVENTS]}, "ORDER BY p.id"
        )
    ]
    notifications += await apply_prediction_rules(session, run.id, alerts)
    return {"events": len(events), "predictions": len(inserted), "alerts": len(alerts)}, notifications


async def close_external_outcomes(
    session: AsyncSession, run_id: int, dataset: str, models: list[dict], now: dt.datetime, source: str = "live"
) -> tuple[dict[str, int], list[Notification]]:
    """Исходы прогнозов внешних моделей по разметке набора на «сейчас» прогона (`live`) или на конец журнала
    набора после конца прогона (`history`)."""
    counts: dict[str, int] = {}
    closed = []
    for model in models:
        sql = _OUTCOME_SQL.get(model.get("outcome") or "")
        if sql is None:
            continue
        params = {
            "run": run_id,
            "model": model["name"],
            "dataset": dataset,
            "now": now,
            "positive": model.get("positive_labels") or [],
            "ignore": model.get("ignore_labels") or [],
            "label_key": model.get("label_key"),
            "source": source,
        }
        rows = (await session.execute(text(sql), params)).all()
        counts[model["name"]] = len(rows)
        closed += rows
    return counts, outcome_updates(run_id, closed)[:MAX_UPDATE_EVENTS]


async def external_tick(
    session: AsyncSession, run: Run, dataset: str, ml: MlClient | None = None
) -> tuple[dict, list[Notification]]:
    """Такт прогона внешнего набора: решения его моделей и исходы по разметке."""
    ml = ml or MlClient()
    try:
        models = await external_models(dataset, ml)
    except MlUnavailable as e:
        log.warning("внешние модели набора %s: %s", dataset, e.reason)
        return {"error": e.reason}, []
    summary: dict = {}
    notifications: list[Notification] = []
    for model in models:
        summary[model["name"]], created = await score_model(session, run, dataset, model, ml)
        notifications += created
    now = run_now(run)
    summary["outcomes"], closed = await close_external_outcomes(session, run.id, dataset, models, now)
    return summary, notifications + closed
