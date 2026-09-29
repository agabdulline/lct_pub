"""Shared causal features for training and as-of daily inference."""

from pathlib import Path

import numpy as np
import pandas as pd
from ml.baseline.daily import FEATURES as BASE_FEATURES
from ml.baseline.daily import build_daily

DAY = np.timedelta64(1, "D")
GAPS = [("2024-04-07", "2024-04-09"), ("2026-06-01", "2026-06-02")]
DATA_END = "2026-06-30"
TYPE_KEYS = {
    "Датчик дыма": "smoke",
    "Состояние фазы": "phase",
    "КД Дверь": "door",
    "Датчик движения": "motion",
    "Переключатель": "switch",
    "Состояние УИР-Р": "uir",
    "Датчик температуры": "temperature",
}
STATE_COLUMNS = [
    "n_unknown",
    "n_disabled",
    "n_normal",
    "n_smoke",
    "n_trigger",
    "n_open",
]


DAILY_INPUT_COLUMNS = [
    "sensor_id",
    "date",
    "n_events",
    "n_alarm",
    "n_fault",
    "n_numeric",
    "numeric_sum",
    "numeric_sumsq",
    "numeric_min",
    "numeric_max",
    "last_value",
    "last_ts",
    *STATE_COLUMNS,
]


def load_inputs(folder: Path):
    return normalize_inputs(
        pd.read_csv(folder / "channels.csv"),
        pd.read_parquet(folder / "sensor_daily.parquet", columns=DAILY_INPUT_COLUMNS),
        pd.read_parquet(folder / "fault_episodes.parquet"),
    )


def normalize_inputs(channels, daily, episodes):
    """Catalog, sensor_daily and fault_episodes in the collector's layout -> feature inputs.

    Shared by the files of the collector (load_inputs) and the service (ml.daily_routing.online)."""
    channels = channels.rename(
        columns={
            "ид_канала_данных": "channel_id",
            "ид_объект": "object_id",
            "тип_датчика": "sensor_type",
            "тег_инженерной_системы": "tag",
        }
    )
    channels["tag_group"] = (
        channels.object_id.astype(str)
        + ":"
        + channels.tag.fillna("").map(lambda value: ".".join(str(value).split(".")[:2]))
    )
    daily = daily[DAILY_INPUT_COLUMNS].rename(
        columns={"sensor_id": "channel_id", "n_numeric": "n_num", "numeric_max": "max"}
    )
    daily["date"] = pd.to_datetime(daily.date)
    daily["mean"] = (daily.numeric_sum / daily.n_num.clip(lower=1)).where(daily.n_num.gt(0))
    episodes = episodes.rename(columns={"sensor_id": "channel_id"})
    return channels, daily, episodes


def selected_types(channels):
    counts = channels.groupby("sensor_type").channel_id.nunique()
    total = channels.channel_id.nunique()
    return {
        TYPE_KEYS[name]: {"name": name, "channels": int(n), "share": float(n / total)}
        for name, n in counts.items()
        if n / total > 0.05
    }


