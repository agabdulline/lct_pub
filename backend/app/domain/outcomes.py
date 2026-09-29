"""Исходы прогнозов (docs/architecture/backend.md §2.3): журнал наполняется фактами — живая проверка P/R.

`fault` — у канала началась неисправность внутри горизонта `[horizon_start, horizon_end)`, `outcome_at` — её
начало; записывается сразу, как только неисправность видна. `no_fault` — горизонт истёк относительно «сейчас»
прогона, `outcome_at` — конец горизонта. В метрики прогноз попадает только после конца горизонта
(`app/domain/metrics.py`). Неисправность — эпизод `Неисправен` из `run_fault_episodes`, как в цели обучения
моделей (`ml/baseline`): `Отключено устройство` исходом не считается, иначе живые P/R разойдутся с офлайн-отчётом.

Досчёт по журналу (`close_outcomes_history`): сценарии проигрывают реальный журнал 2026 года, и будущее после конца
прогона уже есть в базовой истории. У завершённого прогона горизонт (24 ч, у ежедневной модели — 30 сут) по его
часам не истекает, поэтому исходы досчитываются по базе — так же, как их считает офлайн-оценка (`ml/baseline`).
"""

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run
from app.domain.notify import Notification
from app.domain.runs import FINAL_STATUSES

_RUN = "run_id IS NOT DISTINCT FROM :run"
# Исходы по эпизодам «Неисправен» — только у моделей хакатона; внешние модели закрываются по разметке набора
# (`app/domain/external.py`)
HACKATHON_MODELS = ("smoke_alarm", "daily")
_MODELS = "model IN (" + ", ".join(f"'{m}'" for m in HACKATHON_MODELS) + ")"
_MSK = dt.timezone(dt.timedelta(hours=3))
_RETURNING = "RETURNING p.id, p.run_id, p.channel_id, p.model, p.outcome, p.outcome_at, p.outcome_source"

# Поиск по каждому открытому прогнозу через LATERAL: индекс (channel_id, start_ts) сужает эпизоды до горизонта.
# Соединение по хешу читало всю историю эпизодов (~1,4 млн) на каждом такте — 6–8 с и ядро постгреса под нагрузкой
_FAULT = f"""
UPDATE predictions p SET outcome = 'fault', outcome_at = f.start_ts, outcome_source = 'live'
FROM (
    SELECT p.id, f.start_ts
    FROM predictions p CROSS JOIN LATERAL (
        SELECT min(f.start_ts) AS start_ts FROM run_fault_episodes(:run) f
        WHERE f.channel_id = p.channel_id AND f.start_ts >= p.horizon_start AND f.start_ts < p.horizon_end
          AND f.start_ts <= :now AND f.state = 'Неисправен'
    ) f
    WHERE p.{_RUN} AND p.{_MODELS} AND p.status = 'scored' AND p.outcome IS NULL AND f.start_ts IS NOT NULL
) f
WHERE p.id = f.id
{_RETURNING}
"""

_NO_FAULT = f"""
UPDATE predictions p SET outcome = 'no_fault', outcome_at = horizon_end, outcome_source = 'live'
WHERE {_RUN} AND {_MODELS} AND status = 'scored' AND outcome IS NULL AND horizon_end <= :now
{_RETURNING}
"""

# Начало «Неисправен» в базовой истории по всему горизонту. Для каналов сценария база до конца прогона — те же
# события, что прогон уже проиграл (их исходы закрыты по часам прогона раньше); каналы вне сценария прогон не видел —
# у них правда только в базе
_HISTORY_FAULT = f"""
UPDATE predictions p SET outcome = 'fault', outcome_at = f.start_ts, outcome_source = 'history'
FROM (
    SELECT p.id, f.start_ts
    FROM predictions p CROSS JOIN LATERAL (
        SELECT min(f.start_ts) AS start_ts FROM fault_episodes f
        WHERE f.run_id IS NULL AND f.channel_id = p.channel_id AND f.state = 'Неисправен'
          AND f.start_ts >= p.horizon_start AND f.start_ts < p.horizon_end
    ) f
    WHERE p.run_id = :run AND p.{_MODELS} AND p.status = 'scored' AND p.outcome IS NULL AND f.start_ts IS NOT NULL
) f
WHERE p.id = f.id
{_RETURNING}
"""

