"""Extract complete smoke-sensor event sequences needed by the v3 feature code."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ml.v3.src.baseline_core import sha256
from ml.v3_inputs.ingest import SOURCE_COLUMNS, archive_command, journal_source

SMOKE_TYPE = "Датчик дыма"
OUTPUT_NAME = "smoke_events_2022_2026.parquet"


def _empty_events(path: Path) -> None:
    pd.DataFrame(
        {
            "event_id": pd.Series(dtype="int64"),
            "sensor_id": pd.Series(dtype="int64"),
            "ts": pd.Series(dtype="datetime64[ns]"),
            "value": pd.Series(dtype="string"),
        }
    ).to_parquet(path, index=False)


def extract_year(
    raw_dir: Path,
    daily: pd.DataFrame,
    smoke_ids: set[str],
    year: int,
    output_dir: Path,
    chunk_size: int,
    overwrite: bool,
) -> Path:
    """Stream one raw archive and retain every event of registered smoke channels."""
    destination = output_dir / f"events_{year}.parquet"
    audit_path = output_dir / f"audit_{year}.json"
    if destination.exists() and not overwrite:
        return destination

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    started = time.perf_counter()
    records = selected = repeated_headers = 0
    writer = None

    with tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            archive_command(journal_source(raw_dir, year)),
            stdout=subprocess.PIPE,
            stderr=error_file,
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
                repeated = raw.eq(
                    pd.Series(SOURCE_COLUMNS, index=SOURCE_COLUMNS), axis="columns"
                ).all(axis=1)
                repeated_headers += int(repeated.sum())
                raw = raw.loc[~repeated]
                records += len(raw)
                frame = raw.loc[raw["ид_канала_данных"].isin(smoke_ids)].copy()
                if frame.empty:
                    continue
                frame = frame.rename(
                    columns={
                        "ид_события": "event_id",
                        "ид_канала_данных": "sensor_id",
                        "значение_датчика": "value",
                    }
                )
                frame["ts"] = pd.to_datetime(
                    frame["дата"] + " " + frame["время"],
                    format="%Y-%m-%d %H:%M:%S",
                    errors="raise",
                )
                frame = frame[["event_id", "sensor_id", "ts", "value"]]
                frame[["event_id", "sensor_id"]] = frame[
                    ["event_id", "sensor_id"]
                ].astype("int64")
                table = pa.Table.from_pandas(frame, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(
                        temporary, table.schema, compression="zstd"
                    )
                writer.write_table(table)
                selected += len(frame)
            return_code = process.wait()
            if return_code:
                error_file.seek(0)
                raise RuntimeError(error_file.read().decode("utf-8", errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if writer is not None:
                writer.close()

    if writer is None:
        _empty_events(temporary)
    expected = int(
        daily.loc[
            daily["date"].dt.year.eq(year)
            & daily["sensor_id"].astype(str).isin(smoke_ids),
            "n_events",
        ].sum()
    )
    if selected != expected:
        raise AssertionError(
            f"Raw/daily reconciliation failed for {year}: {selected} != {expected}"
        )
    os.replace(temporary, destination)
    audit = {
        "year": year,
        "source_records": records,
        "repeated_header_rows": repeated_headers,
        "smoke_events": selected,
        "expected_events_from_aggregates": expected,
        "sha256": sha256(destination),
        "seconds": time.perf_counter() - started,
    }
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(audit, ensure_ascii=False), flush=True)
    return destination


def combine_parts(parts: list[Path], output: Path) -> int:
    """Combine yearly Parquet parts without loading the full event history into memory."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    writer = None
    rows = 0
    try:
        for part in parts:
            parquet = pq.ParquetFile(part)
            for batch in parquet.iter_batches(batch_size=250_000):
                table = pa.Table.from_batches([batch])
                if writer is None:
                    writer = pq.ParquetWriter(
                        temporary, table.schema, compression="zstd"
                    )
                writer.write_table(table)
                rows += table.num_rows
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        _empty_events(temporary)
    os.replace(temporary, output)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Directory containing channels.csv and sensor_daily.parquet",
    )
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--years", type=int, nargs="+", default=[2022, 2023, 2024, 2025, 2026]
    )
    parser.add_argument("--chunk-size", type=int, default=600_000)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    channels = pd.read_csv(args.data_dir / "channels.csv")
    smoke_ids = set(
        channels.loc[channels["тип_датчика"].eq(SMOKE_TYPE), "ид_канала_данных"].astype(
            str
        )
    )
    daily = pd.read_parquet(args.data_dir / "sensor_daily.parquet")
    parts = [
        extract_year(
            args.raw_dir,
            daily,
            smoke_ids,
            year,
            args.scratch_dir,
            args.chunk_size,
            args.overwrite,
        )
        for year in args.years
    ]
    output = args.output or args.data_dir / OUTPUT_NAME
    rows = combine_parts(parts, output)
    expected = int(
        daily.loc[daily["sensor_id"].astype(str).isin(smoke_ids), "n_events"].sum()
    )
    if rows != expected:
        raise AssertionError(
            f"Combined smoke event count differs from daily aggregates: {rows} != {expected}"
        )
    report = {
        "years": args.years,
        "rows": rows,
        "expected_events_from_aggregates": expected,
        "sha256": sha256(output),
        "output": str(output),
    }
    (args.data_dir / "smoke_events_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
