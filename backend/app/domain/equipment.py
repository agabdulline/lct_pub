"""Эпизоды оборудования в ленте (docs/architecture/backend.md §6.2).

Эпизод — на одном объекте (уровень 2) за окно (`window_minutes`, 5 мин) не меньше `min_channels` (5) не дымовых
каналов ушли в тревожное состояние: «Обесточен», «Неисправен», «Отключено устройство», у насоса ещё «Затоплен», у ИБП —
питание от батарей и неисправность батареи (`backend/config/triage.yaml`, раздел `equipment`). Одиночные
неисправности в ленту не идут — они в заявках `fault_persisting`. Вид — `power_outage`, если больше половины каналов
ушли в «Обесточен», иначе `equipment_fault`. Эпизод закрыт (`end_ts`), когда в норму вернулась доля каналов
`end_share` (половина): после обесточивания часть фаз может оставаться «Обесточен» сутками. Если в `merge_minutes`
после закрытия тот же объект снова набрал порог, это дребезг, а не новое событие: эпизод открывается снова,
новые каналы добавляются к нему. Вердикта модели нет — это информационная строка ленты.

Приём событий (`app/domain/ingest.py`) передаёт сюда переходы каналов пакета: в тревожное состояние (начало) и из
него (возврат). Пока порог не набран, эпизод — кандидат: начала старше окна из него выпадают.
"""

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import try_lock
from app.domain.channels import ChannelInfo
from app.domain.notify import Notification

CONFIG = Path(__file__).resolve().parents[2] / "config" / "triage.yaml"
POWER_OUTAGE, EQUIPMENT_FAULT = "power_outage", "equipment_fault"


@dataclass(frozen=True)
class EquipmentRules:
    min_channels: int
    window: dt.timedelta
    end_share: float
    merge_window: dt.timedelta
    states: frozenset[str]
    by_type: dict[str, frozenset[str]]
    power_state: str
    exclude_types: frozenset[str]
    sse_seconds: int

    def abnormal(self, sensor_type: str, value: str | None) -> bool:
        if value is None or sensor_type in self.exclude_types:
            return False
        return value in self.states or value in self.by_type.get(sensor_type, frozenset())

    def sql_params(self) -> dict:
        """Параметры SQL-условия «состояние тревожное»: общие состояния и пары (тип, значение)."""
        pairs = [(t, v) for t, values in self.by_type.items() for v in sorted(values)]
        return {
            "bad_states": sorted(self.states),
            "bad_types": [t for t, _ in pairs],
            "bad_values": [v for _, v in pairs],
            "excluded": sorted(self.exclude_types),
        }


# Условие «текущее состояние канала тревожное» — для `active_channels`; параметры — `EquipmentRules.sql_params`
SQL_ABNORMAL = """
(c.sensor_type <> ALL(CAST(:excluded AS text[])) AND (s.value_raw = ANY(CAST(:bad_states AS text[]))
 OR (c.sensor_type, s.value_raw) IN (
     SELECT * FROM unnest(CAST(:bad_types AS text[]), CAST(:bad_values AS text[])))))
"""


@cache
def rules(path: Path = CONFIG) -> EquipmentRules:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))["equipment"]
    return EquipmentRules(
        min_channels=d["min_channels"],
        window=dt.timedelta(minutes=d["window_minutes"]),
        end_share=d["end_share"],
        merge_window=dt.timedelta(minutes=d["merge_minutes"]),
        states=frozenset(d["states"]),
        by_type={t: frozenset(v) for t, v in (d.get("by_type") or {}).items()},
        power_state=d["power_state"],
        exclude_types=frozenset(d.get("exclude_types") or []),
        sse_seconds=d.get("sse_seconds", 2),
    )


@dataclass(frozen=True, slots=True)
class Transition:
    info: ChannelInfo
    ts: dt.datetime
    value: str
    onset: bool  # True — ушёл в тревожное, False — вернулся в норму


