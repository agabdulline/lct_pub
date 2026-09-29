"""Подготовка внешнего набора: `data/external/<ключ>/prepared/` (docs/architecture/external-data.md §4.1).

Разбор конкретного набора — модуль с функцией `build(con, dataset, inputs, out_dir) -> dict`: он пишет
`objects.parquet`, `channels.parquet`, `events.parquet`, `labels.parquet` (и для наборов доступа
`credentials.parquet`, `permits.parquet`). Общая часть собирает во временную папку, сверяет инварианты формата,
пишет `report.json` и подменяет `prepared/` целиком — повторный запуск безопасен.
"""

import json
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import get_args

import duckdb

from app.external import azure_pdm, magdeburg, vast2017
from app.external.download import sha256_file, verify
from app.external.registry import Dataset, channel_range, object_range
from app.schemas.api import HackathonSensorType, HackathonSubsystem

log = logging.getLogger("external.prepare")

PREPARERS: dict[str, Callable] = {
    "magdeburg": magdeburg.build,
    "vast2017": vast2017.build,
    "azure_pdm": azure_pdm.build,
}

REQUIRED = ("objects", "channels", "events", "labels")
# Правила хакатона (ежедневная модель, газ, эпизоды оборудования, заявки) завязаны на его типы, подсистемы и виды
# объектов — внешний набор их не повторяет, а подсистема помечена «(внешн.)» (external-data.md §3.1)
HACKATHON_TYPES = set(get_args(HackathonSensorType))
HACKATHON_SYSTEMS = set(get_args(HackathonSubsystem))
HACKATHON_KINDS = {"district", "controlHouse", "guardObject"}
EXTERNAL_MARK = "(внешн.)"
ACCESS = ("credentials", "permits")  # только у наборов доступа, вместе


def prepared_dir(external_dir: Path, dataset: Dataset) -> Path:
    return external_dir / dataset.key / "prepared"


