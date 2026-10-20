"""Решения событийной модели на истории — проверка I полугодия 2026 для режима «Вся история».

Решения — те же, что у связки «v3, где оценила, иначе v0» (#241, `ml.v3_bridge.fallback`, отчёт
`ml/reports/compare-v0-v3/FALLBACK.md`): v0 — её код обучения по архиву, v3 — их код целиком по архиву; путь сервиса с
ними совпадает побитово (`ml/reports/v3/SERVICE.md`, `ml.baseline.parity_run`). Множество — «полугодие»: 7 563
решения, общие у v0 и v3, и неделя 2–8 июня после дня без журнала (у v3 там `coverage_gap`). Решения 29–30 июня не
пишутся: горизонт v3 за краем её периода проверки.

Строки — как у живого прогона (`backend/app/domain/triage.py`): на каждую тревогу по строке `baseline-v0` и `v3`,
решение в момент тревоги + 1 с, горизонт 24 ч, исход — по журналу. `decides` — строка, чьё решение даёт вердикт
связки: v3, где оценила, иначе v0; backend ставит на неё `alarms.prediction_id`.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.history.smoke` → `data/interim/history/smoke_alarm.parquet`.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ml.v3_bridge import compare, fallback
from ml.v3_bridge import offline as v3off

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "interim" / "history"  # loader на проде видит data/interim
TZ = "Europe/Moscow"
HORIZON = pd.Timedelta(hours=24)


def rows(u: pd.DataFrame, t0: pd.DataFrame, thr: dict) -> pd.DataFrame:
    """Две строки на тревогу «полугодия» — v0 и v3 — в раскладке `predictions`."""
    u = u[u.v3_missing.fillna("") != "period_boundary"].merge(
        t0[["channel_id", "decision_time", "alarm_ts"]], on=["channel_id", "decision_time"], how="left"
    )
    assert u.alarm_ts.notna().all() and len(u) == 7563, len(u)
    loc = lambda s: s.dt.tz_localize(TZ)  # noqa: E731 — время архива — наивное московское
    base = pd.DataFrame(
        {
            "model": "smoke_alarm",
            "channel_id": u.channel_id.astype("int64"),
            "alarm_ts": loc(u.alarm_ts),
            "decision_time": loc(u.decision_time),
        }
    )
    base["horizon_start"] = base.decision_time
    base["horizon_end"] = base.decision_time + HORIZON
    fault = u.target.to_numpy() == 1
    onset = base.decision_time + pd.to_timedelta(u.lead_hours.fillna(0).to_numpy(), unit="h")
    base["outcome"] = np.where(fault, "fault", "no_fault")
    base["outcome_at"] = onset.where(fault, base.horizon_end)
    base["outcome_source"] = "history"
    has3 = u.score_v3.notna().to_numpy()
    v0 = base.assign(model_version=compare.V0, status="scored", skip_reason=None, score=u.score_v0.to_numpy(),
                     threshold=thr[compare.V0], decides=~has3)  # fmt: skip
    v3 = base.assign(
        model_version=compare.V3,
        status=np.where(has3, "scored", "skipped"),
        skip_reason=np.where(has3, None, u.v3_missing.to_numpy()),
        score=u.score_v3.to_numpy(),
        threshold=thr[compare.V3],
        decides=has3,
    )
    out = pd.concat([v0, v3], ignore_index=True)
    out["alert"] = np.where(out.status.eq("scored"), out.score >= out.threshold, None)
    return out.sort_values(["decision_time", "channel_id", "model_version"], ignore_index=True)


def check(out: pd.DataFrame) -> dict:
    """P / R каждой версии и связки по записанным строкам — должны совпасть с `fallback.json` (половина года)."""

    def pr(d: pd.DataFrame) -> dict:
        a, y = d.alert.astype(bool).to_numpy(), d.outcome.eq("fault").to_numpy()
        tp = int((a & y).sum())
        return {"decisions": len(d), "positives": int(y.sum()), "alerts": int(a.sum()), "tp": tp,
                "precision": tp / a.sum(), "recall": tp / y.sum()}  # fmt: skip

    scored = out[out.status.eq("scored")]
    return {
        "baseline-v0": pr(scored[scored.model_version.eq(compare.V0)]),
        "v3": pr(scored[scored.model_version.eq(compare.V3)]),
        "v3>baseline-v0": pr(out[out.decides]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", type=Path, default=v3off.ROOT / "data" / "interim" / "v3-check")
    a = ap.parse_args()
    both, thr, ctx = compare.decisions(a.work, None, v3off.ROOT / "data" / "archive",
                                       v3off.ROOT / "data" / "raw" / "справочник_каналов_датчиков.csv")  # fmt: skip
    out = rows(fallback.universe(both, ctx), ctx["t0"], thr)
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT / "smoke_alarm.parquet", index=False)
    result = check(out)
    (OUT / "smoke_alarm.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