def transitions(rows, before: dict[int, str | None], r: EquipmentRules | None = None) -> list[Transition]:
    """Переходы каналов пакета в тревожное состояние и обратно, по времени. `rows` — новые события пакета
    по порядку (`info`, `ts`, `value_raw`), `before` — значение канала до пакета. Если в ту же секунду начала
    канал перешёл и в «Обесточен», началом считается «Обесточен»: у насосов и вентиляторов «Неисправен» приходит
    вместе с ним."""
    r = r or rules()
    bad: dict[int, bool] = {}  # состояние канала — по значению до пакета, затем по его событиям
    out: list[Transition] = []
    last_onset: dict[int, int] = {}
    for row in rows:
        info = row.info
        if info.sensor_type in r.exclude_types:
            continue
        if info.id not in bad:
            bad[info.id] = r.abnormal(info.sensor_type, before.get(info.id))
        now_bad = r.abnormal(info.sensor_type, row.value_raw)
        if now_bad and not bad[info.id]:
            last_onset[info.id] = len(out)
            out.append(Transition(info, row.ts, row.value_raw, True))
        elif now_bad and row.value_raw == r.power_state and info.id in last_onset:
            i = last_onset[info.id]
            if out[i].ts == row.ts and out[i].value != r.power_state:
                out[i] = Transition(info, row.ts, row.value_raw, True)
        elif not now_bad and bad[info.id]:
            last_onset.pop(info.id, None)
            out.append(Transition(info, row.ts, row.value_raw, False))
        bad[info.id] = now_bad
    return out


@dataclass
class _Episode:
    id: int | None
    object_id: int
    qualified: bool
    start_ts: dt.datetime
    last_onset_ts: dt.datetime
    end_ts: dt.datetime | None
    channels: dict[int, list] = field(default_factory=dict)  # channel_id → [onset_ts, value, returned_ts]
    removed: set[int] = field(default_factory=set)
    changed: bool = False
    created: bool = False  # набрал порог в этом пакете
    closed: bool = False  # закрыт в этом пакете
    reopened: bool = False  # открыт снова в этом пакете — дребезг после закрытия

    def kind_value(self, r: EquipmentRules) -> tuple[str, str]:
        values = Counter(v[1] for v in self.channels.values())
        value = values.most_common(1)[0][0] if values else r.power_state
        kind = POWER_OUTAGE if values[r.power_state] * 2 > len(self.channels) else EQUIPMENT_FAULT
        return kind, value


_RUN = "run_id IS NOT DISTINCT FROM :run"


async def _load(
    session: AsyncSession, run_id: int | None, objects: list[int], closed_since: dt.datetime
) -> dict[int, list[_Episode]]:
    """Открытые эпизоды и кандидаты объектов с каналами, а также закрытые не раньше `closed_since` — к ним
    может вернуться дребезг."""
    rows = await session.execute(
        text(
            f"SELECT id, object_id, qualified, start_ts, last_onset_ts, end_ts FROM equipment_episodes "
            f"WHERE {_RUN} AND object_id = ANY(:objects) AND (end_ts IS NULL OR end_ts >= :closed_since) "
            f"ORDER BY start_ts"
        ),
        {"run": run_id, "objects": objects, "closed_since": closed_since},
    )
    episodes = {r.id: _Episode(r.id, r.object_id, r.qualified, r.start_ts, r.last_onset_ts, r.end_ts) for r in rows}
    if episodes:
        chs = await session.execute(
            text(
                "SELECT episode_id, channel_id, onset_ts, value, returned_ts FROM equipment_episode_channels "
                "WHERE episode_id = ANY(:ids)"
            ),
            {"ids": list(episodes)},
        )
        for c in chs:
            episodes[c.episode_id].channels[c.channel_id] = [c.onset_ts, c.value, c.returned_ts]
    by_object: dict[int, list[_Episode]] = defaultdict(list)
    for e in episodes.values():
        by_object[e.object_id].append(e)
    return by_object


