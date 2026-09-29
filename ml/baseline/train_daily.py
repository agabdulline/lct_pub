"""Обучение ежедневной базовой модели v0 по рабочей базе. Запуск из ml-service:
`DATABASE_URL=... PYTHONPATH=.. uv run python -m ml.baseline.train_daily`.

Данные — те же таблицы, что читает ml-service (channel_daily, fault_episodes), базовая история (run_id IS NULL).
Разбиение как у ежедневной v3: обучение 2022–2024, ранняя остановка — I полугодие 2025, порог — II полугодие 2025,
проверка — все сутки 2026 года с наблюдаемым горизонтом. Обучение и подбор — по понедельникам (строки соседних суток
почти одинаковы), проверка — по каждым суткам.
"""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sqlalchemy import create_engine, text

from ml.baseline.daily import CATEGORICAL, FEATURES, TYPES, build_daily
from ml.baseline.train import metrics, pick_threshold

ROOT = Path(__file__).resolve().parents[2]
TZ = "Europe/Moscow"
HISTORY_DAYS = 400  # сколько суток истории до даты решения нужно признакам (окна до 365 суток)
GAPS = [("2024-04-07", "2024-04-09"), ("2026-06-01", "2026-06-02")]
DATA_END = "2026-07-01"
PERIODS = {
    "train": ("2022-01-01", "2025-01-01"),
    "early_stop": ("2025-01-01", "2025-07-01"),
    "threshold": ("2025-07-01", "2026-01-01"),
    "test": ("2026-01-01", "2026-07-01"),
}


