"""Справочники объектов и каналов — основа адресного пространства (docs/architecture/emulator.md §3).

Формат файлов хакатона — docs/data-format.md §6–7. Справочник каналов читается только CSV-парсером: в названиях
есть переносы строк и запятые внутри кавычек. Справочники внешних наборов (docs/architecture/external-data.md §3)
— Parquet в data/processed/emulator/external/<набор>/, их кладёт `make emulator-slices`.
"""

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

log = logging.getLogger("emulator.catalog")

PICKET_RE = re.compile(r"ПК\s?(\d+(?:[,.]\d+)?(?:-\d+)?)")


@dataclass(frozen=True)
class Obj:
    id: int
    level: int
    parent_id: int | None
    kind: str
    name: str
    dataset: str | None = None  # ключ внешнего набора; у хакатона — None


@dataclass(frozen=True)
class Channel:
    id: int
    object_id: int
    subsystem: str
    sensor_type: str
    tag: str
    name: str

    @property
    def picket(self) -> str:
        """Пикет из названия как есть: `12`, `86-85`, `88,5`; пустая строка, если пикета нет."""
        m = PICKET_RE.search(self.name)
        return m.group(1) if m else ""


@dataclass
class Catalog:
    objects: dict[int, Obj] = field(default_factory=dict)
    channels: dict[int, Channel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._index()

    def _index(self) -> None:
        self._children: dict[int | None, list[Obj]] = defaultdict(list)
        for o in sorted(self.objects.values(), key=lambda o: o.id):
            # Родитель района в справочник не входит — такие объекты становятся корнями дерева.
            parent = o.parent_id if o.parent_id in self.objects else None
            self._children[parent].append(o)
        self._channels_of: dict[int, list[Channel]] = defaultdict(list)
        for c in sorted(self.channels.values(), key=lambda c: c.id):
            self._channels_of[c.object_id].append(c)

    def roots(self) -> list[Obj]:
        return self._children[None]

    def merge(self, other: "Catalog") -> list[int]:
        """Добавить справочник внешнего набора. Id наборов не пересекаются с хакатоном (external-data.md §3.1);
        набор, где пересечение всё же есть, не добавляется целиком — возвращаются пересёкшиеся id."""
        clash = sorted((other.objects.keys() & self.objects.keys()) | (other.channels.keys() & self.channels.keys()))
        if not clash:
            self.objects.update(other.objects)
            self.channels.update(other.channels)
            self._index()
        return clash

    def children(self, object_id: int) -> list[Obj]:
        return self._children.get(object_id, [])

    def channels_of(self, object_id: int) -> list[Channel]:
        return self._channels_of.get(object_id, [])

    def descendants(self, object_ids: list[int]) -> set[int]:
        """Объекты вместе со всеми вложенными."""
        result: set[int] = set()
        stack = list(object_ids)
        while stack:
            oid = stack.pop()
            if oid not in result:
                result.add(oid)
                stack.extend(o.id for o in self.children(oid))
        return result

    def channel_ids(self, object_ids: list[int] | None = None) -> set[int]:
        """Каналы объектов вместе с вложенными; без фильтра — все каналы."""
        if not object_ids:
            return set(self.channels)
        scope = self.descendants(object_ids)
        return {c.id for c in self.channels.values() if c.object_id in scope}


def load_catalog(objects_csv: Path, channels_csv: Path) -> Catalog:
    objects: dict[int, Obj] = {}
    with objects_csv.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            parent = row["родитель"].strip()
            obj = Obj(
                id=int(row["ид_объект"]),
                level=int(row["иерархия_уровень"]),
                parent_id=int(parent) if parent else None,
                kind=row["вид_объекта"],
                name=row["диспетчерское_название_объекта"],
            )
            objects[obj.id] = obj
    channels: dict[int, Channel] = {}
    with channels_csv.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            ch = Channel(
                id=int(row["ид_канала_данных"]),
                object_id=int(row["ид_объект"]),
                subsystem=row["тип_инж_системы"],
                sensor_type=row["тип_датчика"],
                tag=row["тег_инженерной_системы"],
                # в названиях бывают переносы строк — в адресном пространстве они не нужны
                name=" ".join(row["название_датчика"].split()),
            )
            channels[ch.id] = ch
    return Catalog(objects=objects, channels=channels)


def load_external_catalog(dataset_dir: Path) -> Catalog:
    """Справочник внешнего набора: `objects.parquet`, `channels.parquet` в формате prepared/ (external-data.md §4.1)."""
    dataset = dataset_dir.name
    with duckdb.connect() as con:  # своё соединение, не общее по умолчанию
        object_rows = con.execute(
            "SELECT id, parent_id, level, kind, name FROM read_parquet($path)",
            {"path": str(dataset_dir / "objects.parquet")},
        ).fetchall()
        channel_rows = con.execute(
            "SELECT id, object_id, system_type, sensor_type, tag, name FROM read_parquet($path)",
            {"path": str(dataset_dir / "channels.parquet")},
        ).fetchall()
    objects = {
        oid: Obj(id=oid, level=int(level), parent_id=parent, kind=kind, name=name, dataset=dataset)
        for oid, parent, level, kind, name in object_rows
    }
    channels = {
        cid: Channel(
            id=cid,
            object_id=object_id,
            subsystem=subsystem or "",
            sensor_type=sensor_type or "",
            tag=tag or "",
            name=" ".join((name or "").split()),
        )
        for cid, object_id, subsystem, sensor_type, tag, name in channel_rows
    }
    return Catalog(objects=objects, channels=channels)


def add_external_catalogs(catalog: Catalog, external_dir: Path) -> list[str]:
    """Влить в справочник все наборы из `external_dir/<набор>/`; вернуть ключи добавленных."""
    added = []
    for dataset_dir in sorted(p for p in external_dir.glob("*") if (p / "objects.parquet").is_file()):
        try:
            clash = catalog.merge(load_external_catalog(dataset_dir))
        except (OSError, duckdb.Error) as e:
            log.error("справочник набора %s не прочитан: %s", dataset_dir.name, e)
            continue
        if clash:
            log.error("набор %s пропущен: id пересекаются со справочником, первый — %s", dataset_dir.name, clash[0])
            continue
        added.append(dataset_dir.name)
    return added


def emulator_catalog(objects_csv: Path, channels_csv: Path, external_dir: Path) -> Catalog:
    """Справочник эмулятора: хакатона и внешних наборов из `make emulator-slices` (external-data.md §5.1). Им же
    подготовка срезов считает сводки заранее — ключ кэша сводки совпадает с тем, что посчитал бы эмулятор."""
    if objects_csv.is_file() and channels_csv.is_file():
        catalog = load_catalog(objects_csv, channels_csv)
    else:
        log.warning(
            "справочников нет (%s, %s) — адресное пространство только со служебными узлами", objects_csv, channels_csv
        )
        catalog = Catalog()
    if datasets := add_external_catalogs(catalog, external_dir):
        log.info("внешние наборы: %s", ", ".join(datasets))
    return catalog
