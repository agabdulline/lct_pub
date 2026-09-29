"""Обучение и проверка pdm_failure_24h — отказ компонента агрегата в ближайшие 24 ч (Azure PdM, синтетика).

Данные — data/external/azure_pdm/prepared (формат docs/architecture/external-data.md, готовит lct-18). Время в файлах —
московское без пояса. Решение — по агрегату раз в час, метка — начало отказа в [T, T + 24 ч). Решения в час самого
отказа (агрегат уже в отказе) не размечаются и не оцениваются.

Проверка — out-of-time (external-data.md §6.4): обучение — январь–август 2015, порог — сентябрь–октябрь, проверка —
ноябрь–декабрь. Решения, чей горизонт пересекает границу периода, убираются. Неделя сценария ext-pdm-week
(23–29.11.2015) — в проверке: живая модель её не видела.

Утечка, о которой предупреждал lct-18: 743 из 761 отказа совпадают по часу, агрегату и компоненту с записью ТО — это
замена после отказа. Окна [T − w, T) её не видят при T ≤ часа отказа; решения в час отказа не оцениваются.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.external.azure_pdm.train`.
"""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, Pool
from sklearn.metrics import precision_recall_curve

from ml.baseline.offline import block, lead_time, pr_curve, rule_block
from ml.external.azure_pdm.features import CATEGORICAL, FEATURES, HISTORY_BY_TAG, build, tags_for

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "external" / "azure_pdm" / "prepared"
OUT = ROOT / "ml" / "models" / "external" / "azure_pdm-failure"
REPORT = ROOT / "ml" / "reports" / "external" / "azure_pdm"
TZ = "Europe/Moscow"
HORIZON = pd.Timedelta(hours=24)
HOUR_NS = pd.Timedelta(hours=1).value
PERIODS = {"train": ("2015-01-01", "2015-09-01"), "threshold": ("2015-09-01", "2015-11-01"),
           "test": ("2015-11-01", "2016-01-01")}  # fmt: skip
WEEK = ("2015-11-23", "2015-11-30")  # сценарий ext-pdm-week
PARAMS = dict(iterations=600, depth=6, learning_rate=0.05, random_seed=42, verbose=False)
# Живая модель видит только то, что сообщает сам агрегат: телеметрию и коды ошибок (no_schedule). Записи службы — журнал
# ТО и отказов — в признаки не идут: в синтетике ТО каждые 15 суток в 06:00, и 702 из 719 отказов приходятся ровно на
# момент ТО агрегата, так что «часов с ТО» выдаёт единственный час, когда отказ вообще возможен. В реальности отказ не
# ждёт планового ТО. Полный набор (full) — для сравнения в отчёте.
SCHEDULE = [f for f in FEATURES if f.startswith(("h_since_", "maint_", "failures_"))]
# Ещё одна закономерность генератора: 1 003 кода ошибки стоят ровно за 24 ч до отказа (у 705 из 719 отказов), поэтому
# «ошибки за 24 ч» почти повторяют окно метки. Вариант telemetry — только телеметрия и паспорт агрегата, для отчёта.
ERROR_FEATURES = [f for f in FEATURES if f.startswith("error")]
VARIANTS = {
    "no_schedule": [f for f in FEATURES if f not in SCHEDULE],
    "full": FEATURES,
    "telemetry": [f for f in FEATURES if f not in SCHEDULE and f not in ERROR_FEATURES],
}


def utc(ts: pd.Series) -> pd.Series:
    ts = pd.to_datetime(ts)
    return ts.dt.tz_localize(TZ).dt.tz_convert("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")


def moment(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=TZ).tz_convert("UTC")


def load(data: Path):
    events = pd.read_parquet(data / "events.parquet")
    events["ts"] = utc(events["ts"])
    channels = pd.read_parquet(data / "channels.parquet").rename(columns={"id": "channel_id"})
    channels = channels.assign(measure=channels["tag"])[["channel_id", "object_id", "measure"]]
    objects = pd.read_parquet(data / "objects.parquet")
    units = objects[objects["kind"] == "machine"].copy()
    meta = [json.loads(m) for m in units["meta"]]
    units = pd.DataFrame({"object_id": units["id"].to_numpy(), "model": [m["model"] for m in meta],
                          "age_years": [float(m["age_years"]) for m in meta]})  # fmt: skip
    labels = pd.read_parquet(data / "labels.parquet")
    labels["component"] = [json.loads(m).get("subtype") for m in labels["meta"]]
    labels["object_id"] = labels["object_id"].astype("int64")
    units["object_id"] = units["object_id"].astype("int64")
    channels["object_id"] = channels["object_id"].astype("int64")
    for c in ("start_ts", "end_ts"):
        labels[c] = utc(labels[c])
    return events, channels, units, labels


