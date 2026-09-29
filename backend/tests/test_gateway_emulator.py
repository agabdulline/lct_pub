"""Шлюз против поднятого эмулятора СМВУ: сценарий → OPC UA → шлюз → приём событий → `events`.

Эмулятор запускается отдельным процессом из `../emulator` (`uv run python -m app.main`) на маленьком справочнике,
совпадающем с тестовым справочником БД. Тест пропускается, если нет `uv` или PostgreSQL стенда.
"""

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import duckdb
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.domain.ingest import IngestResult, ingest_events
from app.gateway.opcua import OpcUaSource
from app.gateway.service import Gateway

pytestmark = pytest.mark.anyio
EMULATOR = Path(__file__).resolve().parents[2] / "emulator"
NAMESPACE = "urn:lct-burrgers:smvu"

# Тот же справочник, что в tests/conftest.py: район 1 → объект 10 → подобъект 100, каналы 1001–1003
OBJECTS_CSV = """\
"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"
1,1,0,district,Район
10,2,1,controlHouse,Объект А
100,3,10,controlHouse,Подобъект А1
"""
CHANNELS_CSV = """\
"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы","название_датчика","ид_объект"
1001,Пожарная охрана,Датчик дыма,"15-11.1.1.1.",ДД ПК1,100
1002,Газовая,Газовый датчик,"15-11.1.2.1.",ГАЗ Д1 ПК2,100
1003,Температурная,Датчик температуры,"15-11.1.3.1.",ТЕМП ПК3,100
"""
SCENARIO = """\
title: e2e
period: {start: 2026-03-28T08:00:00, end: 2026-03-28T08:01:00}
objects: [10]
include_gas: true
speed: 60
"""
SLICE = [
    (1, 1001, "2026-03-28 08:00:10", True, "Обнаружен дым"),
    (2, 1002, "2026-03-28 08:00:12", False, "0.05"),
    (3, 1001, "2026-03-28 08:00:30", True, "Неисправен"),
    (4, 1003, "2026-03-28 08:00:35", False, "-127"),
    (5, 1001, "2026-03-28 08:00:40", False, "Норма"),
    (6, 1001, "2026-03-28 08:00:40", False, "Дыма нет"),  # та же секунда канала
    (7, 1003, "2026-03-28 08:00:50", False, "Неопределен"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port: int, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="GET" if data is None else "POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def write_external_catalog(slices: Path) -> None:
    """Справочник внешнего набора, как его кладёт `make emulator-slices` (external-data.md §5.1)."""
    target = slices / "external" / "testset"
    target.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * FROM (VALUES (9010001, NULL, 1, 'external_site', 'Набор'), "
        f"(9010002, 9010001, 2, 'hall', 'Цех'), (9010101, 9010002, 3, 'sensor_node', 'Узел 01')) "
        f"t(id, parent_id, level, kind, name)) TO '{target / 'objects.parquet'}' (FORMAT parquet)"
    )
    con.execute(
        f"COPY (SELECT 901000101 AS id, 9010101 AS object_id, 'Пожарный мониторинг (внешн.)' AS system_type, "
        f"'Датчик CO' AS sensor_type, 'CO_Room' AS tag, 'CO · узел 01' AS name) "
        f"TO '{target / 'channels.parquet'}' (FORMAT parquet)"
    )


@contextlib.contextmanager
def emulator_process(tmp: Path, external: bool = False):
    if shutil.which("uv") is None or not (EMULATOR / "pyproject.toml").is_file():
        pytest.skip("нет uv или папки emulator")
    (tmp / "objects.csv").write_text(OBJECTS_CSV, encoding="utf-8")
    (tmp / "channels.csv").write_text(CHANNELS_CSV, encoding="utf-8")
    (tmp / "scenarios").mkdir()
    (tmp / "scenarios" / "e2e.yaml").write_text(SCENARIO, encoding="utf-8")
    (tmp / "slices").mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s (event_id BIGINT, channel_id INTEGER, ts TIMESTAMP, is_alarm BOOLEAN, value VARCHAR)")
    con.executemany("INSERT INTO s VALUES (?, ?, ?, ?, ?)", SLICE)
    con.execute(f"COPY s TO '{tmp / 'slices' / 'e2e.parquet'}' (FORMAT parquet)")
    if external:
        write_external_catalog(tmp / "slices")

    opc, control = free_port(), free_port()
    env = {
        **os.environ,
        "OPCUA_ENDPOINT": f"opc.tcp://127.0.0.1:{opc}/lct/smvu",
        "CONTROL_PORT": str(control),
        "OBJECTS_CSV": str(tmp / "objects.csv"),
        "CHANNELS_CSV": str(tmp / "channels.csv"),
        "SCENARIOS_DIR": str(tmp / "scenarios"),
        "SLICES_DIR": str(tmp / "slices"),
        "TICK_SECONDS": "0.05",
    }
    env.pop("VIRTUAL_ENV", None)
    proc = subprocess.Popen(
        ["uv", "run", "--quiet", "python", "-m", "app.main"],
        cwd=EMULATOR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # своя группа: `uv run` и эмулятор замораживаются вместе (SIGSTOP группе)
    )
    try:
        yield f"opc.tcp://127.0.0.1:{opc}/lct/smvu", control, proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def wait_for(condition, timeout: float, what: str) -> None:
    for _ in range(int(timeout / 0.1)):
        with contextlib.suppress(Exception):
            if await condition():
                return
        await asyncio.sleep(0.1)
    raise AssertionError(f"не дождались: {what}")


async def test_gateway_ingests_scenario_from_emulator(session, db_url, tmp_path, monkeypatch):
    # как в работе: приём и статус прогона — своими сессиями с коммитом на пачку
    engine = create_async_engine(db_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.domain.ingest.SessionLocal", sessions)
    monkeypatch.setattr("app.gateway.service.SessionLocal", sessions)
    await session.execute(
        text("INSERT INTO runs (id, scenario, status, sim_start) VALUES (7, 'e2e', 'running', '2026-03-28 08:00+03')")
    )
    await session.commit()  # прогон видят сессии шлюза
    with emulator_process(tmp_path) as (endpoint, control, _):
        source = OpcUaSource(endpoint, NAMESPACE, reconnect_seconds=0.5)
        gateway = Gateway(ingest=ingest_events, flush_seconds=0.2, pairer=source.pairer)
        tasks = [asyncio.create_task(source.run(gateway)), asyncio.create_task(gateway.run())]
        connected = asyncio.Event()
        on_connection = gateway.on_connection

        async def mark(ok: bool) -> None:
            await on_connection(ok)
            if ok:
                connected.set()

        gateway.on_connection = mark
        try:
            await asyncio.wait_for(connected.wait(), 30)
            state = await asyncio.to_thread(http, control, "/start", {"scenario": "e2e", "speed": 60, "run_id": 7})
            assert state["state"] == "running"

            async def finished():
                state = await asyncio.to_thread(http, control, "/state")
                return state["state"] == "finished" and gateway.service.get("State") == "finished"

            await wait_for(finished, 20, "сценарий доигран")

            async def run_finished():
                async with engine.connect() as conn:
                    status = await conn.scalar(text("SELECT status FROM runs WHERE id = 7"))
                return status == "finished" and not gateway.buffer

            await wait_for(run_finished, 20, "прогон закрыт шлюзом, последняя пачка записана")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    await session.rollback()  # увидеть то, что закоммитили сессии шлюза
    rows = (
        await session.execute(
            text(
                "SELECT channel_id, to_char(ts AT TIME ZONE 'Europe/Moscow', 'HH24:MI:SS'), is_alarm, value_raw, "
                "run_id, event_id FROM events ORDER BY ts, value_raw"
            )
        )
    ).all()
    assert rows == [
        (1001, "08:00:10", True, "Обнаружен дым", 7, None),
        (1002, "08:00:12", False, "0.05", 7, None),
        (1001, "08:00:30", True, "Неисправен", 7, None),  # из Bad_DeviceFailure
        (1003, "08:00:35", False, "-127", 7, None),
        (1001, "08:00:40", False, "Дыма нет", 7, None),  # +1 мс отброшена
        (1001, "08:00:40", False, "Норма", 7, None),
        (1003, "08:00:50", False, "Неопределен", 7, None),
    ]
    assert gateway.stats.without_run == 0 and gateway.pairer.pending == 0
    assert gateway.run_id == 7
    # статус и часы прогона — из состояния эмулятора
    run = (await session.execute(text("SELECT status, finished_at IS NOT NULL, sim_now FROM runs WHERE id = 7"))).one()
    assert run[:2] == ("finished", True) and run[2] is not None
    await engine.dispose()


async def discover(endpoint: str) -> list[int]:
    """Обход дерева шлюзом, как при подключении; эмулятор поднимается несколько секунд."""
    from asyncua import Client

    source = OpcUaSource(endpoint, NAMESPACE)
    for _ in range(100):
        with contextlib.suppress(Exception):
            async with Client(url=endpoint, timeout=5) as client:
                return await source.discover(client, await client.get_namespace_index(NAMESPACE))
        await asyncio.sleep(0.1)
    raise AssertionError("эмулятор не поднялся")


async def test_gateway_discovers_external_roots(tmp_path):
    """Каналы внешних наборов — в корне `External`; без него (реальная СМВУ) обход тот же."""
    for external, expected in ((True, [1001, 1002, 1003, 901000101]), (False, [1001, 1002, 1003])):
        tmp = tmp_path / str(external)
        tmp.mkdir()
        with emulator_process(tmp, external) as (endpoint, _, _):
            assert await discover(endpoint) == expected


async def test_gateway_waits_out_stalled_emulator(tmp_path):
    """Эмулятор замер на 3 с (нагрузка, выкатка). Сторож asyncua по умолчанию (1 с) объявлял связь потерянной, шлюз
    переподписывался и терял всё, что эмулятор отправил за время переподключения (прогон 60 стенда — 3 600
    событий). Со сторожем 60 с шлюз ждёт, связь не рвётся; со сторожем 1 с — рвётся, как было."""
    import os
    import signal

    class NoIngest:
        async def __call__(self, run_id, events):
            return IngestResult(accepted=len(events))

    async def stall_and_watch(endpoint: str, proc, watchdog: float) -> list[bool]:
        source = OpcUaSource(endpoint, NAMESPACE, reconnect_seconds=0.5, watchdog_seconds=watchdog)
        gateway = Gateway(ingest=NoIngest(), flush_seconds=0.2, pairer=source.pairer)
        changes: list[bool] = []
        on_connection = gateway.on_connection

        async def mark(ok: bool) -> None:
            changes.append(ok)
            await on_connection(ok)

        gateway.on_connection = mark
        task = asyncio.create_task(source.run(gateway))

        async def connected() -> bool:
            return True in changes

        try:
            await wait_for(connected, 30, "подключение")
            group = os.getpgid(proc.pid)
            os.killpg(group, signal.SIGSTOP)
            await asyncio.sleep(3)
            os.killpg(group, signal.SIGCONT)
            await asyncio.sleep(2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return changes[changes.index(True) :]  # пока эмулятор поднимался — неудачные попытки, не в счёт

    with emulator_process(tmp_path) as (endpoint, _, proc):
        assert await stall_and_watch(endpoint, proc, watchdog=60) == [True]
        assert False in await stall_and_watch(endpoint, proc, watchdog=1)
