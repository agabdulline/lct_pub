"""Решения, метки и признаки базовой модели v0 — один код для обучения и для ml-service.

Постановка как у событийной модели v3 (ml/reports/v3/REPORT_RU.md §2):
- решение принимается по каждой тревоге «Обнаружен дым» в момент T = время тревоги + 1 с;
- допустимость: у канала ≥ 7 дней истории; нет «Неисправен» за 24 ч до тревоги и в ту же секунду;
  одно решение на канал за 24 ч;
- цель: у этого канала начнётся «Неисправен» в [T, T + 24 ч).

Все признаки строятся только по событиям не позже секунды тревоги. Идентификатор канала в признаки не входит —
модель одинаково работает на каналах без истории.
Всё, что нужно признакам, лежит в истории одного объекта: соседи, группы тега и шлейфа — внутри объекта.
"""

import numpy as np
import pandas as pd

ALARM = "Обнаружен дым"
FAULT = "Неисправен"
UNDEFINED = "Неопределен"
DISABLED = "Отключено устройство"

HOUR = np.timedelta64(3600, "s")
DAY = np.timedelta64(86400, "s")
YEAR = np.timedelta64(365 * 86400, "s")

FEATURES = [
    # канал
    "ch_alarms_1d",
    "ch_alarms_7d",
    "ch_alarms_30d",
    "ch_solitary_30d",
    "ch_faults_30d",
    "ch_faults_365d",
    "ch_hours_since_fault",
    "ch_undefined_30d",
    "ch_disabled_365d",
    "ch_events_1d",
    "ch_days_observed",
    "ch_past_n",
    "ch_past_rate",
    # соседи на объекте
    "obj_active_channels_90d",
    "obj_alarm_channels_5m",
    "obj_alarm_channels_1h",
    "obj_alarm_channels_24h",
    "solitary_1h",
    "obj_fault_channels_1h",
    "obj_fault_channels_24h",
    "obj_past_n",
    "obj_past_rate",
    # соседи по пикету: тот же объект, |Δ пикета| ≤ 2 — у v3 этот признак дал выигрыш
    "near_active_channels_90d",
    "near_alarm_channels_1h",
    "near_solitary_1h",
    "near_fault_channels_24h",
    # группа по тегу (2 части) и шлейф (3 части) внутри объекта
    "grp_alarm_channels_1h",
    "grp_fault_channels_24h",
    "grp_past_n",
    "grp_past_rate",
    "loop_alarm_channels_1h",
    "loop_past_n",
    "loop_past_rate",
    # время
    "hour",
    "weekday",
    "month_sin",
    "month_cos",
    "ppr_like",
]


def prepare(events: pd.DataFrame, channels: pd.DataFrame) -> pd.DataFrame:
    """events: channel_id, event_id, ts (наивное московское), value.

    channels: channel_id, object_id, tag, picket_number.
    """
    meta = channels[["channel_id", "object_id", "tag", "picket_number"]].copy()
    parts = meta["tag"].fillna("").str.strip().str.strip(".").str.split(".")
    meta["grp"] = meta["object_id"].astype(str) + ":" + parts.str[:2].str.join(".")
    meta["loop"] = meta["object_id"].astype(str) + ":" + parts.str[:3].str.join(".")
    ev = events.merge(meta[["channel_id", "object_id", "grp", "loop", "picket_number"]], on="channel_id", how="inner")
    ev = ev.sort_values(["channel_id", "ts", "event_id"], kind="stable").reset_index(drop=True)
    prev = ev.groupby("channel_id")["value"].shift()
    ev["is_fault"] = ev["value"].eq(FAULT)
    ev["fault_onset"] = ev["is_fault"] & prev.ne(FAULT)
    ev["is_smoke_alarm"] = ev["value"].eq(ALARM)
    return ev


