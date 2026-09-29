"""Маленькие справочники в формате исходных (docs/data-format.md §6–7) и запущенный эмулятор для тестов."""

import asyncio
import contextlib
import json
import socket
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
import yaml

from app.catalog import Catalog, load_catalog
from app.config import settings
from app.player import Player
from app.server import Emulator

OBJECTS_CSV = """\
"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"
5773,1,3831,district,Район по эксплуатации
7,2,5773,controlHouse,объект Гамма
5961,3,7,guardObject,объект Гамма ОС
5962,3,7,guardObject,объект Гамма ПС
"""

CHANNELS_CSV = """\
"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы","название_датчика","ид_объект"
287267,Пожарная охрана,Датчик дыма,"15-11.1.131.2.",ДД ПК12,5961
287268,Пожарная охрана,Ручной извещатель,"15-11.1.131.3.",РИ ПК86-85,5961
120298,Температурная подсистема,Датчик температуры,"15-11.1.4.1.","Темп. ВШ
ПК88,5",5962
300001,Газовая подсистема,Газовый датчик,"15-12.2.",ГД ВШ,5962
"""


@pytest.fixture
def catalog_files(tmp_path: Path) -> tuple[Path, Path]:
    objects, channels = tmp_path / "objects.csv", tmp_path / "channels.csv"
    objects.write_text(OBJECTS_CSV, encoding="utf-8")
    channels.write_text(CHANNELS_CSV, encoding="utf-8")
    return objects, channels


@pytest.fixture
def catalog(catalog_files) -> Catalog:
    return load_catalog(*catalog_files)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def endpoint(monkeypatch) -> str:
    url = f"opc.tcp://127.0.0.1:{free_port()}/lct/smvu"
    monkeypatch.setattr(settings, "opcua_endpoint", url)
    monkeypatch.setattr(settings, "tick_seconds", 0.1)
    return url


@contextlib.asynccontextmanager
async def running(emulator: Emulator, player: Player | None = None):
    """Эмулятор и, если задан, проигрыватель в фоне текущего цикла asyncio."""
    await emulator.init()
    async with emulator.server:
        task = asyncio.create_task(player.run()) if player else None
        await asyncio.sleep(0.2)
        try:
            yield emulator
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def write_slice(path: Path, rows: list[tuple[int, int, str, bool, str]]) -> None:
    """Срез в формате prepare: (event_id, channel_id, ts, is_alarm, value), время — строкой по Москве."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE s (event_id BIGINT, channel_id INTEGER, ts TIMESTAMP, is_alarm BOOLEAN, value VARCHAR)")
    con.executemany("INSERT INTO s VALUES (?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY s TO '{path}' (FORMAT parquet)")


def write_upload(slices_dir: Path, scenario_id: str, rows: list[tuple], **meta) -> Path:
    """Загруженный сценарий в формате api (13.1): срез и метаданные с обоими наборами ключей."""
    uploaded = slices_dir / "uploaded"
    write_slice(uploaded / f"{scenario_id}.parquet", rows)
    data = {
        "scenario_id": scenario_id,
        "id": scenario_id,
        "title": "Мой журнал",
        "description": None,
        "period": {"start": "2026-08-01T00:00:00", "end": "2026-08-01T01:00:00"},
        "objects": [7],
        "events": len(rows),
        "channels": len({r[1] for r in rows}),
        "warnings": [{"code": "duplicates", "count": 1, "message": "Повтор строки удалён"}],
        "source": {
            "file_name": "журнал.xlsx",
            "size_bytes": 1000,
            "format": {},
            "uploaded_at": "2026-09-27T10:00:00+03:00",
        },
        "source_file": "журнал.xlsx",
        "uploaded_at": "2026-09-27T10:00:00+03:00",
        "include_gas": True,
        "mapping": {},
        **meta,
    }
    path = uploaded / f"{scenario_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


EXTERNAL_OBJECTS = [  # как prepared/ набора (external-data.md §4.1): 1 — набор, 2 — цех, 3 — узлы
    (9010001, None, 1, "external_site", "Набор · цех (внешние данные)"),
    (9010002, 9010001, 2, "hall", "Цех"),
    (9010106, 9010002, 3, "sensor_node", "Узел 06"),
]
EXTERNAL_CHANNELS = [
    (901000600, 9010106, "Пожарный мониторинг (внешн.)", "Узел", "node", "Узел 06 · оценка"),
    (901000601, 9010106, "Пожарный мониторинг (внешн.)", "Датчик CO₂", "CO2_Room", "CO₂ · узел 06"),
    (901000602, 9010106, "Пожарный мониторинг (внешн.)", "Датчик CO", "CO_Room", "CO · узел 06"),
]


def write_prepared(dataset_dir: Path, events: list[tuple] | None = None) -> Path:
    """Справочник и журнал набора в формате prepared/; `dataset_dir` — data/external/<набор>/prepared или копия
    справочника в <slices>/external/<набор>."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE o (id INTEGER, parent_id INTEGER, level SMALLINT, kind VARCHAR, name VARCHAR, meta VARCHAR)"
    )
    con.executemany("INSERT INTO o VALUES (?, ?, ?, ?, ?, NULL)", EXTERNAL_OBJECTS)
    con.execute(f"COPY o TO '{dataset_dir / 'objects.parquet'}' (FORMAT parquet)")
    con.execute(
        "CREATE TABLE c (id INTEGER, object_id INTEGER, system_type VARCHAR, sensor_type VARCHAR, tag VARCHAR, "
        "name VARCHAR, unit VARCHAR)"
    )
    con.executemany("INSERT INTO c VALUES (?, ?, ?, ?, ?, ?, NULL)", EXTERNAL_CHANNELS)
    con.execute(f"COPY c TO '{dataset_dir / 'channels.parquet'}' (FORMAT parquet)")
    if events is not None:
        write_slice(dataset_dir / "events.parquet", events)
    return dataset_dir


def write_scenario(scenarios_dir: Path, name: str, start: str, end: str, **extra) -> None:
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "title": name,
        "period": {"start": datetime.fromisoformat(start), "end": datetime.fromisoformat(end)},
        "objects": [7],
        **extra,
    }
    (scenarios_dir / f"{name}.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


class FakeEmulator:
    """Эмулятор без OPC UA: запоминает, что опубликовано."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.published: list[tuple[int, datetime, str, bool]] = []
        self.resets: list[set[int]] = []
        self.service: dict = {}

    async def publish(self, channel_id, source_ts, value, alarm) -> None:
        self.published.append((channel_id, source_ts, value, alarm))

    async def reset_channels(self, channel_ids) -> None:
        self.resets.append(set(channel_ids))

    async def set_service(self, force_time: bool = True, **values) -> None:
        self.service.update(values)
