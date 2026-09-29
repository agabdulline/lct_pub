"""Прогоны эмулятора (D21, docs/architecture/emulator.md §5, docs/architecture/data.md §3).

Представление прогона — база до `sim_start` плюс данные прогона (SQL-функции `run_*`). Чтобы представление не
заглядывало в будущее и не требовало подмены полей, при создании прогона в него копируется состояние базы
на `sim_start`: состояние каналов, открытые на старте эпизоды неисправностей (открытыми) и итоги часа и дня старта.
Эпизоды тревог не копируются: первая тревога прогона открывает новый эпизод.

Коммит — на вызывающей стороне. Удаление прогона — только командой на сервере (D22): `python -m app.cli runs delete`.
"""

import datetime as dt
import logging
from collections.abc import Callable, Iterable

from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import HACKATHON, RUN_STATUSES, Run

log = logging.getLogger("runs")

ACTIVE_STATUSES = ("running", "paused")
FINAL_STATUSES = ("finished", "stopped")
_TRANSITIONS = {
    "running": {"paused", "finished", "stopped"},
    "paused": {"running", "finished", "stopped"},
    "finished": set(),
    "stopped": set(),
}
_MSK = dt.timezone(dt.timedelta(hours=3))


def enqueue_archive(run_id: int) -> None:
    """Сырые события завершённого прогона — в архив Parquet задачей `runs.archive_run`."""
    if not settings.enqueue_tasks:
        return
    from app.tasks.celery_app import celery_app

    try:
        celery_app.send_task("runs.archive_run", args=[run_id])
    except Exception as e:  # noqa: BLE001 — без Redis прогон всё равно закрывается; архив можно собрать позже
        log.warning("прогон %s: архив не поставлен: %s", run_id, e)


def enqueue_tick(run_id: int) -> None:
    """Такт сразу: после старта — ежедневный прогноз на дату старта, после завершения — досчёт исходов по журналу;
    не ждёт `beat` (backend.md §2.2, §2.3)."""
    if not settings.enqueue_tasks:
        return
    from app.tasks.celery_app import celery_app

    try:
        celery_app.send_task("runs.tick")
    except Exception as e:  # noqa: BLE001 — прогноз посчитает очередной такт `beat`
        log.warning("прогон %s: такт не поставлен: %s", run_id, e)


# Вызываются после коммита транзакции: для каждого завершённого прогона и для созданного.
on_run_closed: list[Callable[[int], None]] = [enqueue_archive, enqueue_tick]  # такт — досчёт исходов по журналу
on_run_created: list[Callable[[int], None]] = [enqueue_tick]


def _after_commit(session: AsyncSession, run_ids: list[int], callbacks: list[Callable[[int], None]]) -> None:
    if not run_ids or not callbacks:
        return

    def fire(_):
        for run_id in run_ids:
            for callback in callbacks:
                callback(run_id)

    event.listen(session.sync_session, "after_commit", fire, once=True)


