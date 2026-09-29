"""Вскрытие под охраной в прогоне (docs/architecture/access.md §4): такт пересчитывает инциденты и сверяет с базой.

Каждый такт — движок `app.domain.access.evaluate` над событиями прогона от его старта до водяного знака
`runs.events_until` (без последних 2 с, пока прогон идёт: волна ±2 с ещё не видна целиком). Состояние охраны на
старте — из базовой истории. Движок детерминирован, поэтому пересчёт с начала даёт те же инциденты и лишь двигает
их вперёд: кандидат → инцидент или «снято после открытия», инцидент → закрыт. Сверка с `access_incidents` по
(подобъект, начало): новое — вставка, изменения — обновление, перевод в инцидент — заявка `access_open_armed`
и `access_incident.created`, остальное — `access_incident.updated` (одно на инцидент за такт).
"""

import datetime as dt
import json
import logging
from collections import defaultdict

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import async_client
from app.db.models import HACKATHON, AccessIncident, Run, WorkOrder
from app.domain.access import (
    AccessRules,
    Event,
    Incident,
    SubObject,
    evaluate,
    finish_run,
    mark_repeated,
    motion_window,
    rules,
)
from app.domain.access_feed import incidents_by_ids
from app.domain.notify import Notification
from app.domain.runs import run_source
from app.domain.work_orders import _due, config, work_orders_by_ids

log = logging.getLogger("access")

RULE = "access_open_armed"
TASK = "access.sync"
DEBOUNCE_SECONDS = 2  # пересчёт после приёма — не чаще раза в 2 с на прогон: строка ленты через 2–3 с, а не до 15
LOCK_SECONDS = 60
PENDING_SECONDS = 120  # safety net: if the pending task is lost, the key expires and a new one can be queued
GUARD_LOOKBACK = dt.timedelta(days=60)  # переходы охраны до старта прогона: состояние и начало охраны

_FIELDS = (
    "status",
    "qualified_ts",
    "end_ts",
    "end_reason",
    "armed_since",
    "disarmed_ts",
    "openings",
    "hatch_openings",
    "off_hours",
    "motion",
    "guard_faulty",
    "repeated",
    "priority",
)


async def sub_objects(session: AsyncSession, r: AccessRules) -> dict[int, SubObject]:
    """Подобъекты хакатона с каналом охраны, их двери и люки."""
    guards = await session.execute(
        text(
            "SELECT c.object_id, coalesce(o.parent_id, o.id) AS parent, o.name, min(c.id) AS guard "
            "FROM channels c JOIN objects o ON o.id = c.object_id "
            "WHERE c.sensor_type = :guard AND c.source = :src GROUP BY 1, 2, 3"
        ),
        {"guard": r.guard_type, "src": HACKATHON},
    )
    openers: dict[int, dict[int, tuple[str, bool]]] = defaultdict(dict)
    rows = await session.execute(
        text("SELECT object_id, id, name, sensor_type FROM channels WHERE sensor_type = ANY(:types)"),
        {"types": list(r.opening_types)},
    )
    for sub, channel, name, sensor in rows:
        openers[sub][channel] = (name, sensor in r.hatch_types)
    return {g.object_id: SubObject(g.object_id, g.parent, g.name, g.guard, dict(openers[g.object_id])) for g in guards}


async def _events(session: AsyncSession, run: Run, subs: dict[int, SubObject], until: dt.datetime, r: AccessRules):
    """Переходы охраны из базы за `GUARD_LOOKBACK` до старта и события прогона: охрана, двери и люки."""
    sub_of = {s.guard_channel_id: s.id for s in subs.values()} | {c: s.id for s in subs.values() for c in s.openers}
    guards = [s.guard_channel_id for s in subs.values()]
    rows = await session.execute(
        text(
            """
            SELECT channel_id, ts, value_raw, is_alarm FROM events
            WHERE channel_id = ANY(:guards) AND value_raw = ANY(:guard_values)
              AND ((run_id IS NULL AND ts >= :from AND ts < :start) OR (run_id = :run AND ts <= :until))
            UNION ALL
            SELECT channel_id, ts, value_raw, is_alarm FROM events
            WHERE run_id = :run AND channel_id = ANY(:openers) AND value_raw = ANY(:door_values)
              AND ts >= :start AND ts <= :until
            """
        ),
        {
            "guards": guards,
            "openers": [c for s in subs.values() for c in s.openers],
            "guard_values": [r.armed, r.disarmed, r.faulty, r.healthy],
            "door_values": [r.opened, r.closed],
            "run": run.id,
            "from": run.sim_start - GUARD_LOOKBACK,
            "start": run.sim_start,
            "until": until,
        },
    )
    out: dict[int, list[Event]] = defaultdict(list)
    for channel, ts, value, alarm in rows:
        out[sub_of[channel]].append(Event(ts, channel, value, bool(alarm)))
    return out


