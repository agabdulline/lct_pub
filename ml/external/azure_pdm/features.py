"""Признаки агрегата Azure PdM для задачи «отказ в ближайшие 24 ч» (pdm_failure_24h). Синтетика.

Вход — журнал в формате data/external/<набор>/prepared (docs/architecture/external-data.md): events (channel_id, ts,
value — строка), channels (channel_id, object_id — агрегат, measure — имя сигнала: volt, rotate, pressure, vibration,
errors, maint, failures). Так же события лежат в представлении прогона, поэтому обучение и ml-service считают признаки
одним кодом.

Решение — по агрегату раз в час, в момент T на границе часа. Все окна — [T − w, T): телеметрия и счётчики берутся
по часовой сетке до часа T − 1 ч включительно; событие в момент T и позже не видно. «Часов с последнего ТО / отказа»
ограничены CAP: сервис читает редкие события за CAP, обучение считает так же — значения совпадают.
"""

import numpy as np
import pandas as pd

TELEMETRY = ["volt", "rotate", "pressure", "vibration"]
ERRORS = [f"error{k}" for k in range(1, 6)]
COMPONENTS = [f"comp{k}" for k in range(1, 5)]
HOUR = pd.Timedelta(hours=1)
CAP_HOURS = 90 * 24  # «давно» — 90 суток и больше
# окно истории по сигналу, минуты: сервис читает каждый сигнал за своё окно
HISTORY_BY_TAG = {
    **{t: 24 * 60 for t in TELEMETRY},
    "errors": 7 * 24 * 60,
    "maint": CAP_HOURS * 60,
    "failures": CAP_HOURS * 60,
}
MODELS = ["model1", "model2", "model3", "model4"]


def tags_for(names: list[str]) -> set[str]:
    """Сигналы, которые читают признаки names: сервис не читает из базы то, что модели не нужно."""
    out = set()
    for f in names:
        head = f.split("_")[0]
        if head in TELEMETRY:
            out.add(head)
        elif f.startswith("error"):
            out.add("errors")
        elif "maint" in f:
            out.add("maint")
        elif "failure" in f:
            out.add("failures")
    return out


READY = pd.Timedelta(hours=24)  # решению нужны сутки телеметрии; редкие сигналы читаются за CAP, но их может не быть
READY_GRACE = HOUR  # телеметрия раз в час: первое показание окна — в T − 24 ч или часом позже
UNITS = True  # коду нужен паспорт агрегата (модель, возраст) — objects.meta


def feature_names() -> list[str]:
    names = []
    for t in TELEMETRY:
        names += [f"{t}_last", f"{t}_mean_3h", f"{t}_std_3h", f"{t}_mean_24h", f"{t}_std_24h", f"{t}_min_24h",
                  f"{t}_max_24h", f"{t}_dev_24h", f"{t}_trend"]  # fmt: skip
    names += [f"{e}_24h" for e in ERRORS] + ["errors_72h", "errors_7d"]
    names += [f"h_since_maint_{c}" for c in COMPONENTS] + ["h_since_maint_any", "maint_90d"]
    names += ["h_since_failure", "failures_90d", "age_years", "model"]
    return names


FEATURES = feature_names()
CATEGORICAL = ["model"]


