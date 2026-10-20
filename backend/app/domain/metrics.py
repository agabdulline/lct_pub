"""Живые метрики по журналу прогнозов (docs/api.md, «Метрики»; AGENTS.md §5 — честные метрики).

Считаются только закрытые прогнозы: горизонт истёк относительно текущего времени прогона и исход записан. Исход
`fault` может появиться в журнале раньше конца горизонта, но в метрики прогноз попадает после — иначе точность
завышается. У завершённого прогона, чьи исходы досчитаны по журналу (`runs.outcomes_until`, app/domain/outcomes.py),
«сейчас» для метрик — конец досчитанных данных: там закрыты все прогнозы с горизонтом внутри данных. Всегда вместе
с P/R: число решений и положительных, упреждение, раздельно каналы с историей и без; `history` — сколько из закрытых
решений получили исход по журналу после конца прогона.
"""

import datetime as dt
import math

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.model_versions import CHAIN_SEP, TASK, chain_of, decided_sql, is_chain, paired_sql, version_sql

_RUN = "run_id IS NOT DISTINCT FROM :run"
DAILY = "daily"
# одна версия модели (15.2): версии событийной модели в P/R не смешиваются; без версии — все прогнозы модели
# версия событийной модели (`NULL` — любая); у связки — её решения тревог, по одному на тревогу
_VERSION = version_sql("predictions.")


def _quantile(sorted_values: list[float], q: float) -> float:
    """Квантиль с линейной интерполяцией (как numpy по умолчанию)."""
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def block(rows: list[tuple[bool, bool, float | None]]) -> dict:
    """rows — (alert, fault, упреждение в секундах) по закрытым решениям."""
    tp = sum(1 for alert, fault, _ in rows if alert and fault)
    fp = sum(1 for alert, fault, _ in rows if alert and not fault)
    fn = sum(1 for alert, fault, _ in rows if not alert and fault)
    tn = len(rows) - tp - fp - fn
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = _ratio(2 * tp, 2 * tp + fp + fn)  # = гармоническое среднее P и R, когда оба определены
    leads = sorted(lead for alert, fault, lead in rows if alert and fault and lead is not None)
    lead_time = None
    if leads:
        lead_time = {
            "min_s": leads[0],
            "p10_s": _quantile(leads, 0.1),
            "p50_s": _quantile(leads, 0.5),
            "p90_s": _quantile(leads, 0.9),
            "max_s": leads[-1],
        }
    return {
        "decisions": len(rows),
        "positives": tp + fn,
        "alerts": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "lead_time": lead_time,
    }


def aggregated(g) -> dict:
    """Блок метрик из строки SQL-агрегата (`model_metrics`): то же, что `block` по строкам; `None` — решений нет."""
    if g is None:
        return block([])
    tp, fp, fn = g.tp, g.fp, g.fn
    lead_time = None
    if g.lead_q is not None:
        p10, p50, p90 = g.lead_q
        lead_time = {"min_s": g.lead_min, "p10_s": p10, "p50_s": p50, "p90_s": p90, "max_s": g.lead_max}
    return {
        "decisions": g.decisions,
        "positives": tp + fn,
        "alerts": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": g.decisions - tp - fp - fn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn),
        "lead_time": lead_time,
    }


async def base_primary(session: AsyncSession) -> str | None:
    """Основная версия событийной модели в базовой истории (режим «Вся история»): та же, что у новых прогонов, если
    у базы есть решения всех её частей (решения истории пишет ML, `outcome_source = 'history'`); иначе `None` — все
    решения базы, как раньше. Версии в P/R не смешиваются."""
    configured = settings.primary_alarm_model
    for version in chain_of(configured) or [configured]:
        found = await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM predictions WHERE run_id IS NULL AND model = :m AND model_version = :v)"
            ),
            {"m": TASK, "v": version},
        )
        if not found:
            return None
    return configured


def closing_now(now: dt.datetime, outcomes_until: dt.datetime | None) -> dt.datetime:
    """«Сейчас» метрик прогона: его часы или конец досчёта по журналу (`outcomes_until`), что позже. Прогноз закрыт и
    входит в метрики, когда у него есть исход и горизонт истёк к этому моменту; журнал фильтрует исходы так же."""
    return outcomes_until if outcomes_until is not None and outcomes_until > now else now


