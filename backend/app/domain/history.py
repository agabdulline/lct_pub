"""Режим «Вся история» (docs/api.md, «Дашборд истории»): журнал хакатона без прогона — помесячно и проблемные объекты.

Только агрегаты: дымовые тревоги и вердикт ППР (`alarms`), начавшиеся неисправности (`fault_episodes`), инциденты
вскрытия под охраной (`access_incidents`) базовой истории (`run_id IS NULL`). Сырые события (33 млн строк) не читаются.
Месяц — по Москве. База меняется только при загрузке журнала, поэтому ответ кэшируется до сдвига конца данных.
"""

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TOP_OBJECTS = 10
TOP_WINDOW = dt.timedelta(days=365)  # проблемные объекты — за последний год данных
SMOKE = "Датчик дыма"

_MONTHS = """
WITH a AS (
    SELECT date_trunc('month', ts AT TIME ZONE 'Europe/Moscow')::date AS m, count(*) AS alarms,
        count(*) FILTER (WHERE verdict = 'ppr') AS ppr
    FROM alarms WHERE run_id IS NULL GROUP BY 1
),
f AS (
    SELECT date_trunc('month', f.start_ts AT TIME ZONE 'Europe/Moscow')::date AS m, count(*) AS faults,
        count(*) FILTER (WHERE c.sensor_type <> :smoke) AS faults_equipment
    FROM fault_episodes f JOIN channels c ON c.id = f.channel_id WHERE f.run_id IS NULL GROUP BY 1
),
x AS (
    SELECT date_trunc('month', start_ts AT TIME ZONE 'Europe/Moscow')::date AS m, count(*) AS access,
        count(*) FILTER (WHERE priority = 'high') AS access_high
    FROM access_incidents WHERE run_id IS NULL AND status IN ('open', 'closed') GROUP BY 1
)
SELECT m, coalesce(a.alarms, 0) AS alarms, coalesce(a.ppr, 0) AS ppr, coalesce(f.faults, 0) AS faults,
    coalesce(f.faults_equipment, 0) AS faults_equipment, coalesce(x.access, 0) AS access,
    coalesce(x.access_high, 0) AS access_high
FROM a FULL JOIN f USING (m) FULL JOIN x USING (m)
ORDER BY m
"""

# объект уровня 2: у подобъекта — родитель
_TOP = """
WITH obj AS (
    SELECT o.id, CASE WHEN o.level = 3 THEN o.parent_id ELSE o.id END AS top
    FROM objects o WHERE o.source = 'hackathon'
),
f AS (
    SELECT obj.top, count(*) AS faults, count(*) FILTER (WHERE c.sensor_type <> :smoke) AS faults_equipment,
        count(DISTINCT f.channel_id) AS channels
    FROM fault_episodes f JOIN channels c ON c.id = f.channel_id JOIN obj ON obj.id = c.object_id
    WHERE f.run_id IS NULL AND f.start_ts >= :since GROUP BY 1
),
a AS (
    SELECT obj.top, count(*) AS alarms, count(*) FILTER (WHERE al.verdict <> 'ppr') AS alarms_out_of_ppr
    FROM alarms al JOIN obj ON obj.id = al.object_id
    WHERE al.run_id IS NULL AND al.ts >= :since GROUP BY 1
),
x AS (
    SELECT obj.top, count(*) AS access
    FROM access_incidents i JOIN obj ON obj.id = i.sub_object_id
    WHERE i.run_id IS NULL AND i.status IN ('open', 'closed') AND i.start_ts >= :since GROUP BY 1
)
SELECT top AS object_id, o.name AS object_name, coalesce(f.faults, 0) AS faults,
    coalesce(f.faults_equipment, 0) AS faults_equipment, coalesce(f.channels, 0) AS fault_channels,
    coalesce(a.alarms, 0) AS alarms, coalesce(a.alarms_out_of_ppr, 0) AS alarms_out_of_ppr,
    coalesce(x.access, 0) AS access
FROM f FULL JOIN a USING (top) FULL JOIN x USING (top) JOIN objects o ON o.id = top
ORDER BY coalesce(f.faults, 0) DESC, top
LIMIT :limit
"""

