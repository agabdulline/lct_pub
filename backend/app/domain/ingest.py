"""Приём событий — общий код для шлюза OPC UA и `POST /ingest/events` (docs/architecture/backend.md §2.1).

Контракт для шлюза: `RawEvent` и `ingest_events(run_id, events)`. Раскладка пакета по слоям
(`docs/architecture/data.md` §2–3) — те же правила, что у загрузчика базы:

1. Газ и движение — в `events_raw`; остальное — в `events`; газ из новых сырых событий — в `events` через зону
   нечувствительности. Повторная доставка отсекается уникальными индексами и дальше не идёт.
2. Итоги часа и суток — из всех новых событий, до отсечения.
3. Эпизоды неисправностей, текущее состояние канала.
4. Дымовые тревоги — в ленту и эпизоды объекта, «Рычаг сдернут» — пересмотр эпизодов (6.4).
5. После коммита — события живой ленты и постановка скоринга тревог с дебаунсом по объекту.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import HACKATHON, ChannelDaily, ChannelHourly, Event, EventRaw
from app.db.session import SessionLocal
from app.domain import equipment
from app.domain.access import rules as access_rules
from app.domain.access_runs import enqueue_sync as enqueue_access_sync
from app.domain.access_runs import has_active as access_active
from app.domain.alarm_episodes import register_alarm, register_lever, rules
from app.domain.channels import ChannelInfo, directory
from app.domain.deadband import Deadband
from app.domain.feed import alarms_by_ids, episodes_by_ids
from app.domain.notify import Notification, publish
from app.domain.scoring_queue import enqueue_alarm_scoring, enqueue_external_scoring
from app.domain.status import channel_status
from app.domain.values import is_fault, value_num

_BATCH = 5_000
DEADBAND_LOOKBACK = dt.timedelta(days=30)
_RUN = "run_id IS NOT DISTINCT FROM :run"


@dataclass(frozen=True, slots=True)
class RawEvent:
    """Одно событие источника: строка журнала или склеенные `Value` и `Alarm` узла OPC UA."""

    channel_id: int
    ts: dt.datetime  # с часовым поясом; без пояса считается Europe/Moscow (допущение A1)
    value_raw: str
    is_alarm: bool
    event_id: int | None = None  # ид_события журнала; OPC UA его не передаёт
    # метка записи у источника (ServerTimestamp OPC UA): по ней шлюз относит событие к прогону, приём её не читает
    written: dt.datetime | None = field(default=None, compare=False)


@dataclass(slots=True)
class IngestResult:
    accepted: int = 0  # записано новых событий
    duplicates: int = 0  # уже были в базе или повторились в пакете
    unknown_channels: int = 0  # каналов нет в справочнике — в рабочую базу не пишутся (data.md §2)
    alarms_enqueued: int = 0  # поставлено задач скоринга (по объектам, с дебаунсом)
    alarms: int = 0  # новых дымовых тревог
    notifications: list[Notification] = field(default_factory=list)
    scoring_objects: set[int] = field(default_factory=set)
    external_sources: set[str] = field(default_factory=set)  # наборы новых событий — для событийных моделей
    access_sync: bool = False  # пересчитать вскрытие под охраной (access.md §4): двери, охрана или идущие инциденты
    run_id: int | None = None


@dataclass(slots=True)
class _Row:
    info: ChannelInfo
    event_id: int | None
    ts: dt.datetime
    is_alarm: bool
    value_raw: str
    value_num: float | None

    @property
    def key(self) -> tuple:
        return (self.info.id, self.ts, self.event_id, self.value_raw)

    def values(self, run_id: int | None) -> dict:
        return {
            "run_id": run_id,
            "channel_id": self.info.id,
            "event_id": self.event_id,
            "ts": self.ts,
            "is_alarm": self.is_alarm,
            "value_raw": self.value_raw,
            "value_num": self.value_num,
        }


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=ZoneInfo(settings.timezone))


# Опорные значения зоны нечувствительности по прогону и ширине зоны — в памяти процесса; для каналов, которых ещё
# не видели, берутся из последнего значимого события в представлении прогона.
_deadbands: dict[tuple[int | None, float], Deadband] = {}
_deadband_ready: dict[int | None, set[int]] = defaultdict(set)


async def ingest_events(
    run_id: int | None, events: list[RawEvent], *, session: AsyncSession | None = None
) -> IngestResult:
    """Записать пакет событий базы (`run_id=None`) или прогона. Повторная доставка не создаёт дублей.

    Без `session` функция сама коммитит и затем публикует события ленты и ставит скоринг. С чужой сессией это
    делает вызывающий: `await session.commit()`, затем `await finalize(result)`."""
    if session is None:
        async with SessionLocal() as own:
            result = await ingest_events(run_id, events, session=own)
            await own.commit()
        await finalize(result)
        return result
    result = IngestResult(run_id=run_id)
    if events:
        await _ingest(session, run_id, events, result)
    return result


async def finalize(result: IngestResult) -> IngestResult:
    """После коммита: события живой ленты и скоринг тревог."""
    await publish(result.notifications)
    result.alarms_enqueued = await enqueue_alarm_scoring(result.run_id, result.scoring_objects)
    if result.run_id is not None:
        await enqueue_external_scoring(result.run_id, result.external_sources)
        if result.access_sync:
            await enqueue_access_sync(result.run_id)
    return result


async def _ingest(session: AsyncSession, run_id: int | None, events: list[RawEvent], result: IngestResult) -> None:
    infos = await directory.get_many(session, {e.channel_id for e in events})
    rows: dict[tuple, _Row] = {}
    for e in events:
        info = infos.get(e.channel_id)
        if info is None:
            result.unknown_channels += 1
            continue
        row = _Row(info, e.event_id, _aware(e.ts), e.is_alarm, e.value_raw, value_num(e.value_raw, info.sensor_type))
        rows.setdefault(row.key, row)
    ordered = sorted(rows.values(), key=lambda r: (r.ts, r.info.id, r.event_id or 0))

    # маршрут — из справочника канала (D31): сырой поток, значимые события и значимые изменения сырого канала с зоной
    raw = [r for r in ordered if r.info.stream == "raw"]
    significant = [r for r in ordered if r.info.stream != "raw"]
    new_raw = await _insert_new(session, EventRaw, raw, run_id)
    new_significant = await _insert_new(session, Event, significant, run_id)
    banded = [r for r in new_raw if r.info.deadband is not None]
    await _insert_new(session, Event, await _deadband_keep(session, run_id, banded), run_id)

    new = sorted(new_raw + new_significant, key=lambda r: (r.ts, r.info.id, r.event_id or 0))
    result.accepted = len(new)
    result.external_sources = {r.info.source for r in new if r.info.source != HACKATHON}
    if run_id is not None and new and not result.external_sources:
        watched = access_rules().watched_types
        result.access_sync = any(r.info.sensor_type in watched for r in new) or await access_active(session, run_id)
    result.duplicates = len(events) - result.unknown_channels - len(new)
    if not new:
        return

    channel_ids = sorted({r.info.id for r in new})
    before = await _statuses(session, run_id, channel_ids)
    await _aggregates(session, run_id, new)
    fault_since = await _fault_episodes(session, run_id, new)
    await _channel_state(session, run_id, new, fault_since)
    changed_channels = await _alarms(session, run_id, new, result)
    # эпизоды оборудования: массовый уход не дымовых каналов объекта в тревожное состояние (equipment.py)
    changes = equipment.transitions(new, {cid: b["value_raw"] for cid, b in before.items()})
    equipment_notes = await equipment.apply_transitions(session, run_id, changes)
    if run_id is not None:
        await session.execute(
            text(
                "UPDATE runs SET sim_now = greatest(coalesce(sim_now, :t), :t), "
                "events_until = greatest(coalesce(events_until, :t), :t), events = events + :n WHERE id = :run"
            ),
            # новые строки events_raw и значимых events — счётчик прогона для /runs (models.Run.events)
            {"run": run_id, "t": new[-1].ts, "n": len(new)},
        )

    touched = sorted(set(channel_ids) | changed_channels)
    after = await _statuses(session, run_id, touched)
    for channel_id in touched:
        prev, cur = before.get(channel_id), after.get(channel_id)
        if cur is None or (prev is not None and prev["status"] == cur["status"]):
            continue
        info = directory.channels[channel_id]
        result.notifications.append(
            Notification(
                run_id,
                "channel.status",
                {
                    "run_id": run_id,
                    "channel_id": channel_id,
                    "sub_object_id": info.sub_object_id,
                    "object_id": info.object_id,
                    "status": cur["status"],
                    "prev_status": prev["status"] if prev else None,
                    "value": cur["value_raw"],
                    "risk": None,
                    "ts": cur["ts"],
                },
            )
        )
    result.notifications += equipment_notes


async def _insert_new(session: AsyncSession, model, rows: list[_Row], run_id: int | None) -> list[_Row]:
    """INSERT … ON CONFLICT DO NOTHING; возвращает только действительно новые строки."""
    new: list[_Row] = []
    for start in range(0, len(rows), _BATCH):
        batch = rows[start : start + _BATCH]
        stmt = (
            insert(model)
            .values([r.values(run_id) for r in batch])
            .on_conflict_do_nothing()
            .returning(model.channel_id, model.ts, model.event_id, model.value_raw)
        )
        inserted = {tuple(k) for k in (await session.execute(stmt)).all()}
        new += [r for r in batch if r.key in inserted]
    return new


async def _deadband_keep(session: AsyncSession, run_id: int | None, gas: list[_Row]) -> list[_Row]:
    """Значимые изменения сырых каналов с зоной нечувствительности — у каждого канала своя ширина."""
    if not gas:
        return []
    ready = _deadband_ready[run_id]
    width = {r.info.id: r.info.deadband for r in gas}
    unknown = sorted(width.keys() - ready)
    if unknown:
        # По каналу — спуск по индексу (channel_id, ts) до последнего события за 30 суток: окно отсекает старые
        # чанки уже при планировании. Значимое показание газа пишется в среднем раз в ~8 ч; если за окно его нет,
        # опорного значения нет и следующее показание просто пишется.
        before = gas[0].ts
        last = await session.execute(
            text(
                "SELECT c.id, l.value_num FROM unnest(CAST(:ids AS integer[])) AS c(id) CROSS JOIN LATERAL ("
                "SELECT value_num FROM run_events(:run) e WHERE e.channel_id = c.id AND e.ts < :before "
                "AND e.ts >= :since ORDER BY e.ts DESC LIMIT 1) l"
            ),
            {"run": run_id, "ids": unknown, "before": before, "since": before - DEADBAND_LOOKBACK},
        )
        for channel_id, value in last:
            _deadband(run_id, width[channel_id]).reference[channel_id] = value
        ready.update(unknown)
    return [r for r in gas if _deadband(run_id, r.info.deadband).keep(r.info.id, r.value_num)]


def _deadband(run_id: int | None, width: float) -> Deadband:
    return _deadbands.setdefault((run_id, width), Deadband(width))


def _group_aggregates(rows: list[_Row], period) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r.info.id, period(r.ts))
        g = groups.setdefault(key, {"n_events": 0, "n_alarm": 0, "n_fault": 0, "nums": []})
        g["n_events"] += 1
        g["n_alarm"] += r.is_alarm
        g["n_fault"] += is_fault(r.value_raw)
        if r.value_num is not None:
            g["nums"].append(r.value_num)
    out = []
    for (channel_id, p), g in groups.items():
        nums = g.pop("nums")
        out.append(
            {
                "channel_id": channel_id,
                "period": p,
                **g,
                "n_num": len(nums),
                "min": min(nums) if nums else None,
                "max": max(nums) if nums else None,
                "mean": sum(nums) / len(nums) if nums else None,
            }
        )
    return out


async def _aggregates(session: AsyncSession, run_id: int | None, rows: list[_Row]) -> None:
    msk = ZoneInfo(settings.timezone)
    for model, column, period in (
        (ChannelHourly, "hour", lambda ts: ts.astimezone(msk).replace(minute=0, second=0, microsecond=0)),
        (ChannelDaily, "date", lambda ts: ts.astimezone(msk).date()),
    ):
        values = []
        for g in _group_aggregates(rows, period):
            g[column] = g.pop("period")
            values.append({"run_id": run_id, **g})
        stmt = insert(model).values(values)
        t, ex = model.__table__.c, stmt.excluded
        n_num = t.n_num + ex.n_num
        stmt = stmt.on_conflict_do_update(
            index_elements=["channel_id", column, "run_id"],
            set_={
                "n_events": t.n_events + ex.n_events,
                "n_alarm": t.n_alarm + ex.n_alarm,
                "n_fault": t.n_fault + ex.n_fault,
                "n_num": n_num,
                "min": func.least(t.min, ex.min),
                "max": func.greatest(t.max, ex.max),
                "mean": case(
                    (n_num == 0, None),
                    else_=(func.coalesce(t.mean, 0) * t.n_num + func.coalesce(ex.mean, 0) * ex.n_num) / n_num,
                ),
            },
        )
        await session.execute(stmt)


async def _fault_episodes(session: AsyncSession, run_id: int | None, rows: list[_Row]) -> dict[int, dt.datetime]:
    """Открыть и закрыть эпизоды неисправностей по порядку событий. Возвращает начало открытых эпизодов
    по каналам пакета (для `channel_state.fault_since`)."""
    ids = sorted({r.info.id for r in rows})
    open_rows = await session.execute(
        text(
            f"SELECT channel_id, id, start_ts FROM fault_episodes WHERE {_RUN} AND end_ts IS NULL "
            "AND channel_id = ANY(:ids)"
        ),
        {"run": run_id, "ids": ids},
    )
    opened: dict[int, tuple[int, dt.datetime]] = {ch: (ep, start) for ch, ep, start in open_rows}
    for r in rows:
        current = opened.get(r.info.id)
        if is_fault(r.value_raw) and current is None:
            # тот же старт в ту же секунду — дребезг: эпизод открывается заново, как склейка в загрузчике
            episode_id = await session.scalar(
                text(
                    "INSERT INTO fault_episodes (run_id, channel_id, start_ts, state) VALUES (:run, :ch, :ts, :state) "
                    "ON CONFLICT (channel_id, start_ts, run_id) DO UPDATE SET end_ts = NULL RETURNING id"
                ),
                {"run": run_id, "ch": r.info.id, "ts": r.ts, "state": r.value_raw},
            )
            opened[r.info.id] = (episode_id, r.ts)
        elif not is_fault(r.value_raw) and current is not None:
            await session.execute(
                text("UPDATE fault_episodes SET end_ts = :ts WHERE id = :id"), {"ts": r.ts, "id": current[0]}
            )
            del opened[r.info.id]
    return {ch: start for ch, (_, start) in opened.items()}


async def _channel_state(
    session: AsyncSession, run_id: int | None, rows: list[_Row], fault_since: dict[int, dt.datetime]
) -> None:
    last: dict[int, _Row] = {}
    last_alarm: dict[int, dt.datetime] = {}
    for r in rows:
        last[r.info.id] = r
        if r.is_alarm:
            last_alarm[r.info.id] = r.ts
    for channel_id, r in last.items():
        await session.execute(
            text(
                """
                INSERT INTO channel_state AS s (run_id, channel_id, ts, value_raw, value_num, is_alarm,
                    last_alarm_at, fault_since)
                VALUES (:run, :ch, :ts, :value_raw, :value_num, :is_alarm, :last_alarm, :fault_since)
                ON CONFLICT (run_id, channel_id) DO UPDATE SET
                    ts = EXCLUDED.ts, value_raw = EXCLUDED.value_raw, value_num = EXCLUDED.value_num,
                    is_alarm = EXCLUDED.is_alarm, fault_since = EXCLUDED.fault_since,
                    last_alarm_at = greatest(s.last_alarm_at, EXCLUDED.last_alarm_at)
                WHERE EXCLUDED.ts >= s.ts
                """
            ),
            {
                "run": run_id,
                "ch": channel_id,
                "ts": r.ts,
                "value_raw": r.value_raw,
                "value_num": r.value_num,
                "is_alarm": r.is_alarm,
                "last_alarm": last_alarm.get(channel_id),
                "fault_since": fault_since.get(channel_id),
            },
        )


async def _alarms(session: AsyncSession, run_id: int | None, rows: list[_Row], result: IngestResult) -> set[int]:
    """Тревоги и рычаги — в ленту и эпизоды. Возвращает каналы, чей статус мог смениться из-за ППР."""
    r_ = rules()
    new_alarms: list[int] = []
    refined: set[int] = set()
    episodes: set[int] = set()
    for r in rows:
        if r.info.sensor_type == r_.alarm_sensor_type and r.value_raw == r_.alarm_value:
            change = await register_alarm(
                session, run_id, r.info.id, r.info.object_id, r.ts, directory.lever_channels.get(r.info.object_id, [])
            )
            if change is None:
                continue
            new_alarms.append(change.alarm_id)
            refined.update(change.refined_alarm_ids)
            episodes.add(change.episode_id)
            result.scoring_objects.add(r.info.object_id)
        elif r.value_raw == r_.lever_value:
            for episode_id, alarm_ids in await register_lever(session, run_id, r.info.object_id, r.ts):
                episodes.add(episode_id)
                refined.update(alarm_ids)
    result.alarms = len(new_alarms)
    if not new_alarms and not episodes:
        return set()

    now = rows[-1].ts
    for alarm in await alarms_by_ids(session, run_id, new_alarms, now):
        result.notifications.append(Notification(run_id, "alarm.created", alarm))
    for alarm in await alarms_by_ids(session, run_id, sorted(refined - set(new_alarms)), now):
        result.notifications.append(Notification(run_id, "alarm.scored", alarm))
    for episode in await episodes_by_ids(session, sorted(episodes)):
        result.notifications.append(Notification(run_id, "alarm_episode.updated", episode))
    if not refined:
        return set()
    channels = await session.scalars(
        text("SELECT DISTINCT channel_id FROM alarms WHERE id = ANY(:ids)"), {"ids": sorted(refined)}
    )
    return set(channels)


async def _statuses(session: AsyncSession, run_id: int | None, channel_ids: list[int]) -> dict[int, dict]:
    """Статус каналов прогона по текущему состоянию и виду эпизода последней тревоги."""
    rows = await session.execute(
        text(
            f"""
            SELECT s.channel_id, c.sensor_type, s.value_raw, s.is_alarm, s.ts,
                (SELECT e.kind FROM alarms a JOIN alarm_episodes e ON e.id = a.episode_id
                 WHERE a.channel_id = s.channel_id AND a.{_RUN} ORDER BY a.ts DESC LIMIT 1) AS alarm_kind
            FROM channel_state s JOIN channels c ON c.id = s.channel_id
            WHERE s.{_RUN} AND s.channel_id = ANY(:ids)
            """
        ),
        {"run": run_id, "ids": channel_ids},
    )
    return {
        r.channel_id: {
            "status": channel_status(r.sensor_type, r.value_raw, r.is_alarm, r.alarm_kind),
            "value_raw": r.value_raw,
            "ts": r.ts,
        }
        for r in rows
    }
