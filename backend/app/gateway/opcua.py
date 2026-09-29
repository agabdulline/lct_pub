"""Адаптер OPC UA: подписка на адресное пространство эмулятора СМВУ (docs/architecture/emulator.md §3, §7).

- Каналы находятся обходом деревьев `Moskollektor` и `External` (внешние наборы, external-data.md §5.1; у реальной
  СМВУ его нет): объекты — `obj/<id>`, каналы — `ch/<id>`.
- Подписка на `Value` и `Alarm` всех каналов и на служебные узлы `Emulator/*`: `Trigger = StatusValueTimestamp`,
  `QueueSize` 100, `DiscardOldest = false` — иначе повтор значения с новой меткой и быстрые изменения внутри
  интервала публикации потерялись бы.
- `Value` и `Alarm` склеиваются по (канал, `SourceTimestamp`) в одно событие журнала; `ServerTimestamp` — метка
  записи у источника, по ней шлюз относит событие к прогону (`service.RunTimeline`).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from asyncua import Client, Node, ua

from app.domain.ingest import RawEvent
from app.gateway.source import EventSink

log = logging.getLogger("gateway.opcua")

PUBLISHING_INTERVAL_MS = 200
QUEUE_SIZE = 100
SUBSCRIBE_BATCH = 5_000
SERVICE_NODES = ("RunId", "EventsSent", "State", "Scenario", "TargetSpeed", "EffectiveSpeed", "SimulatedTime")
ROOTS = ("Moskollektor", "External")

# Bad-статус обнуляет значение (OPC UA Part 4 §7.7.1); коды взаимно однозначны состояниям журнала (§3)
STATE_BY_STATUS = {
    ua.StatusCodes.BadDeviceFailure: "Неисправен",
    ua.StatusCodes.BadOutOfService: "Отключено устройство",
}
NO_DATA = ua.StatusCodes.BadWaitingForInitialData


@dataclass(slots=True)
class _Half:
    received: float
    value: str | None = None
    alarm: bool | None = None
    written: datetime | None = None


class Pairer:
    """Склейка `Value` и `Alarm` канала по метке источника.

    Эмулятор пишет обе переменные с одной меткой, а у событий канала в одну секунду метки различаются на
    миллисекунды (§3). Миллисекунды отбрасываются: время в журнале посекундное. Половина без пары дольше
    `timeout` секунд дополняется последним известным значением другой половины канала.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self._pending: dict[tuple[int, datetime], _Half] = {}
        self._last_value: dict[int, str] = {}
        self._last_alarm: dict[int, bool] = {}
        self.skipped: dict[str, int] = {}

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def value(self, channel_id: int, dv: ua.DataValue, now: float | None = None) -> RawEvent | None:
        code = dv.StatusCode.value if dv.StatusCode else ua.StatusCodes.Good
        if code == NO_DATA or dv.SourceTimestamp is None:
            return None
        if code in STATE_BY_STATUS:
            value = STATE_BY_STATUS[code]
        elif dv.StatusCode.is_bad():
            self._skip(f"bad_status:{ua.StatusCode(code).name}")
            return None
        elif dv.Value is None or dv.Value.Value is None:
            self._skip("empty_value")
            return None
        else:
            value = str(dv.Value.Value)
        return self._put(channel_id, dv.SourceTimestamp, dv.ServerTimestamp, now, value=value)

    def alarm(self, channel_id: int, dv: ua.DataValue, now: float | None = None) -> RawEvent | None:
        code = dv.StatusCode.value if dv.StatusCode else ua.StatusCodes.Good
        if code == NO_DATA or dv.SourceTimestamp is None:
            return None
        if dv.StatusCode.is_bad() or dv.Value is None or dv.Value.Value is None:
            self._skip("bad_alarm")
            return None
        return self._put(channel_id, dv.SourceTimestamp, dv.ServerTimestamp, now, alarm=bool(dv.Value.Value))

    def _put(
        self, channel_id: int, ts: datetime, written: datetime | None, now: float | None, **half
    ) -> RawEvent | None:
        key = (channel_id, ts)
        pending = self._pending.get(key)
        if pending is None:
            received = time.monotonic() if now is None else now
            self._pending[key] = _Half(received=received, written=written, **half)
            return None
        for name, value in half.items():
            setattr(pending, name, value)
        if written is not None and (pending.written is None or written > pending.written):
            pending.written = written  # эмулятор пишет обе половины подряд: позднейшая метка — та же запись
        if pending.value is None or pending.alarm is None:
            return None
        del self._pending[key]
        return self._event(channel_id, ts, pending.value, pending.alarm, pending.written)

    def _event(self, channel_id: int, ts: datetime, value: str, alarm: bool, written: datetime | None) -> RawEvent:
        self._last_value[channel_id], self._last_alarm[channel_id] = value, alarm
        return RawEvent(
            channel_id=channel_id, ts=ts.replace(microsecond=0), value_raw=value, is_alarm=alarm, written=written
        )

    def expire(self, now: float | None = None) -> list[RawEvent]:
        now = time.monotonic() if now is None else now
        events = []
        for key in [k for k, h in self._pending.items() if now - h.received > self.timeout]:
            channel_id, ts = key
            half = self._pending.pop(key)
            value = half.value if half.value is not None else self._last_value.get(channel_id)
            alarm = half.alarm if half.alarm is not None else self._last_alarm.get(channel_id)
            if value is None or alarm is None:
                self._skip("unpaired")
                continue
            self._skip("completed_from_last")
            events.append(self._event(channel_id, ts, value, alarm, half.written))
        return sorted(events, key=lambda e: e.ts)

    @property
    def pending(self) -> int:
        return len(self._pending)


