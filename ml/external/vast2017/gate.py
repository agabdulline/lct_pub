"""vast_gate — проезд без допуска и необычный проезд по VAST 2017 MC1 (docs/architecture/external-data.md §6).

Решение — на каждом проезде: событие датчика проезда, канал — датчик, значение — билет машины. Момент решения
T = время проезда + 1 с, окно [T − HISTORY, T) содержит сам проезд и прежние проезды того же билета — будущего
детектор не видит. Две части:

- **правило допуска** — объект датчика закрыт (у него есть строки допуска), а у билета допуска к нему нет. Это
  определение метки набора, а не качество модели;
- **нормы маршрутов** — выучены на истории до периода проверки по проездам без нарушений: редкость перехода
  «прошлый датчик → этот датчик» для класса машины и редкость времени на участке (слишком быстро или слишком
  долго). Оценка — сумма −log10 вероятностей; порог — квантиль QUANTILE оценок на истории обучения, без разметки.

Один код для обучения и ml-service (D1): `passes` строит признаки проезда из журнала, `score` — оценку.
"""

import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

START = "START"  # первый проезд билета — въезд в заповедник
ALPHA, BETA = 5.0, 1.0  # сглаживание: класс машины → все классы → равномерно по датчикам
MIN_SEGMENT = 20  # проездов участка, от которых есть норма времени
QUANTILE = 0.999  # порог: оценки выше — у 0.1% проездов истории обучения
HISTORY = pd.Timedelta(days=40)  # прошлый проезд билета ищется за 40 суток: самая долгая стоянка в наборе — 35 суток
DECISION_LAG = pd.Timedelta(seconds=1)  # T = время проезда + 1 с: сам проезд — внутри окна [T − w, T)


