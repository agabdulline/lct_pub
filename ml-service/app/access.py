"""Модель доступа «свой или нужна реакция» (`ml/access`, lct-8f): входы из рабочей базы и оценка.

Точку решения — первое открытие кандидата правила «вскрытие под охраной» — присылает backend (docs/architecture/
access.md §4): подобъект, канал и флаг первого открытия, люк ли, с какого момента на охране, исправна ли охрана, канал
охраны и двери-люки подобъекта. История — представление прогона, окнами `ml.access.features.LOOKBACK`:
- прошлые кандидаты правила (`access_incidents` базы до старта прогона и самого прогона). Метка — исход правила:
  0, если охрану сняли в [начало, начало + 5 мин], иначе 1. Статус инцидента для этого не годится: инцидент, который
  модель открыла сразу, при снятии за 5 мин закрывается, а не становится «снято после открытия»;
- снятия охраны подобъекта и открытия его дверей и люков — `events`.
Признаки и модель — тот же код, что при обучении (`ml.access.features`, D1).
"""

import pandas as pd
from sqlalchemy import text

from ml.access.features import DISARMED, GRACE, LOOKBACK, OPENED, History, build

_START = "SELECT sim_start FROM runs WHERE id = :run"

# база — до старта прогона (у самой базы — вся), прогон — его события; постоянные границы: чанки отсекаются
_EVENTS = """
SELECT e.channel_id, e.ts, e.value_raw AS value FROM events e
WHERE e.channel_id = ANY(:channels) AND e.value_raw = :value AND e.ts >= :lo AND e.ts < :hi
  AND ((e.run_id IS NULL AND e.ts < :base_until) OR (CAST(:run AS int) IS NOT NULL AND e.run_id = :run))
"""

_PAST = """
SELECT i.sub_object_id, i.start_ts,
    (SELECT o.channel_id FROM access_incident_openings o WHERE o.incident_id = i.id
     ORDER BY o.ts, o.channel_id LIMIT 1) AS channel_id,
    CASE WHEN i.disarmed_ts IS NOT NULL AND i.disarmed_ts <= i.start_ts + :grace THEN 0 ELSE 1 END AS label
FROM access_incidents i
WHERE i.sub_object_id = ANY(:subs) AND i.status IN ('dismissed', 'open', 'closed')
  AND i.start_ts >= :lo AND i.start_ts < :hi
  AND ((i.run_id IS NULL AND i.start_ts < :base_until) OR (CAST(:run AS int) IS NOT NULL AND i.run_id = :run))
"""


def inputs(conn, run_id: int | None, points: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Прошлые кандидаты, снятия охраны и открытия подобъектов точек — окнами LOOKBACK до самой поздней точки."""
    hi = points["start_ts"].max()
    first = points["start_ts"].min()
    start = conn.execute(text(_START), {"run": run_id}).scalar() if run_id is not None else None
    base_until = start if start is not None else hi + pd.Timedelta(seconds=1)
    subs = sorted(set(points["sub_object_id"]))

    def events(kind: str, value: str) -> pd.DataFrame:
        sub_of = (
            {int(p.guard_channel_id): int(p.sub_object_id) for p in points.itertuples()}
            if kind == "guard"
            else {int(c): int(p.sub_object_id) for p in points.itertuples() for c in p.opener_ids}
        )
        params = {
            "channels": sorted(sub_of),
            "value": value,
            "lo": first - LOOKBACK[kind],
            "hi": hi,
            "base_until": base_until,
            "run": run_id,
        }
        frame = pd.read_sql(text(_EVENTS), conn, params=params)
        frame.insert(0, "sub_object_id", frame["channel_id"].map(sub_of))
        return frame

    guard = events("guard", DISARMED)
    openers = events("openers", OPENED)
    past = pd.read_sql(
        text(_PAST),
        conn,
        params={
            "subs": subs,
            "grace": GRACE.to_pytimedelta(),
            "lo": first - LOOKBACK["past"] - GRACE,
            "hi": hi,
            "base_until": base_until,
            "run": run_id,
        },
    )
    for frame, col in ((guard, "ts"), (openers, "ts"), (past, "start_ts")):
        frame[col] = pd.to_datetime(frame[col], utc=True)
    return past, guard, openers


def features(points: pd.DataFrame, past: pd.DataFrame, guard: pd.DataFrame, openers: pd.DataFrame) -> pd.DataFrame:
    """Признаки точек кодом обучения: `History` по трём кадрам, `build` — по точкам."""
    return build(points, History(past, guard, openers))


def score(model, x: pd.DataFrame) -> list[float]:
    """Вероятность «нужна реакция» в порядке признаков модели (config.features)."""
    return model.model.predict_proba(x[model.config["features"]])[:, 1].tolist()
