"""Каналы и объекты глазами интерфейса: статус, текущее значение, риск ежедневной модели и агрегаты по иерархии.

Одно чтение на запрос: состояние всех каналов прогона (~11.5 тыс. строк), вид эпизода последней тревоги у каналов
в тревоге, последний ежедневный прогноз. Агрегат статуса (docs/api.md, «Граф объектов»): `alarm`, если есть тревога
вне ППР → `ppr` → `fault`, если неисправно ≥ 10% каналов → `risk`, если в риске ≥ 5% → `ok`.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HACKATHON
from app.domain.model_versions import primary_only
from app.domain.status import channel_status, holds_alarm

FAULT_SHARE = 0.10
RISK_SHARE = 0.05
LEVELS = {1: "district", 2: "object", 3: "sub_object"}

_CHANNELS = """
SELECT c.id, c.name, c.tag, c.tag_group, c.system_type, c.sensor_type, c.object_id AS sub_object_id,
    so.name AS sub_object_name, coalesce(so.parent_id, so.id) AS object_id, coalesce(o.name, so.name) AS object_name,
    c.picket_number, c.picket_offset, c.source, s.value_raw, s.ts AS value_ts, s.is_alarm
FROM channels c
JOIN objects so ON so.id = c.object_id
LEFT JOIN objects o ON o.id = so.parent_id
LEFT JOIN run_channel_state(:run) s ON s.channel_id = c.id
WHERE c.source = :source
ORDER BY c.id
"""

_ALARM_KINDS = """
SELECT DISTINCT ON (a.channel_id) a.channel_id, e.kind
FROM run_alarms(:run) a JOIN alarm_episodes e ON e.id = a.episode_id
WHERE a.channel_id = ANY(:ids)
ORDER BY a.channel_id, a.ts DESC
"""

# Прогнозы — только свои у прогона (у базы — с run_id NULL)
_RISK = """
SELECT DISTINCT ON (channel_id) channel_id, score, threshold, alert
FROM predictions
WHERE run_id IS NOT DISTINCT FROM :run AND model = 'daily' AND status = 'scored' AND decision_time <= :now
ORDER BY channel_id, decision_time DESC
"""

# У канала — из последнего оценённого решения событийной модели; пропуски и ежедневная модель не в счёт
_OBJECT_HISTORY = f"""
SELECT DISTINCT ON (channel_id) channel_id, object_history
FROM predictions
WHERE run_id IS NOT DISTINCT FROM :run AND model = 'smoke_alarm' AND status = 'scored' AND decision_time <= :now
  AND {primary_only()}
