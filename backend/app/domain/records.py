"""Прогноз в виде контракта API (docs/api.md, «Схемы»): с названием канала, типом датчика и объектом
уровня 2. Один SQL — для журнала, карточки канала, тревоги и событий живой ленты;
заявка — `app/domain/work_orders.py`."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.model_versions import decided_sql, scores_sql

PREDICTION_SELECT = """
SELECT p.id, p.run_id, p.model, p.model_version, p.channel_id, c.name AS channel_name, c.sensor_type,
    coalesce(so.parent_id, so.id) AS object_id, coalesce(o.name, so.name) AS object_name, p.alarm_id,
    p.decision_time, p.horizon_start, p.horizon_end, p.status, p.skip_reason, p.score, p.threshold, p.alert,
    p.seen_channel, p.object_history, p.context, p.factors, p.outcome, p.outcome_at, p.outcome_source,
    CASE WHEN p.alarm_id IS NULL THEN '[]'::jsonb ELSE {paired} END AS paired,
    CASE WHEN p.alarm_id IS NULL THEN NULL ELSE {decided} END AS decided
FROM predictions p
-- канал — по ключу на каждую строку: при оценке «одна строка» (связка, база) планировщик иначе перебирал весь
-- справочник на каждое решение — страница журнала истории шла 4 с. LIMIT 1 не даёт развернуть подзапрос в соединение
JOIN LATERAL (
    SELECT ch.name, ch.sensor_type, ch.object_id FROM channels ch WHERE ch.id = p.channel_id LIMIT 1
) c ON true
JOIN objects so ON so.id = c.object_id
LEFT JOIN objects o ON o.id = so.parent_id
""".replace("{paired}", scores_sql("p.alarm_id", exclude="p.id", run="p.run_id")).replace(
    "{decided}", decided_sql("p.")
)

RUN = "run_id IS NOT DISTINCT FROM :run"


def row_dict(row) -> dict:
    return dict(row._mapping)


async def predictions_where(session: AsyncSession, where: str, params: dict, tail: str = "") -> list[dict]:
    rows = await session.execute(text(f"{PREDICTION_SELECT} WHERE {where} {tail}"), params)
    return [row_dict(r) for r in rows]


async def prediction(session: AsyncSession, prediction_id: int) -> dict | None:
    found = await predictions_where(session, "p.id = :id", {"id": prediction_id})
    return found[0] if found else None