# Каналы подобъекта (без газа и канала охраны) с событием в ±2 с от открытия — по представлению прогона
_BURST = """
WITH op AS (SELECT e.channel_id, e.ts, c.object_id AS sub FROM events e JOIN channels c ON c.id = e.channel_id
            WHERE e.run_id = :run AND e.channel_id = ANY(:openers) AND e.value_raw = :opened
              AND e.ts >= :start AND e.ts <= :until),
sc AS (SELECT c.id, c.object_id AS sub FROM channels c
       WHERE c.object_id IN (SELECT sub FROM op) AND c.sensor_type <> :gas AND NOT c.id = ANY(:guards))
SELECT op.channel_id, op.ts, count(DISTINCT n.channel_id)
FROM op JOIN LATERAL (
    -- only the ±w slice around this opening: a time-index lookup, not the whole run joined to every opening
    SELECT e.channel_id FROM events e JOIN sc ON sc.id = e.channel_id AND sc.sub = op.sub
    WHERE (e.run_id = :run OR (e.run_id IS NULL AND e.ts < :start)) AND e.ts BETWEEN op.ts - :w AND op.ts + :w
    UNION ALL
    SELECT e.channel_id FROM events_raw e JOIN sc ON sc.id = e.channel_id AND sc.sub = op.sub
    WHERE (e.run_id = :run OR (e.run_id IS NULL AND e.ts < :start)) AND e.ts BETWEEN op.ts - :w AND op.ts + :w
) n ON true
GROUP BY 1, 2
"""


async def _bursts(session, run: Run, subs: dict[int, SubObject], until: dt.datetime, r: AccessRules) -> dict:
    rows = await session.execute(
        text(_BURST),
        {
            "run": run.id,
            "openers": [c for s in subs.values() for c in s.openers],
            "guards": [s.guard_channel_id for s in subs.values()],
            "opened": r.opened,
            "gas": r.gas_type,
            "start": run.sim_start,
            "until": until,
            "w": r.burst_window,
        },
    )
    return {(channel, ts): n for channel, ts, n in rows}


_MOTION = """
SELECT w.k FROM jsonb_to_recordset(CAST(:windows AS jsonb)) AS w(k int, sub int, lo timestamptz, hi timestamptz)
WHERE EXISTS (
    SELECT 1 FROM channels c JOIN LATERAL (
        SELECT 1 FROM events_raw e WHERE e.channel_id = c.id AND e.run_id = :run AND e.value_raw = :motion
          AND e.ts BETWEEN w.lo AND w.hi
        UNION ALL
        SELECT 1 FROM events e WHERE e.channel_id = c.id AND e.run_id = :run AND e.value_raw = :motion
          AND e.ts BETWEEN w.lo AND w.hi
        LIMIT 1
    ) m ON true
    WHERE c.object_id = w.sub AND c.sensor_type = :motion_type
)
"""


async def _motion(session: AsyncSession, run: Run, incidents: list[Incident], r: AccessRules) -> None:
    if not incidents:
        return
    windows = [
        {"k": k, "sub": i.sub_object_id, "lo": lo.isoformat(), "hi": hi.isoformat()}
        for k, i in enumerate(incidents)
        for lo, hi in [motion_window(i, r)]
    ]
    params = {"windows": json.dumps(windows), "run": run.id, "motion": r.motion, "motion_type": r.motion_type}
    hits = await session.execute(text(_MOTION), params)
    for (k,) in hits:
        incidents[k].motion = True


async def _prior_starts(session: AsyncSession, run: Run, r: AccessRules) -> dict[int, list[dt.datetime]]:
    """Старты базовых инцидентов за 7 сут до старта прогона — для «повторяется»."""
    rows = await session.execute(
        text(
            "SELECT sub_object_id, start_ts FROM access_incidents WHERE run_id IS NULL "
            "AND status IN ('open', 'closed') AND start_ts >= :from AND start_ts < :start"
        ),
        {"from": run.sim_start - r.repeat_window, "start": run.sim_start},
    )
    out: dict[int, list[dt.datetime]] = defaultdict(list)
    for sub, ts in rows:
        out[sub].append(ts)
    return out


