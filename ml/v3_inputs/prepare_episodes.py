"""Optional raw-archive rebuild of fault_episodes.parquet; input files are read-only.

Usage: python prepare_episodes.py --raw-dir ORIGINAL_DATA --data-dir data --scratch-dir scratch
Run prepare_dataset.py first if rebuilding the daily aggregates as well.
"""

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ml.v3_inputs.ingest import archive_command, journal_source


def extract(raw_dir, daily, out, years):
    out.mkdir(parents=True, exist_ok=True)
    for year in years:
        dst = out / f"events_{year}.parquet"
        if dst.exists():
            continue
        want = daily.loc[daily.date.dt.year.eq(year) & daily.n_fault.gt(0)]
        keys = set(zip(want.sensor_id.astype(str), want.date.dt.strftime("%Y-%m-%d")))
        ids = {sid for sid, day in keys}
        days = {day for sid, day in keys}
        count = selected = 0
        writer = None
        tick = time.perf_counter()
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                archive_command(journal_source(raw_dir, year)),
                stdout=subprocess.PIPE,
                stderr=err,
            )
            try:
                cols = [
                    "ид_события",
                    "ид_канала_данных",
                    "дата",
                    "время",
                    "значение_датчика",
                ]
                for chunk in pd.read_csv(
                    proc.stdout, usecols=cols, dtype=str, chunksize=500000
                ):
                    count += len(chunk)
                    x = chunk.loc[
                        chunk["ид_канала_данных"].isin(ids) & chunk["дата"].isin(days)
                    ]
                    keep = np.fromiter(
                        (
                            (sid, day) in keys
                            for sid, day in zip(x["ид_канала_данных"], x["дата"])
                        ),
                        bool,
                        count=len(x),
                    )
                    x = x.loc[keep].copy()
                    if not len(x):
                        continue
                    x = x.rename(
                        columns={
                            "ид_события": "event_id",
                            "ид_канала_данных": "sensor_id",
                            "значение_датчика": "value",
                        }
                    )
                    x["ts"] = pd.to_datetime(
                        x["дата"] + " " + x["время"], format="%Y-%m-%d %H:%M:%S"
                    )
                    x = x[["event_id", "sensor_id", "ts", "value"]]
                    x[["event_id", "sensor_id"]] = x[["event_id", "sensor_id"]].astype(
                        "int64"
                    )
                    tab = pa.Table.from_pandas(x, preserve_index=False)
                    if writer is None:
                        writer = pq.ParquetWriter(
                            dst.with_suffix(".tmp"), tab.schema, compression="zstd"
                        )
                    writer.write_table(tab)
                    selected += len(x)
                code = proc.wait()
                if code:
                    err.seek(0)
                    raise RuntimeError(err.read().decode(errors="replace"))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if writer is not None:
                    writer.close()
        assert selected == int(want.n_events.sum()), "Raw/daily reconciliation failed"
        if writer is None:
            pd.DataFrame(
                {
                    "event_id": pd.Series(dtype="int64"),
                    "sensor_id": pd.Series(dtype="int64"),
                    "ts": pd.Series(dtype="datetime64[ns]"),
                    "value": pd.Series(dtype=str),
                }
            ).to_parquet(dst, index=False)
        else:
            dst.with_suffix(".tmp").replace(dst)
        audit = {
            "year": year,
            "source_records": count,
            "selected_events": selected,
            "fault_channel_days": len(want),
            "expected_events_from_aggregates": int(want.n_events.sum()),
            "seconds": time.perf_counter() - tick,
        }
        (out / f"audit_{year}.json").write_text(
            json.dumps(audit, indent=2), encoding="utf-8"
        )
        print(audit, flush=True)


def reconstruct(daily, raw, observed_dates):
    parts = [raw]
    for prefix in ("first", "last"):
        parts.append(
            daily[
                ["sensor_id", f"{prefix}_event_id", f"{prefix}_ts", f"{prefix}_value"]
            ].rename(
                columns={
                    f"{prefix}_event_id": "event_id",
                    f"{prefix}_ts": "ts",
                    f"{prefix}_value": "value",
                }
            )
        )
    x = pd.concat(parts, ignore_index=True).drop_duplicates(
        ["sensor_id", "event_id", "ts", "value"]
    )
    reused = int(x.duplicated(["sensor_id", "event_id"], keep=False).sum())
    unique = len(x)
    x = x.sort_values(["sensor_id", "ts", "event_id"], kind="stable")
    prev = x.groupby("sensor_id", sort=False).value.shift()
    x = x.loc[x.value.ne(prev)]
    next_time = x.groupby("sensor_id", sort=False).ts.shift(-1)
    next_value = x.groupby("sensor_id", sort=False).value.shift(-1)
    fault = x.value.eq("Неисправен")
    ep = (
        x.loc[fault, ["sensor_id", "event_id", "ts"]]
        .rename(columns={"ts": "start"})
        .copy()
    )
    ep["end"] = next_time.loc[fault]
    ep["next_state"] = next_value.loc[fault]
    ep["duration_seconds"] = (ep.end - ep.start).dt.total_seconds()
    ep["start_is_observed"] = prev.reindex(ep.index).notna()
    ep["coverage_uncertain"] = False
    obs = pd.DatetimeIndex(observed_dates)
    for day in pd.date_range(obs.min(), obs.max()).difference(obs):
        ep.loc[
            (ep.start < day + pd.Timedelta(days=1))
            & (ep.end.fillna(obs.max() + pd.Timedelta(days=1)) > day),
            "coverage_uncertain",
        ] = True
    audit = {
        "unique_event_rows": unique,
        "reused_event_id_rows": reused,
        "episodes": len(ep),
        "observed_onsets": int(ep.start_is_observed.sum()),
        "open_episodes": int(ep.end.isna().sum()),
        "zero_duration": int(ep.duration_seconds.eq(0).sum()),
        "ordering_ties": "timestamp, then event_id; within-second order is uncertain",
    }
    return ep, audit


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw-dir", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--scratch-dir", type=Path, required=True)
    args = p.parse_args()
    d = pd.read_parquet(args.data_dir / "sensor_daily.parquet")
    years = [int(y) for y in sorted(d.date.dt.year.unique())]  # numpy int32 is not JSON-serializable (audit)
    extract(args.raw_dir, d, args.scratch_dir, years)
    raw = pd.concat(
        [pd.read_parquet(args.scratch_dir / f"events_{y}.parquet") for y in years],
        ignore_index=True,
    )
    obs = pd.read_csv(args.data_dir / "global_coverage.csv", parse_dates=["date"]).date
    ep, audit = reconstruct(d, raw, obs)
    ep.to_parquet(
        args.data_dir / "fault_episodes.parquet", index=False, compression="zstd"
    )
    (args.data_dir / "episode_audit.json").write_text(
        json.dumps(audit, indent=2), encoding="utf-8"
    )
    print(audit, flush=True)


if __name__ == "__main__":
    main()
