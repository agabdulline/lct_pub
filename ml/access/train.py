"""Обучение и проверка модели «свой или нужна реакция» при открытии под охраной: `ml/reports/access/`.

Точки и метка — `ml.access.dataset` (движок правила), признаки — `ml.access.features`. Сплит только по времени, по
году старта (Москва): обучение 2019–2024, порог — 2025, проверка — I полугодие 2026. Порог — как у ежедневных моделей
команды: максимум min(P / 0.7, R / 0.5) на 2025, на проверке ничего не подбирается.

Положительный класс — «нужна реакция»: охрану за 5 мин не сняли, правило завело бы инцидент. Отчёт — P, R с 90%
бутстрапом по подобъектам, число решений и положительных, двери и люки, подобъекты с историей и без, базовые правила.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.access.train` (после `ml.access.dataset`).
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.metrics import average_precision_score, precision_recall_curve

from ml.access.features import CATEGORICAL, FEATURES, MSK, History, build

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "interim" / "access"
MODEL_DIR = ROOT / "ml" / "models" / "access-open"
REPORT = ROOT / "ml" / "reports" / "access"
SPLIT = {"train": (2019, 2024), "threshold": (2025, 2025), "test": (2026, 2026)}
PARAMS = dict(iterations=400, depth=4, learning_rate=0.03, l2_leaf_reg=10, loss_function="Logloss",
              random_seed=42, verbose=False, allow_writing_files=False, thread_count=4)  # fmt: skip
REQ_P, REQ_R = 0.7, 0.5
BOOT, SEED = 1000, 42
GAMMA = 5961  # «объект Гамма ОС» — систематические открытия под охраной (access.md §3.1)


def load() -> tuple[pd.DataFrame, History]:
    points = pd.read_parquet(DATA / "points.parquet")
    h = History(points, *(pd.read_parquet(DATA / f"{n}.parquet") for n in ("guard", "openers")))
    points = points[points.label.notna()].reset_index(drop=True)
    points["year"] = points.start_ts.dt.tz_convert(MSK).dt.year
    feats = build(points, h)
    points = pd.concat([points.drop(columns=[c for c in FEATURES if c in points]), feats], axis=1)
    return points, h


def part(d: pd.DataFrame, name: str) -> pd.DataFrame:
    lo, hi = SPLIT[name]
    return d[d.year.between(lo, hi)]


def pick_threshold(y: np.ndarray, s: np.ndarray) -> float:
    p, r, t = precision_recall_curve(y, s)
    crit = np.minimum(p[:-1] / REQ_P, r[:-1] / REQ_R)
    return float(t[int(np.argmax(crit))])


def block(y: np.ndarray, alert: np.ndarray, groups: np.ndarray | None = None, score=None) -> dict:
    y, alert = y.astype(bool), alert.astype(bool)
    tp, fp, fn = int((alert & y).sum()), int((alert & ~y).sum()), int((~alert & y).sum())
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    out = {"decisions": len(y), "positives": int(y.sum()), "alerts": int(alert.sum()), "tp": tp, "fp": fp, "fn": fn,
           "precision": p, "recall": r, "passes": bool(p and r and p > REQ_P and r > REQ_R)}  # fmt: skip
    if score is not None and y.any() and (~y).any():
        out["ap"] = float(average_precision_score(y, score))
    if groups is not None and len(y):
        out["ci90"] = boot(y, alert, groups)
    return out


def boot(y: np.ndarray, alert: np.ndarray, groups: np.ndarray) -> dict:
    """90% интервалы P и R — бутстрап по подобъектам: открытия одного подобъекта зависимы."""
    idx = pd.Series(np.arange(len(y))).groupby(groups).indices
    keys = list(idx)
    rng = np.random.default_rng(SEED)
    ps, rs = [], []
    for _ in range(BOOT):
        k = np.concatenate([idx[keys[i]] for i in rng.integers(0, len(keys), len(keys))])
        yb, ab = y[k], alert[k]
        tp = (yb & ab).sum()
        ps.append(tp / ab.sum() if ab.sum() else np.nan)
        rs.append(tp / yb.sum() if yb.sum() else np.nan)
    q = [0.05, 0.95]
    return {"P": [float(x) for x in np.nanquantile(ps, q)], "R": [float(x) for x in np.nanquantile(rs, q)]}


def evaluate(d: pd.DataFrame, score: np.ndarray, thr: float) -> dict:
    y, a, g = d.label.to_numpy(int), score >= thr, d.sub_object_id.to_numpy()
    seen = d.sub_candidates_365d.to_numpy() > 0
    hatch = d.hatch.to_numpy(bool)
    out = {
        "all": block(y, a, g, score),
        "doors": block(y[~hatch], a[~hatch], g[~hatch]),
        "hatches": block(y[hatch], a[hatch]),
        "with_history": block(y[seen], a[seen], g[seen]),
        "without_history": block(y[~seen], a[~seen]),
        "without_gamma": block(y[d.sub_object_id != GAMMA], a[d.sub_object_id != GAMMA], g[d.sub_object_id != GAMMA]),
        "gamma_share": float((d.sub_object_id == GAMMA).mean()),
        "sub_objects": int(d.sub_object_id.nunique()),
    }
    out["baselines"] = {  # правила без модели — на тех же решениях
        "always_reaction": block(y, np.ones(len(y), bool), g),
        "off_hours": block(y, d.off_hours.to_numpy(bool), g),
        "alarm_flag": block(y, d.is_alarm.to_numpy(bool), g),
        "door_rarely_disarmed": block(y, d.door_dismissed_share_365d.to_numpy() < 0.5, g),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--drop", nargs="*", default=[], help="без этих признаков — проверка их вклада")
    a = ap.parse_args()
    global FEATURES
    FEATURES = [f for f in FEATURES if f not in a.drop]
    d, _ = load()
    tr, th, te = part(d, "train"), part(d, "threshold"), part(d, "test")
    model = CatBoostClassifier(**PARAMS, cat_features=CATEGORICAL)
    model.fit(tr[FEATURES], tr.label.astype(int))
    s_th, s_te = model.predict_proba(th[FEATURES])[:, 1], model.predict_proba(te[FEATURES])[:, 1]
    thr = pick_threshold(th.label.to_numpy(int), s_th)
    result = {
        "split": {
            k: {"years": v, "decisions": len(part(d, k)), "positives": int(part(d, k).label.sum())}
            for k, v in SPLIT.items()
        },  # fmt: skip
        "threshold": thr,
        "threshold_period": evaluate(th, s_th, thr),
        "test": evaluate(te, s_te, thr),
        "importance": sorted(
            zip(FEATURES, map(float, model.get_feature_importance()), strict=True), key=lambda x: -x[1]
        ),  # fmt: skip
    }
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / "access_open.cbm"
    model.save_model(str(path))
    t = result["test"]["all"]

    def offline(b: dict) -> dict:
        p, r = b["precision"], b["recall"]
        f1 = 2 * p * r / (p + r) if p and r else None
        return {"precision": p, "recall": r, "f1": f1, "ap": b.get("ap"), "tp": b["tp"], "fp": b["fp"],
                "decisions": b["decisions"], "positives": b["positives"], "ci90": b.get("ci90")}  # fmt: skip

    config = {  # формат согласован с ml-service (lct-1d): label — версия в predictions.model_version
        "model": "access_open",
        "family": "access",
        "version": "v0",
        "label": "access-open-v0",
        "author": "команда",
        "title": "Открытие под охраной: снимут ли охрану — свой или нужна реакция",
        "features": FEATURES,
        "categorical": CATEGORICAL,
        "threshold": round(thr, 6),
        "trees": model.tree_count_,
        "positive": "нужна реакция",
        "positive_class": "охрану не сняли в [T, T + 5 мин] — правило завело бы инцидент",
        "decision": "первое открытие кандидата правила «вскрытие под охраной» (docs/architecture/access.md §3)",
        "train": {"from": "2019-01-01", "to": "2025-01-01"},
        "threshold_period": {"from": "2025-01-01", "to": "2026-01-01"},
        "test": {"from": "2026-01-01", "to": "2026-07-01", "label": "I полугодие 2026"},
        "trained_on": "2019–2024",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "offline_metrics": {
            "period": "I полугодие 2026",
            **offline(t),
            "rule": {
                "name": "флаг тревоги охранной системы у открытия",
                **offline(result["test"]["baselines"]["alarm_flag"]),
            },
        },
        "limitations": [
            "Прокси «свой или чужой» по тому, снимут ли охрану, а не обнаружение проникновения",
            "Метка — «охрану не сняли за 5 мин»; снятие охраны — код или ключ, а не подтверждённый допуск",
            "Какие двери в зоне канала охраны — допущение: зона — подобъект (Q23)",
            "Проверка — 215 решений на 29 подобъектах, 20% из них — один подобъект («объект Гамма ОС»)",
        ],
        "report": "ml/reports/access/REPORT.md",
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (MODEL_DIR / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    # офлайн-прогнозы проверки — сверка сервиса с обучением (признаки и оценка на каждой точке)
    test = te[["sub_object_id", "channel_id", "start_ts", "label", *FEATURES]].assign(score=s_te)
    test.to_csv(MODEL_DIR / "test_2026.csv", index=False, float_format="%.17g")

    def line(name, b):
        p = "—" if b["precision"] is None else f"{b['precision']:.3f}"
        r = "—" if b["recall"] is None else f"{b['recall']:.3f}"
        ci = b.get("ci90", {})
        print(f"{name:24s} n={b['decisions']:4d} pos={b['positives']:4d} al={b['alerts']:4d} P={p} R={r} {ci}")

    print("threshold", round(thr, 4), "trees", model.tree_count_)
    for k in ("all", "doors", "hatches", "with_history", "without_history", "without_gamma"):
        line(k, result["test"][k])
    for k, b in result["test"]["baselines"].items():
        line("base:" + k, b)
    line("2025:all", result["threshold_period"]["all"])
    print("importance", [(f, round(v, 1)) for f, v in result["importance"][:10]])


if __name__ == "__main__":
    main()
