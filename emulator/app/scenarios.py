"""Сценарии эмулятора: встроенные — YAML в emulator/scenarios/ со срезом data/processed/emulator/<name>.parquet,
загруженные — пара <id>.parquet и <id>.json в data/processed/emulator/uploaded/ (пишет api, 13.1).

Формат — docs/architecture/emulator.md §4. Время в сценариях — как в журнале, Europe/Moscow без пояса.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import yaml

log = logging.getLogger("emulator.scenarios")

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_MAX_SPEED = 60.0
UPLOADED_DIR = "uploaded"
EXTERNAL_DIR = "external"  # справочники внешних наборов: <slices_dir>/external/<набор>/{objects,channels}.parquet
# Порядок передачи событий среза: у загруженных журналов без ид события — порядок строк файла
SLICE_ORDER = "ts, event_id, file_row_number"
# Предел скорости по плотности — ступени, чтобы в интерфейсе не было ×137
SPEED_STEPS = (5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0, 1000.0)
TIMELINE_SOURCES = ("journal", "dataset")  # факт журнала | разметка «по описанию набора» (external-data.md §5.2)


@dataclass(frozen=True)
class Moment:
    at: datetime
    text: str
    until: datetime | None = None
    source: str = "journal"


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    start: datetime  # Europe/Moscow, без пояса — как в журнале
    end: datetime
    objects: list[int]  # пусто — все объекты
    include_gas: bool = False
    speed: float = 60.0
    max_speed: float = DEFAULT_MAX_SPEED
    timeline: list[Moment] = field(default_factory=list)
    source: str | None = None  # файл в data/raw вместо годовых журналов ext-journal-YYYY.csv
    origin: str = "builtin"  # builtin | upload — в API поле `source`
    slice_file: Path | None = None  # у загруженных срез лежит в uploaded/
    upload: dict | None = None  # у загруженных: source_file, uploaded_at, warnings, events, channels
    dataset: str | None = None  # ключ внешнего набора: срез — из data/external/<набор>/prepared/

    def slice_path(self, slices_dir: Path) -> Path:
        return self.slice_file or slices_dir / f"{self.name}.parquet"

    def describe(self, slices_dir: Path) -> dict:
        return {
            "name": self.name,
            "source": self.origin,
            "title": self.title,
            "description": self.description,
            "period": {"start": _iso(self.start), "end": _iso(self.end)},
            "objects": self.objects,
            "include_gas": self.include_gas,
            "speed": self.speed,
            "max_speed": self.max_speed,
            "timeline": [
                {"at": _iso(m.at), "until": m.until and _iso(m.until), "text": m.text, "source": m.source}
                for m in self.timeline
            ],
            "dataset": self.dataset,
            "ready": self.slice_path(slices_dir).is_file(),
            **(self.upload or {}),
        }


def _iso(ts: datetime) -> str:
    return ts.replace(tzinfo=MOSCOW).isoformat()


def _naive(value) -> datetime:
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if ts.tzinfo is not None:
        raise ValueError(f"время сценария задаётся без пояса, как в журнале (Europe/Moscow): {value}")
    return ts


def parse_scenario(name: str, data: dict) -> Scenario:
    start, end = _naive(data["period"]["start"]), _naive(data["period"]["end"])
    if end <= start:
        raise ValueError(f"сценарий {name}: конец периода раньше начала")
    speed = float(data.get("speed", 60))
    max_speed = float(data.get("max_speed", DEFAULT_MAX_SPEED))
    if not 1 <= speed <= max_speed:
        raise ValueError(f"сценарий {name}: скорость {speed} вне 1–{max_speed}")
    return Scenario(
        name=name,
        title=data["title"],
        description=" ".join(str(data.get("description", "")).split()),
        start=start,
        end=end,
        objects=[int(o) for o in data.get("objects") or []],
        include_gas=bool(data.get("include_gas", False)),
        speed=speed,
        max_speed=max_speed,
        timeline=[_moment(name, m) for m in data.get("timeline") or []],
        source=data.get("source"),
        dataset=data.get("dataset"),
    )


def _moment(name: str, data: dict) -> Moment:
    source = data.get("source", "journal")
    if source not in TIMELINE_SOURCES:
        raise ValueError(f"сценарий {name}: источник строки timeline {source!r} не из {TIMELINE_SOURCES}")
    until = _naive(data["until"]) if data.get("until") else None
    return Moment(_naive(data["at"]), data["text"], until, source)


def load_scenarios(scenarios_dir: Path) -> dict[str, Scenario]:
    scenarios = {}
    for path in sorted(scenarios_dir.glob("*.yaml")):
        with path.open(encoding="utf-8") as f:
            scenarios[path.stem] = parse_scenario(path.stem, yaml.safe_load(f))
    return scenarios


# --- загруженные сценарии ---------------------------------------------------------------------------------------

_uploads_cache: dict[Path, tuple[tuple, Scenario]] = {}
# сводка среза рядом с ним (scenario_data) — не метаданные загрузки
SUMMARY_SUFFIX = ".summary.json"


def density_max_speed(path: Path, max_rate: float, floor: float = DEFAULT_MAX_SPEED) -> float:
    """Предел скорости по плотности потока: 95-й перцентиль минут с событиями должен проходить без растяжения
    при выходе `max_rate` событий в секунду; ступень из SPEED_STEPS, не ниже `floor`. У загруженных журналов —
    не ниже ×60 (договорённость 13.1); плотным внешним наборам (Magdeburg — ×17) нужен `floor` меньше."""
    # своё соединение: вызывается из потоков HTTP, пока проигрыватель читает срез (общее соединение DuckDB между
    # потоками ломается — «closed pending query result»)
    with duckdb.connect() as con:
        p95 = con.execute(
            "SELECT quantile_cont(n, 0.95) FROM (SELECT count(*) AS n FROM read_parquet($path) "
            "GROUP BY date_trunc('minute', ts))",
            {"path": str(path)},
        ).fetchone()[0]
    limit = max_rate * 60 / p95 if p95 else SPEED_STEPS[-1]
    return max([s for s in SPEED_STEPS if floor <= s <= limit], default=floor)


def _fmt_count(n) -> str:
    return f"{n:,}".replace(",", "\u00a0") if isinstance(n, int) else "?"


def parse_upload(meta: dict, slice_file: Path, max_rate: float) -> Scenario:
    """Сценарий из метаданных загрузки (контракт с api — docs/architecture/emulator.md §4)."""
    start, end = _naive(meta["period"]["start"]), _naive(meta["period"]["end"])
    if end <= start:
        raise ValueError("конец периода не позже начала")
    source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    upload = {
        "source_file": meta.get("source_file") or source.get("file_name"),
        "uploaded_at": meta.get("uploaded_at") or source.get("uploaded_at"),
        "warnings": meta.get("warnings") or [],
        "events": meta.get("events"),
        "channels": meta.get("channels"),
    }
    description = meta.get("description") or (
        f"Журнал из файла «{upload['source_file']}», {start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}: "
        f"событий — {_fmt_count(upload['events'])}, каналов — {_fmt_count(upload['channels'])}."
    )
    max_speed = density_max_speed(slice_file, max_rate) if slice_file.is_file() else DEFAULT_MAX_SPEED
    return Scenario(
        name=slice_file.stem,
        title=meta.get("title") or slice_file.stem,
        description=" ".join(str(description).split()),
        start=start,
        end=end,
        objects=[int(o) for o in meta.get("objects") or []],
        include_gas=bool(meta.get("include_gas", True)),
        speed=DEFAULT_MAX_SPEED,  # ×60, предел не ниже
        max_speed=max_speed,
        origin="upload",
        slice_file=slice_file,
        upload=upload,
    )


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
    except FileNotFoundError:
        return (None,)
    return (st.st_mtime_ns, st.st_size)


def load_uploads(uploaded_dir: Path, max_rate: float) -> dict[str, Scenario]:
    """Загруженные сценарии — перечитываются при каждом вызове, без перезапуска; разбор и плотность потока
    кэшируются по времени изменения файлов. api пишет json последним: есть json — загрузка закончена.
    Битые метаданные пропускаются с записью в лог, чтобы одна загрузка не ломала список."""
    scenarios = {}
    for meta_path in sorted(uploaded_dir.glob("*.json")) if uploaded_dir.is_dir() else []:
        if meta_path.name.endswith(SUMMARY_SUFFIX):
            continue
        slice_file = meta_path.with_suffix(".parquet")
        key = (_stamp(meta_path), _stamp(slice_file), max_rate)
        cached = _uploads_cache.get(meta_path)
        if cached and cached[0] == key:
            scenarios[meta_path.stem] = cached[1]
            continue
        try:
            scenario = parse_upload(json.loads(meta_path.read_text(encoding="utf-8")), slice_file, max_rate)
        except (OSError, ValueError, KeyError, TypeError, duckdb.Error) as e:
            log.warning("загруженный сценарий %s пропущен: %s", meta_path.name, e)
            continue
        _uploads_cache[meta_path] = (key, scenario)
        scenarios[scenario.name] = scenario
    return scenarios


def all_scenarios(scenarios_dir: Path, slices_dir: Path, max_rate: float) -> dict[str, Scenario]:
    """Встроенные, затем загруженные — новые первыми. Загруженный с именем встроенного не показывается."""
    builtin = load_scenarios(scenarios_dir)
    uploads = load_uploads(slices_dir / UPLOADED_DIR, max_rate)
    for name in uploads.keys() & builtin.keys():
        log.warning("загруженный сценарий %s совпадает по имени со встроенным и пропущен", name)
    ordered = sorted(
        (s for name, s in uploads.items() if name not in builtin),
        key=lambda s: str(s.upload.get("uploaded_at") or ""),
        reverse=True,
    )
    return {**builtin, **{s.name: s for s in ordered}}
