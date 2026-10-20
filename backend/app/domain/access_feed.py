"""Инцидент «вскрытие под охраной» в виде контракта API (docs/api.md, «Вскрытие под охраной») — одна форма для
REST и SSE, как у тревог и эпизодов оборудования (`app/domain/feed.py`)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SOURCE_NOTE = "Метка правил охраны, не подтверждённое проникновение"
# модель доступа (lct-8f) — прокси по тому, снимут ли охрану, а не обнаружение проникновения
MODEL_NOTE = "Модель «снимут ли охрану»: прокси «свой или чужой», не обнаружение проникновения"

ACCESS_SELECT = """
SELECT i.id, i.run_id, i.status, i.priority, i.object_id, o.name AS object_name, i.sub_object_id,
    so.name AS sub_object_name, i.guard_channel_id, i.armed_since, i.start_ts, i.qualified_ts, i.end_ts,
    i.end_reason, i.disarmed_ts, i.openings, i.hatch_openings, i.off_hours, i.motion, i.guard_faulty, i.repeated,
    i.work_order_id, i.resolution, i.comment, i.updated_at,
    i.verdict, i.score, i.threshold, i.model_version, i.decided_by, i.model_note,
    (SELECT coalesce(jsonb_agg(jsonb_build_object('id', c.id, 'name', c.name, 'sensor_type', c.sensor_type)
                               ORDER BY f.first, c.id), '[]'::jsonb)
     FROM (SELECT channel_id, min(ts) AS first FROM access_incident_openings WHERE incident_id = i.id
           GROUP BY channel_id) f
     JOIN channels c ON c.id = f.channel_id) AS channels
FROM access_incidents i
JOIN objects o ON o.id = i.object_id
JOIN objects so ON so.id = i.sub_object_id
"""


def incident_to_dict(row) -> dict:
    disarmed_after = None
    if row.disarmed_ts is not None:
        disarmed_after = round((row.disarmed_ts - row.start_ts).total_seconds() / 60)
    return {
        "id": row.id,
        "run_id": row.run_id,
        "status": row.status,
        "priority": row.priority,
        "object_id": row.object_id,
        "object_name": row.object_name,
        "sub_object_id": row.sub_object_id,
        "sub_object_name": row.sub_object_name,
        "guard_channel_id": row.guard_channel_id,
        "armed_since": row.armed_since,
        "start_ts": row.start_ts,
        "qualified_ts": row.qualified_ts,
        "end_ts": row.end_ts,
        "end_reason": row.end_reason,
        "disarmed_after_min": disarmed_after,
        "openings": row.openings,
        "hatch_openings": row.hatch_openings,
        "channels": row.channels,
        "flags": {
            "off_hours": row.off_hours,
            "motion": row.motion,
            "guard_faulty": row.guard_faulty,
            "repeated": row.repeated,
        },
        "work_order_id": row.work_order_id,
        "resolution": row.resolution,
        "comment": row.comment,
        "updated_at": row.updated_at,
        "source_note": MODEL_NOTE if row.decided_by == "model" else SOURCE_NOTE,
        "verdict": row.verdict,
        "score": row.score,
        "threshold": row.threshold,
        "model_version": row.model_version,
        "decided_by": row.decided_by,
        "model_note": row.model_note,
    }


async def incidents_where(session: AsyncSession, where: str, params: dict, tail: str = "") -> list[dict]:
    rows = await session.execute(text(f"{ACCESS_SELECT} WHERE {where} {tail}"), params)
    return [incident_to_dict(r) for r in rows]


async def incidents_by_ids(session: AsyncSession, run_id: int | None, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    return await incidents_where(session, "i.id = ANY(:ids)", {"ids": ids}, "ORDER BY i.start_ts, i.id")


async def incident_openings(session: AsyncSession, incident_id: int) -> list[dict]:
    rows = await session.execute(
        text(
            "SELECT o.channel_id, c.name AS channel_name, c.sensor_type, c.picket_number, o.ts, o.is_alarm, "
            "o.closed_ts FROM access_incident_openings o JOIN channels c ON c.id = o.channel_id "
            "WHERE o.incident_id = :id ORDER BY o.ts, o.channel_id"
        ),
        {"id": incident_id},
    )
    return [dict(r._mapping) for r in rows]
