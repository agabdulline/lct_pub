"""Офлайн-метрики базовых моделей v0 для страницы модели: `ml/models/<версия>/offline.json`.

Модели не переобучаются: берутся отгруженные `.cbm` и `config.json`, решения строятся тем же кодом, что при обучении.
В файле — рабочая точка на тесте, PR-кривая, упреждение, каналы с историей и без, кварталы 2025–2026 и, для ежедневной
модели, типы каналов. ml-service отдаёт его в `/v1/models` (поле `offline`), если sha256 совпадает с моделью.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.baseline.offline [--only smoke_alarm|daily]`.
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

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "ml" / "models"
TZ = "Europe/Moscow"
QUARTERS = ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2"]  # данные — до 1 июля 2026
TEST_LABEL = "I полугодие 2026"
CURVE_POINTS = 50


def _r(x) -> float | None:
    return None if x is None else round(float(x), 4)


def lead_time(seconds: np.ndarray) -> dict | None:
    """Квантили упреждения в секундах — те же ключи, что у живых метрик (/metrics), плюс квартили."""
    if len(seconds) == 0:
        return None
    q = {"min_s": 0, "p10_s": 0.1, "p25_s": 0.25, "p50_s": 0.5, "p75_s": 0.75, "p90_s": 0.9, "max_s": 1}
    return {k: round(float(np.quantile(seconds, v)), 1) for k, v in q.items()}


def block(y: np.ndarray, score: np.ndarray, thr: float, lead_s: np.ndarray | None = None) -> dict:
    """Метрики в формате MetricsBlock живых метрик (docs/api.md) плюс AP. Пустой знаменатель — null."""
    y = np.asarray(y, dtype=float)
    pred = np.asarray(score) >= thr
    pos = y == 1
    tp, fp = int((pred & pos).sum()), int((pred & ~pos).sum())
    fn, tn = int((~pred & pos).sum()), int((~pred & ~pos).sum())
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = None if p is None or r is None else (2 * p * r / (p + r) if p + r else 0.0)
    ap = average_precision_score(y, score) if 0 < pos.sum() < len(y) else None
    return {
        "decisions": len(y),
        "positives": int(pos.sum()),
        "alerts": int(pred.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _r(p),
        "recall": _r(r),
        "f1": _r(f1),
        "ap": _r(ap),
        "lead_time": lead_time(np.asarray(lead_s)[pred & pos]) if lead_s is not None else None,
    }


def rule_block(name: str, y: np.ndarray, fired: np.ndarray) -> dict:
    """Правило без модели: срабатывание 0/1, AP у него не имеет смысла."""
    return {"name": name, **block(y, np.asarray(fired, dtype=float), 0.5), "ap": None}


def pr_curve(y: np.ndarray, score: np.ndarray, n: int = CURVE_POINTS) -> list[dict]:
    """~n точек PR-кривой, равномерно по Recall; для каждого Recall — точка с наибольшим порогом."""
    p, r, t = precision_recall_curve(y, score)
    p, r = p[:-1], r[:-1]  # последняя точка (P = 1, R = 0) без порога
    picked = set()
    for x in np.linspace(r.min(), r.max(), n):
        near = np.flatnonzero(np.isclose(np.abs(r - x), np.abs(r - x).min()))
        picked.add(int(near.max()))
    return [
        {"threshold": _r(t[i]), "precision": _r(p[i]), "recall": _r(r[i])}
        for i in sorted(picked, key=lambda i: (r[i], -t[i]))
    ]


def quarter_role(quarter: str, periods: dict) -> str:
    start = str(pd.Period(quarter, "Q").start_time.date())
    for name in ("train", "early_stop", "threshold", "test"):
        lo, hi = periods[name]
        if lo <= start < hi:
            return name
    return "other"


def quarter_of(ts: pd.Series) -> pd.Series:
    return ts.dt.to_period("Q").astype(str)


def load_model(version: str, name: str) -> tuple[CatBoostClassifier, dict, str]:
    path = MODELS / version / f"{name}.cbm"
    model = CatBoostClassifier()
    model.load_model(str(path))
    cfg = json.loads((MODELS / version / "config.json").read_text(encoding="utf-8"))
    return model, cfg, hashlib.sha256(path.read_bytes()).hexdigest()


def write(version: str, sha: str, payload: dict) -> None:
    out = {"sha256": sha, "computed_at": datetime.now(UTC).isoformat(timespec="seconds"), **payload}
    (MODELS / version / "offline.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def test_period(cfg: dict) -> dict:
    lo, hi = cfg["periods"]["test"]
    return {"from": lo, "to": hi, "label": TEST_LABEL}


EPISODE_KINDS = ["ppr", "group", "solitary"]  # эпизоды тревог рабочей базы (docs/architecture/data.md)


def episode_kinds(db: str, since: str) -> pd.DataFrame:
    """Вид эпизода каждой дымовой тревоги базовой истории: channel_id, alarm_ts (наивное московское), kind."""
    from sqlalchemy import create_engine, text

    sql = text(
        "SELECT a.channel_id, a.ts AS alarm_ts, e.kind FROM alarms a JOIN alarm_episodes e ON e.id = a.episode_id "
        "WHERE a.run_id IS NULL AND a.ts >= :since"
    )
    with create_engine(db).connect() as c:
        k = pd.read_sql(sql, c, params={"since": since})
    k["alarm_ts"] = pd.to_datetime(k.alarm_ts, utc=True).dt.tz_convert(TZ).dt.tz_localize(None).astype("datetime64[ns]")
    return k


def smoke_decisions(archive: Path, cache: Path | None, db: str | None) -> tuple[pd.DataFrame, dict, str]:
    """Размеченные решения событийной модели 2022–2026 с оценкой, кварталом и видом эпизода тревоги (с 2025 года).

    Решения строит код обучения по архиву (ml.baseline.train), вид эпизода — из рабочей базы; у тревоги, не найденной
    в базе, kind = unknown. Без базы (`db=None`) вида эпизода нет — kind = None.
    """
    from ml.baseline.dataset import SMOKE, load_channels, load_events
    from ml.baseline.features import build
    from ml.baseline.train import GAPS, HISTORY_START

    model, cfg, sha = load_model("baseline-v0", "smoke_alarm")
    if cache and cache.exists():
        d = pd.read_parquet(cache)
    else:
        ch = load_channels(archive)
        ev = load_events(archive, ch[ch.sensor_type == SMOKE].channel_id, since=HISTORY_START)
        d = build(ev, ch, data_end="2026-07-01", gaps=GAPS)
        if cache:
            d.to_parquet(cache)
    d = d[d.target.notna()].copy()
    d["score"] = model.predict_proba(d[cfg["features"]])[:, 1]
    d["quarter"] = quarter_of(d.decision_time)
    if db is None:
        return d.assign(kind=None), cfg, sha
    d = d.merge(episode_kinds(db, "2025-01-01"), on=["channel_id", "alarm_ts"], how="left")
    d.loc[d.decision_time >= "2025-01-01", "kind"] = d["kind"].fillna("unknown")
    return d, cfg, sha


def smoke_alarm(archive: Path, cache: Path | None, db: str) -> dict:
    """Событийная модель: решения по дымовым тревогам из архива, как в ml.baseline.train.

    Вид эпизода тревоги (плановая проверка, группа, одиночная) — из рабочей базы: на проверке видно, сколько метрик
    держится на плановых проверках и сколько модель даёт вне их.
    """
    d, cfg, sha = smoke_decisions(archive, cache, db)
    thr, periods = cfg["threshold"], cfg["periods"]
    lo, hi = periods["test"]
    te = d[(d.decision_time >= lo) & (d.decision_time < hi)]
    lead = (te.lead_hours * 3600).to_numpy()
    seen = te.channel_id.isin(set(cfg["seen_channels"])).to_numpy()
    y, s = te.target.to_numpy(), te.score.to_numpy()
    rule = te.solitary_1h.fillna(0).to_numpy()
    kind = te["kind"].to_numpy()
    by_kind = [
        {"kind": k, **block(y[kind == k], s[kind == k], thr, lead[kind == k])}
        for k in [*EPISODE_KINDS, "unknown"]
        if (kind == k).any()
    ]
    return {
        "model": "smoke_alarm",
        "version": cfg["version"],
        "sha256": sha,
        "period": test_period(cfg),
        "threshold": thr,
        "test": block(y, s, thr, lead),
        "seen": block(y[seen], s[seen], thr, lead[seen]),
        "unseen": block(y[~seen], s[~seen], thr, lead[~seen]),
        "seen_rule": "каналы с решениями в обучении 2023–2024",
        "lead_time": lead_time(lead[(s >= thr) & (y == 1)]),
        "pr_curve": pr_curve(y, s),
        "by_quarter": [
            {"quarter": q, "role": quarter_role(q, periods), **block(g.target, g.score, thr, g.lead_hours * 3600)}
            for q, g in d[d.quarter.isin(QUARTERS)].groupby("quarter")
        ],
        "by_type": None,
        "by_episode_kind": by_kind,
        "rule": rule_block("одиночная тревога на объекте за час → неисправность", y, rule),
    }


def daily_decisions(db: str) -> tuple[pd.DataFrame, dict, str, set]:
    """Размеченные решения ежедневной модели — каждые сутки 2025–2026 года — с оценкой и кварталом.

    Ещё возвращает каналы с началами «Неисправен» в период обучения — «с историей» для разбивки seen / unseen.
    """
    from ml.baseline import train_daily as T

    model, cfg, sha = load_model("baseline-v0-daily", "daily")
    feats = cfg["features"]
    ch, daily_, eps = T.load(db)
    parts = []
    for q in QUARTERS:  # по кварталу — чтобы не держать в памяти 3 млн строк с признаками
        per = pd.Period(q, "Q")
        dates = pd.date_range(per.start_time, per.end_time.normalize(), freq="D")
        rows = T.rows(ch, daily_, eps, dates)
        if rows.empty:
            continue
        rows = rows.assign(score=model.predict_proba(rows[feats])[:, 1], quarter=q)
        keep = ["date", "quarter", "channel_id", "sensor_type", "target", "lead_days", "onsets_30d", "score"]
        parts.append(rows[keep])
    tr_lo, tr_hi = cfg["periods"]["train"]
    with_faults = set(eps.loc[(eps.start >= tr_lo) & (eps.start < tr_hi), "channel_id"])
    return pd.concat(parts, ignore_index=True), cfg, sha, with_faults


def daily(db: str) -> dict:
    """Ежедневная модель: каждые сутки 2025–2026 года из рабочей базы, как в ml.baseline.train_daily."""
    d, cfg, sha, with_faults = daily_decisions(db)
    thr, periods = cfg["threshold"], cfg["periods"]
    lo, hi = periods["test"]
    te = d[(d.date >= lo) & (d.date < hi)]
    lead = (te.lead_days * 86400).to_numpy()
    seen = te.channel_id.isin(with_faults).to_numpy()
    y, s = te.target.to_numpy(), te.score.to_numpy()
    by_type = []
    for st, g in te.groupby("sensor_type"):
        b = block(g.target, g.score, thr, g.lead_days * 86400)
        by_type.append({"sensor_type": st, "base_rate": _r(b["positives"] / b["decisions"]), **b})
    by_type.sort(key=lambda b: -b["positives"])
    return {
        "model": "daily",
        "version": cfg["version"],
        "sha256": sha,
        "period": {**test_period(cfg), "label": "1 января — 2 мая 2026"},  # у поздних суток горизонт не наблюдается
        "threshold": thr,
        "test": block(y, s, thr, lead),
        "seen": block(y[seen], s[seen], thr, lead[seen]),
        "unseen": block(y[~seen], s[~seen], thr, lead[~seen]),
        "seen_rule": "каналы с началами «Неисправен» в обучении 2022–2024",
        "lead_time": lead_time(lead[(s >= thr) & (y == 1)]),
        "pr_curve": pr_curve(y, s),
        "by_quarter": [
            {"quarter": q, "role": quarter_role(q, periods), **block(g.target, g.score, thr, g.lead_days * 86400)}
            for q, g in d.groupby("quarter")
        ],
        "by_type": by_type,
        "by_episode_kind": None,
        "rule": rule_block("у канала были начала «Неисправен» за 30 суток", y, (te.onsets_30d > 0).to_numpy()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["smoke_alarm", "daily"])
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    ap.add_argument("--cache", type=Path, help="parquet с решениями событийной модели (ml.baseline.train --cache)")
    ap.add_argument("--db", default="postgresql+psycopg://lct:lct@localhost:15432/lct")
    a = ap.parse_args()
    for name, version, fn in (
        ("smoke_alarm", "baseline-v0", lambda: smoke_alarm(a.archive, a.cache, a.db)),
        ("daily", "baseline-v0-daily", lambda: daily(a.db)),
    ):
        if a.only in (None, name):
            payload = fn()
            sha = payload.pop("sha256")
            write(version, sha, payload)
            t = payload["test"]
            print(name, "тест P", t["precision"], "R", t["recall"], "AP", t["ap"], "| точек", len(payload["pr_curve"]))
            for b in payload["by_quarter"]:
                n = f"{b['decisions']} / {b['positives']}"
                print(f"  {b['quarter']} {b['role']}: {n}  P {b['precision']} R {b['recall']} AP {b['ap']}")


if __name__ == "__main__":
    main()