def _apply(episodes: list[_Episode], t: Transition, r: EquipmentRules) -> None:
    """Переход канала в эпизодах объекта: открытые эпизоды набирают каналы и возвраты, кандидат — окно начал."""
    cid = t.info.id
    if not t.onset:
        for e in episodes:
            ch = e.channels.get(cid)
            if e.qualified and e.end_ts is None and ch is not None and ch[2] is None:
                ch[2] = t.ts
                e.changed = True
                returned = sum(1 for c in e.channels.values() if c[2] is not None)
                if returned >= r.end_share * len(e.channels):
                    e.end_ts, e.closed = t.ts, True
            elif e.qualified and ch is not None and ch[2] is None and t.ts >= ch[0]:
                ch[2] = t.ts  # закрытый: в базу пишет `_closed_returns`, здесь — на случай, если откроется снова
        return
    active = [e for e in episodes if e.qualified and e.end_ts is None and t.ts - e.last_onset_ts <= r.window]
    target = active[-1] if active else next((e for e in episodes if not e.qualified), None)
    if target is None or (not target.qualified and t.ts - target.last_onset_ts > r.window):
        if target is not None:  # кандидат устарел целиком — начинаем заново
            target.removed |= set(target.channels)
            target.channels.clear()
            target.start_ts = t.ts
        else:
            target = _Episode(None, t.info.object_id, False, t.ts, t.ts, None)
            episodes.append(target)
    ch = target.channels.get(cid)
    if ch is None:
        target.channels[cid] = [t.ts, t.value, None]
        target.removed.discard(cid)
    elif ch[2] is not None:  # вернулся и снова ушёл — в эпизоде по первому началу
        ch[2] = None
    elif not target.qualified and ch[0] < t.ts - r.window:  # у кандидата прежнее начало выпало из окна — новое
        ch[0], ch[1] = t.ts, t.value
    target.last_onset_ts = max(target.last_onset_ts, t.ts)
    target.changed = True
    if not target.qualified:
        stale = [c for c, v in target.channels.items() if v[0] < t.ts - r.window]
        for c in stale:
            del target.channels[c]
            target.removed.add(c)
        target.start_ts = min(v[0] for v in target.channels.values())
        if len(target.channels) >= r.min_channels:
            prev = _recently_closed(episodes, target.start_ts, r)
            if prev is None:
                target.qualified, target.created = True, True
            else:
                _reopen(prev, target)


def _recently_closed(episodes: list[_Episode], start: dt.datetime, r: EquipmentRules) -> _Episode | None:
    """Последний закрытый эпизод объекта, если кандидат с началом `start` набрал порог в `merge_window` после
    его закрытия."""
    closed = [e for e in episodes if e.qualified and e.end_ts is not None and start - e.end_ts <= r.merge_window]
    return max(closed, key=lambda e: e.end_ts, default=None)


def _reopen(e: _Episode, candidate: _Episode) -> None:
    """Дребезг: кандидат — продолжение закрытого эпизода. Эпизод открыт снова, каналы кандидата переходят в него;
    вернувшиеся и снова ушедшие — по первому началу, как внутри открытого эпизода."""
    for cid, (onset, value, _) in candidate.channels.items():
        ch = e.channels.get(cid)
        if ch is None:
            e.channels[cid] = [onset, value, None]
        else:
            ch[2] = None
    e.end_ts, e.closed, e.reopened, e.changed = None, False, True, True
    e.last_onset_ts = max(e.last_onset_ts, candidate.last_onset_ts)
    candidate.removed |= set(candidate.channels)
    candidate.channels.clear()


