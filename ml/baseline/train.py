"""Обучение базовой модели v0 и отчёт. Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.baseline.train`.

Разбиение по времени как у v3 (ml/reports/v3/REPORT_RU.md §3): обучение 2023–2024 (2022 — только прогрев истории),
ранняя остановка — I полугодие 2025, порог — II полугодие 2025, проверка — I полугодие 2026.
Порог выбирается максимумом min(Precision / 0.7, Recall / 0.5) на периоде порога — тот же критерий, что у v3.
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

from ml.baseline.dataset import SMOKE, load_channels, load_events
from ml.baseline.features import FEATURES, build

ROOT = Path(__file__).resolve().parents[2]
HISTORY_START = "2022-01-01"  # с этой даты история и при обучении, и в ml-service
GAPS = [("2024-04-07", "2024-04-09"), ("2026-06-01", "2026-06-02")]
PERIODS = {
    "train": ("2023-01-01", "2025-01-01"),
    "early_stop": ("2025-01-01", "2025-07-01"),
    "threshold": ("2025-07-01", "2026-01-01"),
    "test": ("2026-01-01", "2026-07-01"),
}


def period(d: pd.DataFrame, name: str) -> pd.DataFrame:
    a, b = PERIODS[name]
    return d[(d.decision_time >= a) & (d.decision_time < b) & d.target.notna()]


def metrics(y: np.ndarray, score: np.ndarray, thr: float) -> dict:
    pred = score >= thr
    tp, fp, fn = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum()), int((~pred & (y == 1)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {
        "decisions": len(y),
        "positives": int(y.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
        "average_precision": round(float(average_precision_score(y, score)), 4) if y.sum() else None,
    }


def pick_threshold(y: np.ndarray, score: np.ndarray) -> float:
    p, r, t = precision_recall_curve(y, score)
    crit = np.minimum(p[:-1] / 0.7, r[:-1] / 0.5)
    return float(t[int(np.argmax(crit))])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    ap.add_argument("--cache", type=Path, help="parquet с готовыми решениями и признаками")
    ap.add_argument("--out", type=Path, default=ROOT / "ml" / "models" / "baseline-v0")
    ap.add_argument("--report", type=Path, default=ROOT / "ml" / "reports" / "baseline-v0")
    a = ap.parse_args()

    if a.cache and a.cache.exists():
        d = pd.read_parquet(a.cache)
    else:
        ch = load_channels(a.archive)
        ev = load_events(a.archive, ch[ch.sensor_type == SMOKE].channel_id, since=HISTORY_START)
        d = build(ev, ch, data_end="2026-07-01", gaps=GAPS)
        if a.cache:
            d.to_parquet(a.cache)

    tr, es, th, te = (period(d, n) for n in PERIODS)
    model = CatBoostClassifier(
        iterations=3000,
        depth=6,
        learning_rate=0.03,
        loss_function="Logloss",
        eval_metric="PRAUC",
        od_type="Iter",
        od_wait=200,
        random_seed=42,
        verbose=False,
    )
    model.fit(tr[FEATURES], tr.target, eval_set=(es[FEATURES], es.target), use_best_model=True)

    thr = pick_threshold(th.target.to_numpy(), model.predict_proba(th[FEATURES])[:, 1])
    result = {}
    for name, part in (("early_stop", es), ("threshold", th), ("test", te)):
        result[name] = metrics(part.target.to_numpy(), model.predict_proba(part[FEATURES])[:, 1], thr)

    # каналы с решениями в обучении и без них (у v3 на вторых Recall = 0)
    seen = set(tr.channel_id)
    te_score = model.predict_proba(te[FEATURES])[:, 1]
    mask = te.channel_id.isin(seen).to_numpy()
    y = te.target.to_numpy()
    result["test_seen"] = metrics(y[mask], te_score[mask], thr)
    result["test_unseen"] = metrics(y[~mask], te_score[~mask], thr)
    hit = (te_score >= thr) & (y == 1)
    lead = te.lead_hours.to_numpy()[hit]
    result["test_lead_hours"] = (
        {q: round(float(np.quantile(lead, q)), 3) for q in (0, 0.25, 0.5, 0.75, 1)} if hit.any() else {}
    )
    # бейзлайн без модели: «одиночная тревога по объекту → неисправность»
    solitary = te.solitary_1h.fillna(0).to_numpy()
    result["test_rule_solitary"] = metrics(y, solitary, 0.5)

    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / "smoke_alarm.cbm"
    model.save_model(str(path))
    importance = sorted(zip(FEATURES, model.get_feature_importance(), strict=True), key=lambda x: -x[1])
    config = {
        "model": "smoke_alarm",
        "family": "baseline",
        "version": "v0",
        "features": FEATURES,
        "categorical": [],
        "threshold": round(thr, 6),
        "trees": model.tree_count_,
        "periods": PERIODS,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metrics": result,
        "history_start": HISTORY_START,
        "seen_channels": sorted(int(c) for c in seen),
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (a.out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    a.report.mkdir(parents=True, exist_ok=True)
    (a.report / "metrics.json").write_text(
        json.dumps({"metrics": result, "importance": importance[:20]}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print("важность:", [(f, round(v, 1)) for f, v in importance[:12]])
    print("деревьев:", model.tree_count_, "порог:", round(thr, 4))


if __name__ == "__main__":
    main()
