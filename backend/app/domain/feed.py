"""Тревога в виде контракта API (`docs/api.md`, «Тревога») — общее для `/alarms`, `/alarms/{id}` и событий
`alarm.*` живой ленты. Всё читается через представление прогона `run_alarms(:run)`.
"""

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.alarm_episodes import rules
from app.domain.model_versions import CHAIN_SEP, TASK, disagree, scores_sql

ALARM_SELECT = (
    """
SELECT a.id, a.run_id, a.ts, a.channel_id, c.name AS channel_name, c.sensor_type,
    a.object_id, o.name AS object_name, c.object_id AS sub_object_id, so.name AS sub_object_name, c.picket_number,
    e.id AS ep_id, e.object_id AS ep_object_id, e.kind AS ep_kind, e.start_ts AS ep_start, e.end_ts AS ep_end,
    e.n_channels AS ep_channels,
    a.verdict, a.verdict_refined,
    p.id AS p_id, p.status AS p_status, p.score AS p_score, p.threshold AS p_threshold, p.alert AS p_alert,
    p.skip_reason AS p_skip_reason, p.alarm_id AS p_alarm_id, p.decision_time AS p_decision_time,
    p.object_history AS p_object_history, p.model_version AS p_version, {scores} AS scores,
    -- связка: причина пропуска первой её версии, если решала следующая («v3 пропустила: coverage_gap»)
    (SELECT fp.skip_reason FROM predictions fp JOIN runs fr ON fr.id = a.run_id
     WHERE fp.alarm_id = a.id AND fp.model = '{task}' AND fp.status = 'skipped'
       AND position('{sep}' IN coalesce(fr.primary_model, '')) > 0
       AND fp.model_version = split_part(fr.primary_model, '{sep}', 1)
       AND fp.model_version IS DISTINCT FROM p.model_version) AS p_fallback_reason,
    (SELECT count(DISTINCT n.channel_id) FROM run_alarms(:run) n
     WHERE n.object_id = a.object_id AND n.channel_id <> a.channel_id
       AND n.ts >= a.ts - interval '1 hour' AND n.ts < a.ts) AS neighbors_alarmed_1h,
    coalesce(
        (SELECT w.id FROM work_orders w WHERE a.prediction_id IS NOT NULL AND w.prediction_id = a.prediction_id
         ORDER BY w.id LIMIT 1),
        -- заявка по каналу уже открыта раньше — лента ведёт на неё
        (SELECT w.id FROM work_orders w
         WHERE a.verdict = 'sensor_fault' AND w.run_id IS NOT DISTINCT FROM a.run_id AND w.channel_id = a.channel_id
           AND w.rule = 'alarm_model_alert' AND w.status IN ('new', 'in_progress')
         ORDER BY w.id DESC LIMIT 1)
    ) AS work_order_id
FROM run_alarms(:run) a
JOIN channels c ON c.id = a.channel_id
JOIN objects so ON so.id = c.object_id
JOIN objects o ON o.id = a.object_id
LEFT JOIN alarm_episodes e ON e.id = a.episode_id
LEFT JOIN predictions p ON p.id = a.prediction_id
""".replace("{scores}", scores_sql("a.id"))
    .replace("{task}", TASK)
    .replace("{sep}", CHAIN_SEP)
)


def alarm_to_dict(row, now: dt.datetime | None) -> dict:
    """Строка ALARM_SELECT → тревога API. Эпизод открыт (`end_ts: null`), если с последней тревоги прошло не
    больше разрыва эпизода относительно текущего времени прогона."""
    episode = None
    if row.ep_id is not None:
        is_open = now is not None and now - row.ep_end <= rules().gap
        episode = {
            "id": row.ep_id,
            "object_id": row.ep_object_id,
            "kind": row.ep_kind,
            "start_ts": row.ep_start,
            "end_ts": None if is_open else row.ep_end,
            "n_channels": row.ep_channels,
        }
    prediction = None
    if row.p_id is not None:
        prediction = {
            "id": row.p_id,
            "status": row.p_status,
            "score": row.p_score,
            "threshold": row.p_threshold,
            "alert": row.p_alert,
            "skip_reason": row.p_skip_reason,
            "inherited": row.p_alarm_id != row.id,
            "decision_time": row.p_decision_time,
            "object_history": row.p_object_history,
            "decided_by": row.p_version if row.p_status == "scored" else None,
            "fallback_reason": row.p_fallback_reason,
        }
    return {
        "id": row.id,
        "run_id": row.run_id,
        "ts": row.ts,
        "channel_id": row.channel_id,
        "channel_name": row.channel_name,
        "sensor_type": row.sensor_type,
        "object_id": row.object_id,
        "object_name": row.object_name,
        "sub_object_id": row.sub_object_id,
        "sub_object_name": row.sub_object_name,
        "picket_number": row.picket_number,
        "value": rules().alarm_value,
        "episode": episode,
        "verdict": row.verdict,
        "verdict_refined": row.verdict_refined,
        "prediction": prediction,
        "neighbors_alarmed_1h": row.neighbors_alarmed_1h,
        "work_order_id": row.work_order_id,
        "scores": row.scores,
        "disagree": disagree(row.scores),
    }


async def alarms_by_ids(
    session: AsyncSession, run_id: int | None, ids: list[int], now: dt.datetime | None
) -> list[dict]:
    if not ids:
        return []
    rows = await session.execute(
        text(ALARM_SELECT + " WHERE a.id = ANY(:ids) ORDER BY a.ts, a.id"), {"run": run_id, "ids": ids}
    )
    return [alarm_to_dict(r, now) for r in rows]


EPISODE_SELECT = """
SELECT e.id, e.run_id, e.object_id, o.name AS object_name, e.kind, e.start_ts, e.end_ts, e.n_channels, e.n_alarms,
    e.has_lever, e.channel_ids
FROM alarm_episodes e JOIN objects o ON o.id = e.object_id
"""


async def episodes_by_ids(session: AsyncSession, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = await session.execute(text(EPISODE_SELECT + " WHERE e.id = ANY(:ids)"), {"ids": ids})
    return [dict(r._mapping) for r in rows]
