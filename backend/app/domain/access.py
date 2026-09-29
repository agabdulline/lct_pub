"""Вскрытие под охраной (этап 14.3, docs/architecture/access.md): правило по состоянию охраны подобъекта.

Движок — чистая функция `evaluate` над потоком событий одного подобъекта: переходы охраны, открытия и закрытия
дверей и люков. Отметки «движение» и «повторяется» ставятся после — `motion_window`, `mark_repeated`: движения в
журнале миллионы событий, его проверяют запросом по окнам готовых инцидентов. Один и тот же код считает инциденты
прогона (такт, `app/domain/access_runs.py`) и базовой истории (`python -m app.cli access backfill` по архиву). Всё —
только по времени события, без `ид_события`: в живом потоке его нет, шлюз отбрасывает миллисекунды (access.md §3,
README эталона).

- Состояние охраны на момент открытия — по переходам строго раньше его секунды. Секунда, где пришли и «На охране»,
  и «Снято с охраны», состояния не меняет и снятием не считается. Начало охраны — первое «На охране» после снятия.
- В одной секунде у канала, открытого к её началу, сначала закрытие, потом открытие: закрыли и снова открыли, дверь
  открыта. У закрытого — сначала открытие: открыли и закрыли в ту же секунду. Переходы охраны и её исправность — после
  дверей и люков.
- Волна переопроса (5 и больше каналов подобъекта в ±2 с) считается снаружи и приходит готовым числом каналов.
"""

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
from itertools import groupby
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config" / "triage.yaml"
MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class AccessRules:
    guard_type: str
    door_types: tuple[str, ...]
    hatch_types: tuple[str, ...]
    gas_type: str
    opened: str
    closed: str
    armed: str
    disarmed: str
    faulty: str
    healthy: str
    motion: str
    motion_type: str
    disarm_grace: dt.timedelta
    long_armed: dt.timedelta
    episode_gap: dt.timedelta
    burst_channels: int
    burst_window: dt.timedelta
    motion_after: dt.timedelta
    night_hours: tuple[int, int]
    weekend_days: tuple[int, ...]
    repeat_incidents: int
    repeat_window: dt.timedelta

    @property
    def opening_types(self) -> tuple[str, ...]:
        return (*self.door_types, *self.hatch_types)

    @property
    def watched_types(self) -> frozenset[str]:
        """Типы каналов, чьи события двигают правило: двери, люки, охрана."""
        return frozenset((*self.opening_types, self.guard_type))

    @classmethod
    def from_dict(cls, d: dict) -> "AccessRules":
        a = d["access"]
        return cls(
            guard_type=a["guard_type"],
            door_types=tuple(a["door_types"]),
            hatch_types=tuple(a["hatch_types"]),
            gas_type=a["gas_type"],
            opened=a["opened"],
            closed=a["closed"],
            armed=a["armed"],
            disarmed=a["disarmed"],
            faulty=a["faulty"],
            healthy=a["healthy"],
            motion=a["motion"],
            motion_type=a["motion_type"],
            disarm_grace=dt.timedelta(minutes=a["disarm_grace_minutes"]),
            long_armed=dt.timedelta(days=a["long_armed_days"]),
            episode_gap=dt.timedelta(minutes=a["episode_gap_minutes"]),
            burst_channels=a["burst_channels"],
            burst_window=dt.timedelta(seconds=a["burst_seconds"]),
            motion_after=dt.timedelta(minutes=a["motion_after_minutes"]),
            night_hours=tuple(a["night_hours"]),
            weekend_days=tuple(a["weekend_days"]),
            repeat_incidents=a["repeat_incidents"],
            repeat_window=dt.timedelta(days=a["repeat_days"]),
        )


@cache
def rules(path: Path = CONFIG) -> AccessRules:
    return AccessRules.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class SubObject:
    """Подобъект с каналом охраны: его двери и люки (канал → (название, люк ли))."""

    id: int
    object_id: int
    name: str
    guard_channel_id: int
    openers: dict[int, tuple[str, bool]]