def decisions(events: pd.DataFrame, units: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    """Решения по агрегатам раз в час: от суток телеметрии до конца журнала минус горизонт."""
    tel = events[events["channel_id"] % 100 == 1]  # напряжение есть у каждого агрегата с первого часа
    start, end = tel["ts"].min() + HORIZON, events["ts"].max() - HORIZON
    times = pd.date_range(start.ceil("h"), end.floor("h"), freq="h")
    d = pd.DataFrame([(u, t) for u in units["object_id"] for t in times], columns=["object_id", "decision_time"])
    # ближайший отказ агрегата в момент T или позже: метка — в [T, T + 24 ч), упреждение — до его начала
    d = d.sort_values("decision_time")
    f = failures.sort_values("start_ts")[["object_id", "start_ts"]].rename(columns={"start_ts": "next_failure"})
    d = pd.merge_asof(d, f.assign(key=f["next_failure"]), left_on="decision_time", right_on="key",
                      by="object_id", direction="forward").drop(columns="key")  # fmt: skip
    d["lead_s"] = (d["next_failure"] - d["decision_time"]).dt.total_seconds()
    d["y"] = (d["lead_s"] < HORIZON.total_seconds()).astype(int)
    d["in_failure"] = d["lead_s"] == 0  # агрегат уже в отказе — не размечается и не оценивается
    return d.sort_values(["object_id", "decision_time"]).reset_index(drop=True)


def period(d: pd.DataFrame, name: str) -> np.ndarray:
    """Решения периода без хвоста, чей горизонт уходит за его конец; без решений в час отказа."""
    lo, hi = (moment(x) for x in PERIODS[name])
    return ((d.decision_time >= lo) & (d.decision_time + HORIZON <= hi) & ~d.in_failure).to_numpy()


def episodes(g: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    """Серии тревог агрегата подряд по часам; серия верна, если отказ начался в [начало серии, конец + 24 ч)."""
    rows = []
    ns = lambda s: s.dt.tz_convert("UTC").astype("int64").to_numpy()  # noqa: E731 — наносекунды UTC
    fail = {u: np.sort(ns(g_["start_ts"])) for u, g_ in failures.groupby("object_id")}
    hour, horizon = HOUR_NS, HORIZON.value
    for unit, u in g[g.alert].groupby("object_id"):
        t = np.sort(ns(u.decision_time))
        breaks = np.r_[0, np.nonzero(np.diff(t) > hour)[0] + 1, len(t)]
        fs = fail.get(unit, np.array([], dtype="int64"))
        for a, b in zip(breaks[:-1], breaks[1:], strict=False):
            lo, hi = t[a], t[b - 1] + horizon
            hit = bool(((fs >= lo) & (fs < hi)).any())
            rows.append({"object_id": unit, "start": pd.Timestamp(lo, tz="UTC"), "hours": int(b - a), "true": hit})
    return pd.DataFrame(rows, columns=["object_id", "start", "hours", "true"])


def evaluate(te: pd.DataFrame, failures: pd.DataFrame, thr: float, seen_units: set) -> dict:
    y, p = te.y.to_numpy(), te.p.to_numpy()
    lead = te.lead_s.to_numpy()
    lo, hi = te.decision_time.min(), te.decision_time.max() + HORIZON
    fs = failures[(failures.start_ts > lo) & (failures.start_ts <= hi)]
    caught = []
    for f in fs.itertuples():
        unit = te[te.object_id == f.object_id]
        w = unit[(unit.decision_time > f.start_ts - HORIZON) & (unit.decision_time < f.start_ts)]  # решения с меткой 1
        hit = w[w.alert]
        first = (f.start_ts - hit.decision_time.min()).total_seconds() if len(hit) else None
        caught.append({
            "object_id": int(f.object_id),
            "start": str(f.start_ts.tz_convert(TZ)),
            "components": f.components,
            "seen_unit": f.object_id in seen_units,
            "decisions": len(w),
            "caught": bool(len(hit)),
            "lead_s": None if first is None else float(first),
        })  # fmt: skip
    cf = pd.DataFrame(caught)
    ep = episodes(te, failures)
    unit_days = te.object_id.nunique() * (hi - lo).total_seconds() / 86400
    seen = te.object_id.isin(seen_units).to_numpy()
    rule = (te.errors_72h > 0).to_numpy()  # наивное правило для сравнения: была ошибка за 72 ч
    by_comp = []
    for c in ("comp1", "comp2", "comp3", "comp4"):
        part = cf[cf.components.apply(lambda x, c=c: c in x)]
        by_comp.append({"component": c, "failures": len(part), "caught": int(part.caught.sum())})
    return {
        "decisions": block(y, p, thr, lead),
        "seen": block(y[seen], p[seen], thr, lead[seen]),
        "unseen": block(y[~seen], p[~seen], thr, lead[~seen]),
        "rule": rule_block("была ошибка агрегата за 72 ч", y, rule),
        "pr_curve": pr_curve(y, p),
        "failures": {
            "total": len(cf),
            "caught": int(cf.caught.sum()),
            "seen_units": {"total": int(cf.seen_unit.sum()), "caught": int(cf[cf.seen_unit].caught.sum())},
            "unseen_units": {"total": int((~cf.seen_unit).sum()), "caught": int(cf[~cf.seen_unit].caught.sum())},
        },  # fmt: skip
        "lead_time": lead_time(cf.lead_s.dropna().to_numpy()),
        "by_component": by_comp,
        "episodes": {
            "total": len(ep),
            "true": int(ep["true"].sum()),
            "false": int((~ep["true"]).sum()),
            "false_per_unit_month": round(float((~ep["true"]).sum()) / unit_days * 30, 3),
            "median_hours": float(ep.hours.median()) if len(ep) else None,
        },  # fmt: skip
        "by_failure": caught,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path, default=REPORT)
    ap.add_argument("--variant", choices=list(VARIANTS), default="no_schedule")
    ap.add_argument("--oof-only", action="store_true", help="только проверка: metrics_<набор>.json, без модели")
    a = ap.parse_args()
    names = VARIANTS[a.variant]
    by_tag = {t: m for t, m in HISTORY_BY_TAG.items() if t in tags_for(names)}
    cats = [c for c in CATEGORICAL if c in names]
    tick = time.perf_counter()
    events, channels, units, labels = load(a.data)
    failures = (labels[labels.label == "failure"].groupby(["object_id", "start_ts"])["component"]
                .apply(lambda s: sorted(set(s))).rename("components").reset_index())  # fmt: skip
    d = decisions(events, units, failures)
    f = build(events, channels, d[["object_id", "decision_time"]], units)
    d = pd.concat([d, f[FEATURES].reset_index(drop=True)], axis=1)
    print(f"решений {len(d)}, положительных {int(d.y.sum())}, признаков {len(names)} ({a.variant}), "
          f"{time.perf_counter() - tick:.0f} с", flush=True)  # fmt: skip

    tr, va, te = (period(d, n) for n in ("train", "threshold", "test"))
    model = CatBoostClassifier(**PARAMS)
    model.fit(Pool(d.loc[tr, names], d.loc[tr, "y"], cat_features=cats))
    d["p"] = model.predict_proba(Pool(d[names], cat_features=cats))[:, 1]
    # порог — максимум F1 на сентябре–октябре; на проверке не подбирается
    prec, rec, thr_grid = precision_recall_curve(d.loc[va, "y"], d.loc[va, "p"])
    f1 = 2 * prec[:-1] * rec[:-1] / np.maximum(prec[:-1] + rec[:-1], 1e-12)
    thr = float(thr_grid[int(np.argmax(f1))])
    d["alert"] = d.p >= thr
    seen_units = set(failures[failures.start_ts < moment(PERIODS["train"][1])].object_id)
    result = {
        "periods": {
            k: {"from": v[0], "to": v[1], "decisions": int(m.sum()), "positives": int(d.loc[m, "y"].sum())}
            for (k, v), m in zip(PERIODS.items(), (tr, va, te), strict=True)
        },  # fmt: skip
        "threshold": {
            "value": round(thr, 6),
            "rule": "максимум F1 на сентябре–октябре 2015",
            "valid": block(d.loc[va, "y"].to_numpy(), d.loc[va, "p"].to_numpy(), thr),
        },  # fmt: skip
        "test": evaluate(d[te], failures, thr, seen_units),
    }
    wk = d[te & (d.decision_time >= moment(WEEK[0])).to_numpy() & (d.decision_time < moment(WEEK[1])).to_numpy()]
    result["scenario_week"] = {
        "from": WEEK[0],
        "to": WEEK[1],
        "decisions": len(wk),
        "positives": int(wk.y.sum()),
        "alerts": int(wk.alert.sum()),
        "true_alerts": int((wk.alert & (wk.y == 1)).sum()),
    }

    dump = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1, default=str)  # noqa: E731
    importance = sorted(zip(names, model.get_feature_importance(), strict=True), key=lambda t: -t[1])
    metrics = result | {"variant": a.variant, "features": len(names), "importance": importance[:25]}
    metrics["test"] = {k: v for k, v in result["test"].items() if k != "pr_curve"}
    show = {k: v for k, v in metrics["test"].items() if k not in ("by_failure", "seen", "rule")}
    print(dump({"threshold": result["threshold"]["value"], "test": show, "scenario_week": result["scenario_week"]}))
    print("важность:", [(n, round(v, 1)) for n, v in importance[:12]])
    if a.oof_only:
        a.report.mkdir(parents=True, exist_ok=True)
        (a.report / f"metrics_{a.variant}.json").write_text(dump(metrics), encoding="utf-8")
        print(f"всего {time.perf_counter() - tick:.0f} с")
        return

    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / "pdm_failure_24h.cbm"
    model.save_model(str(path))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    config = {
        "model": "pdm_failure_24h",
        "family": "external",
        "dataset": "azure_pdm",
        "version": "v0",
        "label": "pdm-failure-v0",
        "title": "Отказ агрегата в ближайшие 24 ч",
        "synthetic": True,
        "trigger": "hourly",
        "target": "object",
        "target_tag": "machine",
        "horizon": "24h",
        "outcome": "label_in_horizon",  # отказ начался в [T, T + 24 ч); T внутри отказа — без исхода (уже в отказе)
        "positive_labels": ["failure"],
        "alert_class": "failure",  # класс решения с тревогой — для ленты (/v1/external/score, поле class)
        "ignore_labels": [],
        "classes": None,
        "features": names,
        "categorical": cats,
        "variant": a.variant,
        "threshold": round(thr, 6),
        "history_minutes": max(by_tag.values()),
        "history_minutes_by_tag": by_tag,  # только сигналы, которые читают признаки модели
        "eval_scheme": "out-of-time",
        "periods": PERIODS,
        "params": PARAMS,
        "sha256": sha,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "Azure AI Predictive Maintenance (синтетика Microsoft)",
    }
    (a.out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    t = result["test"]
    offline = {
        "sha256": sha,
        "computed_at": config["trained_at"],
        "model": "pdm_failure_24h",
        "version": "v0",
        "period": {"from": PERIODS["test"][0], "to": PERIODS["test"][1], "label": "ноябрь–декабрь 2015"},
        "threshold": round(thr, 6),
        "eval_scheme": "out-of-time",
        "test": t["decisions"],
        "seen": t["seen"],
        "unseen": t["unseen"],
        "seen_rule": "у агрегата был отказ в январе–августе 2015",
        "lead_time": t["lead_time"],
        "pr_curve": t["pr_curve"],
        "by_quarter": None,
        "by_type": None,
        "by_episode_kind": None,
        "by_class": None,
        "by_experiment": None,
        "rule": t["rule"],
    }
    (a.out / "offline.json").write_text(dump(offline), encoding="utf-8")
    a.report.mkdir(parents=True, exist_ok=True)
    (a.report / "metrics.json").write_text(dump(metrics), encoding="utf-8")
    print(f"всего {time.perf_counter() - tick:.0f} с")


if __name__ == "__main__":
    main()