class RunError(ValueError):
    """Ошибка прогона; `code` — машинный код для ответа API (`dataset_not_loaded`…), текст — для людей."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


async def get_run(session: AsyncSession, run_id: int) -> Run | None:
    return await session.get(Run, run_id)


async def latest_run(session: AsyncSession) -> Run | None:
    """Последний прогон сценария; прогон-основа внешнего набора (kind = dataset) — не «последний прогон»."""
    return await session.scalar(select(Run).where(Run.kind == "scenario").order_by(Run.id.desc()).limit(1))


async def run_source(session: AsyncSession, run: Run | None) -> str:
    """Набор данных прогона (этап 14): у прогона-основы — свой, у сценария на наборе — его основы, иначе хакатон."""
    if run is None:
        return HACKATHON
    if run.kind == "dataset":
        return (run.params or {}).get("dataset") or HACKATHON
    if run.base_run_id is not None:
        base = await session.get(Run, run.base_run_id)
        return ((base.params or {}).get("dataset") if base else None) or HACKATHON
    return HACKATHON


async def active_run(session: AsyncSession) -> Run | None:
    return await session.scalar(select(Run).where(Run.status.in_(ACTIVE_STATUSES)).order_by(Run.id.desc()).limit(1))


# Сколько после завершения прогон ещё дообрабатывается тактом: исходы, потерянные оценки тревог
RECENTLY_CLOSED = dt.timedelta(hours=1)


async def recently_closed_runs(session: AsyncSession) -> list[Run]:
    since = dt.datetime.now(dt.UTC) - RECENTLY_CLOSED
    return list(
        await session.scalars(
            select(Run)
            .where(Run.kind == "scenario", Run.status.in_(FINAL_STATUSES), Run.finished_at > since)
            .order_by(Run.id)
        )
    )


async def resolve_run(session: AsyncSession, run: int | None) -> Run | None:
    """Параметр `?run=`: не задан — последний прогон (или база, если прогонов нет), 0 — только база."""
    if run is None:
        return await latest_run(session)
    if run == 0:
        return None
    found = await get_run(session, run)
    if found is None:
        raise RunError(f"прогон {run} не найден")
    return found


async def create_run(
    session: AsyncSession,
    *,
    scenario: str,
    sim_start: dt.datetime,
    sim_end: dt.datetime | None = None,
    speed: float = 1.0,
    params: dict | None = None,
    model_version: str | None = None,
    dataset: str | None = None,
    primary_model: str | None = None,
    shadow_models: list[str] | None = None,
    played: Iterable[int] = (),
) -> Run:
    """Закрыть текущий прогон и создать новый со снимком базы на `sim_start`. После коммита для закрытого прогона
    вызываются `on_run_closed`, для нового — `on_run_created`.

    Текущий прогон, который уже проигран до конца, закрывается как `finished`, а не `stopped`: его часы на
    `sim_end`, или источник доиграл его (`played` — такие прогоны по состоянию эмулятора). Новый старт сразу после
    конца сценария иначе опережал шлюз, который переносит `State = finished` раз в шаг, — и прогон оставался
    `stopped` (e2e подряд, перезапуск из интерфейса).

    `primary_model`, `shadow_models` — версии событийной модели (15.2, `model_versions.select_versions`): основная
    даёт вердикты и заявки, теневые оценивают те же тревоги рядом.

    `dataset` — сценарий на внешнем наборе (этап 14): база прогона — прогон-основа набора (`base_run_id`), снимок —
    из неё. Нет основы — `RunError(code="dataset_not_loaded")`, старт вне журнала набора — `dataset_period_mismatch`.
    """
    if sim_start.tzinfo is None:
        raise RunError("sim_start должен быть с часовым поясом")
    base = await dataset_base(session, dataset) if dataset is not None else None
    if base is not None:
        check_dataset_period(base, sim_start)
    now = dt.datetime.now(dt.UTC)
    done = Run.id.in_(list(played)) | (Run.sim_end.is_not(None) & (Run.sim_now >= Run.sim_end))
    finished = list(
        (
            await session.scalars(
                update(Run)
                .where(Run.status.in_(ACTIVE_STATUSES), done)
                .values(status="finished", finished_at=now, sim_now=func.coalesce(Run.sim_end, Run.sim_now))
                .returning(Run.id)
            )
        ).all()
    )
    stopped = finished + list(
        (
            await session.scalars(
                update(Run)
                .where(Run.status.in_(ACTIVE_STATUSES))
                .values(status="stopped", finished_at=now)
                .returning(Run.id)
            )
        ).all()
    )
    run = Run(
        scenario=scenario,
        status="running",
        sim_start=sim_start,
        sim_end=sim_end,
        sim_now=sim_start,
        speed=speed,
        params=params or {},
        model_version=model_version,
        base_run_id=base.id if base else None,
        primary_model=primary_model,
        shadow_models=shadow_models or [],
    )
    session.add(run)
    await session.flush()
    await snapshot_base(session, run.id, sim_start, base.id if base else None)
    _after_commit(session, stopped, on_run_closed)
    _after_commit(session, [run.id], on_run_created)
    return run


async def dataset_base(session: AsyncSession, dataset: str) -> Run:
    """Прогон-основа внешнего набора (`load-external`); последняя, если их несколько."""
    base = await session.scalar(
        select(Run)
        .where(Run.kind == "dataset", Run.params["dataset"].astext == dataset)
        .order_by(Run.id.desc())
        .limit(1)
    )
    if base is None:
        raise RunError(f"набор {dataset} не загружен: load-external --dataset {dataset}", code="dataset_not_loaded")
    return base


def _msk(t: dt.datetime) -> str:
    return t.astimezone(_MSK).strftime("%Y-%m-%d %H:%M:%S")


def check_dataset_period(base: Run, sim_start: dt.datetime) -> None:
    """Старт сценария на наборе — внутри журнала основы."""
    end = base.sim_end or base.sim_start
    if not base.sim_start <= sim_start <= end:
        raise RunError(
            f"период сценария вне основы набора {base.params.get('dataset')}: старт {_msk(sim_start)}, "
            f"журнал набора — {_msk(base.sim_start)} … {_msk(end)}",
            code="dataset_period_mismatch",
        )


async def set_run_status(session: AsyncSession, run_id: int, status: str) -> Run:
    """Сменить статус; при `finished` / `stopped` после коммита вызываются `on_run_closed`."""
    if status not in RUN_STATUSES:
        raise RunError(f"неизвестный статус {status}")
    run = await get_run(session, run_id)
    if run is None:
        raise RunError(f"прогон {run_id} не найден")
    if status == run.status:
        return run
    if status not in _TRANSITIONS[run.status]:
        raise RunError(f"прогон {run_id}: переход {run.status} → {status} невозможен")
    run.status = status
    if status == "finished" and run.sim_end is not None:
        run.sim_now = run.sim_end  # сценарий проигран до конца; дальше часы прогона не идут
    if status in FINAL_STATUSES:
        run.finished_at = dt.datetime.now(dt.UTC)
        _after_commit(session, [run_id], on_run_closed)
    await session.flush()
    return run


async def update_run_clock(
    session: AsyncSession, run_id: int, sim_now: dt.datetime, speed: float | None = None
) -> None:
    """Симуляционное время прогона (шлюз по SimulatedTime). Время назад не идёт. Принимается только у идущего
    прогона и только внутри его периода `[sim_start, sim_end]`: `RunId` и `SimulatedTime` приходят разными элементами
    подписки, и при смене сценария время нового приходило раньше его `RunId` — часы прошлого прогона уходили к
    старту следующего, а такт решал внешними моделями за концом прогона (прогоны 30 и 31 стенда). Конец сценария
    ставит часы на `sim_end` (`set_run_status`)."""
    await session.execute(
        text(
            "UPDATE runs SET sim_now = greatest(coalesce(sim_now, :t), :t) "
            "WHERE id = :run AND status IN ('running', 'paused') AND :t >= sim_start "
            "AND (sim_end IS NULL OR :t <= sim_end)"
        ),
        {"run": run_id, "t": sim_now},
    )
    if speed is not None and speed > 0:  # в конце сценария фактическая скорость 0 — не затираем последнюю
        await session.execute(update(Run).where(Run.id == run_id).values(speed=speed))


def run_now(run: Run) -> dt.datetime:
    """«Сейчас» прогона для решений такта: его часы, но не дальше конца сценария."""
    now = run.sim_now or run.sim_start
    return min(now, run.sim_end) if run.sim_end is not None else now


async def data_gap(
    session: AsyncSession, run_id: int | None, min_gap: dt.timedelta
) -> tuple[dt.datetime, dt.datetime] | None:
    """Разрыв данных перед прогоном хакатона: от конца базовой истории (её последнего события, как «сейчас» базы) до
    старта прогона, если он дольше `min_gap`. Так бывает у журнала после конца данных (13.1, пример организаторов
    01.08.2026): состояние каналов взято из базы на 30.06, а месяц между ними не наблюдался. У сценариев внутри базы
    старт раньше её конца — разрыва нет. У прогонов внешних наборов база — прогон-основа: правило не про них."""
    if run_id is None:
        return None
    start = await session.scalar(
        text("SELECT sim_start FROM runs WHERE id = :run AND base_run_id IS NULL"), {"run": run_id}
    )
    base_end = await session.scalar(text("SELECT max(ts) FROM channel_state WHERE run_id IS NULL"))
    if start is None or base_end is None or start - base_end <= min_gap:
        return None
    return base_end, start


# --- снимок базы на sim_start --------------------------------------------------------------------------------

# Состояние на старте. Канал, у которого последнее событие базы раньше старта, — копия базового состояния;
# остальным последнее событие до старта ищется спуском по индексу (channel_id, ts).
_SNAPSHOT_STATE_QUIET = """
INSERT INTO channel_state (run_id, channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at, fault_since)
SELECT :run, channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at, fault_since
FROM channel_state WHERE {base} AND ts < :start
"""

_SNAPSHOT_STATE_ACTIVE = """
INSERT INTO channel_state (run_id, channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at, fault_since)
SELECT :run, bs.channel_id, l.ts, l.value_raw, l.value_num, l.is_alarm,
    CASE WHEN bs.last_alarm_at IS NULL THEN NULL
         WHEN bs.last_alarm_at < :start THEN bs.last_alarm_at
         ELSE (SELECT ts FROM events WHERE {base} AND channel_id = bs.channel_id AND is_alarm AND ts < :start
               ORDER BY ts DESC LIMIT 1) END,
    (SELECT start_ts FROM fault_episodes
     WHERE {base} AND channel_id = bs.channel_id AND start_ts < :start AND (end_ts IS NULL OR end_ts >= :start)
     ORDER BY start_ts DESC LIMIT 1)