class Event(NamedTuple):
    ts: dt.datetime  # с поясом, секунда
    channel_id: int
    value: str
    is_alarm: bool = False


@dataclass
class Opening:
    channel_id: int
    ts: dt.datetime
    is_alarm: bool
    closed_ts: dt.datetime | None = None


@dataclass
class Incident:
    sub_object_id: int
    object_id: int
    guard_channel_id: int
    start_ts: dt.datetime
    armed_since: dt.datetime | None
    guard_faulty: bool
    status: str = "candidate"  # candidate → open | dismissed; open → closed
    qualified_ts: dt.datetime | None = None
    end_ts: dt.datetime | None = None
    end_reason: str | None = None  # disarmed, quiet, run_end
    disarmed_ts: dt.datetime | None = None
    openings: list[Opening] = field(default_factory=list)
    open_channels: set[int] = field(default_factory=set)
    last_close: dt.datetime | None = None
    hatch_openings: int = 0
    off_hours: bool = False
    motion: bool = False
    repeated: bool = False

    @property
    def last_open(self) -> dt.datetime:
        return self.openings[-1].ts

    @property
    def active(self) -> bool:
        return self.status in ("candidate", "open")

    @property
    def priority(self) -> str | None:
        if self.status in ("candidate", "dismissed"):
            return None
        return "high" if self.hatch_openings or self.off_hours else "medium"

    def channels(self) -> list[int]:
        return list(dict.fromkeys(o.channel_id for o in self.openings))


@dataclass
class Result:
    incidents: list[Incident]
    skipped: list[tuple[str, dt.datetime]]  # открытия под охраной, отброшенные правилом: burst, long_armed


def off_hours(ts: dt.datetime, r: AccessRules) -> bool:
    local = ts.astimezone(MSK)
    night_from, night_to = r.night_hours
    return local.isoweekday() in r.weekend_days or local.hour >= night_from or local.hour < night_to