def _decisions(ev: pd.DataFrame, data_end: np.datetime64, gaps: list[tuple]) -> pd.DataFrame:
    first_seen = ev.groupby("channel_id")["ts"].min()
    faults = {c: g["ts"].to_numpy() for c, g in ev[ev.is_fault].groupby("channel_id")}
    onsets = {c: g["ts"].to_numpy() for c, g in ev[ev.fault_onset].groupby("channel_id")}
    empty = np.array([], dtype="datetime64[ns]")
    rows = []
    for (cid, obj, grp, loop), g in ev[ev.is_smoke_alarm].groupby(["channel_id", "object_id", "grp", "loop"]):
        f, o = faults.get(cid, empty), onsets.get(cid, empty)
        start = first_seen[cid].to_datetime64()
        last = None
        for ts in g["ts"].to_numpy():
            if ts - start < 7 * DAY:
                continue
            if np.searchsorted(f, ts, "right") > np.searchsorted(f, ts - DAY, "left"):
                continue  # неисправность за 24 ч до тревоги или в ту же секунду
            if last is not None and ts - last < DAY:
                continue  # одно решение на канал за 24 ч
            last = ts
            t = ts + np.timedelta64(1, "s")
            k = np.searchsorted(o, t, "left")
            positive = k < len(o) and o[k] < t + DAY
            observed = t + DAY <= data_end and not any(a < t + DAY and t < b for a, b in gaps)
            lead_h = (o[k] - t) / HOUR if positive else np.nan
            rows.append((cid, obj, grp, loop, ts, t, float(positive) if observed else np.nan, lead_h))
    return pd.DataFrame(
        rows, columns=["channel_id", "object_id", "grp", "loop", "alarm_ts", "decision_time", "target", "lead_hours"]
    ).sort_values("decision_time", kind="stable")


def _count_distinct(times: np.ndarray, ids: np.ndarray, lo, hi, exclude) -> int:
    i, j = np.searchsorted(times, lo, "left"), np.searchsorted(times, hi, "right")
    window = ids[i:j]
    return int(np.unique(window[window != exclude]).size)


def _window(c: dict, kind: str, ts, span, hi: int) -> int:
    """Число событий вида kind у канала в окне [ts − span, ts] по накопленным суммам."""
    lo = np.searchsorted(c["t"], ts - span, "left")
    return int(c[kind][hi] - c[kind][lo])


def _past_rates(dec: pd.DataFrame, key: str) -> tuple[np.ndarray, np.ndarray]:
    """Доля прошлых решений по ключу, после которых была неисправность: только закрытые 24-часовые окна за год."""
    n_out = np.full(len(dec), np.nan)
    rate_out = np.full(len(dec), np.nan)
    for _, idx in dec.groupby(key).indices.items():
        sub = dec.iloc[idx]
        t = sub["decision_time"].to_numpy()
        closed = t + DAY
        y = sub["target"].to_numpy()
        known = ~np.isnan(y)
        cum_n = np.concatenate([[0], np.cumsum(known)])
        cum_pos = np.concatenate([[0], np.cumsum(np.where(known, y, 0.0))])
        hi = np.searchsorted(closed, t, "right")
        lo = np.searchsorted(t, t - YEAR, "left")
        n = cum_n[hi] - cum_n[np.minimum(lo, hi)]
        pos = cum_pos[hi] - cum_pos[np.minimum(lo, hi)]
        n_out[idx] = n
        rate_out[idx] = np.where(n > 0, pos / np.maximum(n, 1), np.nan)
    return n_out, rate_out