def build_features(daily, episodes, channels, dates, only, *, labels=True):
    """All dynamic features use facts strictly before T; targets use observed future starts."""
    dates = pd.DatetimeIndex(sorted(set(pd.to_datetime(dates))))
    frame = build_daily(daily, episodes, channels, dates, only=only)
    t = dates.to_numpy().astype("datetime64[ns]")
    far = np.datetime64("2262-01-01", "ns")
    ep = episodes.copy()
    meta = channels.set_index("channel_id")
    ep["object_id"] = ep.channel_id.map(meta.object_id)
    ep["tag_group"] = ep.channel_id.map(meta.tag_group)
    # Do not filter history using coverage_uncertain: it can describe a future gap.
    context = {}
    for key in ("object_id", "tag_group"):
        context[key] = {}
        for name, g in ep.groupby(key):
            starts = np.sort(g.start.to_numpy())
            ends = np.sort(g.end.fillna(pd.Timestamp(far)).to_numpy())
            observed = np.sort(g.loc[g.start_is_observed, "start"].to_numpy())
            context[key][name] = {
                "active": np.searchsorted(starts, t, "left") - np.searchsorted(ends, t, "left"),
                **{
                    f"onsets_{w}d": np.searchsorted(observed, t, "left")
                    - np.searchsorted(observed, t - w * DAY, "left")
                    for w in (7, 30, 90)
                },
            }
    by_day = {cid: g for cid, g in daily.groupby("channel_id") if cid in set(only)}
    by_ep = {cid: g.sort_values("start") for cid, g in ep.groupby("channel_id")}
    blocks = []
    start_day = dates.min() - pd.Timedelta(days=400)
    grid_size = (dates.max() - start_day).days + 1
    idx = ((dates - start_day) / pd.Timedelta(days=1)).astype(int).to_numpy()
    for cid, base in frame.groupby("channel_id", sort=False):
        base = base.copy().reset_index(drop=True)
        e = by_ep.get(cid, ep.iloc[:0])
        starts = e.start.to_numpy()
        ends = e.end.fillna(pd.Timestamp(far)).to_numpy()
        k = np.searchsorted(starts, t, "left")
        own_active = np.zeros(len(t), dtype=bool)
        if len(e):
            own_active = (k > 0) & (np.maximum.accumulate(ends)[np.maximum(k - 1, 0)] >= t)
        observed = e.loc[e.start_is_observed, "start"].to_numpy()
        own_onsets = {
            w: np.searchsorted(observed, t, "left") - np.searchsorted(observed, t - w * DAY, "left")
            for w in (7, 30, 90, 365)
        }
        for w, value in own_onsets.items():
            base[f"onsets_{w}d"] = value
        for key, prefix in (("object_id", "obj"), ("tag_group", "group")):
            ctx = context[key].get(meta.loc[cid, key], {})
            base[f"{prefix}_in_fault"] = ctx.get("active", np.zeros(len(t))) - own_active
            for w in (7, 30, 90):
                base[f"{prefix}_onsets_{w}d"] = (
                    ctx.get(f"onsets_{w}d", np.zeros(len(t))) - own_onsets[w]
                )
        durations = np.full(len(t), np.nan)
        intervals = np.full(len(t), np.nan)
        for j in range(len(t)):
            done = (ends < t[j]) & (starts >= t[j] - 365 * DAY)
            if done.any():
                durations[j] = np.mean((ends[done] - starts[done]) / np.timedelta64(1, "h"))
            recent = observed[(observed < t[j]) & (observed >= t[j] - 365 * DAY)][-8:]
            if len(recent) >= 2:
                intervals[j] = np.median(np.diff(recent) / DAY)
        base["mean_episode_hours_365d"] = durations
        base["median_recurrence_days"] = intervals
        base["recurrence_phase"] = base.days_since_onset / np.maximum(intervals, 1 / 24)
        grid = np.zeros((len(STATE_COLUMNS) + 3, grid_size))
        g = by_day.get(cid)
        if g is not None:
            g = g.loc[g.date.ge(start_day) & g.date.lt(dates.max())]
            di = ((g.date - start_day) / pd.Timedelta(days=1)).astype(int).to_numpy()
            for row, col in enumerate([*STATE_COLUMNS, "n_num", "numeric_sum", "numeric_sumsq"]):
                grid[row, di] = g[col].fillna(0).to_numpy()
        cum = np.pad(grid.cumsum(axis=1), ((0, 0), (1, 0)))
        sums = {}
        for w in (1, 7, 30, 90):
            win = cum[:, idx] - cum[:, np.maximum(idx - w, 0)]
            sums[w] = win
            for row, col in enumerate(STATE_COLUMNS):
                base[f"{col}_{w}d"] = win[row]
        for w in (7, 30):
            n, total, square = sums[w][-3:]
            base[f"num_std_{w}d"] = np.where(
                n > 0,
                np.sqrt(np.maximum(square / np.maximum(n, 1) - (total / np.maximum(n, 1)) ** 2, 0)),
                np.nan,
            )
            for col in (
                "alarms",
                "faults_msg",
                "n_unknown",
                "n_disabled",
                "n_smoke",
                "n_trigger",
                "n_open",
            ):
                base[f"{col}_fraction_{w}d"] = base[f"{col}_{w}d"] / base[f"events_{w}d"].clip(
                    lower=1
                )
        base["activity_trend"] = (base.events_7d / 7) / (
            (base.events_30d - base.events_7d).clip(lower=0) / 23 + 1
        )
        base["fault_trend"] = (base.onsets_7d + 1) / (
            (base.onsets_30d - base.onsets_7d).clip(lower=0) + 1
        )
        base["num_mean_change"] = base.num_mean_7d - base.num_mean_30d
        base["object_key"] = str(meta.loc[cid, "object_id"])
        base["group_key"] = str(meta.loc[cid, "tag_group"])
        base["status"] = np.where(
            own_active,
            "in_fault",
            np.where(base.days_active_30d.eq(0), "no_coverage", "scored"),
        )
        base["target"], base["lead_days"], base["target_start"] = np.nan, np.nan, pd.NaT
        if labels:
            future = np.searchsorted(observed, t + DAY, "left")
            exists = future < len(observed)
            next_start = observed[np.minimum(future, len(observed) - 1)] if len(observed) else t
            positive = exists & (next_start < t + 30 * DAY)
            covered = t + 30 * DAY <= np.datetime64(DATA_END)
            for a, b in GAPS:
                covered &= ~((t < np.datetime64(b)) & (t + 30 * DAY > np.datetime64(a)))
            unknown = e.loc[~e.start_is_observed, "start"].to_numpy()
            for u in unknown:
                covered &= ~((u >= t + DAY) & (u < t + 30 * DAY))
            base["target"] = np.where(covered, positive.astype(float), np.nan)
            base["lead_days"] = np.where(positive, (next_start - t) / DAY, np.nan)
            base["target_start"] = pd.to_datetime(
                np.where(positive, next_start, np.datetime64("NaT", "ns"))
            )
        blocks.append(base)
    result = pd.concat(blocks, ignore_index=True)
    numeric = result.select_dtypes(include=["float64", "int64"]).columns.difference(["channel_id"])
    result[numeric] = result[numeric].astype("float32")
    return result.sort_values(["date", "channel_id"], kind="stable").reset_index(drop=True)


def feature_columns(frame, mode):
    if mode == "base":
        return list(BASE_FEATURES), ["sensor_type"]
    exclude = {
        "channel_id",
        "date",
        "status",
        "target",
        "lead_days",
        "target_start",
        "object_key",
        "group_key",
        "channel_key",
    }
    columns = [c for c in frame if c not in exclude]
    cats = [c for c in columns if frame[c].dtype == object]
    if mode in ("identity", "advanced_identity", "channel_identity"):
        columns += ["object_key", "group_key"]
        cats += ["object_key", "group_key"]
    if mode == "channel_identity":
        columns += ["channel_key"]
        cats += ["channel_key"]
    return columns, cats
