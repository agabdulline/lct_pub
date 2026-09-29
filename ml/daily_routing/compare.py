"""Ежедневные модели по типам на одних строках и таблица маршрутизации: `ml/reports/daily-routing/`.

Часть 2 задачи B (docs/PLAN.md 15.9). Модели: наша ежедневная v0 и модели ML-разработчика — ежедневная v3 (8 типов),
ансамбль ИБП и модель ручного извещателя. Решения его моделей — эталон `ml.daily_routing.reference` (его код на наших
данных), решения v0 — код её обучения по рабочей базе (`ml.baseline.train_daily`), каждая модель — на своём рабочем
пороге, ничего не подбирается на проверке.

- Проверка: T — каждые сутки с 1.01 по 2.05.2026: горизонт 30 суток закрывается до дня без журнала 1.06.
- Метка — продуктовая, как у журнала прогнозов (`backend/app/domain/outcomes.py`) и обучения v0: в
  [T + 24 ч, T + 30 сут) у канала начинается эпизод «Неисправен» рабочей базы. Метки его моделей другие (у v3 —
  первое следующее начало, у отдельных моделей — эпизоды его сборщика); метрики по ним — справкой, в `own_label`.
- Общие строки типа — (канал, T), где решение выносят обе модели и метка известна. На них — правило маршрутизации.
- Правило (решение владельца): модель ML-разработчика, если на общих строках P > 0.7 и R > 0.5 и она не хуже v0 —
  v0 не проходит пороги или у его модели P и R не ниже; иначе — v0. Типы, которые v0 не оценивает, — его модель, если
  проходит пороги на своих решениях, иначе у типа ежедневного прогноза нет. Насос и вентилятор — за v0 (его совет:
  `ml/models/each-channel/README.md`), фаза — v0: другой модели нет.
- Интервалы — 90% кластерного бутстрапа по каналам (соседние сутки одного канала почти одинаковы), парного: обе модели
  и разность — на одних выборках.
- Упреждение — сутки от T до начала неисправности у верных тревог. Эпизоды — начала неисправности, на которые указывает
  хоть одна строка с меткой 1: «найден», если хоть одна из этих строк — тревога.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.daily_routing.compare`. Нужны рабочая база с базовой
историей (читаются channels, channel_daily, fault_episodes — как в train_daily) и эталон.
"""

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from ml.baseline import train_daily as v0train
from ml.baseline.daily import TYPES as V0_TYPES
from ml.baseline.daily import build_daily
from ml.daily_routing.reference import EACH_MODELS, ROOT, TEST, V3_MODEL
from ml.daily_routing.reference import OUT as REF_OUT
from ml.v3.src.baseline_core import SENSOR_FAMILIES

REPORT = ROOT / "ml" / "reports" / "daily-routing"
V0_DIR = ROOT / "ml" / "models" / "baseline-v0-daily"
REQ_P, REQ_R = 0.7, 0.5
BOOT, SEED = 1000, 42
V3_TYPES = list(SENSOR_FAMILIES)
# Отдельные модели типов, доставленные для сервиса (#243); насос и вентилятор он сам оставляет за v0
EACH = {"ИБП": "ups", "Ручной извещатель": "manual_alarm"}
KEEP_V0 = {
    "Состояние насоса": "его совет — оставить v0: R 95.7% против 83.0% у его модели",
    "Состояние вентилятора": "его совет — оставить v0: R 93.1% против 78.2% у его модели",
}


# ---------- решения


