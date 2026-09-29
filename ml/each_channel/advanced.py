"""Last observed states and fully matured historical 30-day outcome rates."""

import numpy as np
import pandas as pd
from ml.each_channel.features import DAY, GAPS, STATE_COLUMNS


def add_advanced(frame, daily, episodes, channels):
    first = pd.Timestamp("2021-01-01")
    dates = pd.date_range(first, frame.date.max(), freq="D")
    t = dates.to_numpy()
    n = len(dates)
    far = np.datetime64("2262-01-01", "ns")
    by_ep = {cid: g.sort_values("start") for cid, g in episodes.groupby("channel_id")}
    by_day = {cid: g.sort_values("date") for cid, g in daily.groupby("channel_id")}
    meta = channels.set_index("channel_id")
    channel_cum, object_sum, group_sum = {}, {}, {}
    coverage = np.ones(n, dtype=bool)
    for a, b in GAPS:
        coverage &= ~((t < np.datetime64(b)) & (t + 30 * DAY > np.datetime64(a)))
    # These retrospective targets are exposed as features only after their entire horizon has closed.
    for cid in meta.index:
        g = by_day.get(cid)
        active = np.zeros(n, dtype=np.int32)
        if g is not None:
            positions = ((g.date - first) / pd.Timedelta(days=1)).astype(int).to_numpy()
            mask = (positions >= 0) & (positions < n)
            active[positions[mask]] = g.n_events.to_numpy()[mask] > 0
        cs = np.r_[0, active.cumsum()]
        idx = np.arange(n)
        valid = (cs[idx] - cs[np.maximum(idx - 30, 0)] > 0) & coverage
        target = np.zeros(n, dtype=bool)
        e = by_ep.get(cid)
        if e is not None:
            starts = e.start.to_numpy()
            ends = e.end.fillna(pd.Timestamp(far)).to_numpy()
            k = np.searchsorted(starts, t, "left")
            in_fault = (k > 0) & (np.maximum.accumulate(ends)[np.maximum(k - 1, 0)] >= t)
            valid &= ~in_fault
            observed = e.loc[e.start_is_observed, "start"].to_numpy()
            future = np.searchsorted(observed, t + DAY, "left")
            if len(observed):
                target = (future < len(observed)) & (
                    observed[np.minimum(future, len(observed) - 1)] < t + 30 * DAY
                )
            for unknown in e.loc[~e.start_is_observed, "start"].to_numpy():
                valid &= ~((unknown >= t + DAY) & (unknown < t + 30 * DAY))
        values = np.stack([valid, target & valid]).astype(np.int32)
        channel_cum[cid] = np.pad(values.cumsum(axis=1, dtype=np.int32), ((0, 0), (1, 0)))
        for collection, key in (
            (object_sum, meta.loc[cid, "object_id"]),
            (group_sum, meta.loc[cid, "tag_group"]),
        ):
            if key not in collection:
                collection[key] = np.zeros((2, n), dtype=np.int32)
            collection[key] += values
    object_cum = {
        k: np.pad(v.cumsum(axis=1, dtype=np.int64), ((0, 0), (1, 0))) for k, v in object_sum.items()
    }
    group_cum = {
        k: np.pad(v.cumsum(axis=1, dtype=np.int64), ((0, 0), (1, 0))) for k, v in group_sum.items()
    }
    del object_sum, group_sum
    blocks = []
    for cid, block in frame.groupby("channel_id", sort=False):
        block = block.copy()
        points = block.date.to_numpy()
        pos = ((block.date - first) / pd.Timedelta(days=1)).astype(int).to_numpy()
        right = np.maximum(pos - 30 + 1, 0)
        own = channel_cum[cid]
        for w in (180, 365):
            left = np.maximum(pos - w, 0)
            own_values = own[:, right] - own[:, left]
            for prefix, cumulative in (
                ("own", own),
                ("object", object_cum[meta.loc[cid, "object_id"]]),
                ("group", group_cum[meta.loc[cid, "tag_group"]]),
            ):
                values = cumulative[:, right] - cumulative[:, left]
                if prefix != "own":
                    values = values - own_values
                denom, numer = values
                block[f"{prefix}_matured_count_{w}d"] = denom
                block[f"{prefix}_matured_positive_{w}d"] = numer
                block[f"{prefix}_matured_rate_{w}d"] = np.where(
                    denom > 0, numer / np.maximum(denom, 1), np.nan
                )
        block["channel_key"] = str(cid)
        g = by_day.get(cid)
        block["last_state"] = "missing"
        block["last_numeric"] = np.nan
        block["last_numeric_error"] = 0
        block["days_since_event"] = np.nan
        if g is not None and len(g):
            k = np.searchsorted(g.date.to_numpy(), points, "left") - 1
            has = k >= 0
            past = g.iloc[np.maximum(k, 0)]
            raw = past.get("last_value", pd.Series(["missing"] * len(past))).astype(str).to_numpy()
            numbers = pd.to_numeric(pd.Series(raw), errors="coerce").to_numpy()
            invalid = (numbers == -127) | (numbers <= -3000)
            states = np.where(
                np.isfinite(numbers), np.where(invalid, "numeric_error", "numeric"), raw
            )
            block["last_state"] = np.where(has, states, "missing")
            block["last_numeric"] = np.where(has & ~invalid, numbers, np.nan)
            block["last_numeric_error"] = has & invalid
            block["days_since_event"] = np.where(has, (points - past.date.to_numpy()) / DAY, np.nan)
            for column in ["n_alarm", "n_fault", *STATE_COLUMNS]:
                occurred = g.loc[g[column].gt(0), "date"].to_numpy()
                index = np.searchsorted(occurred, points, "left")
                elapsed = (
                    ((points - occurred[np.maximum(index - 1, 0)]) / DAY)
                    if len(occurred)
                    else np.full(len(points), np.nan)
                )
                block[f"days_since_{column}"] = np.where(
                    (index > 0) & (elapsed <= 365), elapsed, np.nan
                )
                for w in (7, 30):
                    block[f"days_with_{column}_{w}d"] = index - np.searchsorted(
                        occurred, points - w * DAY, "left"
                    )
        blocks.append(block)
    result = (
        pd.concat(blocks).sort_values(["date", "channel_id"], kind="stable").reset_index(drop=True)
    )
    numeric = result.select_dtypes(include=["float64", "int64"]).columns.difference(["channel_id"])
    result[numeric] = result[numeric].astype("float32")
    return result
