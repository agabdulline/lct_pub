"""Шлюз на настоящем OPC UA: гонка `RunId` и событий при смене сценария (прогоны 30–33 стенда).

Сервер asyncua собирает уведомления цикла публикации по элементам подписки, в порядке первого изменения элемента
в цикле. Канал, который в цикле уже менялся (хвост прошлого прогона, сброс перед запуском), идёт в уведомлении
раньше `RunId`, и события нового прогона по нему приходят до нового `RunId`. Сервер здесь пишет узлы так же, как
эмулятор (`emulator/app/server.py`): события с меткой записи, `RunId` — пульсом каждый шаг.
"""

import asyncio
import contextlib
import socket
from datetime import UTC, datetime, timedelta

import pytest
from asyncua import Server, ua

from app.domain.ingest import IngestResult
from app.gateway import opcua
from app.gateway.service import Gateway

pytestmark = pytest.mark.anyio
NS = "urn:lct:test"
T = datetime(2026, 3, 28, 5, 0, tzinfo=UTC)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Source:
    """Узлы как у эмулятора: объект, канал 1001 с `Value` и `Alarm`, служебные узлы; метки записи растут."""

    def __init__(self, server: Server, ns: int) -> None:
        self.server, self.ns = server, ns
        self.last = datetime.now(UTC)

    def clock(self) -> datetime:
        self.last = max(datetime.now(UTC), self.last + timedelta(microseconds=1))
        return self.last

    async def event(self, second: int, value: str) -> None:
        now, ts = self.clock(), T + timedelta(seconds=second)
        for name, variant in (("Value", ua.Variant(value, ua.VariantType.String)), ("Alarm", ua.Variant(False))):
            dv = ua.DataValue(Value=variant, SourceTimestamp=ts, ServerTimestamp=now)
            await self.server.write_attribute_value(ua.NodeId(f"ch/1001/{name}", self.ns), dv)

    async def reset(self) -> None:
        now = self.clock()
        for name in ("Value", "Alarm"):
            dv = ua.DataValue(StatusCode=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData), ServerTimestamp=now)
            await self.server.write_attribute_value(ua.NodeId(f"ch/1001/{name}", self.ns), dv)

    async def run_id(self, run: int) -> None:
        now = self.clock()
        dv = ua.DataValue(Value=ua.Variant(run, ua.VariantType.Int64), SourceTimestamp=now, ServerTimestamp=now)
        await self.server.write_attribute_value(ua.NodeId("Emulator/RunId", self.ns), dv)


async def make_server(endpoint: str) -> tuple[Server, Source]:
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    ns = await server.register_namespace(NS)
    root = await server.nodes.objects.add_folder(ua.NodeId("Moskollektor", ns), ua.QualifiedName("Moskollektor", ns))
    emulator = await root.add_object(ua.NodeId("Emulator", ns), ua.QualifiedName("Emulator", ns))
    initial = {
        "RunId": ua.Variant(30, ua.VariantType.Int64),
        "EventsSent": ua.Variant(0, ua.VariantType.Int64),
        "State": ua.Variant("running", ua.VariantType.String),
        "Scenario": ua.Variant("a", ua.VariantType.String),
        "TargetSpeed": ua.Variant(60.0, ua.VariantType.Double),
        "EffectiveSpeed": ua.Variant(60.0, ua.VariantType.Double),
        "SimulatedTime": ua.Variant(T, ua.VariantType.DateTime),
    }
    for name, variant in initial.items():
        await emulator.add_variable(ua.NodeId(f"Emulator/{name}", ns), ua.QualifiedName(name, ns), variant)
    # объекты и каналы — по ссылкам Organizes, как в дереве эмулятора: по ним шлюз обходит каналы
    obj = await root.add_folder(ua.NodeId("obj/100", ns), ua.QualifiedName("100", ns))
    channel = await obj.add_folder(ua.NodeId("ch/1001", ns), ua.QualifiedName("1001", ns))
    for name, data_type in (("Value", ua.ObjectIds.String), ("Alarm", ua.ObjectIds.Boolean)):
        empty = ua.Variant(None, ua.VariantType.Null)
        node = ua.NodeId(f"ch/1001/{name}", ns)
        await channel.add_variable(node, ua.QualifiedName(name, ns), empty, datatype=ua.NodeId(data_type))
    return server, Source(server, ns)