async def _save(session: AsyncSession, run_id: int | None, e: _Episode, r: EquipmentRules) -> None:
    kind, value = e.kind_value(r)
    returned = sum(1 for c in e.channels.values() if c[2] is not None)
    params = {
        "run": run_id,
        "object": e.object_id,
        "kind": kind,
        "value": value,
        "qualified": e.qualified,
        "start": e.start_ts,
        "last": e.last_onset_ts,
        "end": e.end_ts,
        "n": len(e.channels),
        "returned": returned,
    }
    if e.id is None:
        e.id = await session.scalar(
            text(
                "INSERT INTO equipment_episodes (run_id, object_id, kind, value, qualified, start_ts, last_onset_ts, "
                "end_ts, n_channels, n_returned) VALUES (:run, :object, :kind, :value, :qualified, :start, :last, "
                ":end, :n, :returned) RETURNING id"
            ),
            params,
        )
    else:
        await session.execute(
            text(
                "UPDATE equipment_episodes SET kind = :kind, value = :value, qualified = :qualified, "
                "start_ts = :start, last_onset_ts = :last, end_ts = :end, n_channels = :n, n_returned = :returned, "
                "updated_at = now() WHERE id = :id"
            ),
            {**params, "id": e.id},
        )
    if e.removed:
        await session.execute(
            text("DELETE FROM equipment_episode_channels WHERE episode_id = :id AND channel_id = ANY(:ids)"),
            {"id": e.id, "ids": sorted(e.removed)},
        )
    if e.channels:
        await session.execute(
            text(
                "INSERT INTO equipment_episode_channels (episode_id, channel_id, onset_ts, value, returned_ts) "
                "SELECT :id, * FROM unnest(CAST(:ch AS integer[]), CAST(:onset AS timestamptz[]), "
                "CAST(:value AS text[]), CAST(:returned AS timestamptz[])) "
                "ON CONFLICT (episode_id, channel_id) DO UPDATE SET onset_ts = EXCLUDED.onset_ts, "
                "value = EXCLUDED.value, returned_ts = EXCLUDED.returned_ts"
            ),
            {
                "id": e.id,
                "ch": list(e.channels),
                "onset": [v[0] for v in e.channels.values()],
                "value": [v[1] for v in e.channels.values()],
                "returned": [v[2] for v in e.channels.values()],
            },
        )


async def apply_transitions(session: AsyncSession, run_id: int | None, changes: list[Transition]) -> list[Notification]:
    """Обновить эпизоды оборудования по переходам пакета; события живой ленты — после коммита."""
    r = rules()
    if not changes:
        return []
    onset_objects = {t.info.object_id for t in changes if t.onset}
    open_objects = set(
        (
            await session.scalars(
                text(f"SELECT DISTINCT object_id FROM equipment_episodes WHERE {_RUN} AND end_ts IS NULL"),
                {"run": run_id},
            )
        ).all()
    )
    objects = sorted(onset_objects | (open_objects & {t.info.object_id for t in changes}))
    returns = [t for t in changes if not t.onset]
    if not objects:
        await _closed_returns(session, run_id, returns)
        return []
    onsets = [t.ts for t in changes if t.onset]
    closed_since = (min(onsets) if onsets else changes[0].ts) - r.window - r.merge_window
    loaded = await _load(session, run_id, objects, closed_since)
    for t in changes:
        if t.info.object_id in objects:
            _apply(loaded[t.info.object_id], t, r)
    updated: list[tuple[int, str]] = []
    for episodes in loaded.values():
        for e in episodes:
            if not e.changed:
                continue
            if not e.channels:  # кандидат выпал целиком или стал продолжением закрытого эпизода
                if e.id is not None:
                    await session.execute(text("DELETE FROM equipment_episodes WHERE id = :id"), {"id": e.id})
                continue
            await _save(session, run_id, e, r)
            if e.created:
                updated.append((e.id, "equipment_episode.created"))
            elif e.qualified and (e.closed or e.reopened or await try_lock(f"equipment:sse:{e.id}", r.sse_seconds)):
                updated.append((e.id, "equipment_episode.updated"))
    await _closed_returns(session, run_id, returns)  # после сохранения: закрытые в этом пакете — тоже
    if not updated:
        return []
    items = {i["id"]: i for i in await episodes_by_ids(session, run_id, [i for i, _ in updated])}
    return [Notification(run_id, kind, items[i]) for i, kind in updated if i in items]


