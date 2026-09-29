"""Обучение и честная проверка magdeburg_fire — «пожар / помеха / фон» по узлу раз в минуту.

Данные — data/external/magdeburg/prepared (формат docs/architecture/external-data.md, готовит lct-18): тот же журнал,
который проиграет эмулятор. Время в файлах — московское без пояса, как в журнале; в признаках — UTC, как из базы.
Проверка — leave-one-experiment-out: в проверку уходит опыт вместе со следующим за ним фоном, из обучения убирается
час вокруг проверочного отрезка (окна признаков смотрят на час назад). Порог не подбирается: тревога — P(пожар) ≥ 0.5.
Интервалы с меткой unknown не участвуют ни в обучении, ни в оценке.

Живая модель не видит окна сценариев эмулятора UNSEEN (опыты 24, 28–32 и ночь без опытов) и час после каждого.

Наборы признаков: v0 — сигналы узла и сравнение с цехом, v1 — ещё «чем сопровождаются частицы» (features.py).
Сравнение — одной и той же проверкой: `--variant v0 --oof-only` пишет только метрики проверки в metrics_v0.json.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.external.magdeburg.train [--variant v0|v1] [--oof-only]`.
"""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.metrics import average_precision_score

from ml.baseline.offline import block, lead_time, pr_curve
from ml.external.magdeburg.features import FEATURES, HISTORY, MEASURES, build, feature_names_v1

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "external" / "magdeburg" / "prepared"
OUT = ROOT / "ml" / "models" / "external" / "magdeburg-fire"
REPORT = ROOT / "ml" / "reports" / "external" / "magdeburg"
CLASSES = ["background", "nuisance", "fire"]
TZ = "Europe/Moscow"
STEP = pd.Timedelta(minutes=1)
PURGE = pd.Timedelta(hours=1)
THRESHOLD = 0.5
# окна сценариев эмулятора, МСК: живая модель их не видит (согласовано с lct-18, docs/architecture/external-data.md)
UNSEEN = [
    ("2023-07-12 03:00", "2023-07-12 04:00"),  # фон, ночь
    ("2023-07-12 16:35", "2023-07-12 17:20"),  # опыт 24, кабель
    ("2023-07-13 11:05", "2023-07-13 12:25"),  # опыты 28 (выхлоп) и 29 (фитили)
    ("2023-07-13 12:45", "2023-07-13 13:40"),  # опыт 30, дерево
    ("2023-07-13 14:05", "2023-07-13 14:55"),  # опыты 31 (сварка) и 32 (резка): час после дерева их всё равно накрывает
]
TAIL = pd.Timedelta(minutes=30)
OOF = ROOT / "data" / "interim" / "external"  # прогнозы проверки magdeburg_oof_<набор>.parquet — для отчёта, вне git
VARIANTS = {"v0": [f for f in FEATURES if f not in feature_names_v1()], "v1": FEATURES}
PARAMS = dict(
    loss_function="MultiClass",
    iterations=300,
    depth=6,
    learning_rate=0.08,
    auto_class_weights="SqrtBalanced",
    random_seed=42,
    verbose=False,
)


def utc(ts: pd.Series) -> pd.Series:
    """Время журнала — московское без пояса (A1); в признаках и в базе — момент времени, UTC."""
    ts = pd.to_datetime(ts)
    return ts.dt.tz_localize(TZ).dt.tz_convert("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")


def load(data: Path):
    events = pd.read_parquet(data / "events.parquet")
    events["ts"] = utc(events["ts"])
    channels = pd.read_parquet(data / "channels.parquet").rename(columns={"id": "channel_id"})
    channels = channels[channels["tag"].isin(MEASURES)].assign(measure=lambda c: c["tag"])
    objects = pd.read_parquet(data / "objects.parquet")
    labels = pd.read_parquet(data / "labels.parquet")
    meta = [json.loads(m) for m in labels["meta"]]
    labels["experiment"] = [m.get("experiment", -1) for m in meta]
    labels["subtype"] = [m.get("subtype", "") for m in meta]
    for c in ("start_ts", "end_ts"):
        labels[c] = utc(labels[c])
    return events, channels, objects, labels.sort_values("start_ts").reset_index(drop=True)