class FakeIngest:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, list]] = []

    async def __call__(self, run_id, events):
        self.calls.append((run_id, list(events)))
        return IngestResult(accepted=len(events))


class Recording(Gateway):
    """Шлюз, который помнит порядок прихода: события и значения `RunId`."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.arrivals: list[tuple[str, object]] = []
        self.notified = asyncio.Event()

    def on_event(self, event) -> None:
        self.arrivals.append(("event", event.value_raw))
        self.notified.set()
        super().on_event(event)

    async def on_service(self, name: str, value, written=None) -> None:
        if name == "RunId":
            self.arrivals.append(("RunId", value))
            self.notified.set()
        await super().on_service(name, value, written)


async def test_first_events_of_new_scenario_go_to_new_run(monkeypatch):
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/lct/test"
    monkeypatch.setattr(opcua, "PUBLISHING_INTERVAL_MS", 1000)  # все записи смены — в одном цикле публикации
    server, src = await make_server(endpoint)
    ingest = FakeIngest()
    gw = Recording(ingest=ingest)
    source = opcua.OpcUaSource(endpoint, NS)
    async with server:
        task = asyncio.create_task(source.run(gw))
        try:
            for _ in range(100):  # подписка создана, начальные значения пришли — цикл только что опубликован
                await asyncio.sleep(0.05)
                if gw._connected_at is not None and ("RunId", 30) in gw.arrivals:
                    break
            gw.notified.clear()
            await src.run_id(30)
            await asyncio.wait_for(gw.notified.wait(), 5)
            # смена сценария, как у эмулятора: хвост 30, сброс канала, RunId 31, первые события 31
            await src.event(1, "old")
            await src.reset()
            await src.run_id(31)
            await src.event(900, "new1")
            await src.event(901, "new2")
            await asyncio.sleep(1.5)
            await src.run_id(31)  # пульс следующего шага
            await asyncio.sleep(1.5)
            await gw.flush()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    order = [item for item in gw.arrivals if item in {("event", "new1"), ("RunId", 31)}]
    assert order[0] == ("event", "new1")  # гонка воспроизведена: событие 31 пришло раньше его RunId
    runs = {run: [e.value_raw for e in events] for run, events in ingest.calls}
    assert runs == {30: ["old"], 31: ["new1", "new2"]}


async def test_quiet_tail_moves_clock_and_finishes_run(session, monkeypatch):
    """Прогон 54 стенда: `guard-hatch-night` доигран — в конце 17 мин без событий, — а прогон закрыт как прерванный
    (`stopped`), часы в базе — на последнем событии. Узлы шага пишутся, как у эмулятора: `RunId`, `State` при смене,
    `SimulatedTime`, — и последнее пришедшее `SimulatedTime` всегда новее последнего пульса `RunId`: по нему часы в базу
    не шли. `State = finished` пришёл раньше следующего пульса, а сверка прогонов смотрела на сырое состояние."""
    from sqlalchemy import text

    from app.domain.runs import create_run

    start, end = T, T + timedelta(minutes=70)
    run_id = (await create_run(session, scenario="guard-hatch-night", sim_start=start, sim_end=end, speed=60)).id
    await session.execute(
        text("UPDATE runs SET started_at = now() - interval '5 minutes' WHERE id = :r"), {"r": run_id}
    )  # старше отсрочки сверки
    await session.commit()

    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/lct/test"
    monkeypatch.setattr(opcua, "PUBLISHING_INTERVAL_MS", 200)
    server, src = await make_server(endpoint)
    gw = Gateway(ingest=FakeIngest())
    source = opcua.OpcUaSource(endpoint, NS)

    async def write(name: str, value, kind) -> None:
        now = src.clock()
        dv = ua.DataValue(Value=ua.Variant(value, kind), SourceTimestamp=now, ServerTimestamp=now)
        await server.write_attribute_value(ua.NodeId(f"Emulator/{name}", src.ns), dv)

    async def step(sim: datetime, state: str | None = None) -> None:
        await write("RunId", run_id, ua.VariantType.Int64)
        if state:
            await write("State", state, ua.VariantType.String)
        await write("SimulatedTime", sim, ua.VariantType.DateTime)

    async def arrived(condition, what: str) -> None:
        for _ in range(100):
            if condition():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"не дождались: {what}")

    async def db_run() -> tuple:
        await session.rollback()
        row = await session.execute(text("SELECT status, sim_now FROM runs WHERE id = :r"), {"r": run_id})
        return tuple(row.one())

    async with server:
        task = asyncio.create_task(source.run(gw))
        try:
            await arrived(lambda: gw._connected_at is not None and gw.service.get("RunId") == 30, "подписка")
            await step(start, "running")
            for k in range(1, 16):  # хвост без событий: 15 шагов по 0.1 с, ×600
                await asyncio.sleep(0.1)
                await step(start + timedelta(minutes=k))
            await asyncio.sleep(0.5)
            await gw.sync_run()
            status, sim_now = await db_run()
            assert status == "running" and sim_now > start  # часы идут и без событий

            await step(end, "finished")  # конец сценария; следующего пульса RunId ещё нет
            await arrived(lambda: gw.service.get("State") == "finished", "State = finished")
            gw._reconciled_at = 0
            assert await gw.reconcile_runs() == []  # не «прерван»: состояние ещё не отнесено к прогону
            await step(end)  # пульс следующего шага
            await arrived(lambda: gw.settled.get("State") == (run_id, "finished"), "пульс после конца")
            await gw.sync_run()
            gw._reconciled_at = 0
            assert await gw.reconcile_runs() == []
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert await db_run() == ("finished", end)


async def test_lost_events_are_counted_on_the_run(session, monkeypatch):
    """Прогон 60 стенда закрылся `finished` без предупреждения, хотя 3 600 событий не дошли. Источник пишет
    `EventsSent` — сколько событий отправлено до этой метки записи; шлюз сверяет с принятыми, и прогон несёт
    `events_lost` и окна симуляционного времени. Здесь источник «отправил» 7 событий, а до шлюза дошли 5."""
    from sqlalchemy import text

    from app.domain.runs import create_run

    run_id = (await create_run(session, scenario="loss", sim_start=T, sim_end=T + timedelta(hours=1), speed=60)).id
    await session.commit()
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/lct/test"
    monkeypatch.setattr(opcua, "PUBLISHING_INTERVAL_MS", 100)
    server, src = await make_server(endpoint)
    gw = Gateway(ingest=FakeIngest())
    source = opcua.OpcUaSource(endpoint, NS)

    async def write(name: str, value, kind) -> None:
        now = src.clock()
        dv = ua.DataValue(Value=ua.Variant(value, kind), SourceTimestamp=now, ServerTimestamp=now)
        await server.write_attribute_value(ua.NodeId(f"Emulator/{name}", src.ns), dv)

    async def step(minute: int, sent: int) -> None:
        await write("RunId", run_id, ua.VariantType.Int64)
        await write("EventsSent", sent, ua.VariantType.Int64)
        await write("SimulatedTime", T + timedelta(minutes=minute), ua.VariantType.DateTime)

    async with server:
        task = asyncio.create_task(source.run(gw))
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if gw._connected_at is not None and gw.service.get("RunId") == 30:
                    break
            await step(0, 0)
            for second in (1, 2, 3):
                await src.event(second, f"v{second}")
            await step(1, 3)
            await src.event(4, "v4")
            await step(2, 6)  # два события шага 2 «не дошли»: счётчик вырос на 3, записано одно
            await src.event(5, "v5")
            await step(3, 7)
            for minute in range(4, 20):  # пульс: отсчёты становятся сверяемыми (секунда после них)
                await asyncio.sleep(0.1)
                await step(minute, 7)
            await asyncio.sleep(0.3)
            gw._losses_checked_at = 0
            assert await gw.check_losses() == [run_id]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await session.rollback()
    lost, windows = (
        await session.execute(text("SELECT events_lost, loss_windows FROM runs WHERE id = :r"), {"r": run_id})
    ).one()
    assert lost == 2
    assert [(w["start_ts"], w["end_ts"], w["events"]) for w in windows] == [
        ((T + timedelta(minutes=1)).isoformat(), (T + timedelta(minutes=2)).isoformat(), 2)
    ]
