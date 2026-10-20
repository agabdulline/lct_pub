"""Данные модели «свой или нужна реакция» при открытии под охраной — точки решения и метка движком правила.

Точка решения — то, где правило «вскрытие под охраной» (`backend/app/domain/access.py`, docs/architecture/access.md
§3) заводит кандидата: первое открытие двери или люка подобъекта на охране меньше 7 суток, не волна переопроса.
Метка — исход кандидата у того же движка: «снято после открытия» (`dismissed`, охрану сняли в [T, T + 5 мин]) — 0,
инцидент — 1, «нужна реакция». Кандидат на конце журнала без исхода — без метки. Движок и чтение архива — те же, что у
базовой истории (`app.access_history`): правило в ML не переписывается.

Кроме точек — события, из которых строятся признаки (`ml.access.features`): переходы и исправность охраны, открытия и
закрытия дверей и люков.

Запуск из backend (его окружение и движок): `cd backend && PYTHONPATH=..:. uv run python -m ml.access.dataset`.
Результат — `data/interim/access/`: points, guard, openers (parquet).
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from app import access_history as ah
from app.archive import connect
from app.domain.access import MSK, rules

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "interim" / "access"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    ap.add_argument("--memory", default="3GB")
    a = ap.parse_args()
    tick = time.perf_counter()
    r = rules()
    con = connect(a.memory, temp_dir=ah.TEMP_DIR)
    subs = ah._catalog(con, a.archive, r)
    until = ah._journal_until(con, a.archive, subs)
    points, guard, openers = [], [], []
    for sub in subs.values():
        events, _, result = ah._sub_history(con, a.archive, sub, until, r)
        for e in events:
            row = {"sub_object_id": sub.id, "channel_id": e.channel_id, "ts": e.ts, "value": e.value}
            (guard if e.channel_id == sub.guard_channel_id else openers).append(row | {"is_alarm": e.is_alarm})
        for i in result.incidents:
            first = i.openings[0]
            points.append(
                {
                    "sub_object_id": sub.id,
                    "object_id": sub.object_id,
                    "guard_channel_id": sub.guard_channel_id,
                    "start_ts": i.start_ts,
                    "channel_id": first.channel_id,
                    "is_alarm": first.is_alarm,
                    "hatch": sub.openers[first.channel_id][1],
                    "armed_since": i.armed_since,
                    "guard_faulty": i.guard_faulty,
                    "off_hours": i.off_hours,
                    "status": i.status,
                    "disarmed_ts": i.disarmed_ts,
                    "label": None if i.status == "candidate" else int(i.status != "dismissed"),
                }
            )
        print(sub.id, len(events), len(result.incidents), round(time.perf_counter() - tick), flush=True)
    pts = pd.DataFrame(points).sort_values(["start_ts", "sub_object_id"], ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    pts.to_parquet(OUT / "points.parquet", index=False)
    pd.DataFrame(guard).to_parquet(OUT / "guard.parquet", index=False)
    pd.DataFrame(openers).to_parquet(OUT / "openers.parquet", index=False)
    years = pts.start_ts.dt.tz_convert(MSK).dt.year
    report = {
        "until": str(until),
        "points": len(pts),
        "by_year": {
            int(y): {"points": int((years == y).sum()), "positive": int(pts.label[years == y].sum())}
            for y in sorted(years.unique())
        },  # fmt: skip
        "seconds": round(time.perf_counter() - tick),
    }
    (OUT / "dataset.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
