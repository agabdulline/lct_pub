"""Признаки модели «свой или нужна реакция» при открытии под охраной — один код для обучения и для ml-service.

Решение — в момент T первого открытия кандидата правила (`ml.access.dataset`). Всё, кроме самого открытия, — строго
из прошлого, окна [T − w, T): переходы охраны подобъекта, открытия его дверей и люков, движение, прошлые кандидаты
правила с уже известным исходом (старт не позже T − 5 мин: снятие в [старт, старт + 5 мин] к T уже видно).
Движения в признаках нет: в рабочей базе оно хранится сырым потоком только 90 суток, а без него модель не хуже
(отчёт, «Признаки»).

Само открытие — канал, люк ли, флаг тревоги охранной системы — известно в T, как значение дымовой тревоги у событийной
модели. Состояние охраны — с какого момента подобъект на охране, исправна ли охрана — даёт движок правила.

Входы — кадры в раскладке `ml.access.dataset`:
- points / past — кандидаты: sub_object_id, start_ts, channel_id, label (1 — инцидент, 0 — сняли за 5 мин);
- guard — события канала охраны: sub_object_id, ts, value;
- openers — события дверей и люков: sub_object_id, channel_id, ts, value.
Время — с поясом, UTC; часы и дни — по Москве.
"""

import numpy as np
import pandas as pd

MSK = "Europe/Moscow"
ARMED, DISARMED = "На охране", "Снято с охраны"
OPENED = "Не замкнут"
GRACE = pd.Timedelta(minutes=5)  # исход кандидата известен через 5 мин после старта
NIGHT = (22, 6)
WEEKEND = (5, 6)  # pandas: 0 — понедельник

FEATURES = [
    "hour",
    "weekday",
    "off_hours",
    "hatch",
    "is_alarm",
    "guard_faulty",
    "armed_log_min",
    "disarms_24h",
    "disarms_7d",
    "since_disarm_log_min",
    "disarm_same_hour_28d",
    "opens_same_hour_28d",
    "door_opens_30d",
    "door_candidates_365d",
    "door_dismissed_share_365d",
    "sub_candidates_30d",
    "sub_dismissed_share_30d",
    "sub_candidates_365d",
    "sub_dismissed_share_365d",
    "other_opens_10m",
]
CATEGORICAL: list[str] = []
# Сколько истории нужно каждому кадру до T: старше ни один признак не смотрит (сервис читает базу этими окнами)
LOOKBACK = {
    "past": pd.Timedelta(days=365),
    "guard": pd.Timedelta(days=365),
    "openers": pd.Timedelta(days=30),
}
PRIOR = 2.0  # сглаживание долей: (снято + 0.5·PRIOR) / (кандидатов + PRIOR)


def _times(frame: pd.DataFrame, key: str) -> dict:
    return {k: g.sort_values("ts").ts.to_numpy("datetime64[ns]") for k, g in frame.groupby(key)}


def _count(ts: np.ndarray, lo, hi) -> int:
    return int(np.searchsorted(ts, hi, "left") - np.searchsorted(ts, lo, "left"))


class History:
    """События прошлого, разложенные по подобъекту и каналу, — для расчёта признаков в любой точке."""

    def __init__(self, past: pd.DataFrame, guard: pd.DataFrame, openers: pd.DataFrame):
        ns = "datetime64[ns]"
        d = guard[guard.value.eq(DISARMED)]
        self.disarm = _times(d, "sub_object_id")
        self.disarm_hour = {k: pd.DatetimeIndex(v).tz_localize("UTC").tz_convert(MSK).hour.to_numpy()
                            for k, v in self.disarm.items()}  # fmt: skip
        # одинаковые события канала в одну секунду — одно, как в живом потоке
        o = openers[openers.value.eq(OPENED)].drop_duplicates(["channel_id", "ts"])
        self.opens_sub = {k: (g.ts.to_numpy(ns), g.channel_id.to_numpy())
                          for k, g in o.sort_values("ts").groupby("sub_object_id")}  # fmt: skip
        self.opens_ch = _times(o, "channel_id")
        p = past[past.label.notna()].sort_values("start_ts")
        self.cand_sub = {k: (g.start_ts.to_numpy(ns), g.label.to_numpy(float)) for k, g in p.groupby("sub_object_id")}
        self.cand_ch = {k: (g.start_ts.to_numpy(ns), g.label.to_numpy(float)) for k, g in p.groupby("channel_id")}

    @staticmethod
    def _cand(store: dict, key, t: np.datetime64, days: int) -> tuple[int, float]:
        """Кандидаты с известным исходом за `days` суток: сколько и сглаженная доля «сняли за 5 мин»."""
        ts, y = store.get(key, (np.array([], "datetime64[ns]"), np.array([])))
        hi = np.searchsorted(ts, t - GRACE.to_timedelta64(), "right")
        lo = np.searchsorted(ts, t - np.timedelta64(days, "D"), "left")
        n = max(hi - lo, 0)
        dismissed = float((1 - y[lo:hi]).sum()) if n else 0.0
        return n, (dismissed + 0.5 * PRIOR) / (n + PRIOR)


