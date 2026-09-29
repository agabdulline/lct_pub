"""Справочник каналов в памяти процесса: тип датчика, подобъект, объект уровня 2, название и маршрут приёма.

Справочник меняется только загрузчиками (`load-db`, `load-external`); процессы приёма держат его в памяти
и перечитывают, если встретили незнакомый канал.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.values import LEVER_PULLED_VALUE


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    id: int
    sensor_type: str
    sub_object_id: int
    object_id: int  # объект уровня 2 — родитель подобъекта
    name: str
    # маршрут приёма (D31): `raw` — в `events_raw`, `significant` — в `events`; `deadband` — зона нечувствительности,
    # с которой сырой канал дублируется в `events` значимыми изменениями (у хакатона — газ, 0.02)
    stream: str = "significant"
    deadband: float | None = None
    source: str = "hackathon"  # набор данных: у внешнего набора события скорят его событийные модели


# Типы, у которых бывает «Рычаг сдернут» — признак плановой проверки
LEVER_SENSOR_TYPES = frozenset({"Состояние УИР-Р", "Ручной извещатель"})


class ChannelDirectory:
    def __init__(self) -> None:
        self.channels: dict[int, ChannelInfo] = {}
        self.lever_channels: dict[int, list[int]] = {}  # объект → каналы с рычагом

    async def load(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT c.id, c.sensor_type, c.object_id, coalesce(o.parent_id, o.id), c.name, c.stream, c.deadband, "
                "c.source "
                "FROM channels c JOIN objects o ON o.id = c.object_id"
            )
        )
        self.channels = {r[0]: ChannelInfo(*r) for r in rows}
        levers: dict[int, list[int]] = {}
        for c in self.channels.values():
            if c.sensor_type in LEVER_SENSOR_TYPES:
                levers.setdefault(c.object_id, []).append(c.id)
        self.lever_channels = levers

    async def get_many(self, session: AsyncSession, ids: set[int]) -> dict[int, ChannelInfo]:
        if not self.channels or not ids <= self.channels.keys():
            await self.load(session)
        return {i: self.channels[i] for i in ids if i in self.channels}


directory = ChannelDirectory()

__all__ = ["LEVER_PULLED_VALUE", "ChannelDirectory", "ChannelInfo", "directory"]
