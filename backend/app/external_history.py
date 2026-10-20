"""Решения внешних моделей на истории набора — строки `predictions` его прогона-основы (`kind = dataset`) для режима
«Все данные» (журнал прогнозов, дашборд набора).

`python -m app.cli history external [--dataset magdeburg]`: каждая внешняя модель набора — только на своём тестовом
периоде из `/v1/models`: `periods.test` (Azure PdM, VAST 2017), у Magdeburg — `unseen_windows`, опыты вне обучения.
На обучающем периоде решения были бы на виденных моделью данных (AGENTS.md §5).

Тем же кодом, что такт прогона (`app.domain.external`): те же вызовы ml-service по событиям основы, та же запись, исходы
по разметке набора на конец его журнала (`outcome_source = history`). Заявок и уведомлений нет — это история, не поток.
Повторный запуск заменяет решения модели в основе; ML-сервис недоступен посреди — ошибка, решения модели как были.
Вызовы модели идут вне транзакции: у Azure PdM это около часа, а база рвёт простаивающую транзакцию через 5 минут
(`idle_in_transaction_session_timeout`). Замена решений модели — короткая транзакция уже после расчёта.
"""

import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run
from app.domain import external
from app.domain.ml_client import MlClient

log = logging.getLogger("external_history")
MSK = ZoneInfo("Europe/Moscow")
CHUNK = 200  # решений по границам шага на одну запись в базу
PARALLEL = 3  # одновременных вызовов ml-service: решения на разные границы независимы


class BackfillError(RuntimeError):
    pass


def _at(value: str, tz: str | None = None) -> dt.datetime:
    t = dt.datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo(tz) if tz else MSK)  # время без пояса — Москва (AGENTS.md §4)


def test_windows(model: dict, base: Run) -> list[tuple[dt.datetime, dt.datetime]]:
    """Тестовые окна модели `[от, до)` внутри журнала основы; у модели без тестового периода — пусто."""
    test = (model.get("periods") or {}).get("test")
    if test:
        spans = [(_at(test[0]), _at(test[1]))]
    else:
        spans = [(_at(w["from"], w.get("tz")), _at(w["to"], w.get("tz"))) for w in model.get("unseen_windows") or []]
    end = base.sim_end or base.sim_start
    return [(max(lo, base.sim_start), min(hi, end)) for lo, hi in spans if lo < end and hi > base.sim_start]


async def _decide(
    session: AsyncSession, base: Run, dataset: str, model: dict, ml: MlClient, windows: list
) -> tuple[int, list[dict]]:
    """Решения модели на окнах: (сколько решений запрошено, строки `predictions`)."""
    if model.get("trigger") == "on_event":
        now = base.sim_end or base.sim_start
        events = [e for lo, hi in windows for e in await external.run_events(session, base.id, dataset, lo, hi, None)]
        await session.commit()  # без открытой транзакции на время вызовов модели
        rows, complete = await external.event_rows(ml, base.id, model, now, events)
        if not complete:
            raise BackfillError(f"{model['name']}: ML-сервис недоступен — история набора не записана")
        return len(events), rows
    step = external.STEPS.get(model.get("trigger") or "")
    if step is None:
        raise BackfillError(f"{model['name']}: trigger {model.get('trigger')!r} не поддержан")
    times = [
        t
        for lo, hi in windows
        for t in external.decision_times(lo, None, hi - dt.timedelta(microseconds=1), step, limit=None)
    ]
    targets = await external.target_channels(session, dataset, model.get("target_tag"))
    await session.commit()  # без открытой транзакции на время вызовов модели
    chunks = [times[i : i + CHUNK] for i in range(0, len(times), CHUNK)]
    rows: list[dict] = []
    for i in range(0, len(chunks), PARALLEL):
        done = await asyncio.gather(
            *(external.rows_at(ml, base.id, model, c, targets) for c in chunks[i : i + PARALLEL])
        )
        if not all(complete for _, complete in done):
            raise BackfillError(f"{model['name']}: ML-сервис недоступен — история набора не записана")
        rows += [r for part, _ in done for r in part]
        log.info("%s: решено %s из %s границ", model["name"], min((i + PARALLEL) * CHUNK, len(times)), len(times))
    return len(times), rows


async def backfill(session: AsyncSession, base: Run, ml: MlClient) -> dict:
    """Решения всех внешних моделей набора на их тестовых окнах в основе; исходы — по разметке на конец журнала."""
    dataset = base.params["dataset"]
    models = await external.external_models(dataset, ml)
    report: dict = {"run": base.id, "dataset": dataset, "models": {}}
    for model in models:
        started = time.perf_counter()
        windows = test_windows(model, base)
        name = model["name"]
        decisions, rows = await _decide(session, base, dataset, model, ml, windows) if windows else (0, [])
        # прежние решения модели заменяются уже посчитанными — запись одним коротким куском транзакции
        await session.execute(
            text("DELETE FROM predictions WHERE run_id = :run AND model = :model"), {"run": base.id, "model": name}
        )
        written = 0
        for i in range(0, len(rows), 5_000):
            part = rows[i : i + 5_000]
            written += len(
                (await session.execute(external._INSERT, {"run": base.id, "model": name, "rows": part})).all()
            )
        report["models"][name] = {
            "windows": [(lo.isoformat(), hi.isoformat()) for lo, hi in windows],
            "decisions": decisions,
            "predictions": written,
            "alerts": sum(1 for r in rows if r.get("alert")),
            "seconds": round(time.perf_counter() - started),
        }
    now = base.sim_end or base.sim_start
    report["outcomes"], _ = await external.close_external_outcomes(session, base.id, dataset, models, now, "history")
    return report


async def dataset_bases(session: AsyncSession, dataset: str | None) -> list[Run]:
    """Прогоны-основы наборов (`kind = dataset`); у набора — последняя основа."""
    runs = (await session.scalars(select(Run).where(Run.kind == "dataset").order_by(Run.id))).all()
    latest = {r.params.get("dataset"): r for r in runs}
    return [r for d, r in latest.items() if dataset is None or d == dataset]