FROM channel_state bs
CROSS JOIN LATERAL (
    SELECT * FROM (
        (SELECT ts, event_id, value_raw, value_num, is_alarm FROM events
         WHERE {base} AND channel_id = bs.channel_id AND ts < :start ORDER BY ts DESC, event_id DESC LIMIT 1)
        UNION ALL
        (SELECT ts, event_id, value_raw, value_num, is_alarm FROM events_raw
         WHERE {base} AND channel_id = bs.channel_id AND ts < :start ORDER BY ts DESC, event_id DESC LIMIT 1)
    ) x ORDER BY ts DESC, event_id DESC NULLS LAST LIMIT 1
) l
WHERE {bs_base} AND bs.ts >= :start
"""

_SNAPSHOT_FAULTS = """
INSERT INTO fault_episodes (run_id, channel_id, start_ts, end_ts, state)
SELECT :run, channel_id, start_ts, NULL, state FROM fault_episodes
WHERE {base} AND start_ts < :start AND (end_ts IS NULL OR end_ts >= :start)
"""

# Итоги часа и дня старта — из базовых событий до старта: значимые события каналов `stream = significant`
# (без газа и движения хакатона — у них полный ряд в сыром потоке) + сырой поток
_SNAPSHOT_AGGREGATE = """
INSERT INTO {table} (run_id, channel_id, {period}, n_events, n_alarm, n_fault, n_num, min, max, mean)
SELECT :run, channel_id, CAST(:period_value AS {period_type}), count(*), count(*) FILTER (WHERE is_alarm),
    count(*) FILTER (WHERE value_raw IN ('Неисправен', 'Отключено устройство')),
    count(value_num), min(value_num), max(value_num), avg(value_num)
