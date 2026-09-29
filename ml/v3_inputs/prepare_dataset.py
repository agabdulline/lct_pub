"""Rebuild the portable notebook input after src/ingest.py."""

import argparse
import json
from pathlib import Path

import pandas as pd

from ml.v3.src.baseline_core import SENSOR_FAMILIES, dump_json, sha256
from ml.v3_inputs.ingest import journal_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        required=True,
        help="Registries and original 7z archives",
    )
    parser.add_argument("--daily-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Output data/ folder"
    )
    args = parser.parse_args()
    out = args.output_dir
    if out.exists() and list(out.iterdir()):
        raise ValueError(
            "Use a new empty output directory to preserve previous experiments"
        )
    out.mkdir(parents=True, exist_ok=True)
    channels = pd.read_csv(args.raw_dir / "справочник_каналов_датчиков.csv")
    objects = pd.read_csv(args.raw_dir / "справочник_объектов_диспетчер.csv")
    assert channels["ид_канала_данных"].is_unique
    channels.to_csv(out / "channels.csv", index=False)
    objects.to_csv(out / "objects.csv", index=False)
    selected, inventory, coverage, files = [], [], [], []
    for f in sorted(args.daily_dir.glob("events_*.parquet")):
        year = int(f.stem[-4:])
        rpt = json.loads(
            (args.report_dir / f"ingestion_{year}.json").read_text(encoding="utf-8")
        )
        d = pd.read_parquet(f)
        assert not d.duplicated(["sensor_id", "date"]).any()
        assert len(d) == rpt["daily_rows"] and int(d.n_events.sum()) == rpt["rows"]
        j = d.merge(
            channels[["ид_канала_данных", "тип_датчика"]],
            left_on="sensor_id",
            right_on="ид_канала_данных",
            how="left",
            validate="many_to_one",
        )
        assert (
            int(j.loc[j["тип_датчика"].isna(), "n_events"].sum())
            == rpt["unmatched_event_rows"]
        )
        j["sensor_type"] = j["тип_датчика"].fillna("Нет в справочнике")
        j["fault_day"] = j.n_fault.gt(0).astype(int)
        summ = (
            j.groupby("sensor_type")
            .agg(
                channels=("sensor_id", "nunique"),
                daily_rows=("sensor_id", "size"),
                events=("n_events", "sum"),
                fault_days=("fault_day", "sum"),
                fault_events=("n_fault", "sum"),
            )
            .reset_index()
        )
        summ["year"] = year
        inventory.append(summ)
        selected.append(j.loc[j.sensor_type.isin(SENSOR_FAMILIES), list(d.columns)])
        coverage.append(d.groupby("date").n_events.sum().rename("events").reset_index())
        archive = journal_source(args.raw_dir, year)
        files.append(
            {
                "year": year,
                "raw_archive": str(archive),
                "archive_bytes": archive.stat().st_size if archive.exists() else None,
                "daily_source": str(f),
                "daily_sha256": sha256(f),
                "rows": rpt["rows"],
                "daily_rows": len(d),
                "unmatched_events": rpt["unmatched_event_rows"],
                "unmatched_channels": rpt["unmatched_channel_count"],
                "repeated_headers": rpt["repeated_header_rows"],
                "duplicate_diagnostics": rpt["duplicate_diagnostics"],
                "date_min": rpt["date_min"],
                "date_max": rpt["date_max"],
            }
        )
        print(year, len(d), flush=True)
    assert files, "No daily files found"
    combined = pd.concat(selected, ignore_index=True).sort_values(["sensor_id", "date"])
    assert not combined.duplicated(["sensor_id", "date"]).any()
    combined.to_parquet(out / "sensor_daily.parquet", index=False, compression="zstd")
    pd.concat(inventory, ignore_index=True).to_csv(
        out / "inventory_by_type_year.csv", index=False
    )
    pd.concat(coverage, ignore_index=True).sort_values("date").to_csv(
        out / "global_coverage.csv", index=False
    )
    manifest = {
        "source": "Audited per-channel/day aggregates from src/ingest.py",
        "scope": list(SENSOR_FAMILIES),
        "deduplicated": False,
        "historical_registry": "Current snapshot; no installation/replacement/removal history",
        "date_cutoff_exclusive_for_labels": str(combined.date.max().date()),
        "files": files,
        "packaged_files_sha256": {
            p.name: sha256(p) for p in out.iterdir() if p.is_file()
        },
    }
    dump_json(out / "manifest.json", manifest)
    print("Saved", out)


if __name__ == "__main__":
    main()