async def compute_run(session: AsyncSession, run: Run, final: bool, r: AccessRules | None = None):
    """Инциденты прогона по его событиям до водяного знака. `final` — прогон закрыт и все события приняты: волна на
    конце видна целиком, открытые инциденты закрываются с `run_end`."""
    r = r or rules()
    if run.events_until is None:
        return {}, []
    subs = await sub_objects(session, r)
    until = run.events_until if final else run.events_until - r.burst_window
    events = await _events(session, run, subs, until, r)
    if not any(e.channel_id in subs[sid].openers for sid, evs in events.items() for e in evs):
        return subs, []
    burst = await _bursts(session, run, subs, until, r)
    prior = await _prior_starts(session, run, r)
    incidents: list[Incident] = []
    for sid, sub in subs.items():
        if sid not in events:
            continue
        found = evaluate(sub, events[sid], lambda c, t: burst.get((c, t), 1), until, r, since=run.sim_start)
        mark_repeated(found.incidents, prior.get(sid, []), r)
        incidents += found.incidents
    if final:
        finish_run(incidents, run.sim_end if run.status == "finished" and run.sim_end else run.sim_now or until)
    await _motion(session, run, incidents, r)
    return subs, incidents


def _title(sub: SubObject, inc: Incident, template: str) -> str:
    """«Проверить вскрытие под охраной: <подобъект>, <до 3 каналов> и ещё N»."""
    names = [" ".join(sub.openers[c][0].split()) for c in inc.channels()]
    channels = ", ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
    return " ".join(template.format(object=sub.name, channels=channels).split())


async def _order(session: AsyncSession, run_id: int, sub: SubObject, inc: Incident) -> tuple[int | None, list]:
    """Заявка при переводе в инцидент; при открытой заявке подобъекта — ссылка на неё без новой."""
    rc = config().get(RULE, {})
    if not rc.get("enabled", True):
        return None, []
    order = {
        "run_id": run_id,
        "channel_id": sub.guard_channel_id,
        "prediction_id": None,
        "rule": RULE,
        "priority": inc.priority,
        "status": "new",
        "title": _title(sub, inc, rc.get("title", "Проверить вскрытие под охраной: {object}, {channels}")),
        "due_date": _due(inc.qualified_ts, rc.get("due_hours", 1)),
        "created_at": inc.qualified_ts,
        "updated_at": inc.qualified_ts,
    }
    stmt = insert(WorkOrder).values(order).on_conflict_do_nothing().returning(WorkOrder.id)
    created = list((await session.scalars(stmt)).all())
    if created:
        orders = await work_orders_by_ids(session, created)
        return created[0], [Notification(run_id, "work_order.created", w) for w in orders]
    existing = await session.scalar(
        text(
            "SELECT id FROM work_orders WHERE run_id = :run AND channel_id = :ch AND rule = :rule "
            "AND status IN ('new', 'in_progress') ORDER BY id DESC LIMIT 1"
        ),
        {"run": run_id, "ch": sub.guard_channel_id, "rule": RULE},
    )
    return existing, []


def _values(inc: Incident) -> dict:
    return {
        "status": inc.status,
        "qualified_ts": inc.qualified_ts,
        "end_ts": inc.end_ts,
        "end_reason": inc.end_reason,
        "armed_since": inc.armed_since,
        "disarmed_ts": inc.disarmed_ts,
        "openings": len(inc.openings),
        "hatch_openings": inc.hatch_openings,
        "off_hours": inc.off_hours,
        "motion": inc.motion,
        "guard_faulty": inc.guard_faulty,
        "repeated": inc.repeated,
        "priority": inc.priority,
    }


