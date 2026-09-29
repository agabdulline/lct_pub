import asyncio
from datetime import UTC, datetime, timedelta

from asyncua import Client, ua
from conftest import running, write_prepared

from app.catalog import add_external_catalogs
from app.config import settings
from app.server import CHANNEL_PROPERTIES as PROPERTIES
from app.server import Emulator


class Collector:
    def __init__(self) -> None:
        self.items: list[tuple[str, ua.DataValue]] = []

    def datachange_notification(self, node, val, data):
        self.items.append((node.nodeid.Identifier, data.monitored_item.Value))


def test_opcua_publishes_simulated_time(endpoint):
    async def scenario():
        async with running(Emulator()):
            async with Client(endpoint) as client:
                ns = await client.get_namespace_index(settings.opcua_namespace)
                emulator = [f"{ns}:Moskollektor", f"{ns}:Emulator"]
                value = await (await client.nodes.objects.get_child([*emulator, f"{ns}:SimulatedTime"])).read_value()
                state = await (await client.nodes.objects.get_child([*emulator, f"{ns}:State"])).read_value()
        return value, state

    value, state = asyncio.run(scenario())
    assert isinstance(value, datetime)
    assert state == "idle"


def test_address_space_from_catalog(endpoint, catalog):
    async def scenario():
        async with running(Emulator(catalog)):
            async with Client(endpoint) as client:
                ns = await client.get_namespace_index(settings.opcua_namespace)
                path = [f"{ns}:{p}" for p in ("Moskollektor", "5773", "7", "5961", "287267")]
                channel = await client.nodes.objects.get_child(path)
                children = {(await c.read_browse_name()).Name: c for c in await channel.get_children()}
                props = {name: await children[name].read_value() for name in PROPERTIES}
                value = await children["Value"].read_data_value(raise_on_bad_status=False)
                display = (await channel.read_display_name()).Text
                # постоянный строковый идентификатор узла
                same = client.get_node(ua.NodeId("ch/287267/Value", ns))
                same_name = (await same.read_browse_name()).Name
        return children, props, value, display, same_name

    children, props, value, display, same_name = asyncio.run(scenario())
    assert set(children) == {"Value", "Alarm", "SensorType", "Subsystem", "Tag", "Picket"}
    assert props == dict(zip(PROPERTIES, ("Датчик дыма", "Пожарная охрана", "15-11.1.131.2.", "12"), strict=True))
    assert display == "ДД ПК12"
    assert value.StatusCode.value == ua.StatusCodes.BadWaitingForInitialData
    assert same_name == "Value"


def test_publish_sets_quality_and_source_timestamp(endpoint, catalog):
    ts = [datetime(2026, 3, 2, 6, 15, s, tzinfo=UTC) for s in range(4)]

    async def scenario():
        emulator = Emulator(catalog)
        collector = Collector()
        async with running(emulator):
            async with Client(endpoint) as client:
                ns = emulator.ns
                nodes = [client.get_node(ua.NodeId(f"ch/287267/{n}", ns)) for n in ("Value", "Alarm")]
                sub = await client.create_subscription(50, collector)
                trigger = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp)
                await sub._subscribe(nodes, mfilter=trigger, queuesize=100)
                await asyncio.sleep(0.3)
                collector.items.clear()  # начальные значения при создании подписки
                await emulator.publish(287267, ts[0], "Обнаружен дым", True)
                await emulator.publish(287267, ts[1], "Неисправен", True)
                await emulator.publish(287267, ts[2], "Неопределен", False)
                await emulator.publish(287267, ts[3], "Отключено устройство", False)
                await asyncio.sleep(0.5)
        return collector.items

    items = asyncio.run(scenario())
    values = [(dv.SourceTimestamp, dv.StatusCode.value, dv.Value.Value) for n, dv in items if n.endswith("Value")]
    alarms = [(dv.SourceTimestamp, dv.Value.Value) for n, dv in items if n.endswith("Alarm")]
    assert values == [
        (ts[0], ua.StatusCodes.Good, "Обнаружен дым"),
        # Bad-статус: значение обнуляется по OPC UA Part 4 §7.7.1, состояние — в коде
        (ts[1], ua.StatusCodes.BadDeviceFailure, None),
        (ts[2], ua.StatusCodes.Uncertain, "Неопределен"),
        (ts[3], ua.StatusCodes.BadOutOfService, None),
    ]
    # флаг тревоги — как есть; без изменения значения приходит благодаря новой метке
    assert alarms == [(t, a) for t, a in zip(ts, [True, True, False, False], strict=True)]