async def _closed_returns(session: AsyncSession, run_id: int | None, returns: list[Transition]) -> None:
    """Возвраты каналов закрытых эпизодов: эпизод уже закрыт, но `returned_ts` у канала пишется, чтобы число
    вернувшихся совпадало с текущим состоянием (у обесточивания часть фаз возвращается сутками позже)."""
    first: dict[int, dt.datetime] = {}
    for t in returns:
        first.setdefault(t.info.id, t.ts)
    if not first:
        return
    await session.execute(
        text(
            f"""
            UPDATE equipment_episode_channels ec SET returned_ts = r.ts
            FROM equipment_episodes e,
                unnest(CAST(:ch AS integer[]), CAST(:ts AS timestamptz[])) AS r(channel_id, ts)
            WHERE e.id = ec.episode_id AND e.{_RUN} AND e.end_ts IS NOT NULL AND ec.channel_id = r.channel_id
              AND ec.returned_ts IS NULL AND r.ts >= ec.onset_ts
            """
        ),
        {"run": run_id, "ch": list(first), "ts": list(first.values())},
    )


# --- чтение для API и ленты ----------------------------------------------------------------------------------------

EPISODE_SELECT = """
SELECT e.id, e.run_id, e.kind, e.value, e.object_id, o.name AS object_name, e.start_ts, e.end_ts,
    e.n_channels AS channels
FROM equipment_episodes e JOIN objects o ON o.id = e.object_id
"""

_CHANNELS = f"""
SELECT ec.episode_id, ec.channel_id, c.name AS channel_name, c.sensor_type, so.id AS sub_object_id,
    so.name AS sub_object_name, c.picket_number, ec.value, ec.onset_ts AS start_ts, ec.returned_ts AS end_ts,
    s.value_raw AS current_value, s.is_alarm AS current_alarm, coalesce({SQL_ABNORMAL}, false) AS active
FROM equipment_episode_channels ec
JOIN channels c ON c.id = ec.channel_id
JOIN objects so ON so.id = c.object_id
LEFT JOIN channel_state s ON s.channel_id = ec.channel_id AND s.run_id IS NOT DISTINCT FROM :run
WHERE ec.episode_id = ANY(:ids)
ORDER BY ec.episode_id, ec.onset_ts, ec.channel_id
"""


async def episode_channels(session: AsyncSession, run_id: int | None, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    rows = await session.execute(text(_CHANNELS), {"run": run_id, "ids": ids, **rules().sql_params()})
    out: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        out[row.episode_id].append(dict(row._mapping))
    return out


def _summarize(episode: dict, channels: list[dict]) -> dict:
    by_type = Counter(c["sensor_type"] for c in channels)
    subs: dict[int, list] = {}
    for c in channels:
        subs.setdefault(c["sub_object_id"], [c["sub_object_name"], 0])[1] += 1
    sub_objects = sorted(
        ({"id": k, "name": v[0], "channels": v[1]} for k, v in subs.items()), key=lambda s: (-s["channels"], s["id"])
    )
    top = sub_objects[0] if sub_objects else None
    return {
        **episode,
        "sub_object_id": top["id"] if top else None,
        "sub_object_name": top["name"] if top else None,
        "sub_objects": sub_objects,
        "returned_channels": sum(1 for c in channels if c["end_ts"] is not None),
        "active_channels": sum(1 for c in channels if c["active"]),
        "by_type": [{"sensor_type": t, "count": n} for t, n in sorted(by_type.items(), key=lambda x: (-x[1], x[0]))],
    }


async def episodes_where(
    session: AsyncSession, run_id: int | None, where: str, params: dict, tail: str = ""
) -> list[dict]:
    rows = [dict(r._mapping) for r in await session.execute(text(f"{EPISODE_SELECT} WHERE {where} {tail}"), params)]
    channels = await episode_channels(session, run_id, [r["id"] for r in rows])
    return [_summarize(r, channels.get(r["id"], [])) for r in rows]


async def episodes_by_ids(session: AsyncSession, run_id: int | None, ids: list[int]) -> list[dict]:
    return await episodes_where(session, run_id, "e.id = ANY(:ids) AND e.qualified", {"ids": ids})