async def sync_run(session: AsyncSession, run: Run, final: bool = False) -> tuple[dict, list[Notification]]:
    """Такт правила по прогону: пересчёт и сверка с `access_incidents`. Только прогоны хакатона."""
    if await run_source(session, run) != HACKATHON:
        return {}, []
    subs, incidents = await compute_run(session, run, final)
    if not incidents:
        return {}, []
    existing = {
        (row.sub_object_id, row.start_ts): row
        for row in (
            await session.execute(text("SELECT * FROM access_incidents WHERE run_id = :run"), {"run": run.id})
        ).all()
    }
    notes: list[Notification] = []
    created_ids, updated_ids = [], []
    summary = {"incidents": 0, "created": 0, "updated": 0}
    now = dt.datetime.now(dt.UTC)
    for inc in incidents:
        values = _values(inc)
        row = existing.get((inc.sub_object_id, inc.start_ts))
        if row is None:
            new = AccessIncident(
                run_id=run.id,
                object_id=inc.object_id,
                sub_object_id=inc.sub_object_id,
                guard_channel_id=inc.guard_channel_id,
                start_ts=inc.start_ts,
                updated_at=now,
                **values,
            )
            session.add(new)
            await session.flush()
            incident_id, was = new.id, None
        else:
            incident_id, was = row.id, row
            if all(getattr(row, f) == values[f] for f in _FIELDS):
                await _openings(session, incident_id, inc)
                continue
            await session.execute(
                text(
                    "UPDATE access_incidents SET "
                    + ", ".join(f"{f} = :{f}" for f in _FIELDS)
                    + ", updated_at = :now WHERE id = :id"
                ),
                values | {"now": now, "id": incident_id},
            )
        await _openings(session, incident_id, inc)
        qualified = inc.status in ("open", "closed") and (was is None or was.status == "candidate")
        if qualified:
            order_id, order_notes = await _order(session, run.id, subs[inc.sub_object_id], inc)
            if order_id is not None:
                await session.execute(
                    text("UPDATE access_incidents SET work_order_id = :w WHERE id = :id"),
                    {"w": order_id, "id": incident_id},
                )
            notes += order_notes
            created_ids.append(incident_id)
            summary["created"] += 1
        elif inc.status in ("open", "closed") or (was is not None and was.status != inc.status):
            updated_ids.append(incident_id)
            summary["updated"] += 1
    summary["incidents"] = len(incidents)
    for item in await incidents_by_ids(session, run.id, created_ids):
        notes.append(Notification(run.id, "access_incident.created", item))
    for item in await incidents_by_ids(session, run.id, updated_ids):
        if item["status"] in ("open", "closed", "dismissed"):
            notes.append(Notification(run.id, "access_incident.updated", item))
    return summary, notes


_OPENINGS = text(
    """
    INSERT INTO access_incident_openings (incident_id, channel_id, ts, is_alarm, closed_ts)
    SELECT :id, o.channel_id, o.ts, o.is_alarm, o.closed_ts
    FROM jsonb_to_recordset(:rows) AS o(channel_id int, ts timestamptz, is_alarm bool, closed_ts timestamptz)
    ON CONFLICT (incident_id, channel_id, ts) DO UPDATE SET closed_ts = EXCLUDED.closed_ts
    """
).bindparams(bindparam("rows", type_=JSONB))


async def _openings(session: AsyncSession, incident_id: int, inc: Incident) -> None:
    rows = [
        {
            "channel_id": o.channel_id,
            "ts": o.ts.isoformat(),
            "is_alarm": o.is_alarm,
            "closed_ts": o.closed_ts.isoformat() if o.closed_ts else None,
        }
        for o in inc.openings
    ]
    await session.execute(_OPENINGS, {"id": incident_id, "rows": rows})


def lock_key(run_id: int) -> str:
    return f"access:sync:lock:{run_id}"


def pending_key(run_id: int, final: bool = False) -> str:
    return f"access:sync:pending:{run_id}:{int(final)}"


async def clear_pending(run_id: int, final: bool = False) -> None:
    """Снять ключ «задача ждёт»: её вызывает сама задача до чтения данных, новые события поставят следующую."""
    try:
        redis = async_client()
        await redis.delete(pending_key(run_id, final))
        await redis.aclose()
    except Exception as e:  # noqa: BLE001 — без Redis ключ истечёт сам
        log.warning("ключ ожидания пересчёта вскрытия не снят для прогона %s: %s", run_id, e)


async def enqueue_sync(run_id: int, final: bool = False) -> bool:
    """Поставить пересчёт правила по прогону (`access.sync`): не больше одной ОЖИДАЮЩЕЙ задачи на прогон и `final`.
    Ключ «pending» ставится `SET NX` и снимается самой задачей на старте; пока он стоит, новые вызовы ничего не
    шлют, а пачки приёма за 2 с (countdown) сливаются в одну задачу. Потерянную задачу подхватит `runs.tick`."""
    if not settings.enqueue_tasks:
        return False
    from app.tasks.celery_app import celery_app

    try:
        redis = async_client()
        queued = bool(await redis.set(pending_key(run_id, final), "1", nx=True, ex=PENDING_SECONDS))
        await redis.aclose()
        if queued:
            celery_app.send_task(TASK, args=[run_id, final], countdown=DEBOUNCE_SECONDS)
        return queued
    except Exception as e:  # noqa: BLE001 — приём не должен падать из-за очереди
        log.warning("пересчёт вскрытия под охраной не поставлен для прогона %s: %s", run_id, e)
        return False


async def has_active(session: AsyncSession, run_id: int) -> bool:
    """У прогона идут кандидаты или инциденты — их двигает любое новое событие (водяной знак)."""
    return bool(
        await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM access_incidents WHERE run_id = :r AND status IN ('candidate', 'open'))"
            ),
            {"r": run_id},
        )
    )