@dataclass
class Norms:
    """Нормы маршрутов: переходы по всем классам и по классу, времена участков (отсортированы, секунды)."""

    gates: list[str]
    trans: dict[str, dict[str, int]]
    trans_k: dict[str, dict[str, dict[str, int]]]
    times: dict[str, dict[str, list[float]]]
    threshold: float | None = None

    def dump(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def load(cls, text: str) -> "Norms":
        return cls(**json.loads(text))


def passes(events: pd.DataFrame, channels: pd.DataFrame, classes: dict[str, str]) -> pd.DataFrame:
    """Проезды с признаками. events: channel_id, ts (UTC), value — билет; channels: channel_id, object_id, measure —
    имя датчика (channels.tag). Прошлый проезд — по тому же билету среди переданных событий."""
    ch = channels.set_index("channel_id")
    p = events[events["channel_id"].isin(ch.index)].copy()
    p["ts"] = pd.to_datetime(p["ts"], utc=True)
    p["gate"] = p["channel_id"].map(ch["measure"])
    p["object_id"] = p["channel_id"].map(ch["object_id"])
    p["cls"] = p["value"].map(classes).fillna("?")
    order = ["ts", "event_id"] if "event_id" in p else ["ts"]
    p = p.sort_values(order, kind="stable")
    by = p.groupby("value", sort=False)
    p["prev_gate"] = by["gate"].shift().fillna(START)
    p["dt"] = (p["ts"] - by["ts"].shift()).dt.total_seconds()
    return p


def no_permit(p: pd.DataFrame, permits: pd.DataFrame) -> np.ndarray:
    """Проезд к закрытому объекту без допуска. Объект закрыт, если у него есть строки допуска (§3.3);
    допуск действует в [valid_from, valid_to), пустые границы — без ограничения."""
    restricted = set(permits["object_id"])
    windows: dict[tuple, list] = {}
    for c, o, lo, hi in permits[["credential_id", "object_id", "valid_from", "valid_to"]].itertuples(index=False):
        windows.setdefault((c, o), []).append((_utc(lo), _utc(hi)))
    out = np.zeros(len(p), dtype=bool)
    for i, (v, o, t) in enumerate(zip(p["value"], p["object_id"], p["ts"], strict=True)):
        if o not in restricted:
            continue
        allowed = any((lo is None or t >= lo) and (hi is None or t < hi) for lo, hi in windows.get((v, o), []))
        out[i] = not allowed
    return out


def _utc(x) -> pd.Timestamp | None:
    if x is None or pd.isna(x):
        return None
    t = pd.Timestamp(x)
    return t.tz_localize("Europe/Moscow").tz_convert("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def fit(p: pd.DataFrame) -> Norms:
    """Нормы по проездам p (без нарушений допуска)."""
    trans: dict = {}
    trans_k: dict = {}
    for (k, a, b), n in p.groupby(["cls", "prev_gate", "gate"]).size().items():
        trans.setdefault(a, {}).setdefault(b, 0)
        trans[a][b] += int(n)
        trans_k.setdefault(k, {}).setdefault(a, {})[b] = int(n)
    times: dict = {}
    moved = p[p["prev_gate"] != START]
    for (a, b), dt in moved.groupby(["prev_gate", "gate"])["dt"]:
        if len(dt) >= MIN_SEGMENT:
            times.setdefault(a, {})[b] = sorted(float(x) for x in dt)
    gates = sorted(set(p["gate"]) | set(p["prev_gate"]) - {START})
    return Norms(gates=gates, trans=trans, trans_k=trans_k, times=times)


def score(norms: Norms, p: pd.DataFrame) -> pd.DataFrame:
    """s_route — редкость перехода для класса, s_time — редкость времени на участке, score — сумма (−log10)."""
    g = len(norms.gates) or 1
    from_all = {a: sum(bs.values()) for a, bs in norms.trans.items()}
    from_k = {k: {a: sum(bs.values()) for a, bs in ab.items()} for k, ab in norms.trans_k.items()}
    times = {a: {b: np.asarray(x) for b, x in bs.items()} for a, bs in norms.times.items()}
    s_route, s_time, side = [], [], []
    for k, a, b, dt in zip(p["cls"], p["prev_gate"], p["gate"], p["dt"], strict=True):
        p_all = (norms.trans.get(a, {}).get(b, 0) + BETA / g) / (from_all.get(a, 0) + BETA)
        n_k = norms.trans_k.get(k, {}).get(a, {}).get(b, 0)
        p_k = (n_k + ALPHA * p_all) / (from_k.get(k, {}).get(a, 0) + ALPHA)
        s_route.append(-np.log10(p_k))
        x = times.get(a, {}).get(b)
        if x is None or dt != dt:
            s_time.append(0.0)
            side.append(None)
            continue
        lo = (np.searchsorted(x, dt, side="right") + 1) / (len(x) + 2)
        hi = (len(x) - np.searchsorted(x, dt, side="left") + 1) / (len(x) + 2)
        s_time.append(max(0.0, float(-np.log10(2 * min(lo, hi)))))
        side.append("fast" if lo < hi else "slow")
    out = pd.DataFrame({"s_route": s_route, "s_time": s_time, "time_side": side}, index=p.index)
    out["score"] = out["s_route"] + out["s_time"]
    return out


def factors(row: pd.Series, violation: bool) -> list[dict]:
    """Причины оценки проезда — для ответа сервиса и журнала: правило и вклад норм."""
    out = []
    if violation:
        out.append({"feature": "no_permit", "value": True, "contribution": None})
    route = f"{row['prev_gate']} → {row['gate']}"
    out.append({"feature": "route", "value": route, "contribution": round(float(row["s_route"]), 4)})
    if isinstance(row["time_side"], str):  # у участка без нормы времени — None (после reindex — NaN)
        dt = None if row["dt"] != row["dt"] else round(float(row["dt"]), 1)
        out.append({"feature": f"segment_time_{row['time_side']}", "value": dt,
                    "contribution": round(float(row["s_time"]), 4)})  # fmt: skip
    return out


def load_artifact(path) -> Norms:
    """Артефакт модели — нормы и порог (vast_gate.json); ml-service грузит его по config.artifact."""
    return Norms.load(path.read_text(encoding="utf-8"))


def decide(
    norms: Norms,
    targets: pd.DataFrame,
    history: pd.DataFrame,
    channels: pd.DataFrame,
    classes: dict[str, str],
    permits: pd.DataFrame,
) -> pd.DataFrame:
    """Решения по проездам targets (channel_id, ts, value) при прошлых проездах тех же билетов history — в сервисе.

    Строки — в порядке targets; у проезда вне датчиков набора — пустая строка. Сами targets из history убираются:
    к моменту вызова проезд уже может лежать в журнале прогона.
    """
    t = targets[["channel_id", "ts", "value"]].reset_index(drop=True).assign(_k=np.arange(len(targets)))
    t["ts"] = pd.to_datetime(t["ts"], utc=True)
    h = history[["channel_id", "ts", "value"]].assign(ts=lambda x: pd.to_datetime(x["ts"], utc=True))
    h = h.merge(t[["channel_id", "ts", "value"]], how="left", indicator=True)
    h = h[h["_merge"] == "left_only"].drop(columns="_merge").assign(_k=-1)
    p = passes(pd.concat([h, t], ignore_index=True), channels, classes)
    p = p[p["_k"] >= 0]
    p["violation"] = no_permit(p, permits)
    p = p.join(score(norms, p))
    p["alert"] = p["violation"] | (p["score"] >= norms.threshold)
    return p.set_index("_k").reindex(range(len(t)))
