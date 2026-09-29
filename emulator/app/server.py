"""OPC UA сервер эмулятора. Адресное пространство — docs/architecture/emulator.md §3.

Дерево `Objects/Moskollektor/<район>/<объект>/<подобъект>/<канал>` строится из справочников; внешние наборы —
отдельными корнями `Objects/External/<набор>/…` со свойством `Dataset` у корня (external-data.md §5.1). У канала —
переменные `Value` (String, исходное значение журнала) и `Alarm` (Boolean) и свойства из справочника.
Идентификаторы узлов строковые и постоянные: `obj/<ид_объект>`, `ch/<ид_канала>`, `ch/<ид_канала>/Value` и т. д.

Сервер только публикует: события каналов и служебные узлы. Что и когда публиковать, решает проигрыватель.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from asyncua import Server, ua

from app.catalog import Catalog, Channel, Obj
from app.config import settings

log = logging.getLogger("emulator.server")

CHANNEL_PROPERTIES = ("SensorType", "Subsystem", "Tag", "Picket")

# Качество данных (§3): исходное значение → стандартный StatusCode OPC UA
BAD_STATES = {
    "Неисправен": ua.StatusCodes.BadDeviceFailure,
    "Отключено устройство": ua.StatusCodes.BadOutOfService,
}
UNCERTAIN_STATES = {"Неопределен", "Не определено"}


def quality(value: str) -> int:
    if value in BAD_STATES:
        return BAD_STATES[value]
    if value in UNCERTAIN_STATES:
        return ua.StatusCodes.Uncertain
    return ua.StatusCodes.Good


def _object_item(ns: int, node_id: str, parent: ua.NodeId, browse_name: str, display: str) -> ua.AddNodesItem:
    attrs = ua.ObjectAttributes()
    attrs.DisplayName = ua.LocalizedText(display)
    attrs.EventNotifier = 0
    return ua.AddNodesItem(
        ParentNodeId=parent,
        ReferenceTypeId=ua.NodeId(ua.ObjectIds.Organizes),
        RequestedNewNodeId=ua.NodeId(node_id, ns),
        BrowseName=ua.QualifiedName(browse_name, ns),
        NodeClass=ua.NodeClass.Object,
        NodeAttributes=attrs,
        TypeDefinition=ua.NodeId(ua.ObjectIds.BaseObjectType),
    )


def _variable_item(
    ns: int, node_id: str, parent: ua.NodeId, name: str, value: ua.Variant, data_type: int, prop: bool = False
) -> ua.AddNodesItem:
    attrs = ua.VariableAttributes()
    attrs.DisplayName = ua.LocalizedText(name)
    attrs.Value = value
    attrs.DataType = ua.NodeId(data_type)
    attrs.ValueRank = ua.ValueRank.Scalar
    attrs.AccessLevel = ua.AccessLevel.CurrentRead.mask
    attrs.UserAccessLevel = ua.AccessLevel.CurrentRead.mask
    return ua.AddNodesItem(
        ParentNodeId=parent,
        ReferenceTypeId=ua.NodeId(ua.ObjectIds.HasProperty if prop else ua.ObjectIds.HasComponent),
        RequestedNewNodeId=ua.NodeId(node_id, ns),
        BrowseName=ua.QualifiedName(name, ns),
        NodeClass=ua.NodeClass.Variable,
        NodeAttributes=attrs,
        TypeDefinition=ua.NodeId(ua.ObjectIds.PropertyType if prop else ua.ObjectIds.BaseDataVariableType),
    )


def no_data(written: datetime | None = None) -> ua.DataValue:
    """Значение канала, пока в прогоне не было событий: пусто, `BadWaitingForInitialData`."""
    return ua.DataValue(
        Value=ua.Variant(None, ua.VariantType.Null),
        StatusCode=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData),
        ServerTimestamp=written or datetime.now(UTC),
    )


class WriteClock:
    """Метка записи `ServerTimestamp` — строго возрастает, даже если системные часы шагнули назад. По ней шлюз
    относит событие к прогону: `RunId` и события приходят разными элементами подписки, и их порядок в уведомлении
    не гарантирован (§3, «Время»)."""

    STEP = timedelta(microseconds=1)

    def __init__(self) -> None:
        self._last = datetime.min.replace(tzinfo=UTC)

    def __call__(self) -> datetime:
        self._last = max(datetime.now(UTC), self._last + self.STEP)
        return self._last


# Служебные узлы, которые пишутся каждый шаг, даже без изменения: `SimulatedTime` — пульс источника, `RunId` —
# отметка «до этой метки записи прогон был таким» (шлюз по ней относит события к прогону, emulator.md §7)
HEARTBEAT = ("RunId", "SimulatedTime")

# Служебные узлы Moskollektor/Emulator и их типы: запись другого типа сервер отклоняет
SERVICE_NODES = {
    "SimulatedTime": ua.VariantType.DateTime,
    "Scenario": ua.VariantType.String,
    "State": ua.VariantType.String,
    "TargetSpeed": ua.VariantType.Double,
    "EffectiveSpeed": ua.VariantType.Double,
    "RunId": ua.VariantType.Int64,
    # сколько событий прогона отправлено до этой метки записи — шлюз сверяет с принятыми и видит потери (§7)
    "EventsSent": ua.VariantType.Int64,
}


class Emulator:
    def __init__(self, catalog: Catalog | None = None) -> None:
        self.server = Server()
        self.catalog = catalog or Catalog()
        self.ns = 0
        self._service: dict = {}
        self.clock = WriteClock()

    async def init(self) -> None:
        await self.server.init()
        self.server.set_endpoint(settings.opcua_endpoint)
        self.server.set_server_name("LCT СМВУ — эмулятор")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])  # v1: только внутри сети Docker (§8)
        self.ns = ns = await self.server.register_namespace(settings.opcua_namespace)
        root = await self.server.nodes.objects.add_object(
            ua.NodeId("Moskollektor", ns), ua.QualifiedName("Moskollektor", ns)
        )
        emulator = await root.add_object(ua.NodeId("Emulator", ns), ua.QualifiedName("Emulator", ns))
        initial = {
            "SimulatedTime": datetime.now(UTC),
            "Scenario": "",
            "State": "idle",
            "TargetSpeed": 1.0,
            "EffectiveSpeed": 0.0,
            "RunId": 0,
            "EventsSent": 0,
        }
        for name, vtype in SERVICE_NODES.items():
            await emulator.add_variable(
                ua.NodeId(f"Emulator/{name}", ns), ua.QualifiedName(name, ns), ua.Variant(initial[name], vtype)
            )
        self._service = dict(initial)
        external = None
        if any(o.dataset for o in self.catalog.roots()):
            node = await self.server.nodes.objects.add_object(
                ua.NodeId("External", ns), ua.QualifiedName("External", ns)
            )
            external = node.nodeid
        await self._build_tree(root.nodeid, external)

    async def _build_tree(self, root: ua.NodeId, external: ua.NodeId | None = None) -> None:
        """Дерево объектов и каналов одним пакетом AddNodes: ~80 тыс. узлов за ~4 с против ~7 с поштучно."""
        started = time.perf_counter()
        ns = self.ns
        items: list[ua.AddNodesItem] = []

        def add_object(parent: ua.NodeId, obj: Obj) -> None:
            node = ua.NodeId(f"obj/{obj.id}", ns)
            items.append(_object_item(ns, node.Identifier, parent, str(obj.id), obj.name))
            for ch in self.catalog.channels_of(obj.id):
                add_channel(node, ch)
            for child in self.catalog.children(obj.id):
                add_object(node, child)

        def add_channel(parent: ua.NodeId, ch: Channel) -> None:
            base = f"ch/{ch.id}"
            node = ua.NodeId(base, ns)
            items.append(_object_item(ns, base, parent, str(ch.id), ch.name))
            # тип значения задаёт DataType узла: первая запись проверяется по нему
            empty = ua.Variant(None, ua.VariantType.Null)
            for name, data_type in (("Value", ua.ObjectIds.String), ("Alarm", ua.ObjectIds.Boolean)):
                items.append(_variable_item(ns, f"{base}/{name}", node, name, empty, data_type))
            if not settings.channel_properties:
                return
            for name, value in zip(CHANNEL_PROPERTIES, (ch.sensor_type, ch.subsystem, ch.tag, ch.picket), strict=True):
                variant = ua.Variant(value, ua.VariantType.String)
                items.append(_variable_item(ns, f"{base}/{name}", node, name, variant, ua.ObjectIds.String, prop=True))

        for obj in self.catalog.roots():
            if obj.dataset is None:
                add_object(root, obj)
                continue
            add_object(external, obj)
            dataset = ua.Variant(obj.dataset, ua.VariantType.String)
            node = ua.NodeId(f"obj/{obj.id}", ns)
            items.append(
                _variable_item(ns, f"obj/{obj.id}/Dataset", node, "Dataset", dataset, ua.ObjectIds.String, True)
            )
        results = await self.server.iserver.isession.add_nodes(items)
        failed = [
            (item.RequestedNewNodeId, r.StatusCode)
            for item, r in zip(items, results, strict=True)
            if not r.StatusCode.is_good()
        ]
        if failed:
            raise RuntimeError(f"адресное пространство: не создано {len(failed)} узлов, первый — {failed[0]}")
        await self.reset_channels(self.catalog.channels)
        log.info(
            "адресное пространство: %d объектов, %d каналов, %d узлов за %.1f с",
            len(self.catalog.objects),
            len(self.catalog.channels),
            len(items),
            time.perf_counter() - started,
        )

    def channel_node(self, channel_id: int, name: str) -> ua.NodeId:
        return ua.NodeId(f"ch/{channel_id}/{name}", self.ns)

    async def reset_channels(self, channel_ids) -> None:
        """Каналы без данных: при старте сервера и перед новым прогоном — чтобы значения прошлого прогона
        не выглядели текущими, а первое событие прогона всегда отличалось от предыдущего значения узла."""
        for channel_id in channel_ids:
            for name in ("Value", "Alarm"):
                await self.server.write_attribute_value(self.channel_node(channel_id, name), no_data(self.clock()))

    async def publish(self, channel_id: int, source_ts: datetime, value: str, alarm: bool) -> None:
        """Событие журнала: `Value` и `Alarm` с одной меткой источника — шлюз склеивает их по (канал, метка).

        При Bad-статусе сервер по OPC UA Part 4 §7.7.1 обнуляет значение — состояние восстанавливается из кода.
        `ServerTimestamp` — метка записи (`WriteClock`), одна на обе переменные.
        """
        now = self.clock()
        await self.server.write_attribute_value(
            self.channel_node(channel_id, "Value"),
            ua.DataValue(
                Value=ua.Variant(value, ua.VariantType.String),
                StatusCode=ua.StatusCode(quality(value)),
                SourceTimestamp=source_ts,
                ServerTimestamp=now,
            ),
        )
        await self.server.write_attribute_value(
            self.channel_node(channel_id, "Alarm"),
            ua.DataValue(
                Value=ua.Variant(alarm, ua.VariantType.Boolean),
                SourceTimestamp=source_ts,
                ServerTimestamp=now,
            ),
        )

    async def set_service(self, force_time: bool = True, **values) -> None:
        """Служебные узлы. Пишутся только изменившиеся значения; `SimulatedTime` и `RunId` (`HEARTBEAT`) — каждый
        раз, с меткой «сейчас». Шлюз подписан с `StatusValueTimestamp`: по `SimulatedTime` он видит, что источник
        жив, даже когда симуляционное время стоит (пауза, конец сценария), а по `RunId` знает, каким был прогон до
        этой метки записи, и относит к нему события с меткой раньше. Узлы пишутся в порядке аргументов, у каждого —
        своя метка `WriteClock`."""
        for name, value in values.items():
            if name not in SERVICE_NODES:
                raise KeyError(name)
            if self._service.get(name) == value and not (force_time and name in HEARTBEAT):
                continue
            self._service[name] = value
            now = self.clock()
            await self.server.write_attribute_value(
                ua.NodeId(f"Emulator/{name}", self.ns),
                ua.DataValue(Value=ua.Variant(value, SERVICE_NODES[name]), SourceTimestamp=now, ServerTimestamp=now),
            )
