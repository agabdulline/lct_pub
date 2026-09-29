"""Restore missing channel types from the supplied ZIP of annual 7z journals."""

import argparse
import contextlib
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

import duckdb
import pandas as pd

DAILY_COLUMNS = [
    "sensor_id",
    "date",
    "n_events",
    "n_alarm",
    "n_fault",
    "n_unknown",
    "n_disabled",
    "n_normal",
    "n_smoke",
    "n_trigger",
    "n_open",
    "n_numeric",
    "numeric_sum",
    "numeric_sumsq",
    "numeric_min",
    "numeric_max",
    "last_value",
    "last_ts",
]


# Daily aggregates and fault episodes of the restored types; also used by the service over events from the working
# database (ml.daily_routing.online) — {events} is a relation with sensor_id, event_id, ts, alarm, value.
DAILY_SQL = """
        SELECT sensor_id, CAST(ts AS DATE) AS date, count(*) AS n_events,
          sum(alarm::INT) AS n_alarm, sum((value='Неисправен')::INT) AS n_fault,
          sum((value IN ('Неопределен','Не определено'))::INT) AS n_unknown,
          sum((value='Отключено устройство')::INT) AS n_disabled,
          sum((value='Норма')::INT) AS n_normal, sum((value='Обнаружен дым')::INT) AS n_smoke,
          sum((value IN ('Обнаружено движение','Рычаг сдернут'))::INT) AS n_trigger,
          sum((value='Не замкнут')::INT) AS n_open,
          count(TRY_CAST(value AS DOUBLE)) AS n_numeric,
          coalesce(sum(TRY_CAST(value AS DOUBLE)),0) AS numeric_sum,
          coalesce(sum(pow(TRY_CAST(value AS DOUBLE),2)),0) AS numeric_sumsq,
          min(TRY_CAST(value AS DOUBLE)) AS numeric_min, max(TRY_CAST(value AS DOUBLE)) AS numeric_max,
          arg_max(value, struct_pack(ts:=ts, event_id:=event_id, value:=value)) AS last_value,
          max(ts) AS last_ts
        FROM {events} GROUP BY sensor_id, CAST(ts AS DATE)
    """

EPISODES_SQL = """
        WITH ordered AS (
          SELECT *, value='Неисправен' AS fault,
            lag(value='Неисправен') OVER (PARTITION BY sensor_id ORDER BY ts,event_id,value) AS prev
          FROM {events}
        ), changes AS (
          SELECT * FROM ordered WHERE prev IS NULL OR fault!=prev
        ), spans AS (
          SELECT *, lead(ts) OVER (PARTITION BY sensor_id ORDER BY ts,event_id,value) AS end_ts
          FROM changes
        )
        SELECT sensor_id, ts AS start, end_ts AS "end", prev IS NOT NULL AS start_is_observed,
          false AS coverage_uncertain FROM spans WHERE fault
    """


def daily_and_episodes(con, events="events"):
    daily = con.execute(DAILY_SQL.format(events=events)).df()
    episodes = con.execute(EPISODES_SQL.format(events=events)).df()
    return daily, episodes


def sql_path(path):
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


def prepare(source: Path, archive: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    channels = pd.read_csv(source / "channels.csv")
    original = pd.read_parquet(source / "sensor_daily.parquet", columns=DAILY_COLUMNS)
    present_types = set(
        channels.loc[channels["ид_канала_данных"].isin(original.sensor_id), "тип_датчика"]
    )
    missing = channels.loc[~channels["тип_датчика"].isin(present_types), ["ид_канала_данных"]]
    missing = missing.rename(columns={"ид_канала_данных": "sensor_id"})
    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=4")
    con.execute(f"SET temp_directory={sql_path(out / 'duckdb_tmp')}")
    con.register("missing_ids", missing)
    with contextlib.ExitStack() as stack:
        # A directory holds the same annual journals already unpacked to CSV (our data/raw); a file is the ZIP of 7z
        z = None if archive.is_dir() else stack.enter_context(zipfile.ZipFile(archive))
        for year in range(2021, 2027):
            dest = out / f"events_{year}.parquet"
            if dest.exists():
                continue
            raw = out / "raw" / str(year)
            csv_file = raw / f"ext-journal-{year}.csv"
            if z is None:
                csv_file = archive / f"ext-journal-{year}.csv"
                if not csv_file.exists():
                    raise FileNotFoundError(csv_file)
            else:
                raw.mkdir(parents=True, exist_ok=True)
            packed = raw / f"ext-journal-{year}.7z"
            if not csv_file.exists():
                print(f"Extracting {year}", flush=True)
                if not packed.exists():
                    with (
                        z.open(f"dataset/ext-journal-{year}.7z") as src,
                        packed.open("wb") as dst,
                    ):
                        shutil.copyfileobj(src, dst)
                import py7zr  # only for the ZIP of 7z; the service imports the SQL above without it

                with py7zr.SevenZipFile(packed) as seven:
                    for name in seven.getnames():
                        if not (raw / name).resolve().is_relative_to(raw.resolve()):
                            raise ValueError("Archive member leaves the data directory")
                    seven.extractall(raw)
                if not csv_file.exists():
                    files = list(raw.rglob("*.csv"))
                    if len(files) != 1:
                        raise ValueError(f"Expected one journal in {raw}")
                    csv_file = files[0]
            print(f"Filtering {year}", flush=True)
            con.execute(f"""
                COPY (
                  SELECT DISTINCT TRY_CAST(j."ид_канала_данных" AS BIGINT) AS sensor_id,
                    TRY_CAST(j."ид_события" AS BIGINT) AS event_id,
                    TRY_CAST(j."дата" || ' ' || j."время" AS TIMESTAMP) AS ts,
                    j."тревожное" IN ('t','true') AS alarm,
                    j."значение_датчика" AS value
                  FROM read_csv({sql_path(csv_file)}, header=true, all_varchar=true) j
                  JOIN missing_ids m ON TRY_CAST(j."ид_канала_данных" AS BIGINT)=m.sensor_id
                  WHERE TRY_CAST(j."дата" AS DATE) IS NOT NULL
                ) TO {sql_path(dest)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
    con.execute(
        f"CREATE VIEW events AS SELECT * FROM read_parquet({sql_path(out / 'events_*.parquet')})"
    )
    extra_daily, extra_eps = daily_and_episodes(con)
    original_eps = pd.read_parquet(source / "fault_episodes.parquet", columns=list(extra_eps))
    daily = pd.concat([original, extra_daily], ignore_index=True)
    daily["date"] = pd.to_datetime(daily.date)
    if daily.duplicated(["sensor_id", "date"]).any():
        raise ValueError("Duplicate channel-day after merge")
    daily.sort_values(["sensor_id", "date"]).to_parquet(out / "sensor_daily.parquet", index=False)
    pd.concat([original_eps, extra_eps], ignore_index=True).sort_values(
        ["sensor_id", "start"]
    ).to_parquet(out / "fault_episodes.parquet", index=False)
    channels.to_csv(out / "channels.csv", index=False)
    metadata = {
        "prepared_source": str(source),
        "archive": str(archive),
        "restored_types": sorted(set(channels["тип_датчика"]) - present_types),
        "restored_years": list(range(2021, 2027)),
        "daily_rows": len(daily),
        "extra_daily_rows": len(extra_daily),
        "extra_episodes": len(extra_eps),
        "dedup": "channel,event_id,ts,alarm,value within year",
        "channels_sha256": hashlib.sha256((source / "channels.csv").read_bytes()).hexdigest(),
    }
    (out / "manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source, args.archive, args.out)