async def metrics_now(session: AsyncSession, run_id: int | None, now: dt.datetime) -> dt.datetime:
    if run_id is None:
        return now
    until = await session.scalar(text("SELECT outcomes_until FROM runs WHERE id = :run"), {"run": run_id})
    return closing_now(now, until)


async def model_metrics(
    session: AsyncSession,
    run_id: int | None,
    model: str,
    now: dt.datetime,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    model_version: str | None = None,
    paired: bool = False,
    sensor_type: str | None = None,
) -> dict:
    """Метрики модели в прогоне за период по `decision_time`. Событийная модель — одна версия: `model_version` или
    основная версия прогона (15.2); у связки — её решения тревог (то, что дало вердикт); `paired` — только решения,
    оценённые всеми версиями прогона, — сравнение версий на одних решениях (там связка — это v3). Ежедневная — смесь
    моделей по таблице маршрутизации (задача C), `model_version` — одна модель; `by_version` и `by_sensor_type` —
    закрытые решения по моделям и типам датчиков. `sensor_type` — только каналы этого типа."""
    outcomes_until, primary = None, None
    if run_id is not None:
        sql = text("SELECT outcomes_until, primary_model FROM runs WHERE id = :run")
        row = (await session.execute(sql, {"run": run_id})).first()
        if row is not None:
            outcomes_until, primary = row
    elif model == TASK and not model_version:
        primary = await base_primary(session)
    now = closing_now(now, outcomes_until)
    version = model_version or (primary if model == TASK else None)
    params = {"run": run_id, "model": model, "now": now, "since": since, "until": until, "version": version}
    params["sensor_type"] = sensor_type
    period = (
        "AND (CAST(:since AS timestamptz) IS NULL OR decision_time >= :since) "
        "AND (CAST(:until AS timestamptz) IS NULL OR decision_time < :until) "
        "AND (CAST(:sensor_type AS text) IS NULL "
        "     OR channel_id IN (SELECT st.id FROM channels st WHERE st.sensor_type = :sensor_type))"
    )
    if paired:
        period += f" AND {paired_sql('predictions.')}"
    # sensor type — only for the daily breakdown: there the many rows get a hash join; on the few event decisions the
    # planner expected one row and scanned the whole directory per decision (2 s for 7.5k decisions of history)
    daily = model == DAILY
    types_column = "ch.sensor_type" if daily else "NULL::text"
    types_join = "JOIN channels ch ON ch.id = predictions.channel_id" if daily else ""
    # closed decisions aggregated in SQL in one pass — all, by channel history, by model and by sensor type: the
    # history scope holds ~730k daily decisions, loading them row by row took seconds
    groups = (
        await session.execute(
            text(
                f"""
                WITH c AS (
                    SELECT alert, outcome = 'fault' AS fault, seen_channel, outcome_source = 'history' AS history,
                        extract(epoch FROM outcome_at - decision_time)::float8 AS lead_s, model_version,
                        {types_column} AS sensor_type
                    FROM predictions {types_join}
                    WHERE {_RUN} AND model = :model AND {_VERSION} AND status = 'scored' AND alert IS NOT NULL
                        AND outcome IS NOT NULL AND horizon_end <= :now {period}
                )
                SELECT GROUPING(seen_channel) AS g_seen, GROUPING(model_version) AS g_version,
                    GROUPING(sensor_type) AS g_type, seen_channel, model_version, sensor_type,
                    count(*) AS decisions,
                    count(*) FILTER (WHERE alert AND fault) AS tp,
                    count(*) FILTER (WHERE alert AND NOT fault) AS fp,
                    count(*) FILTER (WHERE NOT alert AND fault) AS fn,
                    count(*) FILTER (WHERE history) AS history,
                    min(lead_s) FILTER (WHERE alert AND fault) AS lead_min,
                    max(lead_s) FILTER (WHERE alert AND fault) AS lead_max,
                    percentile_cont(ARRAY[0.1, 0.5, 0.9]) WITHIN GROUP (ORDER BY lead_s)
                        FILTER (WHERE alert AND fault AND lead_s IS NOT NULL) AS lead_q
                FROM c
                GROUP BY GROUPING SETS ((), (seen_channel), (model_version), (sensor_type))
                """
            ),
            params,
        )
    ).all()
    stats = (
        await session.execute(
            text(
                f"""
                SELECT count(*) FILTER (WHERE outcome IS NULL OR horizon_end > :now) AS open,
                    count(*) FILTER (WHERE outcome = 'fault' AND horizon_end > :now) AS open_fault,
                    count(*) FILTER (WHERE alert) AS alerts_total,
                    count(*) FILTER (WHERE alert AND outcome = 'fault') AS alerts_confirmed,
                    count(*) FILTER (WHERE outcome IS NULL) AS outcome_open,
                    count(DISTINCT model_version) AS versions, min(model_version) AS only_version,
                    (SELECT model_version FROM predictions WHERE {_RUN} AND model = :model AND {_VERSION}
                     ORDER BY decision_time DESC LIMIT 1) AS version,
                    (SELECT ARRAY[extract(epoch FROM horizon_start - decision_time) / 3600,
                                  extract(epoch FROM horizon_end - decision_time) / 3600]
                     FROM predictions WHERE {_RUN} AND model = :model ORDER BY decision_time DESC LIMIT 1) AS horizon
                FROM predictions WHERE {_RUN} AND model = :model AND {_VERSION} AND status = 'scored' {period}
                """
            ),
            params,
        )
    ).one()

    total = next((g for g in groups if g.g_seen and g.g_version and g.g_type), None)
    seen = {g.seen_channel: g for g in groups if not g.g_seen and g.seen_channel is not None}

    def by(rows: list) -> dict[str, dict]:
        return {k: aggregated(g) for k, g in sorted(rows, key=lambda kg: kg[0])}

    closed = aggregated(total)
    horizon = [round(float(h), 3) for h in stats.horizon] if stats.horizon else None
    return {
        "model": model,
        "model_version": _version(model, version, stats),
        "from": since,
        "to": until,
        "open": stats.open,
        "open_fault": stats.open_fault,
        "horizon_hours": horizon,
        "note": _note(closed["decisions"], stats.open, stats.open_fault, horizon, outcomes_until),
        "history": total.history if total else 0,
        "outcomes_until": outcomes_until,
        # для сводки прогона (`live`): все тревоги модели, сбывшиеся из них и решения без исхода — без учёта
        # конца горизонта, нижняя граница точности в интерфейсе
        "alerts_total": stats.alerts_total,
        "alerts_confirmed": stats.alerts_confirmed,
        "outcome_open": stats.outcome_open,
        "all": closed,
        "seen": aggregated(seen.get(True)),
        "unseen": aggregated(seen.get(False)),
        "by_version": by([(g.model_version or "—", g) for g in groups if not g.g_version]) if model == DAILY else None,
        "by_sensor_type": by([(g.sensor_type or "—", g) for g in groups if not g.g_type]) if model == DAILY else None,
    }