_cache: dict[str, tuple[dt.datetime, dict]] = {}


async def history_summary(session: AsyncSession, end: dt.datetime) -> dict:
    """Помесячные итоги и проблемные объекты за год до `end` — конца данных базовой истории."""
    cached = _cache.get("summary")
    if cached is not None and cached[0] == end:
        return cached[1]
    months = [
        {
            "month": r.m,
            "smoke_alarms": r.alarms,
            "ppr_alarms": r.ppr,
            "ppr_share": r.ppr / r.alarms if r.alarms else None,
            "faults_started": r.faults,
            "faults_equipment": r.faults_equipment,
            "access_incidents": r.access,
            "access_high": r.access_high,
        }
        for r in await session.execute(text(_MONTHS), {"smoke": SMOKE})
    ]
    since = end - TOP_WINDOW
    top = [
        dict(r._mapping)
        for r in await session.execute(text(_TOP), {"smoke": SMOKE, "since": since, "limit": TOP_OBJECTS})
    ]
    result = {
        "from": months[0]["month"] if months else None,
        "to": end,
        "months": months,
        "top_from": since,
        "top_objects": top,
    }
    _cache["summary"] = (end, result)
    return result


_DATASET_MONTHS = """
WITH d AS (
    SELECT date_trunc('month', date)::date AS m, sum(n_events) AS events, sum(n_alarm) AS alarm_events
    FROM channel_daily WHERE run_id = :run GROUP BY 1
),
p AS (
    SELECT date_trunc('month', decision_time AT TIME ZONE 'Europe/Moscow')::date AS m, count(*) AS decisions,
        count(*) FILTER (WHERE alert) AS alerts
    FROM predictions WHERE run_id = :run GROUP BY 1
)
SELECT m, coalesce(d.events, 0) AS events, coalesce(d.alarm_events, 0) AS alarm_events,
    coalesce(p.decisions, 0) AS decisions, coalesce(p.alerts, 0) AS alerts
FROM d FULL JOIN p USING (m) ORDER BY m
"""

_DATASET_SIZE = """
SELECT (SELECT count(*) FROM objects WHERE source = :source AND level = 2) AS objects,
    (SELECT count(*) FROM channels WHERE source = :source) AS channels
"""

_dataset_cache: dict[int, tuple[tuple[dt.datetime | None, int | None], dict]] = {}


async def dataset_history(session: AsyncSession, run) -> dict:
    """Режим «Все данные»: история набора — его прогон-основа по месяцам (события, тревожные события, решения и
    тревоги моделей, если они записаны); своя шкала времени, с хакатоном не смешивается. События основы не меняются
    после загрузки, решения моделей дописывает `history external` — ответ кэшируется до сдвига конца основы или
    нового решения в ней."""
    end = run.sim_now or run.sim_end
    last = await session.scalar(text("SELECT max(id) FROM predictions WHERE run_id = :run"), {"run": run.id})
    cached = _dataset_cache.get(run.id)
    if cached is not None and cached[0] == (end, last):
        return cached[1]
    dataset = (run.params or {}).get("dataset")
    months = [dict(r._mapping) | {"month": r.m} for r in await session.execute(text(_DATASET_MONTHS), {"run": run.id})]
    for m in months:
        m.pop("m", None)
    size = (await session.execute(text(_DATASET_SIZE), {"source": dataset})).one()
    result = {
        "dataset": dataset,
        "run_id": run.id,
        "from": run.sim_start,
        "to": end,
        "objects": size.objects,
        "channels": size.channels,
        "months": months,
    }
    _dataset_cache[run.id] = ((end, last), result)
    return result
