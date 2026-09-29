"""Сверка онлайн-пути отдельных моделей с эталоном (AGENTS.md §5): каждые сутки проверки I полугодия 2026.

Онлайн — `ml.daily_routing.online` по рабочей базе, базовая история (`run_id IS NULL`), всё строго до даты решения T:
тот же путь, что у ml-service. Эталон — `ml.daily_routing.reference` (его код по журналу). Сравниваются статус
решения, все признаки моделей побитово, оценки, порог и готовность извещателя, тревоги.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.daily_routing.parity` (~24 с на дату, 122 даты — ~50 мин;
в два процесса — `--part 0 2 --out parity-0.json`, `--part 1 2 --out parity-1.json`, затем
`--merge parity-0.json parity-1.json`). Итог — `ml/reports/daily-routing/parity.json`, `summary` — сводка.
"""

import argparse
import json
import os
import time

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype
from sqlalchemy import create_engine, text

from ml.daily_routing.online import Ensemble, ManualAlarm, SequenceCache, inputs
from ml.daily_routing.reference import OUT as REF_OUT
from ml.daily_routing.reference import ROOT, TEST

REPORT = ROOT / "ml" / "reports" / "daily-routing"
TZ = "Europe/Moscow"
SHOW = ("scored", "status_diff", "feature_diff", "score_max_abs_diff", "alert_diff")


def compare(online: pd.DataFrame, ref: pd.DataFrame, features: list[str], extra: list[str]) -> dict:
    """Строки одной даты: статус, признаки, оценки. Возвращает счётчики расхождений и худшие примеры."""
    m = online.merge(ref, on=["channel_id", "date"], how="outer", suffixes=("_o", "_r"), indicator=True)
    out = {
        "rows": len(m),
        "only_online": int(m._merge.eq("left_only").sum()),
        "only_ref": int(m._merge.eq("right_only").sum()),
    }
    both = m[m._merge.eq("both")]
    out["status_diff"] = int((both.status_o != both.status_r).sum())
    scored = both[both.status_r.eq("scored")]
    diffs = {}
    for c in features:
        a, b = scored[f"{c}_o"], scored[f"{c}_r"]
        if not (is_numeric_dtype(a) and is_numeric_dtype(b)):
            bad = (a.astype(str) != b.astype(str)).to_numpy()
        else:
            x, y = a.to_numpy(dtype=float), b.to_numpy(dtype=float)
            bad = ~((x == y) | (np.isnan(x) & np.isnan(y)))
        if bad.any():
            diffs[c] = int(bad.sum())
    out["feature_diff"] = diffs
    for c in extra:
        x, y = scored[f"{c}_o"].to_numpy(dtype=float), scored[f"{c}_r"].to_numpy(dtype=float)
        d = np.abs(x - y)
        d = d[~(np.isnan(x) & np.isnan(y))]
        out[f"{c}_max_abs_diff"] = float(np.nanmax(d)) if len(d) else 0.0
        out[f"{c}_exact"] = int((d == 0).sum())
    out["alert_diff"] = int((both.alert_o.fillna(False).astype(bool) != both.alert_r.fillna(False).astype(bool)).sum())
    out["scored"] = len(scored)
    return out


def summarize(result: dict) -> dict:
    """Итог по всем датам: решения, расхождения статуса, признаков (где и сколько), оценок и тревог."""
    out = {}
    for model in ("ups", "manual_alarm"):
        rows = result[model]
        features: dict[str, int] = {}
        for r in rows:
            for f, n in r["feature_diff"].items():
                features[f] = features.get(f, 0) + n
        out[model] = {
            "dates": len(rows),
            "decisions": sum(r["scored"] for r in rows),
            "status_diff": sum(r["status_diff"] for r in rows),
            "rows_only_online": sum(r["only_online"] for r in rows),
            "rows_only_reference": sum(r["only_ref"] for r in rows),
            "alert_diff": sum(r["alert_diff"] for r in rows),
            "score_exact": sum(r["score_exact"] for r in rows),
            "score_max_abs_diff": max(r["score_max_abs_diff"] for r in rows),
            "dates_with_feature_diff": sum(1 for r in rows if r["feature_diff"]),
            "feature_diff_cells": features,
        }
    out["manual_alarm"]["threshold_max_abs_diff"] = max(r["threshold_max_abs_diff"] for r in result["manual_alarm"])
    out["months_equal"] = all(
        m["online"]["threshold"] == m["reference"]["threshold"] and m["online"]["ready"] == m["reference"]["ready"]
        for m in result["months"].values()
    )
    seconds = [d["total_s"] for d in result["days"]]
    out["seconds_per_date"] = {"mean": round(sum(seconds) / len(seconds), 1), "max": max(seconds)}
    return out