# Начала нет — `no_fault`, только если горизонт целиком внутри данных: до их конца и без пропущенных дней журнала
# от решения до конца горизонта — как метка офлайн-оценки (`observed` в ml/baseline/features.py и daily.py)
_HISTORY_NO_FAULT = f"""
UPDATE predictions p SET outcome = 'no_fault', outcome_at = horizon_end, outcome_source = 'history'
WHERE run_id = :run AND {_MODELS} AND status = 'scored' AND outcome IS NULL AND horizon_end <= :data_end
  AND NOT EXISTS (
      SELECT 1 FROM unnest(CAST(:gap_from AS timestamptz[]), CAST(:gap_to AS timestamptz[])) AS g(a, b)
      WHERE p.decision_time < g.b AND g.a < p.horizon_end
  )
{_RETURNING}
"""

# Дни без событий в журнале (docs/data-format.md, «Пропущенные дни»): отсутствие событий — не исправность.
# Те же пропуски исключает офлайн-оценка (`GAPS` в ml/baseline/train.py)
MISSING_DAYS = (dt.date(2024, 4, 7), dt.date(2024, 4, 8), dt.date(2026, 6, 1))

# Сколько событий `prediction.updated` отдаём в ленту за один такт: у ежедневной модели в конце горизонта за раз
# закрываются тысячи прогнозов — их интерфейс подхватит обычным перечитыванием журнала
MAX_UPDATE_EVENTS = 500


def outcome_updates(run_id: int | None, rows: list) -> list[Notification]:
    return [
        Notification(
            run_id,
            "prediction.updated",
            {
                "run_id": r.run_id,
                "id": r.id,
                "channel_id": r.channel_id,
                "model": r.model,
                "outcome": r.outcome,
                "outcome_at": r.outcome_at,
                "outcome_source": r.outcome_source,
            },
        )
        for r in rows[:MAX_UPDATE_EVENTS]
    ]


async def close_outcomes(
    session: AsyncSession, run_id: int | None, now: dt.datetime
) -> tuple[dict[str, int], list[Notification]]:
    """Закрыть исходы по часам прогона; возвращает счётчики и события `prediction.updated` (после коммита)."""
    params = {"run": run_id, "now": now}
    fault = (await session.execute(text(_FAULT), params)).all()
    no_fault = (await session.execute(text(_NO_FAULT), params)).all()
    return {"fault": len(fault), "no_fault": len(no_fault)}, outcome_updates(run_id, fault + no_fault)


async def data_end(session: AsyncSession) -> dt.datetime | None:
    """Конец данных журнала — полночь по Москве после дня последнего события базовой истории (у журнала 2026 года —
    01.07.2026 00:00), как `data_end` офлайн-оценки. Без базовой истории — `None`."""
    last = await session.scalar(text("SELECT max(ts) FROM channel_state WHERE run_id IS NULL"))
    if last is None:
        return None
    return dt.datetime.combine(last.astimezone(_MSK).date() + dt.timedelta(days=1), dt.time(), _MSK)


async def close_outcomes_history(session: AsyncSession, run: Run) -> tuple[dict[str, int], list[Notification]]:
    """Исходы завершённого или остановленного прогона: сначала по его часам (`live`), остальное — по базовой
    истории (`history`). Открытыми остаются прогнозы, чей горизонт выходит за конец данных или задевает пропущенный
    день. Повтор ничего не меняет — закрываются только прогнозы без исхода."""
    if run.status not in FINAL_STATUSES:
        raise ValueError(f"прогон {run.id} ещё идёт ({run.status}): исходы по журналу — только у завершённого")
    counts, notifications = await close_outcomes(session, run.id, run.sim_now or run.sim_start)
    fault = (await session.execute(text(_HISTORY_FAULT), {"run": run.id})).all()
    no_fault = []
    end = await data_end(session)
    if end is not None:
        gaps = [dt.datetime.combine(d, dt.time(), _MSK) for d in MISSING_DAYS]
        params = {
            "run": run.id,
            "data_end": end,
            "gap_from": gaps,
            "gap_to": [g + dt.timedelta(days=1) for g in gaps],
        }
        no_fault = (await session.execute(text(_HISTORY_NO_FAULT), params)).all()
        await session.execute(
            text("UPDATE runs SET outcomes_until = greatest(outcomes_until, :end) WHERE id = :run"),
            {"run": run.id, "end": end},
        )
    counts |= {"history_fault": len(fault), "history_no_fault": len(no_fault)}
    return counts, (notifications + outcome_updates(run.id, fault + no_fault))[:MAX_UPDATE_EVENTS]