def build(events: pd.DataFrame, channels: pd.DataFrame, data_end, gaps=(), only=None) -> pd.DataFrame:
    """Все допустимые решения с признаками и метками (метка NaN, если горизонт не наблюдается).

    only — набор (channel_id, alarm_ts): построчные признаки считаются только для этих решений, остальные строки
    остаются с NaN. Решения, метки и исходы прошлых решений считаются по всей истории, как при обучении, поэтому
    признаки нужных строк совпадают с полным расчётом. Нужно ml-service: запрос — единицы тревог, история — годы.
    """
    ev = prepare(events, channels)
    data_end = np.datetime64(pd.Timestamp(data_end))
    gaps = [(np.datetime64(pd.Timestamp(a)), np.datetime64(pd.Timestamp(b))) for a, b in gaps]
    dec = _decisions(ev, data_end, gaps).reset_index(drop=True)
    if dec.empty:
        return dec.reindex(columns=[*dec.columns, *FEATURES])

    # По каждому каналу — время событий и накопленные счётчики: любой счётчик в окне = разность двух сумм.
    chan = {}
    for cid, g in ev.groupby("channel_id"):
        v = g["value"].to_numpy()
        onset = g["fault_onset"].to_numpy()
        chan[cid] = {
            "t": g["ts"].to_numpy(),
            "alarm": np.concatenate([[0], np.cumsum(v == ALARM)]),
            "onset": np.concatenate([[0], np.cumsum(onset)]),
            "undefined": np.concatenate([[0], np.cumsum(v == UNDEFINED)]),
            "disabled": np.concatenate([[0], np.cumsum(v == DISABLED)]),
            "onset_t": g["ts"].to_numpy()[onset],
        }

    alarms = ev[ev.is_smoke_alarm]
    onsets = ev[ev.fault_onset]

    def arrays(frame, key):
        """Время и каналы событий по ключу, отсортированные по времени — для поиска окон."""
        out = {}
        for k, g in frame.groupby(key):
            g = g.sort_values("ts", kind="stable")
            out[k] = (g["ts"].to_numpy(), g["channel_id"].to_numpy())
        return out

    picket = channels.set_index("channel_id")["picket_number"].astype("float64").to_dict()

    def near_count(times, ids, lo, hi, cid, pk):
        if np.isnan(pk):
            return np.nan
        i, j = np.searchsorted(times, lo, "left"), np.searchsorted(times, hi, "right")
        w = ids[i:j]
        w = w[w != cid]
        pks = np.array([picket.get(x, np.nan) for x in w])
        return int(np.unique(w[np.abs(pks - pk) <= 2]).size)

    obj_alarm, obj_onset = arrays(alarms, "object_id"), arrays(onsets, "object_id")
    grp_alarm, grp_onset = arrays(alarms, "grp"), arrays(onsets, "grp")
    loop_alarm = arrays(alarms, "loop")
    none = (np.array([], dtype="datetime64[ns]"), np.array([], dtype="int64"))

    # Активность каналов объекта по дням: «соседи, сообщавшие что-либо за 90 дней» — по суточной сетке.
    day0 = ev["ts"].min().normalize()
    ev_day = ((ev["ts"] - day0) // pd.Timedelta(days=1)).to_numpy()
    n_days = int(ev_day.max()) + 2
    activity = {}
    for obj, g in ev.assign(day=ev_day).groupby("object_id"):
        ids = np.unique(g["channel_id"].to_numpy())
        pos = {c: i for i, c in enumerate(ids)}
        grid = np.zeros((len(ids), n_days), dtype=np.int32)
        grid[[pos[c] for c in g["channel_id"].to_numpy()], g["day"].to_numpy()] = 1
        cum = np.concatenate([np.zeros((len(ids), 1), np.int32), np.cumsum(grid, axis=1)], axis=1)
        activity[obj] = (pos, cum, np.array([picket.get(c, np.nan) for c in ids]))

    # одиночность прошлых тревог канала — нужна для ch_solitary_30d
    solitary_alarm = {}
    for _obj, (at, ac) in obj_alarm.items():
        flags = np.array([_count_distinct(at, ac, at[i] - HOUR, at[i], ac[i]) == 0 for i in range(len(at))])
        for cid in np.unique(ac):
            m = ac == cid
            solitary_alarm[cid] = (at[m], np.concatenate([[0], np.cumsum(flags[m])]))

    feats = {name: np.full(len(dec), np.nan) for name in FEATURES}
    # По массивам numpy, а не itertuples: pandas.Timestamp ломает np.searchsorted по datetime64.
    col_ts = dec["alarm_ts"].to_numpy().astype("datetime64[ns]")
    col_ch, col_obj = dec["channel_id"].to_numpy(), dec["object_id"].to_numpy()
    col_grp, col_loop = dec["grp"].to_numpy(), dec["loop"].to_numpy()
    if only is None:
        rows = range(len(dec))
    else:
        wanted = {(int(c), pd.Timestamp(t)) for c, t in only}
        rows = [i for i in range(len(dec)) if (int(col_ch[i]), pd.Timestamp(col_ts[i])) in wanted]
    for i in rows:
        ts, cid = col_ts[i], col_ch[i]
        obj_id, grp_id, loop_id = col_obj[i], col_grp[i], col_loop[i]
        c = chan[cid]
        t = c["t"]
        hi = np.searchsorted(t, ts, "right")

        feats["ch_alarms_1d"][i] = _window(c, "alarm", ts, DAY, hi)
        feats["ch_alarms_7d"][i] = _window(c, "alarm", ts, 7 * DAY, hi)
        feats["ch_alarms_30d"][i] = _window(c, "alarm", ts, 30 * DAY, hi)
        sa_t, sa_cum = solitary_alarm.get(cid, (none[0], np.array([0])))
        j0, j1 = np.searchsorted(sa_t, ts - 30 * DAY, "left"), np.searchsorted(sa_t, ts, "right")
        feats["ch_solitary_30d"][i] = int(sa_cum[j1] - sa_cum[j0])
        feats["ch_faults_30d"][i] = _window(c, "onset", ts, 30 * DAY, hi)
        feats["ch_faults_365d"][i] = _window(c, "onset", ts, YEAR, hi)
        k = np.searchsorted(c["onset_t"], ts, "right")
        feats["ch_hours_since_fault"][i] = (ts - c["onset_t"][k - 1]) / HOUR if k else np.nan
        feats["ch_undefined_30d"][i] = _window(c, "undefined", ts, 30 * DAY, hi)
        feats["ch_disabled_365d"][i] = _window(c, "disabled", ts, YEAR, hi)
        feats["ch_events_1d"][i] = hi - np.searchsorted(t, ts - DAY, "left")
        feats["ch_days_observed"][i] = (ts - t[0]) / DAY

        pos, cum, row_pk = activity[obj_id]
        d = int((pd.Timestamp(ts) - day0) // pd.Timedelta(days=1))
        recent = cum[:, d + 1] - cum[:, max(d - 89, 0)]
        active = int((recent > 0).sum() - (recent[pos[cid]] > 0))
        pk = picket.get(cid, np.nan)
        if np.isnan(pk):
            near_active = np.nan
        else:
            near = (recent > 0) & (np.abs(row_pk - pk) <= 2)
            near_active = int(near.sum() - near[pos[cid]])
        feats["obj_active_channels_90d"][i] = active
        at, ac = obj_alarm.get(obj_id, none)
        feats["obj_alarm_channels_5m"][i] = _count_distinct(at, ac, ts - 300 * np.timedelta64(1, "s"), ts, cid)
        feats["obj_alarm_channels_1h"][i] = n1h = _count_distinct(at, ac, ts - HOUR, ts, cid)
        feats["obj_alarm_channels_24h"][i] = _count_distinct(at, ac, ts - DAY, ts, cid)
        feats["solitary_1h"][i] = np.nan if active == 0 else float(n1h == 0)
        feats["near_active_channels_90d"][i] = near_active
        feats["near_alarm_channels_1h"][i] = near_1h = near_count(at, ac, ts - HOUR, ts, cid, pk)
        feats["near_solitary_1h"][i] = np.nan if not near_active else float(near_1h == 0)
        ot, oc = obj_onset.get(obj_id, none)
        feats["obj_fault_channels_1h"][i] = _count_distinct(ot, oc, ts - HOUR, ts, cid)
        feats["obj_fault_channels_24h"][i] = _count_distinct(ot, oc, ts - DAY, ts, cid)
        feats["near_fault_channels_24h"][i] = near_count(ot, oc, ts - DAY, ts, cid, pk)

        gt, gc = grp_alarm.get(grp_id, none)
        feats["grp_alarm_channels_1h"][i] = _count_distinct(gt, gc, ts - HOUR, ts, cid)
        got, goc = grp_onset.get(grp_id, none)
        feats["grp_fault_channels_24h"][i] = _count_distinct(got, goc, ts - DAY, ts, cid)
        lt, lc = loop_alarm.get(loop_id, none)
        feats["loop_alarm_channels_1h"][i] = _count_distinct(lt, lc, ts - HOUR, ts, cid)

        stamp = pd.Timestamp(ts)
        feats["hour"][i] = stamp.hour
        feats["weekday"][i] = stamp.weekday()
        feats["month_sin"][i] = np.sin(2 * np.pi * stamp.month / 12)
        feats["month_cos"][i] = np.cos(2 * np.pi * stamp.month / 12)
        feats["ppr_like"][i] = float(n1h >= 9 and stamp.weekday() < 5 and 8 <= stamp.hour <= 12)

    for key, prefix in (("channel_id", "ch"), ("object_id", "obj"), ("grp", "grp"), ("loop", "loop")):
        feats[f"{prefix}_past_n"], feats[f"{prefix}_past_rate"] = _past_rates(dec, key)

    return pd.concat([dec, pd.DataFrame(feats)], axis=1)


def skip_reason(events: pd.DataFrame, channels: pd.DataFrame, channel_id: int, alarm_ts) -> str | None:
    """Почему по тревоге нет решения — те же правила допустимости, что в _decisions; None — решение есть.

    Причины как в docs/api.md §2: alarm_not_found, history_lt_7d, fault_same_second, fault_recent_24h,
    decided_within_24h.
    """
    ev = prepare(events[events["channel_id"] == channel_id], channels)
    ts = np.datetime64(pd.Timestamp(alarm_ts), "ns")
    own = ev[ev["channel_id"] == channel_id]
    t = own["ts"].to_numpy()
    if not ((own["ts"] == pd.Timestamp(ts)) & own["is_smoke_alarm"]).any():
        return "alarm_not_found"
    if len(t) == 0 or ts - t[0] < 7 * DAY:
        return "history_lt_7d"
    faults = own.loc[own["is_fault"], "ts"].to_numpy()
    if (faults == ts).any():
        return "fault_same_second"
    if np.searchsorted(faults, ts, "right") > np.searchsorted(faults, ts - DAY, "left"):
        return "fault_recent_24h"
    dec = _decisions(ev, ts + DAY, [])
    if not ((dec["channel_id"] == channel_id) & (dec["alarm_ts"] == pd.Timestamp(ts))).any():
        return "decided_within_24h"
    return None
