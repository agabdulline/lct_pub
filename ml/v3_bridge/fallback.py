"""Связка событийных моделей «v3, где она оценила; где пропустила — v0»: офлайн на решениях I полугодия 2026.

Решения — те же, что в сравнении v0 и v3 (`ml.v3_bridge.compare`, `ml/reports/compare-v0-v3/`): v0 — её код обучения
по архиву, v3 — их код целиком по архиву; каждая — на своём рабочем пороге. Все решения v3 на проверке есть у v0, так
что связка — это решения v0, где у v3 решения нет, и решения v3 на остальных. Пропуск модели при неисправности — пропуск
(FN): «пропуск» v3 в сервисе означает, что тревоги по ней не будет. Интервалы — 90% парного кластерного бутстрапа по
объекто-суткам, как в сравнении.

Два множества решений:
- «полугодие» — общие решения и неделя после пропуска журнала 1.06 (правило v3 `coverage_gap`), как раздел 5
  сравнения;
- «все решения v0» — плюс решения 29–30.06: окно исхода v3 в период её проверки не помещается, в сервисе она их
  оценила бы — это граница периода, а не пропуск модели.

Вид эпизода тревоги не нужен — базы не требует. Запуск из ml-service:
`PYTHONPATH=.. uv run python -m ml.v3_bridge.fallback`. Кэши — как у `ml.v3_bridge.compare` в --work.

Итог дописывается полем `fallback` в `ml/models/compare_smoke_alarm.json` (GET /v1/models/compare): та же форма, что
`fallback.json`, числа округлены. `--only-compare` — только это, из готового fallback.json, без пересчёта.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ml.v3_bridge import compare
from ml.v3_bridge import offline as v3off

OUT = compare.OUT
POLICIES = ("v0", "v3", "combo")
GAP = ("2026-06-02", "2026-06-09")  # неделя после дня без журнала 1.06: v3 молчит по правилу покрытия


def universe(both: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    """Все решения v0 на проверке с оценкой v3, где она есть, и причиной, почему её нет."""
    t0 = ctx["t0"][["channel_id", "decision_time", "object_id", "target", "score", "lead_hours"]]
    u = t0.rename(columns={"score": "score_v0"}).merge(
        both[["channel_id", "decision_time", "score_v3"]], on=["channel_id", "decision_time"], how="left"
    )
    assert u.score_v3.notna().sum() == len(both), "решение v3 без решения v0 на проверке"
    missing = u.score_v3.isna()
    in_gap = u.decision_time.between(*GAP)
    boundary = u.decision_time + pd.Timedelta(days=1) > pd.Timestamp(v3off.LABEL_END)
    u["v3_missing"] = np.select(
        [~missing, in_gap, boundary], [None, "coverage_gap", "period_boundary"], default="other"
    )
    u["cluster"] = u.object_id.astype(str) + "_" + u.decision_time.dt.date.astype(str)
    return u


def alerts(u: pd.DataFrame, thr: dict) -> dict[str, np.ndarray]:
    """Тревога каждой политики; молчание — нет тревоги."""
    a0 = u.score_v0.to_numpy() >= thr[compare.V0]
    has3 = u.score_v3.notna().to_numpy()
    a3 = has3 & (u.score_v3.fillna(-1).to_numpy() >= thr[compare.V3])
    return {"v0": a0, "v3": a3, "combo": np.where(has3, a3, a0)}


def decided(u: pd.DataFrame) -> dict[str, np.ndarray]:
    has3 = u.score_v3.notna().to_numpy()
    return {"v0": np.ones(len(u), bool), "v3": has3, "combo": np.ones(len(u), bool)}


def point(y: np.ndarray, a: np.ndarray, d: np.ndarray, lead: np.ndarray) -> dict:
    tp, fp, fn = int((a & y).sum()), int((a & ~y).sum()), int((~a & y).sum())
    lead_tp = lead[a & y & ~np.isnan(lead)] * 60
    return {
        "decisions": int(d.sum()),
        "silent": int((~d).sum()),
        "positives": int(y.sum()),
        "alerts": int(a.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "fn_silent": int((~d & y).sum()),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "lead_minutes": {
            "p10": float(np.quantile(lead_tp, 0.1)),
            "p50": float(np.median(lead_tp)),
            "p90": float(np.quantile(lead_tp, 0.9)),
        }
        if len(lead_tp)
        else None,
    }


def evaluate(u: pd.DataFrame, thr: dict) -> dict:
    """Точки и 90% интервалы P, R трёх политик и разностей связки с каждой моделью — на одних выборках."""
    y = u.target.to_numpy() == 1
    a, d, lead = alerts(u, thr), decided(u), u.lead_hours.to_numpy(dtype=float)
    out = {p: point(y, a[p], d[p], lead) for p in POLICIES}
    groups = u.groupby("cluster").indices
    keys = list(groups)
    rng = np.random.default_rng(compare.SEED)
    boot = {p: {"P": [], "R": []} for p in POLICIES}
    for _ in range(compare.BOOT):
        idx = np.concatenate([groups[keys[k]] for k in rng.integers(0, len(keys), len(keys))])
        yb = y[idx]
        for p in POLICIES:
            ab = a[p][idx]
            tp = (ab & yb).sum()
            boot[p]["P"].append(tp / ab.sum() if ab.sum() else np.nan)
            boot[p]["R"].append(tp / yb.sum() if yb.sum() else np.nan)
    q = [0.05, 0.95]
    for p in POLICIES:
        out[p]["ci90"] = {m: list(np.nanquantile(boot[p][m], q)) for m in ("P", "R")}
    for other in ("v0", "v3"):
        out[f"combo_minus_{other}"] = {
            m: {
                "value": out["combo"][key] - out[other][key],
                "ci90": list(np.nanquantile(np.array(boot["combo"][m]) - np.array(boot[other][m]), q)),
            }
            for m, key in (("P", "precision"), ("R", "recall"))
        }
    out["clusters"] = {
        "object_days": len(keys),
        "with_faults": int(u[u.target == 1].cluster.nunique()),
        "boot": compare.BOOT,
        "seed": compare.SEED,
    }
    return out


def fallback_part(u: pd.DataFrame, thr: dict) -> dict:
    """Решения, где решала v0 за v3: сколько, почему v3 молчит, как на них работает v0."""
    y, a0 = u.target.to_numpy() == 1, u.score_v0.to_numpy() >= thr[compare.V0]
    lead = u.lead_hours.to_numpy(dtype=float)
    out = {}
    for reason in ("coverage_gap", "period_boundary", "other"):
        m = (u.v3_missing == reason).to_numpy()
        if m.any():
            out[reason] = point(y[m], a0[m], np.ones(m.sum(), bool), lead[m])
    m = u.score_v3.isna().to_numpy()
    out["all"] = point(y[m], a0[m], np.ones(m.sum(), bool), lead[m])
    return out


def _rounded(x):
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v) for v in x]
    return round(x, 4) if isinstance(x, float) else x


def enrich_compare(result: dict, path: Path = v3off.COMPARE) -> dict:
    """Поле `fallback` в файле сравнения версий — для `/v1/models/compare` и страницы «Модели»."""
    c = json.loads(path.read_text(encoding="utf-8"))
    c["fallback"] = _rounded(result)
    path.write_text(json.dumps(c, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, default=v3off.ROOT / "data" / "interim" / "v3-check")
    ap.add_argument("--archive", type=Path, default=v3off.ROOT / "data" / "archive")
    ap.add_argument("--raw", type=Path, default=v3off.ROOT / "data" / "raw" / "справочник_каналов_датчиков.csv")
    ap.add_argument("--only-compare", action="store_true", help="только поле fallback в compare_smoke_alarm.json")
    args = ap.parse_args()
    if args.only_compare:
        enrich_compare(json.loads((OUT / "fallback.json").read_text(encoding="utf-8")))
        return
    both, thr, ctx = compare.decisions(args.work, None, args.archive, args.raw)
    u = universe(both, ctx)
    half = u[u.v3_missing.fillna("") != "period_boundary"]
    result = {
        "thresholds": {"v0": thr[compare.V0], "v3": thr[compare.V3]},
        "requirement": {"precision": compare.REQ_P, "recall": compare.REQ_R},
        "half_year": evaluate(half, thr),
        "all_v0_decisions": evaluate(u, thr),
        "common": evaluate(u[u.score_v3.notna()], thr),
        "fallback": fallback_part(u, thr),
        "v3_missing": {k: int(v) for k, v in u.v3_missing.fillna("decided").value_counts().items()},
    }
    (OUT / "fallback.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    enrich_compare(result)
    for name in ("half_year", "all_v0_decisions"):
        r = result[name]
        print(name, {p: (r[p]["decisions"], r[p]["positives"], r[p]["alerts"], r[p]["precision"], r[p]["recall"])
                     for p in POLICIES})  # fmt: skip


if __name__ == "__main__":
    main()