def test_player_streams_scenario_over_opcua(endpoint, catalog, tmp_path):
    """Сквозной путь: срез → проигрыватель → OPC UA → подписка клиента, как у шлюза."""
    from conftest import write_scenario, write_slice

    from app.player import Player

    write_scenario(tmp_path / "sc", "e2e", "2026-03-28T08:00:00", "2026-03-28T08:01:00")
    write_slice(
        tmp_path / "sl" / "e2e.parquet",
        [
            (1, 287267, "2026-03-28 08:00:10", True, "Обнаружен дым"),
            (2, 287267, "2026-03-28 08:00:30", True, "Неисправен"),
            (3, 287267, "2026-03-28 08:00:40", False, "Норма"),
            (4, 287267, "2026-03-28 08:00:40", False, "Дыма нет"),  # та же секунда — +1 мс
            (5, 120298, "2026-03-28 08:00:40", False, "18"),
        ],
    )

    async def scenario():
        emulator = Emulator(catalog)
        player = Player(emulator, tmp_path / "sc", tmp_path / "sl", max_rate=200, tick=0.05)
        collector = Collector()
        async with running(emulator, player):
            async with Client(endpoint) as client:
                ns = emulator.ns
                ids = [f"ch/{c}/{n}" for c in (287267, 120298) for n in ("Value", "Alarm")]
                ids += ["Emulator/State", "Emulator/RunId"]
                sub = await client.create_subscription(50, collector)
                trigger = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp)
                await sub._subscribe([client.get_node(ua.NodeId(i, ns)) for i in ids], mfilter=trigger, queuesize=100)
                await asyncio.sleep(0.2)
                await player.start("e2e", speed=60, run_id=42)
                for _ in range(60):
                    await asyncio.sleep(0.05)
                    if player.state == "finished":
                        break
                await asyncio.sleep(0.3)
        return collector.items, player.state

    items, state = asyncio.run(scenario())
    assert state == "finished"
    t = datetime(2026, 3, 28, 5, 0, tzinfo=UTC)  # 08:00 по Москве

    def events(node):
        return [
            (dv.SourceTimestamp, dv.StatusCode.value, dv.Value.Value)
            for n, dv in items
            if n == node and dv.StatusCode.value != ua.StatusCodes.BadWaitingForInitialData
        ]

    ms = timedelta(milliseconds=1)
    assert events("ch/287267/Value") == [
        (t + timedelta(seconds=10), ua.StatusCodes.Good, "Обнаружен дым"),
        (t + timedelta(seconds=30), ua.StatusCodes.BadDeviceFailure, None),
        (t + timedelta(seconds=40), ua.StatusCodes.Good, "Норма"),
        (t + timedelta(seconds=40) + ms, ua.StatusCodes.Good, "Дыма нет"),
    ]
    assert [(ts, v) for ts, _, v in events("ch/287267/Alarm")] == [
        (t + timedelta(seconds=10), True),
        (t + timedelta(seconds=30), True),
        (t + timedelta(seconds=40), False),
        (t + timedelta(seconds=40) + ms, False),
    ]
    assert events("ch/120298/Value") == [(t + timedelta(seconds=40), ua.StatusCodes.Good, "18")]
    assert [v for _, _, v in events("Emulator/State")][-1] == "finished"
    assert [v for _, _, v in events("Emulator/RunId")][-1] == 42


def test_write_clock_strictly_increases(monkeypatch):
    """Метка записи не повторяется и не идёт назад, даже если системные часы шагнули назад."""
    import app.server as server

    fixed = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    clock = server.WriteClock()
    monkeypatch.setattr(server, "datetime", Frozen)
    first, second = clock(), clock()
    fixed -= timedelta(seconds=5)
    third = clock()
    assert first < second < third and second - first == timedelta(microseconds=1)