def load(db: str, types: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Каналы, суточные итоги и эпизоды «Неисправен» базовой истории — как `ml.baseline.train_daily.load`."""
    since = pd.Timestamp(TEST[0]) - pd.Timedelta(days=v0train.HISTORY_DAYS + 1)
    with create_engine(db).connect() as c:
        ch = pd.read_sql(
            text("SELECT id AS channel_id, object_id, sensor_type FROM channels WHERE sensor_type = ANY(:t)"),
            c,
            params={"t": types},
        )
        ids = ch.channel_id.tolist()
        daily = pd.read_sql(
            text(
                "SELECT channel_id, date, n_events, n_alarm, n_fault, n_num, mean, max FROM channel_daily "
                "WHERE run_id IS NULL AND channel_id = ANY(:ids) AND date >= :since"
            ),
            c,
            params={"ids": ids, "since": since.date()},
        )
        eps = pd.read_sql(
            text(
                "SELECT channel_id, start_ts, end_ts FROM fault_episodes WHERE run_id IS NULL "
                "AND state = 'Неисправен' AND channel_id = ANY(:ids)"
            ),
            c,
            params={"ids": ids},
        )
    for col in ("start_ts", "end_ts"):
        eps[col] = pd.to_datetime(eps[col], utc=True).dt.tz_convert(v0train.TZ).dt.tz_localize(None)
    return ch, daily, eps.rename(columns={"start_ts": "start", "end_ts": "end"})


def labelled(db: str) -> tuple[pd.DataFrame, set]:
    """Все (канал, T) проверки у типов v0, v3 и отдельных моделей: продуктовая метка, упреждение и решение v0.

    v0 — отдельным вызовом по её восьми типам: признаки объекта (`obj_*`) у неё считаются по каналам этих типов, как
    при обучении. Метки остальных типов — тем же кодом по их каналам. Ещё возвращает каналы с началами «Неисправен»
    до 2026 года — «с историей» (AGENTS.md §5)."""
    from catboost import CatBoostClassifier

    cfg = json.loads((V0_DIR / "config.json").read_text(encoding="utf-8"))
    model = CatBoostClassifier().load_model(str(V0_DIR / "daily.cbm"))
    dates = pd.date_range(*TEST)
    ch, daily, eps = load(db, V0_TYPES)
    v0 = build_daily(daily, eps, ch, dates, data_end=v0train.DATA_END, gaps=v0train.GAPS)
    scored = v0.status.eq("scored")
    v0["score_v0"] = np.nan
    v0.loc[scored, "score_v0"] = model.predict_proba(v0.loc[scored, cfg["features"]])[:, 1]
    v0["alert_v0"] = v0.score_v0.ge(cfg["threshold"])
    history = set(eps.loc[eps.start < TEST[0], "channel_id"])
    rest = [t for t in dict.fromkeys(V3_TYPES + list(EACH)) if t not in V0_TYPES]
    ch2, daily2, eps2 = load(db, rest)
    other = build_daily(daily2, eps2, ch2, dates, data_end=v0train.DATA_END, gaps=v0train.GAPS)
    history |= set(eps2.loc[eps2.start < TEST[0], "channel_id"])
    keep = ["channel_id", "date", "sensor_type", "status", "target", "lead_days"]
    rows = pd.concat([v0[[*keep, "score_v0", "alert_v0"]], other[keep]], ignore_index=True)
    rows["v0_decided"] = rows.score_v0.notna()
    rows = rows.rename(columns={"status": "status_v0"})
    return rows, history


def his(rows: pd.DataFrame) -> pd.DataFrame:
    """Решения моделей ML-разработчика из эталона: какая модель, решение вынесено ли, оценка, тревога."""
    v3 = v3_decisions().assign(model="v3", decided=lambda x: x.eligible)
    v3["own_status"] = v3.status
    parts = [v3[["channel_id", "date", "model", "decided", "own_status", "score", "alert"]]]
    for name in EACH.values():
        f = pd.read_parquet(REF_OUT / f"{name}.parquet")
        f = f.loc[f.date.between(*TEST)].assign(model=name, decided=lambda x: x.status.eq("scored"))
        f = f.rename(columns={"status": "own_status", "target": "own_target", "lead_days": "own_lead_days"})
        parts.append(f[["channel_id", "date", "model", "decided", "own_status", "score", "alert", "own_target"]])
    h = pd.concat(parts, ignore_index=True)
    h["alert"] = h.alert.fillna(False).astype(bool) & h.decided
    return rows.merge(h, on=["channel_id", "date"], how="left", validate="one_to_one")


def v3_decisions() -> pd.DataFrame:
    cols = ["sensor_id", "date", "status", "eligible", "score", "alert"]
    files = sorted((REF_OUT / "v3").glob("*.parquet"))
    v3 = pd.concat([pd.read_parquet(p, columns=cols) for p in files], ignore_index=True)
    return v3.rename(columns={"sensor_id": "channel_id"})


def reproduction() -> dict:
    """Эталон повторяет его отчёты: v3 — `ml/v3/artifacts/reports/daily_evaluation.json` (тест 2026, его метка),
    ИБП и извещатель — метрики проверки из их `config.json` (`reference/*_check.json`)."""
    v3 = v3_decisions()
    v3 = v3[v3.eligible].reset_index(drop=True)
    v3["target"] = v3_own_label(v3)
    lab = v3[v3.target.notna()]
    y, a = lab.target.eq(1).to_numpy(), lab.alert.to_numpy(dtype=bool)
    got = {"rows": len(lab), "positives": int(y.sum()), "alerts": int(a.sum()), "tp": int((a & y).sum())}
    report = json.loads((V3_MODEL.parent / "reports" / "daily_evaluation.json").read_text(encoding="utf-8"))["test"]
    expected = {k: report[k] for k in got}
    out = {"v3": {"got": got, "expected": expected, "reproduced": got == expected}}
    for name in EACH.values():
        check = json.loads((REF_OUT / f"{name}_check.json").read_text(encoding="utf-8"))
        out[name] = {k: check[k] for k in ("reproduced", "metrics_protocol", "expected")}
    return out


def v3_own_label(frame: pd.DataFrame) -> pd.Series:
    """Метка ежедневной v3 по её определению (`ml.v3.src.features.make_channel`): первое следующее наблюдаемое начало
    «Неисправен» его сборщика — в [T + 1 сут, T + 30 сут), все 30 суток с T — дни журнала, горизонт до 30.06.2026."""
    from ml.daily_routing.reference import BUNDLE

    eps = pd.read_parquet(BUNDLE / "fault_episodes.parquet")
    starts_of = {
        cid: g.to_numpy("datetime64[ns]")
        for cid, g in eps.loc[eps.start_is_observed].sort_values("start").groupby("sensor_id").start
    }
    known = pd.DatetimeIndex(pd.read_csv(BUNDLE / "global_coverage.csv", parse_dates=["date"]).date)
    days = pd.DatetimeIndex(sorted(frame.date.unique()))
    covered = pd.Series([pd.date_range(t, periods=30).isin(known).all() for t in days], index=days)
    day, h = np.timedelta64(1, "D"), np.timedelta64(30, "D")
    y = pd.Series(np.nan, index=frame.index)
    for cid, idx in frame.groupby("channel_id").groups.items():
        t = frame.loc[idx, "date"].to_numpy("datetime64[ns]")
        starts = starts_of.get(cid, np.array([], "datetime64[ns]"))
        k = np.searchsorted(starts, t, "left")
        nxt = starts[np.minimum(k, len(starts) - 1)] if len(starts) else t
        pos = (k < len(starts)) & (nxt >= t + day) & (nxt < t + h)
        ok = covered.reindex(frame.loc[idx, "date"]).to_numpy() & (t + h <= np.datetime64("2026-06-30"))
        y.loc[idx] = np.where(ok, pos.astype(float), np.nan)
    return y


# ---------- метрики


def point(d: pd.DataFrame, alert: str) -> dict:
    y = d.target.to_numpy() == 1
    a = d[alert].to_numpy(dtype=bool)
    tp, fp, fn = int((a & y).sum()), int((a & ~y).sum()), int((~a & y).sum())
    lead = d.lead_days.to_numpy(dtype=float)[a & y]
    onset = d.date + pd.to_timedelta(d.lead_days, unit="D")
    ep = pd.DataFrame({"c": d.channel_id, "o": onset.dt.round("s"), "a": a})[y]
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return {
        "decisions": len(d),
        "positives": int(y.sum()),
        "alerts": int(a.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": p,
        "recall": r,
        "passes": p is not None and r is not None and p > REQ_P and r > REQ_R,
        "lead_days": {k: float(np.quantile(lead, q)) for k, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))}
        if len(lead)
        else None,
        "channels": int(d.channel_id.nunique()),
        "positive_channels": int(d.loc[y, "channel_id"].nunique()),
        "caught_channels": int(d.loc[a & y, "channel_id"].nunique()),
        "episodes": int(ep[["c", "o"]].drop_duplicates().shape[0]),
        "caught_episodes": int(ep.loc[ep.a, ["c", "o"]].drop_duplicates().shape[0]),
    }


def boot(d: pd.DataFrame, alerts: list[str]) -> dict:
    """90% интервалы P и R каждой тревоги и разностей с первой — парный бутстрап по каналам."""
    y = d.target.to_numpy() == 1
    a = {k: d[k].to_numpy(dtype=bool) for k in alerts}
    groups = d.groupby("channel_id").indices
    keys = list(groups)
    rng = np.random.default_rng(SEED)
    res = {k: {"P": [], "R": []} for k in alerts}
    for _ in range(BOOT):
        idx = np.concatenate([groups[keys[i]] for i in rng.integers(0, len(keys), len(keys))])
        yb = y[idx]
        for k in alerts:
            ab = a[k][idx]
            tp = (ab & yb).sum()
            res[k]["P"].append(tp / ab.sum() if ab.sum() else np.nan)
            res[k]["R"].append(tp / yb.sum() if yb.sum() else np.nan)
    q = [0.05, 0.95]
    out = {k: {m: [float(x) for x in np.nanquantile(v[m], q)] for m in ("P", "R")} for k, v in res.items()}
    base = alerts[0]
    for k in alerts[1:]:
        out[f"{k}_minus_{base}"] = {
            m: [float(x) for x in np.nanquantile(np.array(res[k][m]) - np.array(res[base][m]), q)] for m in ("P", "R")
        }
    return out


def split_history(d: pd.DataFrame, alert: str, history: set) -> dict:
    seen = d.channel_id.isin(history)
    return {"with_history": point(d[seen], alert), "without_history": point(d[~seen], alert)}


def decide(sensor_type: str, v0: dict | None, mine: dict | None, model: str | None) -> tuple[str | None, str]:
    """Правило маршрутизации типа: чья модель и почему."""
    if sensor_type in KEEP_V0:
        return "v0", KEEP_V0[sensor_type]
    if model is None:
        return ("v0", "другой модели нет") if v0 is not None else (None, "моделей нет")
    if v0 is None:
        if mine["passes"]:
            return model, "v0 тип не оценивает; его модель проходит P > 0.7 и R > 0.5"
        return None, "v0 тип не оценивает; его модель не проходит пороги — ежедневного прогноза у типа нет"
    if not mine["passes"]:
        return "v0", "его модель не проходит P > 0.7 и R > 0.5 на общих строках"
    if not v0["passes"]:
        return model, "его модель проходит пороги на общих строках, v0 — нет"
    if mine["precision"] >= v0["precision"] and mine["recall"] >= v0["recall"]:
        return model, "обе проходят пороги; у его модели P и R не ниже"
    return "v0", "обе проходят пороги; у его модели P или R ниже, чем у v0"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "postgresql+psycopg://lct:lct@localhost:15432/lct"))
    ap.add_argument("--work", type=Path, default=ROOT / "data" / "interim" / "daily-routing")
    a = ap.parse_args()
    cache = a.work / "labelled.parquet"
    if cache.exists():
        rows, history = pd.read_parquet(cache), set(json.loads((a.work / "history.json").read_text()))
    else:
        rows, history = labelled(a.db)
        a.work.mkdir(parents=True, exist_ok=True)
        rows.to_parquet(cache, index=False)
        (a.work / "history.json").write_text(json.dumps(sorted(int(x) for x in history)))
    d = his(rows)
    d["decided"] = d.decided.fillna(False).astype(bool)
    d["alert"] = d.alert.fillna(False).astype(bool)
    known = d.target.notna()
    v3_rows = d.model.eq("v3")
    d.loc[v3_rows, "own_target"] = v3_own_label(d.loc[v3_rows & d.decided])
    types = list(dict.fromkeys(V0_TYPES + V3_TYPES + list(EACH)))
    result = {
        "test": {"from": TEST[0], "to": TEST[1], "label": "I полугодие 2026: T с 1.01 по 2.05, горизонт до 1.06"},
        "thresholds": {
            "v0": json.loads((V0_DIR / "config.json").read_text(encoding="utf-8"))["threshold"],
            "v3": json.loads((V3_MODEL / "model_config.json").read_text(encoding="utf-8"))["threshold"],
            "ups": json.loads((EACH_MODELS / "ups" / "config.json").read_text(encoding="utf-8"))["threshold"],
            "manual_alarm": "помесячный причинный (окно 90 суток, готовность 1 500), см. reference/manual_alarm_check",
        },
        "requirement": {"precision": REQ_P, "recall": REQ_R},
        "types": {},
    }
    for t in types:
        g = d[d.sensor_type.eq(t) & known]
        model = g.model.dropna().iloc[0] if g.model.notna().any() else None
        has_v0 = t in V0_TYPES
        entry = {"model": model, "v0": has_v0}
        common = g[g.v0_decided & g.decided] if has_v0 and model else None
        if common is not None:
            entry["common"] = {"v0": point(common, "alert_v0"), model: point(common, "alert")}
            entry["common_ci90"] = boot(common, ["alert_v0", "alert"])
            entry["common_by_history"] = {
                "v0": split_history(common, "alert_v0", history),
                model: split_history(common, "alert", history),
            }
        own = {}
        if has_v0:
            own["v0"] = point(g[g.v0_decided], "alert_v0")
        if model:
            own[model] = point(g[g.decided], "alert")
            lab = g[g.decided & g.own_target.notna()].assign(target=lambda x: x.own_target)
            entry["own_label"] = point(lab, "alert") if len(lab) else None
        entry["own_decisions"] = own
        v0m = entry["common"]["v0"] if common is not None else own.get("v0")
        mine = entry["common"][model] if common is not None else own.get(model)
        entry["route"], entry["why"] = decide(t, v0m, mine, model)
        result["types"][t] = entry
        print(t, entry["route"], "|", entry["why"], flush=True)
    result["coverage"] = coverage(d[known], result["types"])
    result["reproduction"] = reproduction()
    print(json.dumps(result["reproduction"], ensure_ascii=False, default=str))
    ROUTING.write_text(json.dumps(routing_config(result), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "compare.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result["coverage"], ensure_ascii=False, indent=1))


ROUTING = ROOT / "ml" / "models" / "daily_routing.json"
ROUTING_VERSION = "2026-09-29"
MODEL_IDS = {"v0": "baseline-v0", "ups": "ups_ensemble", "manual_alarm": "manual_alarm"}  # id в ответе сервиса
ADAPTED = {
    "baseline-v0": {"config": "baseline-v0-daily/config.json"},
    "ups_ensemble": {"config": "each-channel/ups/config.json", "adapter": "ensemble"},
    "manual_alarm": {"config": "each-channel/manual-alarm/config.json", "adapter": "manual_alarm"},
}
KEYS = ("decisions", "positives", "alerts", "tp", "fp", "precision", "recall", "lead_days")
KEYS += ("episodes", "caught_episodes")


def routing_config(result: dict) -> dict:
    """Таблица маршрутизации для ml-service (`ml/models/daily_routing.json`): тип → модель, почему, её метрики
    на строках решения (общих с v0, если v0 тип оценивает), итог против одной v0."""
    routes, unrouted = [], []
    for t, e in result["types"].items():
        if e["route"] is None:
            unrouted.append({"sensor_type": t, "why": e["why"]})
            continue
        m = e["common"][e["route"]] if "common" in e else e["own_decisions"][e["route"]]
        routes.append(
            {
                "sensor_type": t,
                "model": MODEL_IDS[e["route"]],
                "why": e["why"],
                "offline": {k: m[k] for k in KEYS},
            }
        )
    cov = result["coverage"]
    return {
        "version": ROUTING_VERSION,
        "rule": "модель ML-разработчика, если на общих с v0 строках P > 0.7, R > 0.5 и не хуже v0; иначе v0 (A21)",
        "report": "ml/reports/daily-routing/REPORT.md",
        "test": result["test"],
        "models": ADAPTED,
        "routes": routes,
        "unrouted": unrouted,
        "offline": {k: {x: cov[k][x] for x in KEYS} for k in ("routing", "v0_only")},
    }


def coverage(d: pd.DataFrame, types: dict) -> dict:
    """Итоговая маршрутизация против одной v0: сколько каналов с решением, отказов в поле зрения и найденных."""
    route = {t: e["route"] for t, e in types.items()}
    alert = np.where(d.sensor_type.map(route).eq("v0"), d.alert_v0, d.alert)
    decided = np.where(d.sensor_type.map(route).eq("v0"), d.v0_decided, d.decided) & d.sensor_type.map(route).notna()
    routed = d.assign(alert_r=alert.astype(bool))[decided.astype(bool)]
    only_v0 = d[d.v0_decided]
    y = d.target.to_numpy() == 1
    onsets = pd.DataFrame({"c": d.channel_id, "o": (d.date + pd.to_timedelta(d.lead_days, unit="D")).dt.round("s")})
    return {
        "channels_of_types": int(d.channel_id.nunique()),
        "episodes_of_types": int(onsets[y].drop_duplicates().shape[0]),
        "routing": point(routed, "alert_r"),
        "v0_only": point(only_v0, "alert_v0"),
    }


if __name__ == "__main__":
    main()
