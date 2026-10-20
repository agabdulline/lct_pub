"""Решения ежедневной модели на истории — проверка I полугодия 2026 по таблице маршрутизации для «Вся история».

Тем же кодом, что сравнение и эталон #250 (`ml.daily_routing.compare`, `ml.daily_routing.reference`), с которым путь
сервиса совпадает (`ml/reports/daily-routing/REPORT.md` §6):
- каналы типов `baseline-v0` — решения v0 кодом её обучения по рабочей базе (`compare.labelled`);
- ИБП (`ups_ensemble`) и ручной извещатель (`manual_alarm`) — решения эталона: его код на журнале
  (`data/interim/daily-routing/reference/ups.parquet`, `manual_alarm.parquet`), рабочая оценка — как в сервисе;
- исход — продуктовая метка (`backend/app/domain/outcomes.py`): начало «Неисправен» в [T + 24 ч, T + 30 сут).

Сутки T — с 1 января по 2 мая 2026: горизонт закрывается до дня без журнала 1 июня, как на проверке #250. Строка — на
каждый канал типа с маршрутом: `scored` или `skipped` с причиной (`in_fault`, `no_coverage`); типы без модели не
пишутся.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.history.daily` (рабочая база с базовой историей — чтение,
эталон — `ml.daily_routing.reference ups`, `manual`) → `data/interim/history/daily.parquet`.
"""

import argparse
import json
import os

import numpy as np
import pandas as pd

from ml.daily_routing.compare import labelled
from ml.daily_routing.reference import OUT as REF_OUT
from ml.daily_routing.reference import ROOT, TEST

OUT = ROOT / "data" / "interim" / "history"
ROUTING = ROOT / "ml" / "models" / "daily_routing.json"
TZ = "Europe/Moscow"
REF_FILES = {"ups_ensemble": "ups.parquet", "manual_alarm": "manual_alarm.parquet"}


def rows(labels: pd.DataFrame, routes: dict[str, str], v0_threshold: float) -> pd.DataFrame:
    """Строка на канал маршрута и сутки: решение его модели и продуктовая метка."""
    labels = labels[labels.date.between(*TEST)]
    parts = []
    v0 = labels[labels.sensor_type.map(routes).eq("baseline-v0")]
    parts.append(
        pd.DataFrame(
            {
                "channel_id": v0.channel_id,
                "date": v0.date,
                "model_version": "baseline-v0",
                "status": np.where(v0.v0_decided, "scored", "skipped"),
                "skip_reason": np.where(v0.v0_decided, None, v0.status_v0),
                "score": v0.score_v0,
                "threshold": v0_threshold,
                "alert": v0.alert_v0.where(v0.v0_decided),
            }
        )
    )
    for model_id, name in REF_FILES.items():
        ref = pd.read_parquet(REF_OUT / name, columns=["channel_id", "date", "status", "score", "threshold", "alert"])
        ref = ref[ref.date.between(*TEST)]
        scored = ref.status.eq("scored") & ref.score.notna()
        parts.append(
            pd.DataFrame(
                {
                    "channel_id": ref.channel_id,
                    "date": ref.date,
                    "model_version": model_id,
                    "status": np.where(scored, "scored", "skipped"),
                    "skip_reason": np.where(scored, None, ref.status),
                    "score": ref.score.where(scored),
                    "threshold": ref.threshold,
                    "alert": ref.alert.where(scored),
                }
            )
        )
    out = pd.concat(parts, ignore_index=True)
    out = out.merge(labels[["channel_id", "date", "target", "lead_days"]], on=["channel_id", "date"], how="left")
    t = out.date.dt.tz_localize(TZ)
    out["model"] = "daily"
    out["decision_time"] = t
    out["horizon_start"] = t + pd.Timedelta(hours=24)
    out["horizon_end"] = t + pd.Timedelta(days=30)
    fault = out.target.eq(1)
    out["outcome"] = np.where(out.target.isna(), None, np.where(fault, "fault", "no_fault"))
    onset = t + pd.to_timedelta(out.lead_days.fillna(0), unit="D")
    out["outcome_at"] = onset.where(fault, out.horizon_end).where(out.target.notna())
    out["outcome_source"] = np.where(out.target.isna(), None, "history")
    out["alert"] = out.alert.astype("boolean")
    cols = ["model", "model_version", "channel_id", "decision_time", "horizon_start", "horizon_end", "status",
            "skip_reason", "score", "threshold", "alert", "outcome", "outcome_at", "outcome_source"]  # fmt: skip
    return out[cols].sort_values(["decision_time", "channel_id"], ignore_index=True)


def check(out: pd.DataFrame) -> dict:
    """P / R решений с исходом — смесь по маршрутам и каждая модель: сверка с `coverage.routing` в compare.json."""

    def pr(d: pd.DataFrame) -> dict:
        a, y = d.alert.astype(bool).to_numpy(), d.outcome.eq("fault").to_numpy()
        tp = int((a & y).sum())
        p = tp / a.sum() if a.sum() else None
        r = tp / y.sum() if y.sum() else None
        return {"decisions": len(d), "positives": int(y.sum()), "alerts": int(a.sum()), "tp": tp, "precision": p,
                "recall": r}  # fmt: skip

    done = out[out.status.eq("scored") & out.outcome.notna()]
    res = {"rows": len(out), "scored": int(out.status.eq("scored").sum()), "routing": pr(done)}
    res |= {v: pr(g) for v, g in done.groupby("model_version")}
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "postgresql+psycopg://lct:lct@localhost:15432/lct"))
    a = ap.parse_args()
    cfg = json.loads(ROUTING.read_text(encoding="utf-8"))
    routes = {r["sensor_type"]: r["model"] for r in cfg["routes"]}
    v0_cfg = json.loads((ROOT / "ml" / "models" / "baseline-v0-daily" / "config.json").read_text(encoding="utf-8"))
    labels, _ = labelled(a.db)
    out = rows(labels, routes, v0_cfg["threshold"])
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT / "daily.parquet", index=False)
    result = check(out)
    (OUT / "daily.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
