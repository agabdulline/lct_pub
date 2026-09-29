"""Срезы сценариев из исходного журнала: `uv run python -m app.prepare [сценарий ...]` из папки emulator/.

Правила чтения — docs/data-format.md §8: CSV-парсером, без строк-заголовков внутри файла, сортировка по времени,
дедупликация по (канал, ид_события, время, значение). Значения не нормализуются — эмулятор изображает источник
и передаёт их как есть. Каналы вне справочника в срез не попадают: в адресном пространстве для них нет узлов.
Срез — Parquet, отсортированный по (время, ид_события).

Сценарий внешнего набора (`dataset: <ключ>`) режется из `data/external/<набор>/prepared/events.parquet` — журнал уже
очищен подготовкой набора (external-data.md §4.1). Справочник набора копируется в
`<slices_dir>/external/<набор>/`: эмулятор монтирует только data/raw и data/processed/emulator (§5.1).
"""

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

import duckdb

from app.catalog import Catalog, add_external_catalogs, emulator_catalog, load_catalog
from app.config import settings
from app.scenario_data import ScenarioData
from app.scenarios import EXTERNAL_DIR, SLICE_ORDER, Scenario, load_scenarios

log = logging.getLogger("emulator.prepare")

GAS = "Газовый датчик"


def scenario_channels(catalog: Catalog, scenario: Scenario) -> list[int]:
    ids = catalog.channel_ids(scenario.objects)
    if not scenario.include_gas:
        ids = {c for c in ids if catalog.channels[c].sensor_type != GAS}
    return sorted(ids)


