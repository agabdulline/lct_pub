"""Признаки узла Magdeburg Industrial Hall для задачи «пожар / помеха / фон» (magdeburg_fire).

Вход — журнал событий в формате data/external/<набор>/prepared (docs/architecture/external-data.md): events
(channel_id, ts, value — строка) и channels (channel_id, object_id — узел, measure — исходная колонка). Так же события
лежат в рабочей базе и в представлении прогона, поэтому обучение и ml-service считают признаки одним кодом.

Решение — по узлу в момент T (раз в минуту). Все окна — [T − w, T): ряд узла сворачивается в 10-секундные корзины
от полуночи, признак в T берётся из корзины, которая кончается ровно в T. Нужна история не короче HISTORY (1 ч):
тогда признаки на окне сервиса совпадают с признаками на всём ряду.
"""

import numpy as np
import pandas as pd

MEASURES = [
    "CO2_Room",
    "CO_Room",
    "H2_Room",
    "VOC_Room_RAW",
    "PM05_Room",
    "PM10_Room",
    "PM25_Room",
    "PM40_Room",
    "PM100_Room",
    "PM_Total_Room",
    "PM_Room_Typical_Size",
    "Temperature_Room",
    "Humidity_Room",
]
ROOM = ["CO2_Room", "CO_Room", "H2_Room", "VOC_Room_RAW", "PM_Total_Room", "Temperature_Room"]  # сравнение с узлами
ROOM_V1 = ["Humidity_Room"]  # v1: горение сушит воздух, сварка и аэрозоли — нет
# v1 — «чем сопровождаются частицы»: пожар даёт вместе с дымом тепло, ЛОС, CO₂ и CO, а дым-машина, сварка и резка —
# частицы почти без них. Отношение — разность log1p отклонений от фона в характерных единицах сигнала (не подбирались
# по качеству: порядок величины отклонений при опытах)
SCALE = {"PM_Total_Room": 100.0, "VOC_Room_RAW": 1.0, "Temperature_Room": 0.5, "CO_Room": 1.0, "CO2_Room": 50.0,
         "H2_Room": 0.5, "Humidity_Room": 1.0}  # fmt: skip
WITH_PM = ["VOC_Room_RAW", "Temperature_Room", "CO_Room", "CO2_Room", "H2_Room"]
SIZES = ["PM05_Room", "PM10_Room", "PM25_Room"]  # доли размеров в сумме частиц
RATE = ["CO_Room", "CO2_Room", "H2_Room", "VOC_Room_RAW", "PM_Total_Room", "Temperature_Room", "Humidity_Room"]
BIN = pd.Timedelta(seconds=10)
WINDOWS = {"1m": 6, "5m": 30, "15m": 90}  # число корзин
HISTORY = pd.Timedelta(minutes=65)  # фон — до T − 60 мин, плюс запас на корзины
BASE_LO, BASE_HI = 360, 180  # фон — медиана корзин [T − 60 мин, T − 30 мин)


def feature_names() -> list[str]:
    own = []
    for m in MEASURES:
        own += [f"{m}_last", f"{m}_d_base", f"{m}_r_base", f"{m}_d_5m", f"{m}_d_15m"]
        own += [f"{m}_mean_{w}" for w in WINDOWS] + [f"{m}_max_{w}" for w in ("5m", "15m")]
    room = [f"room_{m}_{k}" for m in ROOM for k in ("max_d_base", "mean_d_base", "rank")]
    return own + room + ["nodes_seen"] + feature_names_v1()


def feature_names_v1() -> list[str]:
    """Признаки v1 — отделить помехи с частицами (сварка, дым-машина, резка) от пожара."""
    ratio = [f"lr_{short(m)}_pm" for m in WITH_PM]
    sizes = [f"{m}_share{k}" for m in SIZES for k in ("", "_d_base")]
    rate = [f"{m}_d_1m" for m in RATE] + [f"{m}_rise_5m" for m in RATE]
    room = [f"room_{m}_{k}" for m in ROOM_V1 for k in ("max_d_base", "mean_d_base", "rank")]
    room += [f"room_lr_{short(m)}_pm" for m in WITH_PM] + ["room_humidity_min_d_base"]
    return ratio + sizes + rate + room


def short(measure: str) -> str:
    return measure.removesuffix("_Room").removesuffix("_Room_RAW").lower()


def log_ratio(a: pd.Series, b: pd.Series, ma: str, mb: str) -> pd.Series:
    """log1p положительного отклонения a минус то же для b, в характерных единицах (SCALE)."""
    return np.log1p(a.clip(lower=0) / SCALE[ma]) - np.log1p(b.clip(lower=0) / SCALE[mb])


FEATURES = feature_names()