def _service_value(dv: ua.DataValue):
    return None if dv.Value is None else dv.Value.Value


class _Handler:
    def __init__(self, sink: EventSink, pairer: Pairer) -> None:
        self.sink = sink
        self.pairer = pairer

    async def datachange_notification(self, node: Node, val, data) -> None:
        ident = node.nodeid.Identifier
        dv = data.monitored_item.Value
        if ident.startswith("ch/"):
            _, channel, kind = ident.split("/")
            add = self.pairer.value if kind == "Value" else self.pairer.alarm
            event = add(int(channel), dv)
            if event is not None:
                self.sink.on_event(event)
        elif ident.startswith("Emulator/"):
            await self.sink.on_service(ident.removeprefix("Emulator/"), _service_value(dv), dv.ServerTimestamp)

    def status_change_notification(self, status) -> None:
        log.warning("подписка: смена статуса %s", status)


def _request(node_id: ua.NodeId, handle: int, queue_size: int) -> ua.MonitoredItemCreateRequest:
    params = ua.MonitoringParameters(
        ClientHandle=handle,
        SamplingInterval=0,
        QueueSize=queue_size,
        DiscardOldest=False,
        Filter=ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp),
    )
    return ua.MonitoredItemCreateRequest(
        ItemToMonitor=ua.ReadValueId(NodeId=node_id, AttributeId=ua.AttributeIds.Value),
        MonitoringMode=ua.MonitoringMode.Reporting,
        RequestedParameters=params,
    )


