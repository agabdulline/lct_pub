"""Адаптер источника событий (D17): протокол реальной СМВУ неизвестен, приём от него не зависит.

Адаптер подключается к источнику и отдаёт приёмнику готовые события журнала и служебные значения источника.
OPC UA — первая реализация (`opcua.py`); MQTT и МЭК 60870-5-104 — возможные следующие.
"""

from datetime import datetime
from typing import Protocol

from app.domain.ingest import RawEvent


class EventSink(Protocol):
    def on_event(self, event: RawEvent) -> None:
        """Событие журнала — склеенное, с исходным значением и посекундной меткой; `event.written` — метка записи
        у источника, если он её даёт."""

    async def on_service(
        self, name: str, value: str | float | int | datetime | None, written: datetime | None = None
    ) -> None:
        """Служебное значение источника: `SimulatedTime`, `RunId`, `State`, `EffectiveSpeed` и т. п.; `written` —
        метка записи у источника: по хронологии `RunId` в этих метках события относятся к прогону."""

    async def on_connection(self, connected: bool) -> None:
        """Связь с источником появилась или пропала."""


class SourceAdapter(Protocol):
    async def run(self, sink: EventSink) -> None:
        """Подключаться, подписываться и передавать события, переподключаясь при обрыве. Не возвращается."""