def node_series(events: pd.DataFrame, channels: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """Ряды узлов: индекс — время (UTC), колонки — показания MEASURES (числа)."""
    ch = channels.set_index("channel_id")[["object_id", "measure"]]
    ev = events[events["channel_id"].isin(ch.index)].copy()
    ev["value"] = pd.to_numeric(ev["value"], errors="coerce")
    ev = ev.join(ch, on="channel_id")
    ev["ts"] = pd.to_datetime(ev["ts"], utc=True)
    out = {}
    for node, g in ev.groupby("object_id"):
        wide = g.pivot_table(index="ts", columns="measure", values="value", aggfunc="last")
        out[int(node)] = wide.reindex(columns=MEASURES).sort_index()
    return out


def _bins(wide: pd.DataFrame) -> pd.DataFrame:
    # корзины от полуночи: границы одинаковы при любом начале ряда — на окне сервиса и на всём наборе
    return wide.resample(BIN, origin="start_day", label="left", closed="left").last()


def node_features(wide: pd.DataFrame, times: pd.DatetimeIndex) -> pd.DataFrame:
    """Признаки одного узла в моменты times (UTC): корзина [T − 10 с, T) и окна до неё."""
    b = _bins(wide)
    grid = pd.date_range(b.index.min(), times.max(), freq=BIN, tz="UTC")
    b = b.reindex(grid)
    last = b.ffill(limit=5)  # последнее показание не старше минуты
    base = b.rolling(BASE_LO - BASE_HI, min_periods=30).median().shift(BASE_HI)
    cols = {}
    for m in MEASURES:
        x, lx = b[m], last[m]
        cols[f"{m}_last"] = lx
        cols[f"{m}_d_base"] = lx - base[m]
        cols[f"{m}_r_base"] = (lx + 1) / (base[m].abs() + 1)
        cols[f"{m}_d_5m"] = lx - last[m].shift(WINDOWS["5m"])
        cols[f"{m}_d_15m"] = lx - last[m].shift(WINDOWS["15m"])
        for w, n in WINDOWS.items():
            cols[f"{m}_mean_{w}"] = x.rolling(n, min_periods=1).mean()
        for w in ("5m", "15m"):
            cols[f"{m}_max_{w}"] = x.rolling(WINDOWS[w], min_periods=1).max()
    # v1: отношения к частицам, доли размеров, скорость роста за минуту
    for m in WITH_PM:
        cols[f"lr_{short(m)}_pm"] = log_ratio(cols[f"{m}_d_base"], cols["PM_Total_Room_d_base"], m, "PM_Total_Room")
    total, base_total = last["PM_Total_Room"], base["PM_Total_Room"]
    for m in SIZES:
        share = last[m] / total.where(total > 0)
        cols[f"{m}_share"] = share
        cols[f"{m}_share_d_base"] = share - base[m] / base_total.where(base_total > 0)
    for m in RATE:
        d1 = last[m] - last[m].shift(WINDOWS["1m"])
        cols[f"{m}_d_1m"] = d1
        cols[f"{m}_rise_5m"] = d1.rolling(WINDOWS["5m"], min_periods=1).max()
    f = pd.DataFrame(cols, index=grid)
    at = times - BIN  # корзина, которая кончается в T
    return f.reindex(at).set_axis(times)


def build(events: pd.DataFrame, channels: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    """decisions: object_id (узел), decision_time (UTC). Возвращает decisions + FEATURES в том же порядке строк."""
    series = node_series(events, channels)
    times = pd.DatetimeIndex(pd.to_datetime(decisions["decision_time"], utc=True).unique()).sort_values()
    per_node = {n: node_features(w, times) for n, w in series.items()}
    # сравнение с остальными узлами цеха: насколько отклонение этого узла от фона выше, чем у других
    room = {}
    for m in ROOM + ROOM_V1:
        d = pd.DataFrame({n: f[f"{m}_d_base"] for n, f in per_node.items()})
        room[m] = d
    peak = {m: room[m].max(axis=1) for m in [*WITH_PM, "PM_Total_Room"]}  # по всему цеху, с этим узлом
    room_ratio = {m: log_ratio(peak[m], peak["PM_Total_Room"], m, "PM_Total_Room") for m in WITH_PM}
    humidity_min = room["Humidity_Room"].min(axis=1)
    rows = []
    for n, f in per_node.items():
        g = f.copy()
        for m in ROOM + ROOM_V1:
            others = room[m].drop(columns=[n])
            g[f"room_{m}_max_d_base"] = others.max(axis=1)
            g[f"room_{m}_mean_d_base"] = others.mean(axis=1)
            g[f"room_{m}_rank"] = room[m].rank(axis=1, pct=True)[n]
        for m in WITH_PM:
            g[f"room_lr_{short(m)}_pm"] = room_ratio[m]
        g["room_humidity_min_d_base"] = humidity_min
        g["nodes_seen"] = room["PM_Total_Room"].notna().sum(axis=1).astype(float)
        g["object_id"] = n
        g["decision_time"] = g.index
        rows.append(g.reset_index(drop=True))
    feats = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["object_id", "decision_time"])
    out = decisions.assign(decision_time=pd.to_datetime(decisions["decision_time"], utc=True)).merge(
        feats, on=["object_id", "decision_time"], how="left"
    )
    for c in FEATURES:
        if c not in out:
            out[c] = np.nan
    return out