def decisions(events: pd.DataFrame, channels: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Решения по узлам раз в минуту; класс — опыт, идущий в момент T, иначе фон."""
    ts = pd.to_datetime(events["ts"], utc=True)
    start = (ts.min() + HISTORY).ceil("min")
    times = pd.date_range(start, ts.max().floor("min"), freq=STEP)
    nodes = sorted(channels["object_id"].unique())
    d = pd.DataFrame([(n, t) for t in times for n in nodes], columns=["object_id", "decision_time"])
    d["label"], d["experiment"], d["subtype"], d["since_start"] = "background", 0, "", np.nan
    starts = labels.groupby("experiment")["start_ts"].min()  # время от начала опыта, у опыта из двух меток — от первой
    for e in labels.itertuples():
        m = (d.decision_time >= e.start_ts) & (d.decision_time < e.end_ts)
        d.loc[m, ["label", "experiment", "subtype"]] = [e.label, e.experiment, e.subtype]
        d.loc[m, "since_start"] = (d.loc[m, "decision_time"] - starts[e.experiment]).dt.total_seconds()
    return d


def folds(d: pd.DataFrame, labels: pd.DataFrame):
    """Опыт k и фон до следующего опыта — проверка; обучение — всё остальное без часа вокруг."""
    exp = labels[labels.label != "unknown"].groupby("experiment")["start_ts"].min().sort_values()
    starts = [*exp.to_list(), d.decision_time.max() + STEP]
    known = (d.label != "unknown").to_numpy()
    for k, (experiment, lo) in enumerate(exp.items()):
        hi = starts[k + 1]
        test = (d.decision_time >= lo) & (d.decision_time < hi)
        purge = (d.decision_time >= lo - PURGE) & (d.decision_time < hi + PURGE)
        yield experiment, test.to_numpy() & known, (~purge).to_numpy() & known


def fit(x: pd.DataFrame, y: pd.Series) -> CatBoostClassifier:
    m = CatBoostClassifier(**PARAMS, class_names=CLASSES)
    m.fit(x, y)
    return m


def evaluate(d: pd.DataFrame, labels: pd.DataFrame) -> dict:
    """Метрики по отложенным опытам: решения, опыты, фон."""
    y_fire = (d.label == "fire").to_numpy().astype(float)
    p_fire = d.p_fire.to_numpy()
    # главная задача набора: во время опыта отличить пожар от помехи — только решения во время опытов
    fn = d[d.label.isin(["fire", "nuisance"])]
    fire_vs_nuisance = block((fn.label == "fire").to_numpy().astype(float), fn.p_fire.to_numpy(), THRESHOLD)
    nuisance_alerts = [
        {"subtype": k, "decisions": len(g), "fire_alert_share": round(float((g.p_fire >= THRESHOLD).mean()), 3)}
        for k, g in fn[fn.label == "nuisance"].groupby("subtype")
    ]
    by_class = []
    for cls, col in (("fire", "p_fire"), ("nuisance", "p_nuisance")):
        y = (d.label == cls).to_numpy().astype(float)
        by_class.append({"class": cls, **block(y, d[col].to_numpy(), THRESHOLD)})
    experiments = []
    for e in labels[labels.label != "unknown"].drop_duplicates("experiment").itertuples():
        g = d[d.experiment == e.experiment]
        e_labels = labels[labels.experiment == e.experiment]
        hit = g[g.p_fire >= THRESHOLD]
        first = hit.since_start.min() if len(hit) else None
        row = {
            "experiment": int(e.experiment),
            "label": e.label,
            "subtype": e.subtype,
            "subtypes": sorted(set(e_labels.subtype)),
            "minutes": round((e_labels.end_ts.max() - e_labels.start_ts.min()).total_seconds() / 60, 1),
            "decisions": len(g),
            "fire_alerts": int(len(hit)),
            "nodes_alerted": int(hit.object_id.nunique()),
            "first_alert_s": None if first is None or np.isnan(first) else float(first),
            "predicted": g[["p_background", "p_nuisance", "p_fire"]].mean().idxmax().removeprefix("p_"),
        }
        experiments.append(row)
    ex = pd.DataFrame(experiments)
    fire_ex, nuis_ex = ex[ex.label == "fire"], ex[ex.label == "nuisance"]
    bg = d[d.label == "background"].copy()
    # хвост опыта: фон в первые TAIL после конца опыта — дым и газы ещё в цеху, метка уже «фон»
    ends = labels.loc[labels.label != "unknown", "end_ts"].sort_values().to_numpy()
    k = np.searchsorted(ends, bg.decision_time.to_numpy(), side="right")
    since_end = np.where(k > 0, bg.decision_time.to_numpy() - ends[np.maximum(k - 1, 0)], np.timedelta64("NaT", "ns"))
    bg["in_tail"] = pd.Series(since_end, index=bg.index) <= TAIL
    tails = {}
    for name, part in (("after_experiment", bg[bg["in_tail"]]), ("quiet", bg[~bg["in_tail"]])):
        minutes = part.decision_time.nunique()
        alerted = part[part.p_fire >= THRESHOLD].decision_time.nunique()
        tails[name] = {"hours": round(minutes / 60, 1), "minutes_with_fire_alert": int(alerted),
                       "false_alerts_per_hour": round(alerted / max(minutes / 60, 1e-9), 3)}  # fmt: skip
    bg_minutes = bg.decision_time.nunique()
    bg_alert_minutes = bg[bg.p_fire >= THRESHOLD].decision_time.nunique()
    detect = fire_ex.first_alert_s.dropna().to_numpy()
    return {
        "decisions": len(d),
        "fire_vs_rest": block(y_fire, p_fire, THRESHOLD),  # время до тревоги — по опытам, time_to_detect
        "fire_vs_nuisance": fire_vs_nuisance,
        "nuisance_fire_alerts": nuisance_alerts,
        "by_class": by_class,
        "pr_curve": pr_curve(y_fire, p_fire),
        "experiments": {
            "fire": {"total": len(fire_ex), "detected": int(fire_ex.first_alert_s.notna().sum())},
            "nuisance": {"total": len(nuis_ex), "with_fire_alert": int((nuis_ex.fire_alerts > 0).sum())},
        },
        "time_to_detect": lead_time(detect),
        "background": {
            "hours": round(bg_minutes / 60, 1),
            "minutes_with_fire_alert": int(bg_alert_minutes),
            "false_alerts_per_hour": round(bg_alert_minutes / max(bg_minutes / 60, 1e-9), 3),
            "tail_minutes": int(TAIL.total_seconds() // 60),
            **tails,
        },
        "by_experiment": experiments,
        "ap_fire": round(float(average_precision_score(y_fire, p_fire)), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path, default=REPORT)
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="v0")  # живая; v1 не прошёл проверку — REPORT.md
    ap.add_argument("--oof-only", action="store_true", help="только проверка: metrics_<набор>.json, без модели")
    a = ap.parse_args()
    names = VARIANTS[a.variant]
    tick = time.perf_counter()
    events, channels, objects, labels = load(a.data)
    d = decisions(events, channels, labels)
    f = build(events, channels, d[["object_id", "decision_time"]])
    d = pd.concat([d.reset_index(drop=True), f[names].reset_index(drop=True)], axis=1)
    print(f"решений {len(d)}, признаков {len(names)} ({a.variant}), {time.perf_counter() - tick:.0f} с", flush=True)
    for c in ("p_background", "p_nuisance", "p_fire"):
        d[c] = np.nan
    for exp, test, train in folds(d, labels):
        m = fit(d.loc[train, names], d.loc[train, "label"])
        p = m.predict_proba(d.loc[test, names])
        d.loc[test, ["p_background", "p_nuisance", "p_fire"]] = p[:, [CLASSES.index(c) for c in CLASSES]]
        print(f"опыт {exp}: проверка {int(test.sum())}, обучение {int(train.sum())}", flush=True)
    scored = d[d.p_fire.notna()]
    OOF.mkdir(parents=True, exist_ok=True)
    keep = ["object_id", "decision_time", "label", "experiment", "subtype", "since_start"]
    scored[[*keep, "p_background", "p_nuisance", "p_fire"]].to_parquet(OOF / f"magdeburg_oof_{a.variant}.parquet")
    result = evaluate(scored, labels)
    dump = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1, default=str)  # noqa: E731
    summary = ("fire_vs_rest", "fire_vs_nuisance", "experiments", "time_to_detect", "background", "ap_fire")
    if a.oof_only:
        a.report.mkdir(parents=True, exist_ok=True)
        out = {"variant": a.variant, "features": len(names)} | {k: v for k, v in result.items() if k != "pr_curve"}
        (a.report / f"metrics_{a.variant}.json").write_text(dump(out), encoding="utf-8")
        print(dump({k: result[k] for k in summary}))
        print(f"всего {time.perf_counter() - tick:.0f} с")
        return
    live = (d.label != "unknown").to_numpy().copy()
    windows = [(pd.Timestamp(a, tz=TZ).tz_convert("UTC"), pd.Timestamp(b, tz=TZ).tz_convert("UTC")) for a, b in UNSEEN]
    for lo, hi in windows:  # окно сценария и час после него — окна признаков смотрят на час назад
        live &= ~((d.decision_time >= lo) & (d.decision_time < hi + PURGE)).to_numpy()
    final = fit(d.loc[live, names], d.loc[live, "label"])
    seen = set(d.loc[live, "experiment"])
    holdout = sorted(int(x) for x in labels["experiment"].unique() if x > 0 and x not in seen)
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / "magdeburg_fire.cbm"
    final.save_model(str(path))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    importance = sorted(zip(names, final.get_feature_importance(), strict=True), key=lambda t: -t[1])
    config = {
        "model": "magdeburg_fire",
        "family": "external",
        "dataset": "magdeburg",
        "version": a.variant,
        "label": f"magdeburg-fire-{a.variant}",
        "title": "Пожар против помехи на узлах цеха",
        "synthetic": False,
        "trigger": "every_60s",
        "target": "object",
        "target_tag": "node",  # служебный канал цели у узла (external-data.md §3.2)
        "horizon": "now",
        "outcome": "label_at_decision_time",  # исход решения T — метка опыта, идущего в T (fire / nuisance / фон)
        "positive_labels": ["fire"],  # помеха с тревогой «пожар» — ложная тревога
        "ignore_labels": ["unknown"],
        "classes": CLASSES,
        "features": names,
        "categorical": [],
        "threshold": THRESHOLD,
        "history_minutes": int(HISTORY.total_seconds() // 60),
        "eval_scheme": "leave-one-experiment-out",
        "unseen_experiments": holdout,
        "unseen_windows": [{"from": a, "to": b, "tz": TZ} for a, b in UNSEEN],
        "trees": final.tree_count_,
        "params": PARAMS,
        "sha256": sha,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "https://data.mendeley.com/datasets/yghykzm4km/1 (CC BY 4.0)",
    }
    (a.out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    period = {"from": str(d.decision_time.min()), "to": str(d.decision_time.max()), "label": "10–13.07.2023, все опыты"}
    offline = {
        "sha256": sha,
        "computed_at": config["trained_at"],
        "model": "magdeburg_fire",
        "version": a.variant,
        "period": period,
        "threshold": THRESHOLD,
        "eval_scheme": "leave-one-experiment-out",
        "test": result["fire_vs_rest"],
        "fire_vs_nuisance": result["fire_vs_nuisance"],  # главная задача набора: решения только во время опытов
        "seen": None,
        "unseen": None,
        "seen_rule": None,
        "lead_time": result["time_to_detect"],
        "pr_curve": result["pr_curve"],
        "by_quarter": None,
        "by_type": None,
        "by_episode_kind": None,
        "by_class": result["by_class"],
        "by_experiment": result["by_experiment"],
        "rule": None,
    }
    (a.out / "offline.json").write_text(dump(offline), encoding="utf-8")
    a.report.mkdir(parents=True, exist_ok=True)
    metrics = {k: v for k, v in result.items() if k != "pr_curve"} | {"importance": importance[:25]}
    (a.report / "metrics.json").write_text(dump(metrics), encoding="utf-8")
    print(dump({k: result[k] for k in summary}))
    print(pd.DataFrame(result["by_experiment"]).to_string(index=False))
    print("важность:", [(n, round(v, 1)) for n, v in importance[:12]])
    print(f"всего {time.perf_counter() - tick:.0f} с")


if __name__ == "__main__":
    main()
