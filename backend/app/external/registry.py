"""Реестр внешних наборов и диапазоны идентификаторов (docs/architecture/external-data.md §2, §3.1)."""

from dataclasses import dataclass
from pathlib import Path

import yaml

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "external_datasets.yaml"

OBJECT_BASE = 9_000_000
OBJECT_STEP = 10_000
CHANNEL_BASE = 900_000_000
CHANNEL_STEP = 1_000_000


@dataclass(frozen=True)
class DatasetFile:
    name: str
    url: str
    sha256: str
    size: int | None = None


@dataclass(frozen=True)
class Dataset:
    key: str
    k: int
    title: str
    short: str
    direction: str
    url: str
    license: str
    synthetic: bool
    files: tuple[DatasetFile, ...]
    meta: dict


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Dataset]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["datasets"]
    out = {}
    seen_k = set()
    for key, d in raw.items():
        k = int(d["k"])
        if not 1 <= k <= 9 or k in seen_k:
            raise ValueError(f"{key}: номер набора k={k} вне 1–9 или повторяется")
        seen_k.add(k)
        files = tuple(DatasetFile(f["name"], f["url"], f["sha256"].lower(), f.get("size")) for f in d["files"])
        known = {"k", "title", "short", "direction", "url", "license", "synthetic", "files"}
        out[key] = Dataset(
            key=key,
            k=k,
            title=d["title"],
            short=d["short"],
            direction=d["direction"],
            url=d["url"],
            license=d["license"],
            synthetic=bool(d["synthetic"]),
            files=files,
            meta={name: value for name, value in d.items() if name not in known},
        )
    return out


def get_dataset(key: str, path: Path = REGISTRY_PATH) -> Dataset:
    registry = load_registry(path)
    if key not in registry:
        raise SystemExit(f"Набора «{key}» нет в реестре {path.name}: {', '.join(sorted(registry))}")
    return registry[key]


def object_id(k: int, n: int) -> int:
    if not 0 < n < OBJECT_STEP:
        raise ValueError(f"номер объекта {n} вне 1–{OBJECT_STEP - 1}")
    return OBJECT_BASE + k * OBJECT_STEP + n


def channel_id(k: int, n: int) -> int:
    if not 0 <= n < CHANNEL_STEP:
        raise ValueError(f"номер канала {n} вне 0–{CHANNEL_STEP - 1}")
    return CHANNEL_BASE + k * CHANNEL_STEP + n


def object_range(k: int) -> tuple[int, int]:
    """Полуинтервал [от, до) id объектов набора."""
    return OBJECT_BASE + k * OBJECT_STEP, OBJECT_BASE + (k + 1) * OBJECT_STEP


def channel_range(k: int) -> tuple[int, int]:
    """Полуинтервал [от, до) id каналов набора."""
    return CHANNEL_BASE + k * CHANNEL_STEP, CHANNEL_BASE + (k + 1) * CHANNEL_STEP