def evaluate(
    sub: SubObject,
    events: Iterable[Event],
    burst: Callable[[int, dt.datetime], int],
    until: dt.datetime,
    r: AccessRules,
    since: dt.datetime | None = None,
) -> Result:
    """Инциденты подобъекта по потоку до водяного знака `until` включительно.

    `events` — переходы и исправность охраны (канал охраны), открытия и закрытия дверей и люков; остальное
    пропускается, сортировка не нужна. `burst(канал, T)` — число каналов подобъекта с событием в ±2 с от открытия.
    Открытия раньше `since` не рассматриваются: так прогон начинает с состояния охраны из базы, а инциденты считает по
    своим событиям. Конец прогона (`run_end`) ставит вызывающий — `finish_run`."""
    grace, gap = r.disarm_grace, r.episode_gap
    armed: bool | None = None
    armed_since: dt.datetime | None = None
    faulty = False
    cur: Incident | None = None
    done: list[Incident] = []
    skipped: list[tuple[str, dt.datetime]] = []

    def quiet_end(inc: Incident) -> dt.datetime | None:
        if inc.open_channels:
            return None
        end = inc.last_open + gap
        return max(end, inc.last_close) if inc.last_close else end

    def advance(now: dt.datetime, inclusive: bool) -> None:
        """Сроки до секунды `now`: перевод кандидата (снятие в [start, start + 5 мин] включительно ещё отменяет его)
        и тишина."""
        nonlocal cur
        if cur is None:
            return
        if cur.status == "candidate":
            due = cur.start_ts + grace
            if due < now or (inclusive and due <= now):
                cur.status, cur.qualified_ts = "open", due
        # открытие в секунду конца тишины ещё продолжает инцидент: тишина кончилась, если её момент раньше `now`
        if cur.status == "open" and (end := quiet_end(cur)) is not None and (end < now or (inclusive and end <= now)):
            cur.status, cur.end_ts, cur.end_reason = "closed", end, "quiet"
            done.append(cur)
            cur = None

    door_open: dict[int, bool] = {}  # дверь или люк открыт по последнему событию — порядок в одной секунде

    def rank(e: Event) -> tuple[int, int]:
        if e.channel_id == sub.guard_channel_id:
            return 2, e.channel_id
        first = r.closed if door_open.get(e.channel_id) else r.opened
        return (0 if e.value == first else 1), e.channel_id

    stream = sorted((e for e in events if e.ts <= until), key=lambda e: e.ts)
    for second, group in groupby(stream, key=lambda e: e.ts):
        group = sorted(group, key=rank)
        advance(second, inclusive=False)
        guard_values = set()
        for e in group:
            if e.channel_id == sub.guard_channel_id:
                guard_values.add(e.value)
                continue
            if e.channel_id not in sub.openers:
                continue
            if e.value in (r.opened, r.closed):
                door_open[e.channel_id] = e.value == r.opened
            if e.value == r.opened:
                if (since is not None and e.ts < since) or armed is not True:
                    continue
                if burst(e.channel_id, e.ts) >= r.burst_channels:
                    skipped.append(("burst", e.ts))
                    continue
                if cur is None:
                    if armed_since is None or e.ts - armed_since >= r.long_armed:
                        skipped.append(("long_armed", e.ts))
                        continue
                    cur = Incident(sub.id, sub.object_id, sub.guard_channel_id, e.ts, armed_since, faulty)
                    cur.off_hours = off_hours(e.ts, r)
                cur.openings.append(Opening(e.channel_id, e.ts, e.is_alarm))
                cur.open_channels.add(e.channel_id)
                if sub.openers[e.channel_id][1]:
                    cur.hatch_openings += 1
            elif e.value == r.closed and cur is not None and e.channel_id in cur.open_channels:
                cur.open_channels.discard(e.channel_id)
                cur.last_close = e.ts
                for o in reversed(cur.openings):
                    if o.channel_id == e.channel_id:
                        o.closed_ts = o.closed_ts or e.ts
                        break
        if r.armed in guard_values and r.disarmed in guard_values:
            pass  # повторная передача: состояние не меняется, снятием не считается
        elif r.disarmed in guard_values:
            if cur is not None:
                cur.status = "dismissed" if cur.status == "candidate" else "closed"
                cur.end_ts = cur.disarmed_ts = second
                cur.end_reason = "disarmed"
                done.append(cur)
                cur = None
            armed = False
        elif r.armed in guard_values:
            if armed is not True:
                armed_since = second
            armed = True
        if r.faulty in guard_values:
            faulty = True
        elif r.healthy in guard_values:
            faulty = False
    advance(until, inclusive=True)
    return Result(done + ([cur] if cur is not None else []), skipped)


def motion_window(inc: Incident, r: AccessRules) -> tuple[dt.datetime, dt.datetime]:
    """Окно отметки «движение» — `[start, последнее открытие + 5 мин]`, включительно; движение — «Обнаружено
    движение» с каналов типа «Датчик движения» подобъекта (у дверей бывает то же значение — не в счёт)."""
    return inc.start_ts, inc.last_open + r.motion_after


def mark_repeated(incidents: list[Incident], prior_starts: Iterable[dt.datetime], r: AccessRules) -> None:
    """«Повторяется» — у подобъекта 3 и больше инцидентов (не кандидатов и не «снято после открытия») со стартом
    в `(start − 7 сут, start]`. `prior_starts` — старты инцидентов подобъекта до потока: база до старта прогона."""
    starts = sorted([*prior_starts, *(i.start_ts for i in incidents if i.status in ("open", "closed"))])
    for inc in incidents:
        lo = inc.start_ts - r.repeat_window
        inc.repeated = sum(1 for s in starts if lo < s <= inc.start_ts) >= r.repeat_incidents


def finish_run(incidents: list[Incident], end: dt.datetime) -> list[Incident]:
    """Конец прогона: открытый инцидент закрывается с `run_end`, кандидат остаётся кандидатом (access.md §4)."""
    for inc in incidents:
        if inc.status == "open":
            inc.status, inc.end_ts, inc.end_reason = "closed", end, "run_end"
    return incidents
