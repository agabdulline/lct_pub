"""Скачивание файлов внешнего набора с проверкой sha256 (docs/architecture/external-data.md §4.1)."""

import hashlib
import logging
from pathlib import Path

import httpx

from app.external.registry import Dataset, DatasetFile

log = logging.getLogger("external.download")

CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def raw_dir(external_dir: Path, dataset: Dataset) -> Path:
    return external_dir / dataset.key / "raw"


def _fetch(file: DatasetFile, target: Path, timeout: float) -> None:
    part = target.with_name(target.name + ".part")
    h = hashlib.sha256()
    with httpx.stream("GET", file.url, follow_redirects=True, timeout=timeout) as r:
        r.raise_for_status()
        with part.open("wb") as out:
            for chunk in r.iter_bytes(CHUNK):
                out.write(chunk)
                h.update(chunk)
    if h.hexdigest() != file.sha256:
        part.unlink()
        raise SystemExit(f"{file.name}: sha256 {h.hexdigest()} не совпал с реестром {file.sha256} — файл не сохранён")
    part.replace(target)


def download(dataset: Dataset, external_dir: Path, timeout: float = 120.0) -> list[dict]:
    """Качает файлы набора, которых нет или у которых sha256 не совпал. Возвращает сведения по каждому файлу."""
    out_dir = raw_dir(external_dir, dataset)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for f in dataset.files:
        target = out_dir / f.name
        if target.exists() and sha256_file(target) == f.sha256:
            log.info("%s: уже скачан, sha256 совпал", f.name)
            status = "present"
        else:
            log.info("%s: скачиваю %s", f.name, f.url)
            _fetch(f, target, timeout)
            status = "downloaded"
        result.append({"name": f.name, "status": status, "size": target.stat().st_size, "sha256": f.sha256})
    return result


def verify(dataset: Dataset, external_dir: Path) -> dict[str, Path]:
    """Пути к файлам набора; останавливается, если файла нет или sha256 не совпал."""
    paths = {}
    for f in dataset.files:
        path = raw_dir(external_dir, dataset) / f.name
        if not path.exists():
            raise SystemExit(f"{path} нет — сначала `python -m app.cli external download --dataset {dataset.key}`")
        if sha256_file(path) != f.sha256:
            raise SystemExit(f"{path}: sha256 не совпал с реестром — скачайте заново")
        paths[f.name] = path
    return paths