ORDER BY channel_id, decision_time DESC
"""

_SEEN = f"""
SELECT DISTINCT ON (channel_id) channel_id, seen_channel
FROM predictions
WHERE run_id IS NOT DISTINCT FROM :run AND seen_channel IS NOT NULL AND decision_time <= :now AND {primary_only()}
ORDER BY channel_id, decision_time DESC
"""


@dataclass
class ChannelView:
    """Поля схемы «Канал» (docs/api.md)."""

    id: int
    name: str
    tag: str
    tag_group: str | None
    subsystem: str
    sensor_type: str
    object_id: int
    object_name: str
    sub_object_id: int
    sub_object_name: str
    picket_number: int | None
    picket_offset: float | None
    status: str
    value: str | None
    value_ts: object
    is_alarm: bool
    risk: float | None
    risk_threshold: float | None
    seen_channel: bool | None
    object_history: bool | None
    source: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Aggregate:
    channels: int = 0
    alarm: int = 0
    ppr: int = 0
    in_fault: int = 0
    at_risk: int = 0
    max_risk: float | None = None
    ids: list[int] = field(default_factory=list)

    def add(self, c: ChannelView) -> None:
        self.channels += 1
        self.ids.append(c.id)
        if c.status == "alarm":
            self.alarm += 1
        elif c.status == "ppr":
            self.ppr += 1
        elif c.status == "fault":
            self.in_fault += 1
        elif c.status == "risk":
            self.at_risk += 1
        if c.risk is not None and (self.max_risk is None or c.risk > self.max_risk):
            self.max_risk = c.risk

    @property
    def status(self) -> str:
        if self.alarm:
            return "alarm"
        if self.ppr:
            return "ppr"
        if self.channels and self.in_fault >= self.channels * FAULT_SHARE:
            return "fault"
        if self.channels and self.at_risk >= self.channels * RISK_SHARE:
            return "risk"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "channels": self.channels,
            "active_alarms": self.alarm + self.ppr,
            "at_risk": self.at_risk,
            "in_fault": self.in_fault,
            "max_risk": self.max_risk,
            "status": self.status,
        }


def aggregate(channels: Iterable[ChannelView]) -> Aggregate:
    agg = Aggregate()
    for c in channels:
        agg.add(c)
    return agg


async def channel_views(
    session: AsyncSession, run_id: int | None, now, source: str = HACKATHON
) -> dict[int, ChannelView]:
    """Все каналы справочника с состоянием в представлении прогона."""
    rows = (await session.execute(text(_CHANNELS), {"run": run_id, "source": source})).all()
    alarm_ids = [r.id for r in rows if r.value_raw is not None and holds_alarm(r.sensor_type, r.value_raw, r.is_alarm)]
    kinds = {}
    if alarm_ids:
        rows_kinds = await session.execute(text(_ALARM_KINDS), {"run": run_id, "ids": alarm_ids})
        kinds = {channel_id: kind for channel_id, kind in rows_kinds}
    risk = {r.channel_id: r for r in await session.execute(text(_RISK), {"run": run_id, "now": now})}
    seen = {channel_id: flag for channel_id, flag in await session.execute(text(_SEEN), {"run": run_id, "now": now})}
    history = {
        channel_id: flag
        for channel_id, flag in await session.execute(text(_OBJECT_HISTORY), {"run": run_id, "now": now})
    }
    views = {}
    for r in rows:
        forecast = risk.get(r.id)
        views[r.id] = ChannelView(
            id=r.id,
            name=r.name,
            tag=r.tag,
            tag_group=r.tag_group,
            subsystem=r.system_type,
            sensor_type=r.sensor_type,
            object_id=r.object_id,
            object_name=r.object_name,
            sub_object_id=r.sub_object_id,
            sub_object_name=r.sub_object_name,
            picket_number=r.picket_number,
            picket_offset=r.picket_offset,
            status=channel_status(
                r.sensor_type, r.value_raw, r.is_alarm, kinds.get(r.id), bool(forecast and forecast.alert)
            ),
            value=r.value_raw,
            value_ts=r.value_ts,
            is_alarm=bool(r.is_alarm),
            risk=forecast.score if forecast else None,
            risk_threshold=forecast.threshold if forecast else None,
            seen_channel=seen.get(r.id),
            object_history=history.get(r.id),
            source=r.source,
        )
    return views


@dataclass
class ObjectTree:
    objects: dict[int, dict]  # id → {id, parent_id, level, kind, name, source}
    children: dict[int, list[int]]
    district_id: int | None

    def path(self, object_id: int) -> list[dict]:
        out = []
        current = self.objects.get(object_id)
        while current is not None:
            out.append(current)
            current = self.objects.get(current["parent_id"]) if current["parent_id"] else None
        return list(reversed(out))

    def sub_objects_under(self, object_id: int) -> set[int]:
        """Подобъекты (уровень 3) под объектом любого уровня, включая его самого."""
        found, stack = set(), [object_id]
        while stack:
            oid = stack.pop()
            if self.objects[oid]["level"] == "sub_object":
                found.add(oid)
            stack.extend(self.children.get(oid, []))
        return found


async def object_tree(session: AsyncSession, source: str | None = None) -> ObjectTree:
    """Дерево объектов набора `source`; без него — все наборы (для фильтров по объекту, где чужих строк нет)."""
    rows = (
        await session.execute(
            text(
                "SELECT id, parent_id, level, kind, name, source FROM objects "
                "WHERE CAST(:source AS text) IS NULL OR source = :source ORDER BY level, name"
            ),
            {"source": source},
        )
    ).all()
    objects = {
        r.id: {
            "id": r.id,
            "parent_id": r.parent_id,
            "level": LEVELS[r.level],
            "kind": r.kind,
            "name": r.name,
            "source": r.source,
        }
        for r in rows
    }
    children: dict[int, list[int]] = defaultdict(list)
    district = None
    for r in rows:
        if r.parent_id in objects:
            children[r.parent_id].append(r.id)
        if r.level == 1:
            district = r.id
    return ObjectTree(objects, children, district)


def by_sub_object(views: dict[int, ChannelView]) -> dict[int, list[ChannelView]]:
    out: dict[int, list[ChannelView]] = defaultdict(list)
    for c in views.values():
        out[c.sub_object_id].append(c)
    return out


def by_sensor_type(channels: Iterable[ChannelView]) -> list[dict]:
    groups: dict[str, Aggregate] = defaultdict(Aggregate)
    for c in channels:
        groups[c.sensor_type].add(c)
    return sorted(
        (
            {
                "sensor_type": t,
                "channels": a.channels,
                "at_risk": a.at_risk,
                "in_fault": a.in_fault,
                "max_risk": a.max_risk,
            }
            for t, a in groups.items()
        ),
        key=lambda x: (-x["channels"], x["sensor_type"]),
    )