def connect(memory_limit: str = "2GB") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    # Метки prepared/ — московское время без пояса (A1); DuckDB переводит метки с поясом в этот пояс
    con.execute("SET TimeZone = 'Europe/Moscow'")
    con.execute(f"SET memory_limit = '{memory_limit}'")
    return con


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _check(con: duckdb.DuckDBPyConnection, out: Path, dataset: Dataset) -> dict:
    """Инварианты формата, которые проверяет и `load-external`: диапазоны id, иерархия, журнал."""
    for name in REQUIRED:
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({_q(out / (name + '.parquet'))})")
    o_lo, o_hi = object_range(dataset.k)
    c_lo, c_hi = channel_range(dataset.k)
    problems = []

    def one(sql: str) -> int:
        return con.execute(sql).fetchone()[0]

    if n := one(f"SELECT count(*) FROM objects WHERE id < {o_lo} OR id >= {o_hi}"):
        problems.append(f"объектов вне диапазона набора [{o_lo}, {o_hi}): {n}")
    if n := one(f"SELECT count(*) FROM channels WHERE id < {c_lo} OR id >= {c_hi}"):
        problems.append(f"каналов вне диапазона набора [{c_lo}, {c_hi}): {n}")
    if n := one("SELECT count(*) - count(DISTINCT id) FROM objects") + one(
        "SELECT count(*) - count(DISTINCT id) FROM channels"
    ):
        problems.append(f"повторяющихся id объектов и каналов: {n}")
    if n := one(
        "SELECT count(*) FROM channels c LEFT JOIN objects o ON o.id = c.object_id WHERE o.level IS DISTINCT FROM 3"
    ):
        problems.append(f"каналов не на объекте уровня 3: {n}")
    if n := one(
        "SELECT count(*) FROM objects c LEFT JOIN objects p ON p.id = c.parent_id "
        "WHERE c.level > 1 AND (p.id IS NULL OR p.level <> c.level - 1)"
    ):
        problems.append(f"объектов с неверным родителем: {n}")
    if n := one("SELECT count(*) FROM events e ANTI JOIN channels c ON c.id = e.channel_id"):
        problems.append(f"событий каналов вне справочника: {n}")
    if n := one("SELECT count(*) - count(DISTINCT event_id) FROM events"):
        problems.append(f"повторяющихся event_id: {n}")
    unsorted = one(
        "SELECT count(*) FROM (SELECT ts, lag(ts) OVER (ORDER BY event_id) AS prev FROM events) WHERE ts < prev"
    )
    if unsorted:
        problems.append(f"событий не по времени относительно event_id: {unsorted}")
    if n := one("SELECT count(*) FROM labels WHERE end_ts IS NOT NULL AND end_ts <= start_ts"):
        problems.append(f"меток с пустым интервалом: {n}")
    types = {r[0] for r in con.execute("SELECT DISTINCT sensor_type FROM channels").fetchall()}
    if clash := sorted(types & HACKATHON_TYPES):
        problems.append(f"типы датчиков хакатона у внешнего набора: {', '.join(clash)}")
    systems = {r[0] for r in con.execute("SELECT DISTINCT system_type FROM channels").fetchall()}
    if clash := sorted(s for s in systems if s in HACKATHON_SYSTEMS or not s.endswith(EXTERNAL_MARK)):
        problems.append(f"подсистема хакатона или без пометки «{EXTERNAL_MARK}»: {', '.join(clash)}")
    kinds = {r[0] for r in con.execute("SELECT DISTINCT kind FROM objects").fetchall()}
    if clash := sorted(kinds & HACKATHON_KINDS):
        problems.append(f"виды объектов хакатона у внешнего набора: {', '.join(clash)}")
    access = [name for name in ACCESS if (out / f"{name}.parquet").exists()]
    if access and len(access) != len(ACCESS):
        problems.append("пропуска и допуски пишутся вместе: credentials.parquet и permits.parquet")
    elif access:
        for name in ACCESS:
            con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({_q(out / (name + '.parquet'))})")
        if n := one("SELECT count(*) - count(DISTINCT credential_id) FROM credentials"):
            problems.append(f"повторяющихся пропусков: {n}")
        if n := one("SELECT count(*) FROM permits p ANTI JOIN credentials c USING (credential_id)"):
            problems.append(f"допусков на неизвестный пропуск: {n}")
        if n := one("SELECT count(*) FROM permits p ANTI JOIN objects o ON o.id = p.object_id"):
            problems.append(f"допусков на объект вне справочника: {n}")
    if problems:
        raise SystemExit("prepared/ не прошёл проверку:\n  " + "\n  ".join(problems))

    ts_min, ts_max = con.execute("SELECT min(ts), max(ts) FROM events").fetchone()
    by_day = con.execute("SELECT CAST(ts AS DATE)::VARCHAR, count(*) FROM events GROUP BY 1 ORDER BY 1").fetchall()
    return {
        "rows": {name: one(f"SELECT count(*) FROM {name}") for name in (*REQUIRED, *access)},
        "objects_by_level": dict(con.execute("SELECT level, count(*) FROM objects GROUP BY 1 ORDER BY 1").fetchall()),
        "channels_with_events": one("SELECT count(DISTINCT channel_id) FROM events"),
        "events_period": {"from": str(ts_min), "to": str(ts_max)},
        "events_by_day": dict(by_day),
        "labels_by_label": dict(con.execute("SELECT label, count(*) FROM labels GROUP BY 1 ORDER BY 1").fetchall()),
    }


def prepare(dataset: Dataset, external_dir: Path, memory_limit: str = "2GB") -> dict:
    if dataset.key not in PREPARERS:
        raise SystemExit(f"Подготовки набора «{dataset.key}» пока нет: {', '.join(sorted(PREPARERS))}")
    started = time.monotonic()
    inputs = verify(dataset, external_dir)
    out = prepared_dir(external_dir, dataset)
    tmp = out.with_name("prepared.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    con = connect(memory_limit)
    details = PREPARERS[dataset.key](con, dataset, inputs, tmp)
    files = sorted(p.name for p in tmp.glob("*.parquet"))
    missing = [n for n in REQUIRED if f"{n}.parquet" not in files]
    if missing:
        raise SystemExit(f"подготовка не записала: {', '.join(missing)}")
    checks = _check(con, tmp, dataset)
    con.close()
    report = {
        "dataset": dataset.key,
        "k": dataset.k,
        "title": dataset.title,
        "license": dataset.license,
        "synthetic": dataset.synthetic,
        "inputs": {f.name: {"sha256": f.sha256, "size": inputs[f.name].stat().st_size} for f in dataset.files},
        "outputs": {name: {"sha256": sha256_file(tmp / name), "size": (tmp / name).stat().st_size} for name in files},
        **checks,
        "details": details,
        "seconds": round(time.monotonic() - started, 1),
    }
    (tmp / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    shutil.rmtree(out, ignore_errors=True)
    tmp.replace(out)
    log.info("%s: prepared/ за %.1f с", dataset.key, report["seconds"])
    return report
