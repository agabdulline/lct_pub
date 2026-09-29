"""Ежедневная базовая модель v0: начнётся ли «Неисправен» у канала в [T + 24 ч, T + 30 сут).

Постановка — как у ежедневной модели v3 (ml/reports/v3/REPORT_RU.md §2), кроме правила метки:
- решение в начале суток D (T = D 00:00 по Москве) для каналов восьми типов;
- допустимость: канал на момент T не в неисправности; за 30 суток до T были события (покрытие);
- цель — в [T + 24 ч, T + 30 сут) начинается «Неисправен» (первое начало не раньше T + 24 ч попадает в окно).
  Начало в первые 24 ч метку не отменяет — в этом отличие от v3: у неё цель — первое следующее начало, и начало
  в первые сутки делает строку отрицательной (допущение A16 в docs/QUESTIONS.md). Горизонт должен наблюдаться
  целиком, пропуски данных — метка неизвестна.

Признаки строятся из дневных итогов (channel_daily) и эпизодов неисправностей (fault_episodes) — тех же таблиц
рабочей базы, что читает ml-service. Идентификатора канала в признаках нет. Расчёт векторный по каждому каналу.
"""

import numpy as np
import pandas as pd

# Восемь типов с заметным числом эпизодов «Неисправен» с 2022 года — допущение к Q17 (какие типы у v3).
TYPES = [
    "Датчик дыма",
    "Состояние насоса",
    "Состояние вентилятора",
    "Датчик температуры",
    "Газовый датчик",
    "Состояние фазы",
    "Ручной извещатель",
    "ИБП",
]
DAY = np.timedelta64(1, "D")
HOUR = np.timedelta64(1, "h")
HORIZON_START, HORIZON_END = 1 * DAY, 30 * DAY
# Давность последнего начала и восстановления — не дальше года, иначе пусто: сервис читает историю за history_days
# (400 суток), и признак не должен зависеть от того, сколько истории загружено.
RECENCY_CAP = 365

FEATURES = [
    "sensor_type",
    "events_1d",
    "events_7d",
    "events_30d",
    "events_90d",
    "days_active_30d",
    "days_active_90d",
    "alarms_7d",
    "alarms_30d",
    "alarms_90d",
    "faults_msg_7d",
    "faults_msg_30d",
    "faults_msg_90d",
    "num_mean_7d",
    "num_mean_30d",
    "num_max_7d",
    "onsets_7d",
    "onsets_30d",
    "onsets_90d",
    "onsets_365d",
    "days_since_onset",
    "days_since_recovery",
    "fault_hours_30d",
    "mean_episode_hours_365d",
    "obj_in_fault",
    "obj_onsets_7d",
    "month_sin",
    "month_cos",
    "weekday",
]
CATEGORICAL = ["sensor_type"]


def _window(cum: np.ndarray, idx: np.ndarray, days: int) -> np.ndarray:
    """Сумма по окну [D − days, D) по накопленной сумме, индексы — номера дней."""
    return cum[idx] - cum[np.maximum(idx - days, 0)]


def _capped(days: np.ndarray) -> np.ndarray:
    return np.where(days <= RECENCY_CAP, days, np.nan)


