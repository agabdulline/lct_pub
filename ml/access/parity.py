"""Сверка пути сервиса модели доступа с офлайн-эталоном lct-8f (docs/architecture/ml-service.md §6).

Эталон — `ml/models/access-open/test_2026.csv`: точки проверки, их 20 признаков и оценка модели (`ml.access.train`,
архив журнала). Путь сервиса — точки решения так, как их присылает backend (кандидаты правила из `access_incidents`
базы: подобъект, первое открытие и его флаг, люк ли, начало охраны, исправность, канал охраны, двери и люки
подобъекта), история — из рабочей базы (`app.access.inputs`), признаки и оценка — `POST /v1/access/score`. Совпадать
должны набор точек, признаки (побитово, NaN = NaN) и оценки (допуск 1e-12).

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.access.parity`. Код выхода 1 — расхождение.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sqlalchemy import text

from ml.access.features import FEATURES

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "ml" / "models" / "access-open" / "access_open.cbm"
REFERENCE = MODEL.parent / "test_2026.csv"
# backend/config/triage.yaml, раздел access: двери и люки подобъекта, из них люки
OPENING_TYPES = ("КД Дверь", "КД Люк", "9-секционный люк")
HATCH_TYPES = ("КД Люк", "9-секционный люк")


def reference() -> pd.DataFrame:
    ref = pd.read_csv(REFERENCE, float_precision="round_trip")  # %.17g — побитово, а не быстрым парсером
    ref["start_ts"] = pd.to_datetime(ref["start_ts"], utc=True)
    return ref


_POINTS = """
SELECT i.sub_object_id, i.start_ts, i.armed_since, i.guard_faulty, i.guard_channel_id, f.channel_id, f.is_alarm,
    c.sensor_type = ANY(:hatches) AS hatch,
    (SELECT array_agg(o.id ORDER BY o.id) FROM channels o
     WHERE o.object_id = i.sub_object_id AND o.sensor_type = ANY(:types)) AS opener_ids
FROM access_incidents i
CROSS JOIN LATERAL (SELECT channel_id, is_alarm FROM access_incident_openings
                    WHERE incident_id = i.id ORDER BY ts, channel_id LIMIT 1) f
JOIN channels c ON c.id = f.channel_id
WHERE i.run_id IS NULL AND i.status IN ('dismissed', 'open', 'closed') AND i.start_ts >= :lo AND i.start_ts < :hi
ORDER BY i.start_ts, i.sub_object_id
"""


def served(lo: pd.Timestamp, hi: pd.Timestamp) -> pd.DataFrame:
    """Точки периода из кандидатов правила базы и их признаки путём сервиса — по точке за запрос, как у backend."""
    from app import access, history

    with history.engine().connect() as conn:
        points = pd.read_sql(
            text(_POINTS), conn, params={"lo": lo, "hi": hi, "types": list(OPENING_TYPES), "hatches": list(HATCH_TYPES)}
        )
        for col in ("start_ts", "armed_since"):
            points[col] = pd.to_datetime(points[col], utc=True)
        rows = []
        for k in range(len(points)):
            one = points.iloc[[k]].reset_index(drop=True)
            past, guard, opens = access.inputs(conn, None, one)
            rows.append(access.features(one, past, guard, opens))
    return pd.concat([points[["sub_object_id", "channel_id", "start_ts"]], pd.concat(rows, ignore_index=True)], axis=1)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    started = time.perf_counter()
    ref = reference()
    lo, hi = ref.start_ts.min(), ref.start_ts.max() + pd.Timedelta(seconds=1)
    got = served(lo, hi)
    key = ["sub_object_id", "channel_id", "start_ts"]
    both = ref.merge(got, on=key, how="outer", suffixes=("_ref", "_got"), indicator=True)
    lost = both[both._merge != "both"]
    both = both[both._merge == "both"].reset_index(drop=True)
    model = CatBoostClassifier()
    model.load_model(str(MODEL))
    got_x = both[[f"{f}_got" for f in FEATURES]].set_axis(FEATURES, axis=1)
    s_got = model.predict_proba(got_x)[:, 1]
    bad = []
    for f in FEATURES:
        x, y = both[f"{f}_ref"].to_numpy(float), both[f"{f}_got"].to_numpy(float)
        same = (x == y) | (np.isnan(x) & np.isnan(y))
        if not same.all():
            k = int(np.flatnonzero(~same)[0])
            bad.append((f, int((~same).sum()), both.loc[k, key].to_dict(), x[k], y[k]))
    diff = float(np.abs(both["score"].to_numpy() - s_got).max()) if len(both) else 0.0
    print(f"точек: эталон {len(ref)}, сервис {len(got)}, общих {len(both)}, без пары {len(lost)}")
    print(f"признаков {len(FEATURES)}; расходятся: {bad or 'нет'}")
    print(f"max |Δ оценки| = {diff:.3g}; {time.perf_counter() - started:.1f} с")
    return 1 if len(lost) or bad or diff > 1e-12 else 0


if __name__ == "__main__":
    sys.exit(main())