def journal_files(raw_dir: Path, scenario: Scenario) -> list[Path]:
    """Годовые журналы периода или файл `source` сценария — например, пример организаторов за 01.08.2026 с кавычками
    и `true` / `false` (docs/data-format.md §2): DuckDB снимает кавычки сам, флаг читается как `t` / `true`."""
    if scenario.source:
        files = [raw_dir / scenario.source]
    else:
        years = range(scenario.start.year, scenario.end.year + 1)
        files = [raw_dir / f"ext-journal-{y}.csv" for y in years]
    missing = [f for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError(f"нет журнала: {', '.join(map(str, missing))}")
    return files


CATALOG_FILES = ("objects.parquet", "channels.parquet")


def copy_external_catalog(external_dir: Path, dataset: str, out_dir: Path) -> Path:
    """Справочник набора из prepared/ — рядом со срезами, откуда его читает эмулятор."""
    source = external_dir / dataset / "prepared"
    missing = [f for f in CATALOG_FILES if not (source / f).is_file()]
    if missing:
        raise FileNotFoundError(f"набор {dataset} не подготовлен: нет {', '.join(missing)} в {source}")
    target = out_dir / EXTERNAL_DIR / dataset
    target.mkdir(parents=True, exist_ok=True)
    for name in CATALOG_FILES:
        tmp = target / f"{name}.tmp"
        shutil.copyfile(source / name, tmp)
        tmp.replace(target / name)
    return target


def prepare(
    scenario: Scenario, catalog: Catalog, raw_dir: Path, out_dir: Path, external_dir: Path | None = None
) -> dict:
    started = time.perf_counter()
    channels = scenario_channels(catalog, scenario)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = scenario.slice_path(out_dir)
    tmp = target.with_suffix(".parquet.tmp")
    con = duckdb.connect()
    con.execute("CREATE TABLE chans AS SELECT unnest($1::INTEGER[]) AS channel_id", [channels])
    if scenario.dataset:
        events = (external_dir or settings.external_dir) / scenario.dataset / "prepared" / "events.parquet"
        if not events.is_file():
            raise FileNotFoundError(f"нет журнала набора {scenario.dataset}: {events}")
        con.execute(
            f"""
            COPY (
              SELECT event_id, channel_id, ts, is_alarm, value
              FROM read_parquet($events, file_row_number = true)
              WHERE ts >= $start AND ts < $end AND channel_id IN (SELECT channel_id FROM chans)
              ORDER BY {SLICE_ORDER}
            ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)
            """,
            {"events": str(events), "start": scenario.start, "end": scenario.end},
        )
        return _report(con, scenario, channels, tmp, target, started)
    files = [str(f) for f in journal_files(raw_dir, scenario)]
    con.execute(
        f"""
        COPY (
          SELECT event_id, channel_id, ts, is_alarm, value FROM (
            SELECT DISTINCT ON (channel_id, event_id, ts, value) * FROM (
              -- TRY_CAST: оптимизатор может вычислить приведение раньше фильтра заголовков внутри файла
              SELECT TRY_CAST(ид_события AS BIGINT) AS event_id,
                     TRY_CAST(ид_канала_данных AS INTEGER) AS channel_id,
                     TRY_CAST(дата || ' ' || время AS TIMESTAMP) AS ts,
                     тревожное IN ('t', 'true') AS is_alarm, значение_датчика AS value
              FROM read_csv($files, header = true, all_varchar = true)
              WHERE ид_события <> 'ид_события' AND дата BETWEEN $d0 AND $d1
            )
            WHERE ts >= $start AND ts < $end AND channel_id IN (SELECT channel_id FROM chans)
          )
          ORDER BY ts, event_id
        ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)
        """,
        {
            "files": files,
            "d0": scenario.start.date().isoformat(),
            "d1": scenario.end.date().isoformat(),
            "start": scenario.start,
            "end": scenario.end,
        },
    )
    return _report(con, scenario, channels, tmp, target, started)


def _report(con, scenario: Scenario, channels: list[int], tmp: Path, target: Path, started: float) -> dict:
    tmp.replace(target)
    events, alarms, active = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE is_alarm), count(DISTINCT channel_id) FROM '{target}'"
    ).fetchone()
    report = {
        "scenario": scenario.name,
        "events": events,
        "alarms": alarms,
        "channels_in_scope": len(channels),
        "channels_with_events": active,
        "seconds": round(time.perf_counter() - started, 1),
        "path": str(target),
    }
    log.info("%s", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Срезы сценариев эмулятора из data/raw")
    parser.add_argument("names", nargs="*", help="сценарии; по умолчанию все")
    parser.add_argument(
        "--summaries-only", action="store_true", help="только сводки уже готовых срезов, без пересборки самих срезов"
    )
    args = parser.parse_args(argv)
    scenarios = load_scenarios(settings.scenarios_dir)
    unknown = set(args.names) - set(scenarios)
    if unknown:
        log.error("нет сценариев: %s; есть: %s", ", ".join(sorted(unknown)), ", ".join(scenarios))
        return 2
    names = args.names or list(scenarios)
    if args.summaries_only:
        summaries(scenarios, [n for n in names if scenarios[n].slice_path(settings.slices_dir).is_file()])
        return 0
    # справочник хакатона нужен только сценариям журнала хакатона
    hackathon = any(not scenarios[n].dataset for n in names)
    catalog = load_catalog(settings.objects_csv, settings.channels_csv) if hackathon else Catalog()
    for dataset in sorted({scenarios[n].dataset for n in names if scenarios[n].dataset}):
        try:
            copy_external_catalog(settings.external_dir, dataset, settings.slices_dir)
        except FileNotFoundError as e:
            if args.names:
                raise
            # все сценарии разом (bootstrap): набор не скачан — его сценарии пропускаются, остальные готовятся
            log.warning("%s — сценарии набора пропущены: make external D=%s", e, dataset)
            names = [n for n in names if scenarios[n].dataset != dataset]
    add_external_catalogs(catalog, settings.slices_dir / EXTERNAL_DIR)
    for name in names:
        prepare(scenarios[name], catalog, settings.raw_dir, settings.slices_dir)
    summaries(scenarios, names)
    return 0


def summaries(scenarios: dict[str, Scenario], names: list[str]) -> None:
    """Сводки срезов — заранее, справочником эмулятора: в контейнере срезы только на чтение, и эмулятор берёт
    сводку с диска, а не считает её при первом просмотре сценария (scenario_data)."""
    data = ScenarioData(
        emulator_catalog(settings.objects_csv, settings.channels_csv, settings.slices_dir / EXTERNAL_DIR)
    )
    for name in names:
        started = time.monotonic()
        try:
            data.summary(scenarios[name], scenarios[name].slice_path(settings.slices_dir))
        except Exception:  # noqa: BLE001 — срез готов и без сводки: эмулятор посчитает её сам
            log.exception("сводка %s не посчитана", name)
            continue
        log.info("сводка %s — %.1f с", name, time.monotonic() - started)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sys.exit(main())