class OpcUaSource:
    """Источник событий по OPC UA — эмулятор СМВУ или реальная система с тем же адресным пространством."""

    def __init__(
        self, endpoint: str, namespace: str, reconnect_seconds: float = 3.0, watchdog_seconds: float = 60.0
    ) -> None:
        self.endpoint = endpoint
        self.namespace = namespace
        self.reconnect_seconds = reconnect_seconds
        # Сторож asyncua раз в `watchdog_intervall` читает состояние сервера с таким же таймаутом; по умолчанию 1 с.
        # Источник, замерший дольше секунды (эмулятор под нагрузкой, GIL занят сводкой сценария), считался потерянным:
        # шлюз переподключался и переподписывался, и всё, что источник отправил за 5–7 с переподключения, терялось
        # (прогон 60 стенда — 3 600 событий). Пока источник замер, он и не публикует — очередь подписки его догонит.
        # Обрыв сокета виден сразу, долгий сторож задерживает только обнаружение полуоткрытого соединения.
        self.watchdog_seconds = watchdog_seconds
        self.pairer = Pairer()

    async def discover(self, client: Client, ns: int) -> list[int]:
        """Каналы из обхода деревьев объектов; корня `External` может не быть."""
        channels: list[int] = []
        level = []
        for root in ROOTS:
            node = client.get_node(ua.NodeId(root, ns))
            try:
                await node.read_browse_name()
            except ua.UaStatusCodeError:
                continue
            level.append(node)
        while level:
            children = await asyncio.gather(*(n.get_children(refs=ua.ObjectIds.Organizes) for n in level))
            level = []
            for child in (c for group in children for c in group):
                ident = child.nodeid.Identifier
                if not isinstance(ident, str):
                    continue
                if ident.startswith("ch/"):
                    channels.append(int(ident.removeprefix("ch/")))
                elif ident.startswith("obj/"):
                    level.append(child)
        return sorted(channels)

    async def subscribe(self, client: Client, ns: int, sink: EventSink, channels: list[int]) -> None:
        handler = _Handler(sink, self.pairer)
        sub = await client.create_subscription(PUBLISHING_INTERVAL_MS, handler)
        handle = 0
        # служебные узлы — первыми: RunId нужен до событий, чтобы пометить их прогоном
        run = await client.get_node(ua.NodeId("Emulator/RunId", ns)).read_data_value()
        # чтение отдаёт только метку источника; у служебных узлов эмулятора она и есть метка записи
        await sink.on_service("RunId", _service_value(run), run.ServerTimestamp or run.SourceTimestamp)
        requests = []
        for name in SERVICE_NODES:
            handle += 1
            # RunId и SimulatedTime пишутся каждый шаг (0.1 с): очередь с запасом, чтобы смена RunId не потерялась
            requests.append(_request(ua.NodeId(f"Emulator/{name}", ns), handle, QUEUE_SIZE))
        await sub.create_monitored_items(requests)

        started = time.perf_counter()
        nodes = [ua.NodeId(f"ch/{c}/{kind}", ns) for c in channels for kind in ("Value", "Alarm")]
        failed = 0
        for i in range(0, len(nodes), SUBSCRIBE_BATCH):
            batch = []
            for node_id in nodes[i : i + SUBSCRIBE_BATCH]:
                handle += 1
                batch.append(_request(node_id, handle, QUEUE_SIZE))
            results = await sub.create_monitored_items(batch)
            failed += sum(isinstance(r, ua.StatusCode) for r in results)
        log.info(
            "подписка: %d каналов, %d элементов за %.1f с, отказов %d",
            len(channels),
            len(nodes),
            time.perf_counter() - started,
            failed,
        )

    async def run(self, sink: EventSink) -> None:
        while True:
            try:
                async with Client(url=self.endpoint, timeout=10, watchdog_intervall=self.watchdog_seconds) as client:
                    ns = await client.get_namespace_index(self.namespace)
                    started = time.perf_counter()
                    channels = await self.discover(client, ns)
                    log.info("обход дерева: %d каналов за %.1f с", len(channels), time.perf_counter() - started)
                    await self.subscribe(client, ns, sink, channels)
                    await sink.on_connection(True)
                    log.info("подключён к %s", self.endpoint)
                    while True:
                        await asyncio.sleep(2)
                        await client.check_connection()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — шлюз должен переподключаться при любом сбое
                log.warning("нет связи с источником %s: %s", self.endpoint, e)
                await sink.on_connection(False)
                await asyncio.sleep(self.reconnect_seconds)