def build_daily(
    daily: pd.DataFrame,
    episodes: pd.DataFrame,
    channels: pd.DataFrame,
    dates,
    data_end=None,
    gaps=(),
    only=None,
) -> pd.DataFrame:
    """Строки (канал, сутки) с признаками, статусом и меткой.

    daily: channel_id, date, n_events, n_alarm, n_fault, n_num, mean, max.
    episodes: channel_id, start, end — наивное московское время, end = NaT у открытого эпизода.
    channels: channel_id, object_id, sensor_type — все каналы нужных типов на объектах: по ним считаются признаки
    объекта. only — каналы, для которых нужны строки (по умолчанию все из channels).
    """
    dates = pd.DatetimeIndex(pd.to_datetime(sorted(set(pd.to_datetime(list(dates)).normalize()))))
    t = dates.to_numpy().astype("datetime64[ns]")
    day0 = (dates.min() - pd.Timedelta(days=400)).normalize()
    n_days = int((dates.max() - day0) / pd.Timedelta(days=1)) + 2
    idx = ((dates - day0) / pd.Timedelta(days=1)).astype(int).to_numpy()
    data_end = np.datetime64(pd.Timestamp(data_end), "ns") if data_end is not None else None
    gaps = [(np.datetime64(pd.Timestamp(a), "ns"), np.datetime64(pd.Timestamp(b), "ns")) for a, b in gaps]

    daily = daily[daily["channel_id"].isin(channels["channel_id"])].copy()
    daily["d"] = ((pd.to_datetime(daily["date"]) - day0) / pd.Timedelta(days=1)).astype(int)
    daily = daily[(daily["d"] >= 0) & (daily["d"] < n_days)]
    eps = episodes[episodes["channel_id"].isin(channels["channel_id"])].copy()
    eps["start"] = pd.to_datetime(eps["start"]).astype("datetime64[ns]")
    eps["end"] = pd.to_datetime(eps["end"]).astype("datetime64[ns]")
    far = np.datetime64("2262-01-01", "ns")

    # По объекту: начала и концы эпизодов всех каналов — сколько каналов в неисправности на момент T
    obj_of = channels.set_index("channel_id")["object_id"]
    eps["object_id"] = eps["channel_id"].map(obj_of)
    obj_in_fault, obj_onsets = {}, {}
    for obj, g in eps.groupby("object_id"):
        starts = np.sort(g["start"].to_numpy())
        ends = np.sort(g["end"].fillna(pd.Timestamp(far)).to_numpy())
        obj_in_fault[obj] = np.searchsorted(starts, t, "right") - np.searchsorted(ends, t, "right")
        obj_onsets[obj] = np.searchsorted(starts, t, "left") - np.searchsorted(starts, t - 7 * DAY, "left")

    by_day = {c: g for c, g in daily.groupby("channel_id")}
    by_eps = {c: g.sort_values("start") for c, g in eps.groupby("channel_id")}
    zeros = np.zeros(len(t))
    frames = []
    rows_for = channels if only is None else channels[channels["channel_id"].isin(set(only))]
    for ch in rows_for.itertuples(index=False):
        cid = ch.channel_id
        grid = np.zeros((7, n_days))
        g = by_day.get(cid)
        if g is not None:
            d = g["d"].to_numpy()
            n_num = g["n_num"].to_numpy(dtype=float)
            grid[0, d] = g["n_events"].to_numpy()
            grid[1, d] = g["n_alarm"].to_numpy()
            grid[2, d] = g["n_fault"].to_numpy()
            grid[3, d] = n_num
            grid[4, d] = np.nan_to_num(g["mean"].to_numpy(dtype=float)) * n_num
            grid[5, d] = g["n_events"].to_numpy() > 0
            grid[6, d] = np.nan_to_num(g["max"].to_numpy(dtype=float), nan=-np.inf)
        cum = np.concatenate([np.zeros((6, 1)), np.cumsum(grid[:6], axis=1)], axis=1)
        f = {"channel_id": np.full(len(t), cid), "date": dates, "sensor_type": ch.sensor_type}
        for name, row, w in (
            ("events_1d", 0, 1),
            ("events_7d", 0, 7),
            ("events_30d", 0, 30),
            ("events_90d", 0, 90),
            ("days_active_30d", 5, 30),
            ("days_active_90d", 5, 90),
            ("alarms_7d", 1, 7),
            ("alarms_30d", 1, 30),
            ("alarms_90d", 1, 90),
            ("faults_msg_7d", 2, 7),
            ("faults_msg_30d", 2, 30),
            ("faults_msg_90d", 2, 90),
        ):
            f[name] = _window(cum[row], idx, w)
        for name, w in (("num_mean_7d", 7), ("num_mean_30d", 30)):
            n = _window(cum[3], idx, w)
            f[name] = np.where(n > 0, _window(cum[4], idx, w) / np.maximum(n, 1), np.nan)
        mx = np.array([grid[6, max(i - 7, 0) : i].max(initial=-np.inf) for i in idx])
        f["num_max_7d"] = np.where(np.isfinite(mx), mx, np.nan)

        e = by_eps.get(cid)
        starts = e["start"].to_numpy() if e is not None else np.array([], "datetime64[ns]")
        ends = e["end"].to_numpy() if e is not None else np.array([], "datetime64[ns]")
        for name, w in (("onsets_7d", 7), ("onsets_30d", 30), ("onsets_90d", 90), ("onsets_365d", 365)):
            f[name] = np.searchsorted(starts, t, "left") - np.searchsorted(starts, t - w * DAY, "left")
        k = np.searchsorted(starts, t, "left")  # эпизоды, начавшиеся до T
        last_start = np.where(k > 0, starts[np.maximum(k - 1, 0)] if len(starts) else t, np.datetime64("NaT", "ns"))
        f["days_since_onset"] = _capped(np.where(k > 0, (t - last_start) / DAY, np.nan))
        closed = ends[~np.isnat(ends)] if len(ends) else ends
        closed = np.sort(closed)
        kc = np.searchsorted(closed, t, "left")
        f["days_since_recovery"] = _capped(
            np.where(kc > 0, (t - closed[np.maximum(kc - 1, 0)]) / DAY if len(closed) else np.nan, np.nan)
        )
        in_fault = np.zeros(len(t), bool)
        fault_hours = np.zeros(len(t))
        mean_dur = np.full(len(t), np.nan)
        if len(starts):
            end_filled = np.where(np.isnat(ends), far, ends)
            # в неисправности — если не закончился хоть один эпизод, начатый до T, как бы давно он ни начался
            latest_end = np.maximum.accumulate(end_filled)
            in_fault = (k > 0) & (latest_end[np.maximum(k - 1, 0)] > t)
            for j in range(len(t)):
                lo = np.searchsorted(starts, t[j] - 400 * DAY, "left")
                s, en = starts[lo : k[j]], end_filled[lo : k[j]]
                a, b = np.maximum(s, t[j] - 30 * DAY), np.minimum(en, t[j])
                fault_hours[j] = np.clip((b - a) / HOUR, 0, None).sum()
                done = (en <= t[j]) & (s >= t[j] - 365 * DAY)
                if done.any():
                    mean_dur[j] = float(((en[done] - s[done]) / HOUR).mean())
        f["fault_hours_30d"], f["mean_episode_hours_365d"] = fault_hours, mean_dur

        obj = ch.object_id
        f["obj_in_fault"] = obj_in_fault.get(obj, zeros) - in_fault
        own_onsets_7d = f["onsets_7d"]
        f["obj_onsets_7d"] = obj_onsets.get(obj, zeros) - own_onsets_7d
        f["month_sin"], f["month_cos"] = np.sin(2 * np.pi * dates.month / 12), np.cos(2 * np.pi * dates.month / 12)
        f["weekday"] = dates.weekday

        # статус и метка
        status = np.where(in_fault, "in_fault", np.where(f["days_active_30d"] == 0, "no_coverage", "scored"))
        # первое начало неисправности не раньше T + 1 сут: начало в первые сутки не заслоняет следующее
        nxt = np.searchsorted(starts, t + HORIZON_START, "left")
        has_next = nxt < len(starts)
        first = starts[np.minimum(nxt, len(starts) - 1)] if len(starts) else t
        delta = np.where(has_next, first - t, np.timedelta64("NaT", "ns"))
        positive = has_next & (delta < HORIZON_END)
        observed = np.ones(len(t), bool)
        if data_end is not None:
            observed &= t + HORIZON_END <= data_end
        for a, b in gaps:
            observed &= ~((a < t + HORIZON_END) & (t < b))
        f["status"] = status
        f["target"] = np.where(observed, positive.astype(float), np.nan)
        f["lead_days"] = np.where(positive, delta / DAY, np.nan)
        frames.append(pd.DataFrame(f))
    if not frames:  # нет каналов нужных типов — например, запрошены только неподдерживаемые
        return pd.DataFrame(columns=["channel_id", "date", *FEATURES, "status", "target", "lead_days"])
    return pd.concat(frames, ignore_index=True)