def merge(parts: list[str]) -> None:
    """Части параллельного запуска (`--part K N --out parity-K.json`) → parity.json с итогом."""
    result = {"days": [], "ups": [], "manual_alarm": [], "months": {}}
    for name in parts:
        r = json.loads((REPORT / name).read_text(encoding="utf-8"))
        for k in ("days", "ups", "manual_alarm"):
            result[k] += r[k]
        result["months"].update(r["months"])
        (REPORT / name).unlink()
    for k in ("days", "ups", "manual_alarm"):
        result[k].sort(key=lambda r: r["date"])
    result = {"summary": summarize(result), **result}
    (REPORT / "parity.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "postgresql+psycopg://lct:lct@localhost:15432/lct"))
    ap.add_argument("--every", type=int, default=1, help="каждые N суток проверки")
    ap.add_argument("--days", nargs="*", help="только эти даты")
    ap.add_argument("--part", type=int, nargs=2, metavar=("K", "N"), help="K-я из N частей дат")
    ap.add_argument("--out", default="parity.json")
    ap.add_argument("--merge", nargs="*", help="собрать части parity-K.json в parity.json")
    a = ap.parse_args()
    if a.merge:
        merge(a.merge)
        return
    engine = create_engine(a.db)
    ups, manual = Ensemble(), ManualAlarm()
    ref_u = pd.read_parquet(REF_OUT / "ups.parquet")
    ref_m = pd.read_parquet(REF_OUT / "manual_alarm.parquet")
    feats_u = list(dict.fromkeys(f for _, cfg, _ in ups.parts for f in cfg["features"]))
    score_u = ["score", *[f"score_{cfg['version']}" for _, cfg, _ in ups.parts]]
    with engine.connect() as c:
        rows = c.execute(text("SELECT DISTINCT date FROM channel_daily WHERE run_id IS NULL"))
        observed = {pd.Timestamp(r[0]) for r in rows}
    days = [pd.Timestamp(d) for d in a.days] if a.days else list(pd.date_range(*TEST))[:: a.every]
    if a.part:
        days = days[a.part[0] :: a.part[1]]
    result = {"days": [], "ups": [], "manual_alarm": [], "months": {}}
    cache = SequenceCache()  # как в сервисе: события неисправности дочитываются от даты к дате
    for day in days:
        tick = time.perf_counter()
        with engine.connect() as c:
            x = inputs(c, None, day, [ups.type, manual.type], observed, TZ, cache)
        t_in = time.perf_counter() - tick
        u = ups.score(x)
        mm = manual.score(x)
        t_all = time.perf_counter() - tick
        ru = ref_u[ref_u.date.eq(day)]
        rm = ref_m[ref_m.date.eq(day)]
        cu = compare(u, ru[["channel_id", "date", "status", "alert", *feats_u, *score_u]], feats_u, score_u)
        cm = compare(
            mm,
            rm[["channel_id", "date", "status", "alert", "threshold", *manual.config["features"], "score"]],
            manual.config["features"],
            ["score", "threshold"],
        )
        month = manual.month(x, None)
        ref_month = rm[["threshold", "ready", "matured_positives"]].iloc[0].to_dict() if len(rm) else {}
        ref_month = {k: (bool(v) if k == "ready" else float(v)) for k, v in ref_month.items()}
        result["months"][month["month"]] = {"online": month, "reference": ref_month}
        result["days"].append({"date": str(day.date()), "inputs_s": round(t_in, 1), "total_s": round(t_all, 1)})
        result["ups"].append({"date": str(day.date()), **cu})
        result["manual_alarm"].append({"date": str(day.date()), **cm})
        print(
            json.dumps(
                {
                    "date": str(day.date()),
                    "s": round(t_all, 1),
                    "ups": {k: cu[k] for k in SHOW},
                    "manual": {k: cm[k] for k in SHOW},
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    REPORT.mkdir(parents=True, exist_ok=True)
    if a.out == "parity.json":
        result = {"summary": summarize(result), **result}
    (REPORT / a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