def load(url: str):
    eng = create_engine(url)
    with eng.connect() as c:
        ch = pd.read_sql(
            text("SELECT id AS channel_id, object_id, sensor_type FROM channels WHERE sensor_type = ANY(:t)"),
            c,
            params={"t": TYPES},
        )
        ids = ch["channel_id"].tolist()
        daily = pd.read_sql(
            text(
                "SELECT channel_id, date, n_events, n_alarm, n_fault, n_num, mean, max "
                "FROM channel_daily WHERE run_id IS NULL AND channel_id = ANY(:ids) "
                "AND date >= '2021-01-01'"
            ),
            c,
            params={"ids": ids},
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
        eps[col] = pd.to_datetime(eps[col], utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    eps = eps.rename(columns={"start_ts": "start", "end_ts": "end"})
    return ch, daily, eps


def rows(ch, daily, eps, dates) -> pd.DataFrame:
    d = build_daily(daily, eps, ch, dates, data_end=DATA_END, gaps=GAPS)
    return d[(d["status"] == "scored") & d["target"].notna()].sort_values("date", kind="stable")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "postgresql+psycopg://lct:lct@localhost:15432/lct"))
    ap.add_argument("--out", type=Path, default=ROOT / "ml" / "models" / "baseline-v0-daily")
    ap.add_argument("--report", type=Path, default=ROOT / "ml" / "reports" / "baseline-v0")
    ap.add_argument("--drop", nargs="*", default=[], help="исключить признаки — для проверки их вклада")
    a = ap.parse_args()
    features = [f for f in FEATURES if f not in a.drop]
    categorical = [f for f in CATEGORICAL if f in features]

    ch, daily, eps = load(a.db)
    mondays = pd.date_range("2022-01-03", "2025-12-29", freq="W-MON")
    weekly = rows(ch, daily, eps, mondays)
    test = rows(ch, daily, eps, pd.date_range(*PERIODS["test"], freq="D", inclusive="left"))

    def part(d, name):
        lo, hi = PERIODS[name]
        return d[(d["date"] >= lo) & (d["date"] < hi)]

    tr, es, th = part(weekly, "train"), part(weekly, "early_stop"), part(weekly, "threshold")
    model = CatBoostClassifier(
        iterations=3000,
        depth=6,
        learning_rate=0.05,
        loss_function="Logloss",
        eval_metric="PRAUC",
        od_type="Iter",
        od_wait=200,
        random_seed=42,
        verbose=False,
        has_time=True,
        cat_features=categorical,
    )
    model.fit(tr[features], tr["target"], eval_set=(es[features], es["target"]), use_best_model=True)
    thr = pick_threshold(th["target"].to_numpy(), model.predict_proba(th[features])[:, 1])

    result = {}
    for name, p in (("early_stop", es), ("threshold", th), ("test", test)):
        result[name] = metrics(p["target"].to_numpy(), model.predict_proba(p[features])[:, 1], thr)
    score = model.predict_proba(test[features])[:, 1]
    y = test["target"].to_numpy()
    hit = (score >= thr) & (y == 1)
    lead = test["lead_days"].to_numpy()[hit]
    result["test_lead_days"] = (
        {q: round(float(np.quantile(lead, q)), 2) for q in (0, 0.25, 0.5, 0.75, 1)} if hit.any() else {}
    )
    # бейзлайн без модели: «были начала неисправности за 30 суток»
    result["test_rule_recent_fault"] = metrics(y, (test["onsets_30d"] > 0).to_numpy().astype(float), 0.5)
    by_type = {}
    for st, g in test.assign(score=score).groupby("sensor_type"):
        yt = g["target"].to_numpy()
        by_type[st] = {
            **metrics(yt, g["score"].to_numpy(), thr),
            "rule_recent_fault": metrics(yt, (g["onsets_30d"] > 0).to_numpy().astype(float), 0.5),
        }
    result["test_by_type"] = by_type
    # сколько тревог в сутки получит диспетчер и сколько разных каналов
    alerted = test.assign(alert=score >= thr, hit=hit)
    per_day = alerted.groupby("date")["alert"].sum()
    result["test_alerts"] = {
        "days": int(per_day.size),
        "per_day_mean": round(float(per_day.mean()), 1),
        "per_day_max": int(per_day.max()),
        "channels_alerted": int(alerted.loc[alerted["alert"], "channel_id"].nunique()),
        "channels_with_onset": int(alerted.loc[alerted["target"] == 1, "channel_id"].nunique()),
        "channels_caught": int(alerted.loc[alerted["hit"], "channel_id"].nunique()),
    }
    # раздельно каналы с историей неисправностей за период обучения и без неё (AGENTS.md §5)
    lo, hi = PERIODS["train"]
    seen = set(eps.loc[(eps["start"] >= lo) & (eps["start"] < hi), "channel_id"])
    with_history = test["channel_id"].isin(seen).to_numpy()
    result["test_by_history"] = {
        name: {"channels": int(test.loc[m, "channel_id"].nunique()), **metrics(y[m], score[m], thr)}
        for name, m in (("with_train_faults", with_history), ("without_train_faults", ~with_history))
    }

    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / "daily.cbm"
    model.save_model(str(path))
    importance = sorted(zip(features, model.get_feature_importance(), strict=True), key=lambda x: -x[1])
    config = {
        "model": "daily",
        "family": "baseline",
        "version": "v0",
        "features": features,
        "categorical": categorical,
        "types": TYPES,
        "threshold": round(thr, 6),
        "trees": model.tree_count_,
        "periods": PERIODS,
        "history_days": HISTORY_DAYS,
        "horizon_days": [1, 30],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metrics": result,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (a.out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    a.report.mkdir(parents=True, exist_ok=True)
    (a.report / "daily_metrics.json").write_text(
        json.dumps({"metrics": result, "importance": importance}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "test_by_type"}, ensure_ascii=False, indent=1))
    print("важность целиком:", [(f, round(v, 2)) for f, v in importance])
    for k, v in by_type.items():
        r = v["rule_recent_fault"]
        print(
            f"{k}: P {v['precision']} R {v['recall']} AP {v['average_precision']} | правило P {r['precision']} "
            f"R {r['recall']} | решений {v['decisions']} положительных {v['positives']}"
        )
    print("важность:", [(f, round(v, 1)) for f, v in importance[:12]])
    print("деревьев:", model.tree_count_, "порог:", round(thr, 4), "| строк обучения:", len(tr), "проверки:", len(test))


if __name__ == "__main__":
    main()
