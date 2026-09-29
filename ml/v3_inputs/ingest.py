"""Strict streaming ingestion of the source 7z journals into daily aggregates.

Fault counts represent the raw ``Неисправен`` state, not verified breakdowns.
Only bounded chunk/month data are held in memory; raw CSV is never extracted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SOURCE_COLUMNS = [
    "ид_события",
    "ид_канала_данных",
    "дата",
    "время",
    "тревожное",
    "значение_датчика",
]
COUNT_COLUMNS = [
    "n_events",
    "n_alarm",
    "n_fault",
    "n_unknown",
    "n_disabled",
    "n_normal",
    "n_smoke",
    "n_trigger",
    "n_open",
    "n_string_other",
    "n_numeric",
]
SUM_COLUMNS = COUNT_COLUMNS + ["numeric_sum", "numeric_sumsq"]
STATE_COLUMNS = {
    "Неисправен": "n_fault",
    "Неопределен": "n_unknown",
    "Отключено устройство": "n_disabled",
    "Норма": "n_normal",
    "Обнаружен дым": "n_smoke",
}
KEYS = ["date", "sensor_id"]


def journal_source(data_dir: Path, year: int) -> Path:
    """Journal of a year: the organizers' 7z, or the same CSV already unpacked (our data/raw has CSV only)."""
    archive = data_dir / f"ext-journal-{year}.7z"
    csv = archive.with_suffix(".csv")
    return csv if not archive.is_file() and csv.is_file() else archive


def archive_command(archive: Path) -> list[str]:
    """Stream a single-CSV 7z on Kaggle/Linux or Windows without extraction; an unpacked CSV is streamed as is."""
    if archive.suffix == ".csv":
        return ["cat", str(archive)]
    sevenzip = shutil.which("7zz") or shutil.which("7z")
    if sevenzip:
        return [sevenzip, "x", "-so", "-bsp0", str(archive)]
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        return [bsdtar, "-xOf", str(archive)]
    if sys.platform == "win32" and shutil.which("tar"):
        return ["tar", "-xOf", str(archive)]
    raise RuntimeError(
        "Install 7z/7zz or bsdtar to rebuild raw 7z archives. The packaged Parquet needs no archiver."
    )


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def scan_repeated_headers(archive: Path, report_path: Path) -> dict:
    """Audit embedded header neighborhoods without materializing the CSV."""
    findings = []
    with tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            archive_command(archive), stdout=subprocess.PIPE, stderr=error_file
        )
        initial_header = process.stdout.readline().rstrip(b"\r\n")
        marker = b"\n" + initial_header + b"\n"
        tail, bytes_read, line_count = b"", len(initial_header) + 1, 1
        last_position = -1
        while True:
            block = process.stdout.read(8 * 1024 * 1024)
            if not block:
                break
            combined = tail + block
            search_from = 0
            while True:
                found = combined.find(marker, search_from)
                if found == -1:
                    break
                absolute = bytes_read - len(tail) + found + 1
                if absolute > last_position:
                    before = combined[:found].splitlines()[-1:]
                    after = combined[found + len(marker) :].splitlines()[:1]
                    findings.append(
                        {
                            "byte_position": absolute,
                            "preceding_record": [x.decode("utf-8") for x in before],
                            "following_record": [x.decode("utf-8") for x in after],
                        }
                    )
                    last_position = absolute
                search_from = found + len(marker)
            tail = combined[-2048:]
            bytes_read += len(block)
            line_count += block.count(b"\n")
        result = process.wait()
        if result:
            error_file.seek(0)
            raise RuntimeError(error_file.read().decode("utf-8", errors="replace"))
    audit = {
        "archive": str(archive),
        "uncompressed_bytes": bytes_read,
        "newline_count": line_count,
        "repeated_header_contexts": findings,
    }
    atomic_json(report_path, audit)
    return audit