def _version(model: str, version: str | None, stats) -> str | None:
    """Версия в ответе метрик: связка — как запрошена; ежедневная без версии — смесь моделей по таблице
    маршрутизации: одна модель среди решений выборки — её версия, несколько — null; иначе — версия последнего
    решения."""
    if is_chain(version):
        return version
    if model == DAILY and version is None and stats.versions:
        return stats.only_version if stats.versions == 1 else None
    return stats.version


def _note(
    decisions: int, open_count: int, open_fault: int, horizon: list[float] | None, outcomes_until: dt.datetime | None
) -> str | None:
    """Пояснение для интерфейса, когда закрытых прогнозов мало: P/R ещё не на чем считать, а не «модель молчит»."""
    if not open_count:
        return None
    if outcomes_until is not None:
        return (
            f"Исходы после конца прогона досчитаны по журналу; открыто {open_count} — горизонт выходит за конец данных "
            "или задевает пропущенный день журнала."
        )
    days = f"{horizon[1] / 24:g} сут" if horizon and horizon[1] >= 48 else f"{horizon[1]:g} ч" if horizon else ""
    head = (
        f"Прогноз закрывается, когда истекает горизонт ({days}): "
        if days
        else "Прогноз закрывается по концу горизонта: "
    )
    tail = f"открыто {open_count}"
    if open_fault:
        tail += f", у {open_fault} неисправность уже началась — в метрики они войдут после конца горизонта"
    return head + tail + ("" if decisions else "; закрытых решений пока нет") + "."


def live(metrics: dict) -> dict:
    """Короткая сводка для прогона и дашборда: P/R по закрытым, решения, положительные, досчитанные по журналу;
    `alerts`, `alerts_confirmed`, `open` — по всем оценённым решениям, без учёта конца горизонта."""
    b = metrics["all"]
    return {
        "precision": b["precision"],
        "recall": b["recall"],
        "decisions": b["decisions"],
        "positives": b["positives"],
        "history": metrics["history"],
        "alerts": metrics["alerts_total"],
        "alerts_confirmed": metrics["alerts_confirmed"],
        "open": metrics["outcome_open"],
    }