def point_features(h: History, p) -> dict:
    """Признаки одной точки решения: p — sub_object_id, channel_id, start_ts, is_alarm, hatch, armed_since,
    guard_faulty."""
    T = pd.Timestamp(p.start_ts)
    t = np.datetime64(T.tz_convert("UTC").tz_localize(None), "ns")
    local = T.tz_convert(MSK)
    day = np.timedelta64(1, "D")
    sub, ch = p.sub_object_id, p.channel_id
    f = {
        "hour": local.hour,
        "weekday": local.weekday(),
        "off_hours": int(local.weekday() in WEEKEND or local.hour >= NIGHT[0] or local.hour < NIGHT[1]),
        "hatch": int(bool(p.hatch)),
        "is_alarm": int(bool(p.is_alarm)),
        "guard_faulty": int(bool(p.guard_faulty)),
    }
    armed = pd.Timestamp(p.armed_since) if p.armed_since is not None and not pd.isna(p.armed_since) else None
    f["armed_log_min"] = float(np.log1p((T - armed).total_seconds() / 60)) if armed is not None else np.nan
    dis = h.disarm.get(sub, np.array([], "datetime64[ns]"))
    f["disarms_24h"] = _count(dis, t - day, t)
    f["disarms_7d"] = _count(dis, t - 7 * day, t)
    k = np.searchsorted(dis, t, "left")
    since = (t - dis[k - 1]) / np.timedelta64(1, "m") if k else np.inf
    within = since <= LOOKBACK["guard"] / pd.Timedelta(minutes=1)
    f["since_disarm_log_min"] = float(np.log1p(since)) if within else np.nan
    # обычный график подобъекта: снятия и открытия в этот час суток (±1 ч) за 4 недели, в среднем за сутки
    hours = {(local.hour + d) % 24 for d in (-1, 0, 1)}
    lo28 = np.searchsorted(dis, t - 28 * day, "left")
    f["disarm_same_hour_28d"] = float(np.isin(h.disarm_hour.get(sub, np.array([]))[lo28:k], list(hours)).sum()) / 28
    ots, och = h.opens_sub.get(sub, (np.array([], "datetime64[ns]"), np.array([])))
    a, b = np.searchsorted(ots, t - 28 * day, "left"), np.searchsorted(ots, t, "left")
    oh = pd.DatetimeIndex(ots[a:b]).tz_localize("UTC").tz_convert(MSK).hour.to_numpy() if b > a else np.array([])
    f["opens_same_hour_28d"] = float(np.isin(oh, list(hours)).sum()) / 28
    f["door_opens_30d"] = _count(h.opens_ch.get(ch, np.array([], "datetime64[ns]")), t - 30 * day, t)
    f["door_candidates_365d"], f["door_dismissed_share_365d"] = History._cand(h.cand_ch, ch, t, 365)
    f["sub_candidates_30d"], f["sub_dismissed_share_30d"] = History._cand(h.cand_sub, sub, t, 30)
    f["sub_candidates_365d"], f["sub_dismissed_share_365d"] = History._cand(h.cand_sub, sub, t, 365)
    a = np.searchsorted(ots, t - np.timedelta64(10, "m"), "left")
    f["other_opens_10m"] = int((och[a:b] != ch).sum())
    return f


def build(points: pd.DataFrame, h: History) -> pd.DataFrame:
    """Признаки всех точек; порядок столбцов — FEATURES."""
    rows = [point_features(h, p) for p in points.itertuples(index=False)]
    return pd.DataFrame(rows, columns=FEATURES, index=points.index)