FROM (
    SELECT e.channel_id, e.is_alarm, e.value_raw, e.value_num FROM events e JOIN channels c ON c.id = e.channel_id
    WHERE {e_base} AND e.ts >= :from AND e.ts < :start AND c.stream = 'significant'
    UNION ALL
    SELECT channel_id, is_alarm, value_raw, value_num FROM events_raw
    WHERE {base} AND ts >= :from AND ts < :start
) x
GROUP BY channel_id
"""


def _base_sql(sql: str, base: int | None) -> str:
    """Базовая часть снимка: у хакатона — строки `run_id IS NULL`, у сценария на наборе — строки его основы.
    Условие подставляется текстом, а не `IS NOT DISTINCT FROM`: так остаются индексы по `run_id`."""
    cond = "run_id IS NULL" if base is None else "run_id = :base"
    return sql.replace("{bs_base}", f"bs.{cond}").replace("{e_base}", f"e.{cond}").replace("{base}", cond)


async def snapshot_base(session: AsyncSession, run_id: int, start: dt.datetime, base: int | None = None) -> None:
    params = {"run": run_id, "start": start, "base": base}
    await session.execute(text(_base_sql(_SNAPSHOT_STATE_QUIET, base)), params)
    await session.execute(text(_base_sql(_SNAPSHOT_STATE_ACTIVE, base)), params)
    await session.execute(text(_base_sql(_SNAPSHOT_FAULTS, base)), params)
    local = start.astimezone(_MSK)
    hour_start = local.replace(minute=0, second=0, microsecond=0)
    day_start = hour_start.replace(hour=0)
    for table, period, period_type, value, frm in (
        ("channel_hourly", "hour", "timestamptz", hour_start, hour_start),
        ("channel_daily", "date", "date", day_start.date(), day_start),
    ):
        sql = _base_sql(_SNAPSHOT_AGGREGATE, base).format(table=table, period=period, period_type=period_type)
        await session.execute(text(sql), {**params, "period_value": value, "from": frm})


# --- удаление (только командой на сервере, D22) --------------------------------------------------------------

# Порядок — по внешним ключам: заявки → прогнозы (ссылки тревог на прогнозы обнуляются заранее) → тревоги → эпизоды
_RUN_TABLES = (
    "access_incidents",  # открытия удаляются каскадом; до заявок — ссылка work_order_id
    "equipment_episodes",  # каналы эпизодов удаляются каскадом
    "work_orders",
    "predictions",
    "alarms",
    "alarm_episodes",
    "fault_episodes",
    "channel_state",
    "channel_daily",
    "channel_hourly",
    "events_raw",
    "events",
)


# Гипертаблицы: у прогонов внешнего набора строки лежат в чанках хакатона, часто сжатых (журнал набора — за годы
# до 2026). Удаление ограничивается каналами набора — сегменты сжатия идут по channel_id, чужие не распаковываются.
_HYPERTABLES = {"events": "ts", "events_raw": "ts", "channel_hourly": "hour"}


async def delete_run(session: AsyncSession, run_id: int) -> dict[str, int]:
    """Удалить прогон и все его данные. Возвращает число удалённых строк по таблицам."""
    run = await get_run(session, run_id)
    if run is None:
        raise RunError(f"прогон {run_id} не найден")
    dependent = (await session.scalars(select(Run.id).where(Run.base_run_id == run_id).order_by(Run.id))).all()
    if dependent:
        raise RunError(
            f"прогон {run_id} — основа набора, на неё ссылаются прогоны {list(dependent)}: сначала удалить их",
            code="dataset_in_use",
        )
    source = await run_source(session, run)
    channels = None
    if source != HACKATHON:
        channels = list((await session.scalars(text("SELECT id FROM channels WHERE source = :s"), {"s": source})).all())
    await session.execute(text("UPDATE alarms SET prediction_id = NULL WHERE run_id = :run"), {"run": run_id})
    deleted = {}
    for table in _RUN_TABLES:
        sql, params = f"DELETE FROM {table} WHERE run_id = :run", {"run": run_id}
        if channels is not None and table in _HYPERTABLES:
            sql += f" AND channel_id = ANY(:channels) AND {_HYPERTABLES[table]} >= :since"
            params |= {"channels": channels, "since": run.sim_start - dt.timedelta(days=1)}
        deleted[table] = (await session.execute(text(sql), params)).rowcount
    await session.execute(text("DELETE FROM runs WHERE id = :run"), {"run": run_id})
    return deleted