MODELS = ("smoke_alarm", "daily")

# Сводки `live` всех прогонов одним запросом — для таблицы прогонов. Закрытое решение — то же правило, что у
# `model_metrics`: исход записан и горизонт кончился к «сейчас» прогона или к концу досчёта по журналу
_LIVE_COLUMNS = """
    count(*) FILTER (WHERE c.closed) AS decisions,
    count(*) FILTER (WHERE c.closed AND p.alert AND p.outcome = 'fault') AS tp,
    count(*) FILTER (WHERE c.closed AND p.alert AND p.outcome = 'no_fault') AS fp,
    count(*) FILTER (WHERE c.closed AND NOT p.alert AND p.outcome = 'fault') AS fn,
    count(*) FILTER (WHERE c.closed AND p.outcome_source = 'history') AS history,
    count(*) FILTER (WHERE p.alert) AS alerts,
    count(*) FILTER (WHERE p.alert AND p.outcome = 'fault') AS alerts_confirmed,
    count(*) FILTER (WHERE p.outcome IS NULL) AS open
FROM predictions p
JOIN runs r ON r.id = p.run_id
CROSS JOIN LATERAL (
    SELECT p.alert IS NOT NULL AND p.outcome IS NOT NULL
        AND p.horizon_end <= greatest(coalesce(r.sim_now, r.sim_start), r.outcomes_until) AS closed
) c
"""
# событийная модель — основная версия прогона (15.2), у связки — её решения, как в `model_metrics` по умолчанию
_LIVE_BY_RUN = f"""
SELECT p.run_id, p.model, {_LIVE_COLUMNS}
WHERE p.run_id = ANY(:runs) AND p.status = 'scored'
    AND (p.model <> '{TASK}' OR r.primary_model IS NULL OR p.model_version = r.primary_model
         OR (position('{CHAIN_SEP}' IN r.primary_model) > 0
             AND p.model_version = ANY (string_to_array(r.primary_model, '{CHAIN_SEP}')) AND {decided_sql("p.")}))
GROUP BY p.run_id, p.model
"""
# каждая версия событийной модели на парных решениях — у прогонов с теневыми версиями (`live.versions`)
_LIVE_VERSIONS = f"""
SELECT p.run_id, p.model_version, {_LIVE_COLUMNS}
WHERE p.run_id = ANY(:runs) AND p.model = '{TASK}' AND p.status = 'scored'
    AND (cardinality(r.shadow_models) > 0 OR position('{CHAIN_SEP}' IN coalesce(r.primary_model, '')) > 0)
    AND {paired_sql("p.")}
GROUP BY p.run_id, p.model_version
"""


def _live_row(r) -> dict:
    return {
        "precision": _ratio(r.tp, r.tp + r.fp),
        "recall": _ratio(r.tp, r.tp + r.fn),
        "decisions": r.decisions,
        "positives": r.tp + r.fn,
        "history": r.history,
        "alerts": r.alerts,
        "alerts_confirmed": r.alerts_confirmed,
        "open": r.open,
    }


async def live_by_run(session: AsyncSession, run_ids: list[int]) -> dict[int, dict[str, dict]]:
    """`live` по моделям для каждого прогона — запросами с группировкой, те же поля, что у `live()`. `versions` —
    каждая версия событийной модели на решениях, оценённых всеми версиями прогона (15.2)."""
    empty = {"precision": None, "recall": None, "decisions": 0, "positives": 0, "history": 0}
    empty |= {"alerts": 0, "alerts_confirmed": 0, "open": 0}
    # модели хакатона — всегда, внешние модели набора (этап 14) — в `external` по имени задачи, если есть решения
    out: dict[int, dict] = {
        run_id: {model: dict(empty) for model in MODELS} | {"external": {}, "versions": {}} for run_id in run_ids
    }
    if not run_ids:
        return out
    for r in await session.execute(text(_LIVE_BY_RUN), {"runs": run_ids}):
        if r.model in MODELS:
            out[r.run_id][r.model] = _live_row(r)
        else:
            out[r.run_id]["external"][r.model] = _live_row(r)
    for r in await session.execute(text(_LIVE_VERSIONS), {"runs": run_ids}):
        out[r.run_id]["versions"][r.model_version] = _live_row(r)
    return out
