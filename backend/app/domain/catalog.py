"""Справочники объектов и каналов: чтение CSV и производные поля (docs/architecture/data.md §4).

Справочник каналов читается только CSV-парсером: в названиях есть переносы строк и запятые в кавычках.
"""

import csv
import re
from dataclasses import asdict, dataclass
from pathlib import Path

CHANNELS_FILE = "справочник_каналов_датчиков.csv"
OBJECTS_FILE = "справочник_объектов_диспетчер.csv"

# «ПК511+5», «ПК 56+8», «ПК4+4,5»: номер пикета и смещение после «+» в единицах названия
_PICKET = re.compile(r"ПК\s?(\d+)(?:\+(\d+(?:,\d+)?))?")


@dataclass(frozen=True, slots=True)
class ObjectRow:
    id: int
    parent_id: int | None
    level: int
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class ChannelRow:
    id: int
    object_id: int
    system_type: str
    sensor_type: str
    tag: str
    name: str
    picket_number: int | None
    picket_offset: float | None
    tag_group: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_picket(name: str) -> tuple[int | None, float | None]:
    """Первый пикет в названии канала; в диапазоне «ПК86-85» — первый номер."""
    m = _PICKET.search(name)
    if not m:
        return None, None
    offset = float(m.group(2).replace(",", ".")) if m.group(2) else None
    return int(m.group(1)), offset


def tag_group(tag: str) -> str | None:
    """Первые две части технического тега: «15-11.1» из «15-11.1.131.2.»."""
    parts = [p for p in tag.strip().rstrip(".").split(".") if p]
    return ".".join(parts[:2]) if parts else None


def read_objects(raw_dir: Path) -> list[ObjectRow]:
    """Объекты справочника. Родитель вне справочника (у района — 3831) заменяется на пустой."""
    with open(raw_dir / OBJECTS_FILE, encoding="utf-8", newline="") as f:
        raw = list(csv.DictReader(f))
    ids = {int(r["ид_объект"]) for r in raw}
    rows = []
    for r in raw:
        parent = int(r["родитель"]) if r["родитель"].strip() else None
        rows.append(
            ObjectRow(
                id=int(r["ид_объект"]),
                parent_id=parent if parent in ids else None,
                level=int(r["иерархия_уровень"]),
                kind=r["вид_объекта"],
                name=r["диспетчерское_название_объекта"],
            )
        )
    return rows


def read_channels(raw_dir: Path) -> list[ChannelRow]:
    rows = []
    with open(raw_dir / CHANNELS_FILE, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            name = r["название_датчика"]
            number, offset = parse_picket(name)
            rows.append(
                ChannelRow(
                    id=int(r["ид_канала_данных"]),
                    object_id=int(r["ид_объект"]),
                    system_type=r["тип_инж_системы"],
                    sensor_type=r["тип_датчика"],
                    tag=r["тег_инженерной_системы"],
                    name=name,
                    picket_number=number,
                    picket_offset=offset,
                    tag_group=tag_group(r["тег_инженерной_системы"]),
                )
            )
    return rows
