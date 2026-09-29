"""Загрузка событий дымовых датчиков из общего архива Parquet (docs/architecture/data.md §2, D26).

Архив собирает backend: `python -m app.cli build-archive`. Время в архиве — UTC; признаки считаются по местному
времени Москвы, поэтому здесь переводим в наивное московское.
"""

from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

SMOKE = "Датчик дыма"
TZ = "Europe/Moscow"


def load_channels(archive: Path) -> pd.DataFrame:
    ch = pd.read_parquet(archive / "catalog" / "channels.parquet")
    return ch.rename(columns={"id": "channel_id"})


def load_events(archive: Path, channel_ids, since=None, until=None) -> pd.DataFrame:
    """События каналов из архива: channel_id, event_id, ts (наивное московское), value."""
    dataset = ds.dataset(archive / "events", format="parquet", partitioning="hive")
    flt = ds.field("channel_id").isin(list(channel_ids))
    if since is not None:
        flt &= ds.field("year") >= pd.Timestamp(since).year
    table = dataset.to_table(columns=["event_id", "channel_id", "ts", "value_raw"], filter=flt)
    ev = table.to_pandas()
    ev["ts"] = ev["ts"].dt.tz_convert(TZ).dt.tz_localize(None).astype("datetime64[ns]")
    ev = ev.rename(columns={"value_raw": "value"})
    if since is not None:
        ev = ev[ev["ts"] >= pd.Timestamp(since)]
    if until is not None:
        ev = ev[ev["ts"] < pd.Timestamp(until)]
    ev["channel_id"] = ev["channel_id"].astype("int64")
    return ev.sort_values(["channel_id", "ts", "event_id"], kind="stable").reset_index(drop=True)