def aggregate_chunk(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    if list(raw.columns) != SOURCE_COLUMNS:
        raise ValueError(f"Unexpected CSV schema: {list(raw.columns)}")
    if raw[SOURCE_COLUMNS[:-1]].isna().any().any():
        raise ValueError("Missing required identity/time/alarm data")
    alarm_text = raw["тревожное"].str.lower()
    bad_alarm = ~alarm_text.isin(["t", "f", "true", "false"])
    if bad_alarm.any():
        raise ValueError(
            f"Invalid alarm values: {raw.loc[bad_alarm, 'тревожное'].value_counts().to_dict()}"
        )
    dates = pd.to_datetime(raw["дата"], format="%Y-%m-%d", errors="raise")
    timestamps = pd.to_datetime(
        raw["дата"] + " " + raw["время"], format="%Y-%m-%d %H:%M:%S", errors="raise"
    )
    values = raw["значение_датчика"]
    numeric = pd.to_numeric(values, errors="coerce")
    if np.isinf(numeric.to_numpy(dtype=float)).any():
        raise ValueError("Infinite numeric sensor value")
    is_numeric = numeric.notna()
    frame = pd.DataFrame(
        {
            "date": dates,
            "sensor_id": raw["ид_канала_данных"],
            "n_events": np.ones(len(raw), dtype=np.int32),
            "n_alarm": alarm_text.isin(["t", "true"]).astype(np.int32),
            "n_numeric": is_numeric.astype(np.int32),
            "numeric_sum": numeric.fillna(0).astype(float),
            "numeric_sumsq": numeric.fillna(0).astype(float).pow(2),
            "numeric_min": numeric.astype(float),
            "numeric_max": numeric.astype(float),
        }
    )
    for value, column in STATE_COLUMNS.items():
        frame[column] = values.eq(value).astype(np.int32)
    # Keep the exact strings in the report. These broad counts are supplementary.
    trigger = values.str.contains(
        "Сработ|Сработал|Тревог", case=False, regex=True, na=False
    )
    opened = values.str.contains("Открыт", case=False, regex=False, na=False)
    frame["n_trigger"] = trigger.astype(np.int32)
    frame["n_open"] = opened.astype(np.int32)
    frame["n_string_other"] = (~is_numeric & ~values.isin(STATE_COLUMNS)).astype(
        np.int32
    )
    agg = {column: "sum" for column in SUM_COLUMNS}
    agg.update(numeric_min="min", numeric_max="max")
    daily = frame.groupby(KEYS, sort=False, observed=True).agg(agg)
    boundaries = pd.DataFrame(
        {
            "date": dates,
            "sensor_id": raw["ид_канала_данных"],
            "ts": timestamps,
            "event_id": raw["ид_события"],
            "value": values,
        }
    )
    boundaries = boundaries.sort_values(["ts", "event_id"], kind="stable")
    for position, keep in [("first", "first"), ("last", "last")]:
        selected = boundaries.drop_duplicates(KEYS, keep=keep).set_index(KEYS)
        selected = selected[["ts", "event_id", "value"]].rename(
            columns=lambda x, prefix=position: f"{prefix}_{x}"
        )
        daily = daily.join(selected, how="left", validate="one_to_one")
    daily = daily.reset_index()
    return daily, values[~is_numeric].value_counts(dropna=False), timestamps


def combine_month(frame: pd.DataFrame) -> pd.DataFrame:
    agg = {column: "sum" for column in SUM_COLUMNS}
    agg.update(numeric_min="min", numeric_max="max")
    daily = frame.groupby(KEYS, sort=True, observed=True).agg(agg)
    for position, ascending in [("first", True), ("last", False)]:
        columns = [f"{position}_ts", f"{position}_event_id", f"{position}_value"]
        chosen = (
            frame.sort_values(columns[:2], ascending=ascending, kind="stable")
            .drop_duplicates(KEYS)
            .set_index(KEYS)
        )
        daily = daily.join(chosen[columns], validate="one_to_one")
    for column in COUNT_COLUMNS:
        daily[column] = daily[column].astype(np.int64)
    return daily.reset_index()


def ingest_year(
    data_dir: Path, output_dir: Path, report_dir: Path, year: int, chunk_size: int
) -> Path:
    started = time.perf_counter()
    archive = journal_source(data_dir, year)
    output = output_dir / f"events_{year}.parquet"
    report_path = report_dir / f"ingestion_{year}.json"
    if not archive.is_file():
        raise FileNotFoundError(archive)
    channels = pd.read_csv(
        data_dir / "справочник_каналов_датчиков.csv",
        encoding="utf-8",
        usecols=["ид_канала_данных"],
    )
    known = set(channels.iloc[:, 0].astype(int))
    report = {
        "archive": str(archive),
        "archive_bytes": archive.stat().st_size,
        "year": year,
        "status": "running",
        "rows": 0,
        "csv_records_read": 0,
        "repeated_header_rows": 0,
        "repeated_header_record_numbers": [],
        "chunks": 0,
        "duplicate_diagnostics": {
            "method": "exact within each chunk plus overlap against immediately previous chunk; distant/cross-year duplicates are not detected",
            "within_chunk_repeated_ids": 0,
            "previous_chunk_overlap_ids": 0,
            "deduplicated": False,
        },
        "boundary_selection": "first/last by timestamp then event_id; event_id is only a deterministic tie-break and does not resolve simultaneous contradictory states",
        "target_caveat": "n_fault counts raw Неисправен state events; it is not a verified equipment breakdown label",
        "parse_policy": "UTF-8 strict; exact six-column schema; exact repeated header rows counted and excluded; all other malformed rows raise; strict IDs, dates, times, alarm values",
    }
    values_counter, alarm_counter, unmatched_counter = Counter(), Counter(), Counter()
    previous_ids = np.array([], dtype=np.int64)
    seen_channels = set()
    min_date = max_date = None
    sample_disorder = 0
    writers = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    # Scratch files contain only daily chunk summaries and are removed on exit.
    with tempfile.TemporaryDirectory(
        prefix=f"_aggregate_{year}_", dir=output_dir
    ) as tmp_name:
        tmp = Path(tmp_name)
        with tempfile.TemporaryFile() as error_file:
            process = subprocess.Popen(
                archive_command(archive), stdout=subprocess.PIPE, stderr=error_file
            )
            try:
                dtypes = {column: str for column in SOURCE_COLUMNS}
                for raw in pd.read_csv(
                    process.stdout,
                    encoding="utf-8",
                    encoding_errors="strict",
                    dtype=dtypes,
                    keep_default_na=False,
                    skip_blank_lines=False,
                    chunksize=chunk_size,
                    on_bad_lines="error",
                ):
                    if list(raw.columns) != SOURCE_COLUMNS:
                        raise ValueError(f"Unexpected CSV schema: {list(raw.columns)}")
                    repeated_header = raw.eq(
                        pd.Series(SOURCE_COLUMNS, index=SOURCE_COLUMNS), axis="columns"
                    ).all(axis=1)
                    if repeated_header.any():
                        report["repeated_header_rows"] += int(repeated_header.sum())
                        report["repeated_header_record_numbers"].extend(
                            (
                                np.flatnonzero(repeated_header.to_numpy())
                                + report["csv_records_read"]
                                + 2
                            ).tolist()
                        )
                    report["csv_records_read"] += len(raw)
                    raw = raw.loc[~repeated_header].copy()
                    if raw.empty:
                        continue
                    raw["ид_события"] = raw["ид_события"].astype("int64")
                    raw["ид_канала_данных"] = raw["ид_канала_данных"].astype("int64")
                    daily, string_counts, timestamps = aggregate_chunk(raw)
                    ids = raw["ид_события"].to_numpy()
                    report["duplicate_diagnostics"]["within_chunk_repeated_ids"] += int(
                        raw["ид_события"].duplicated().sum()
                    )
                    report["duplicate_diagnostics"]["previous_chunk_overlap_ids"] += (
                        int(np.intersect1d(ids, previous_ids, assume_unique=False).size)
                    )
                    previous_ids = ids.copy()
                    values_counter.update(
                        {str(k): int(v) for k, v in string_counts.items()}
                    )
                    alarm_counter.update(
                        {
                            str(k): int(v)
                            for k, v in raw["тревожное"].value_counts().items()
                        }
                    )
                    unmatched_counter.update(
                        {
                            int(k): int(v)
                            for k, v in raw.loc[
                                ~raw["ид_канала_данных"].isin(known), "ид_канала_данных"
                            ]
                            .value_counts()
                            .items()
                        }
                    )
                    seen_channels.update(raw["ид_канала_данных"].unique().tolist())
                    chunk_min, chunk_max = timestamps.min(), timestamps.max()
                    min_date = (
                        chunk_min if min_date is None else min(min_date, chunk_min)
                    )
                    max_date = (
                        chunk_max if max_date is None else max(max_date, chunk_max)
                    )
                    sample_disorder += int(
                        (timestamps.diff().dt.total_seconds() < 0).sum()
                    )
                    for month, part in daily.groupby(
                        daily["date"].dt.strftime("%Y-%m"), sort=False
                    ):
                        table = pa.Table.from_pandas(part, preserve_index=False)
                        if month not in writers:
                            writers[month] = pq.ParquetWriter(
                                tmp / f"{month}.parquet",
                                table.schema,
                                compression="zstd",
                            )
                        writers[month].write_table(table)
                    report["rows"] += len(raw)
                    report["chunks"] += 1
                    if report["chunks"] % 10 == 0:
                        report.update(
                            elapsed_seconds=round(time.perf_counter() - started, 2),
                            date_min=str(min_date),
                            date_max=str(max_date),
                        )
                        atomic_json(report_path, report)
                        print(
                            json.dumps(
                                {
                                    "year": year,
                                    "rows": report["rows"],
                                    "through": str(max_date),
                                    "elapsed_seconds": report["elapsed_seconds"],
                                }
                            ),
                            flush=True,
                        )
                result = process.wait()
                error_file.seek(0)
                stderr = error_file.read().decode("utf-8", errors="replace")
                if result:
                    raise RuntimeError(f"tar returned {result}: {stderr}")
                report["archive_stderr"] = stderr
            except BaseException as exc:
                process.kill()
                process.wait()
                report.update(status="failed", error=repr(exc))
                atomic_json(report_path, report)
                raise
            finally:
                for writer in writers.values():
                    writer.close()
        output_tmp = output.with_suffix(".parquet.tmp")
        row_count = total_events = 0
        output_writer = None
        try:
            for monthly in sorted(tmp.glob("*.parquet")):
                daily = combine_month(pd.read_parquet(monthly))
                row_count += len(daily)
                total_events += int(daily.n_events.sum())
                table = pa.Table.from_pandas(daily, preserve_index=False)
                if output_writer is None:
                    output_writer = pq.ParquetWriter(
                        output_tmp, table.schema, compression="zstd"
                    )
                output_writer.write_table(table)
                print(
                    json.dumps(
                        {
                            "year": year,
                            "month_combined": monthly.stem,
                            "daily_rows": len(daily),
                        }
                    ),
                    flush=True,
                )
        finally:
            if output_writer is not None:
                output_writer.close()
        if (
            report["csv_records_read"]
            != report["rows"] + report["repeated_header_rows"]
        ):
            raise AssertionError("CSV record accounting mismatch")
        if total_events != report["rows"]:
            raise AssertionError(
                f"Lost/duplicated rows: source={report['rows']} aggregation={total_events}"
            )
        os.replace(output_tmp, output)
    report.update(
        status="complete",
        output=str(output),
        output_bytes=output.stat().st_size,
        daily_rows=row_count,
        date_min=str(min_date),
        date_max=str(max_date),
        elapsed_seconds=round(time.perf_counter() - started, 2),
        string_value_counts=dict(values_counter.most_common()),
        raw_alarm_counts=dict(alarm_counter),
        seen_channels=len(seen_channels),
        unmatched_channels={str(k): v for k, v in unmatched_counter.most_common()},
        unmatched_channel_count=len(unmatched_counter),
        unmatched_event_rows=sum(unmatched_counter.values()),
        within_chunk_timestamp_backward_steps=sample_disorder,
        schema={field.name: str(field.type) for field in pq.read_schema(output)},
    )
    atomic_json(report_path, report)
    print(
        json.dumps(
            {
                "complete": year,
                "output": str(output),
                "rows": report["rows"],
                "daily_rows": row_count,
                "seconds": report["elapsed_seconds"],
            }
        ),
        flush=True,
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=[2025, 2026, 2019, 2020, 2021, 2022, 2023, 2024],
    )
    parser.add_argument("--chunk-size", type=int, default=300_000)
    args = parser.parse_args()
    report_dir = args.report_dir or args.output_dir.parent.parent / "reports"
    for year in args.years:
        ingest_year(args.data_dir, args.output_dir, report_dir, year, args.chunk_size)
    completed = []
    for path in sorted(report_dir.glob("ingestion_20??.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("status") == "complete":
            completed.append(report)
    values, alarms, unknown = Counter(), Counter(), Counter()
    for report in completed:
        values.update(report["string_value_counts"])
        alarms.update(report["raw_alarm_counts"])
        unknown.update(report["unmatched_channels"])
    atomic_json(
        report_dir / "ingestion_summary.json",
        {
            "years": [report["year"] for report in completed],
            "rows": sum(report["rows"] for report in completed),
            "daily_rows": sum(report["daily_rows"] for report in completed),
            "date_min": min((report["date_min"] for report in completed), default=None),
            "date_max": max((report["date_max"] for report in completed), default=None),
            "string_value_counts": dict(values.most_common()),
            "raw_alarm_counts": dict(alarms),
            "unmatched_channels": dict(unknown.most_common()),
            "unmatched_event_rows": sum(unknown.values()),
            "duplicate_diagnostics_limit": "bounded exact within-chunk/adjacent-chunk diagnostic only; no deduplication",
        },
    )


if __name__ == "__main__":
    main()