def unit_features(ev: pd.DataFrame, times: pd.DatetimeIndex) -> pd.DataFrame:
    """Признаки одного агрегата в моменты times (UTC, границы часов). ev: ts, measure, value."""
    at = times - HOUR  # последний час окна [T − w, T)
    start = min(ev["ts"].min().floor("h") if len(ev) else at.min(), at.min())
    grid = pd.date_range(start, at.max(), freq="h", tz="UTC")
    cols: dict[str, pd.Series] = {}
    tel = ev[ev["measure"].isin(TELEMETRY)]
    wide = tel.assign(value=pd.to_numeric(tel["value"], errors="coerce"), ts=tel["ts"].dt.floor("h"))
    wide = wide.pivot_table(index="ts", columns="measure", values="value", aggfunc="last")
    wide = wide.reindex(index=grid, columns=TELEMETRY)
    for t in TELEMETRY:
        x = wide[t]
        last = x.ffill(limit=2)
        m24 = x.rolling(24, min_periods=12).mean()
        cols[f"{t}_last"] = last
        cols[f"{t}_mean_3h"] = x.rolling(3, min_periods=2).mean()
        cols[f"{t}_std_3h"] = x.rolling(3, min_periods=2).std()
        cols[f"{t}_mean_24h"] = m24
        cols[f"{t}_std_24h"] = x.rolling(24, min_periods=12).std()
        cols[f"{t}_min_24h"] = x.rolling(24, min_periods=12).min()
        cols[f"{t}_max_24h"] = x.rolling(24, min_periods=12).max()
        cols[f"{t}_dev_24h"] = last - m24
        cols[f"{t}_trend"] = cols[f"{t}_mean_3h"] - m24
    err = _counts(ev[ev["measure"] == "errors"], grid, ERRORS)
    for e in ERRORS:
        cols[f"{e}_24h"] = err[e].rolling(24, min_periods=1).sum()
    total = err.sum(axis=1)
    cols["errors_72h"] = total.rolling(72, min_periods=1).sum()
    cols["errors_7d"] = total.rolling(7 * 24, min_periods=1).sum()
    maint = _counts(ev[ev["measure"] == "maint"], grid, COMPONENTS)
    for c in COMPONENTS:
        cols[f"h_since_maint_{c}"] = _hours_since(maint[c] > 0, grid)
    cols["h_since_maint_any"] = _hours_since(maint.sum(axis=1) > 0, grid)
    cols["maint_90d"] = maint.sum(axis=1).rolling(CAP_HOURS, min_periods=1).sum()
    fail = _counts(ev[ev["measure"] == "failures"], grid, COMPONENTS).sum(axis=1)
    cols["h_since_failure"] = _hours_since(fail > 0, grid)
    cols["failures_90d"] = fail.rolling(CAP_HOURS, min_periods=1).sum()
    f = pd.DataFrame(cols, index=grid)
    return f.reindex(at).set_axis(times)


def _counts(ev: pd.DataFrame, grid: pd.DatetimeIndex, kinds: list[str]) -> pd.DataFrame:
    """События по часам сетки: сколько событий каждого значения (код ошибки, компонент) пришлось на час."""
    if ev.empty:
        return pd.DataFrame(0.0, index=grid, columns=kinds)
    hour = ev["ts"].dt.floor("h")
    c = pd.crosstab(hour, ev["value"]).reindex(index=grid, columns=kinds, fill_value=0)
    return c.astype(float)


def _hours_since(hit: pd.Series, grid: pd.DatetimeIndex) -> pd.Series:
    """Часов от последнего часа с событием до часа сетки включительно + 1 — то есть до момента T; не больше CAP."""
    pos = np.arange(len(grid), dtype=float)
    last = pd.Series(np.where(hit.to_numpy(), pos, np.nan), index=grid).ffill()
    h = pos - last.to_numpy() + 1
    return pd.Series(np.minimum(np.nan_to_num(h, nan=CAP_HOURS), CAP_HOURS), index=grid)


def build(
    events: pd.DataFrame, channels: pd.DataFrame, decisions: pd.DataFrame, units: pd.DataFrame | None = None
) -> pd.DataFrame:
    """decisions: object_id (агрегат), decision_time (UTC). units: object_id, model, age_years — паспорт агрегата.

    Возвращает decisions + FEATURES в том же порядке строк.
    """
    ch = channels.set_index("channel_id")[["object_id", "measure"]]
    ev = events[events["channel_id"].isin(ch.index)].copy()
    ev["ts"] = pd.to_datetime(ev["ts"], utc=True)
    ev = ev.join(ch, on="channel_id")
    d = decisions.assign(decision_time=pd.to_datetime(decisions["decision_time"], utc=True))
    parts = []
    by_unit = dict(tuple(ev.groupby("object_id")))
    for unit, g in d.groupby("object_id", sort=False):
        times = pd.DatetimeIndex(g["decision_time"].unique()).sort_values()
        e = by_unit.get(unit, ev.iloc[:0])
        f = unit_features(e, times)
        f["object_id"] = unit
        f["decision_time"] = f.index
        parts.append(f.reset_index(drop=True))
    feats = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["object_id", "decision_time"])
    if units is None:
        units = pd.DataFrame(columns=["object_id", "model", "age_years"])
    meta = units.set_index("object_id")[["model", "age_years"]]
    feats = feats.join(meta, on="object_id")
    out = d.merge(feats, on=["object_id", "decision_time"], how="left")
    out["model"] = out["model"].fillna("unknown").astype(str)
    for c in FEATURES:
        if c not in out:
            out[c] = np.nan
    return out