def test_events_belong_to_run_by_write_time(endpoint, catalog, tmp_path):
    """Новый запуск посреди прогона. `RunId` и события — разные элементы подписки, а сервер собирает уведомления
    цикла публикации по элементам: события нового прогона по каналу, который в этом цикле уже менялся (сброс перед
    запуском, хвост прошлого прогона), приходят раньше нового `RunId`. Прогон события однозначно восстанавливается
    по меткам записи: `RunId` пишется каждый шаг, метки возрастают (emulator.md §3, §7)."""
    from conftest import write_scenario, write_slice

    from app.player import Player

    # два сценария на одних каналах, событие в секунду, как Magdeburg: 30 → 31 на стенде
    for name, hour in (("a", 8), ("b", 9)):
        write_scenario(tmp_path / "sc", name, f"2026-03-28T0{hour}:00:00", f"2026-03-28T0{hour}:10:00")
        rows = [
            (i, (287267, 120298)[i % 2], f"2026-03-28 0{hour}:0{i // 60}:{i % 60:02d}", False, str(i))
            for i in range(1, 400)
        ]
        write_slice(tmp_path / "sl" / f"{name}.parquet", rows)

    async def scenario():
        emulator = Emulator(catalog)
        player = Player(emulator, tmp_path / "sc", tmp_path / "sl", max_rate=2000, tick=0.05)
        sent: dict[tuple[int, datetime], int] = {}  # (канал, метка источника) → прогон проигрывателя при записи
        publish = emulator.publish

        async def recording(channel_id, source_ts, value, alarm):
            sent[(channel_id, source_ts)] = player.run_id
            await publish(channel_id, source_ts, value, alarm)

        emulator.publish = recording
        collector = Collector()
        async with running(emulator, player):
            async with Client(endpoint) as client:
                ids = [f"ch/{c}/Value" for c in (287267, 120298)] + ["Emulator/RunId"]
                sub = await client.create_subscription(200, collector)
                trigger = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp)
                await sub._subscribe(
                    [client.get_node(ua.NodeId(i, emulator.ns)) for i in ids], mfilter=trigger, queuesize=1000
                )
                await player.start("a", speed=60, run_id=41)
                await asyncio.sleep(0.5)
                await player.start("b", speed=60, run_id=42)
                await asyncio.sleep(0.5)
                await player.stop()
                await asyncio.sleep(0.5)
        return collector.items, sent

    items, sent = asyncio.run(scenario())
    run_marks = [(dv.ServerTimestamp, dv.Value.Value) for n, dv in items if n == "Emulator/RunId"]
    assert len(run_marks) > 10  # пульс: RunId пишется каждый шаг, не только при смене
    changes = [(ts, run) for k, (ts, run) in enumerate(run_marks) if k == 0 or run != run_marks[k - 1][1]]

    def run_at(written: datetime) -> int:
        return [run for ts, run in changes if ts <= written][-1]

    events = {
        (int(n.split("/")[1]), dv.SourceTimestamp): run_at(dv.ServerTimestamp)
        for n, dv in items
        if n.startswith("ch/") and dv.StatusCode.value != ua.StatusCodes.BadWaitingForInitialData
    }
    assert set(sent.values()) == {41, 42}
    assert events == sent


def test_channel_properties_can_be_disabled(endpoint, catalog, monkeypatch):
    """Без свойств у канала только `Value` и `Alarm` — вдвое меньше памяти на машине разработчика."""
    monkeypatch.setattr(settings, "channel_properties", False)

    async def scenario():
        emulator = Emulator(catalog)
        async with running(emulator):
            async with Client(endpoint) as client:
                channel = client.get_node(ua.NodeId("ch/287267", emulator.ns))
                return {(await c.read_browse_name()).Name for c in await channel.get_children()}

    assert asyncio.run(scenario()) == {"Value", "Alarm"}


def test_external_dataset_is_a_separate_root(endpoint, catalog, tmp_path):
    """Внешний набор — корень `Objects/External/<набор>` со свойством `Dataset` (external-data.md §5.1)."""
    write_prepared(tmp_path / "external" / "magdeburg")
    add_external_catalogs(catalog, tmp_path / "external")

    async def scenario():
        async with running(Emulator(catalog)):
            async with Client(endpoint) as client:
                ns = await client.get_namespace_index(settings.opcua_namespace)
                root = await client.nodes.objects.get_child([f"{ns}:External", f"{ns}:9010001"])
                dataset = await (await root.get_child(f"{ns}:Dataset")).read_value()
                channel = await root.get_child([f"{ns}:9010002", f"{ns}:9010106", f"{ns}:901000601"])
                display = (await channel.read_display_name()).Text
                hackathon = [
                    (await c.read_browse_name()).Name
                    for c in await client.get_node(ua.NodeId("Moskollektor", ns)).get_children()
                ]
        return dataset, display, hackathon

    dataset, display, hackathon = asyncio.run(scenario())
    assert dataset == "magdeburg" and display == "CO₂ · узел 06"
    assert "9010001" not in hackathon and "5773" in hackathon  # в дерево хакатона набор не попадает
