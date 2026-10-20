"""Снимки настоящих ответов ml-service для контрактных тестов backend: backend/tests/fixtures/ml/.

Модели — настоящие из ml/models, история — маленькая синтетика вместо базы: снимки детерминированы и собираются без
базы и без data/. `elapsed_ms` в снимках — 0. Запуск после любой правки контракта или моделей:

    cd ml-service && PYTHONPATH=.. uv run python -m app.contract_fixtures

Файлы: `<имя>.json` — ответ как есть, `<имя>_request.json` — запрос, которым он получен. `models_compare.json` —
`GET /v1/models/compare?model=smoke_alarm`, `alarm_score_<версия>.json` — одна пачка тревог, оценённая каждой версией
событийной модели (теневой скоринг, 15.2), `daily_score.json` — ежедневный прогноз по типам (15.4),
`access_score.json` — модель доступа «свой или нужна реакция» (lct-8f).
"""

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from app import history
from app.main import app
from ml.external.azure_pdm.features import TELEMETRY
from ml.external.magdeburg.features import MEASURES
from ml.v3_bridge.alarm import AlarmRows

OUT = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "ml"
MAGDEBURG_NODES = [9010106, 9010107, 9010108, 9010109, 9010110, 9010111, 9010112, 9010113, 9010115]
T0 = pd.Timestamp("2023-07-13 08:00:00", tz="UTC")


def magdeburg_journal() -> tuple[pd.DataFrame, pd.DataFrame]:
    """9 узлов цеха: канал цели «node» и 13 сигналов раз в 10 с за 2 ч. Через 90 мин — горение по всему цеху, сильнее
    всего у узла 9010108: растут частицы, CO, CO₂, ЛОС, H₂ и температура, падает влажность."""
    rng = np.random.default_rng(7)
    level = {"CO2_Room": 450, "CO_Room": 0.5, "H2_Room": 1.0, "VOC_Room_RAW": 1.0, "PM05_Room": 80, "PM10_Room": 10,
             "PM25_Room": 1, "PM40_Room": 0.2, "PM100_Room": 0.05, "PM_Total_Room": 90, "PM_Room_Typical_Size": 0.5,
             "Temperature_Room": 25, "Humidity_Room": 50}  # fmt: skip
    fire = {"CO2_Room": 200, "CO_Room": 1.5, "H2_Room": 1.0, "VOC_Room_RAW": 3, "PM05_Room": 2500, "PM10_Room": 400,
            "PM25_Room": 10, "PM_Total_Room": 3000, "Temperature_Room": 2, "Humidity_Room": -1}  # fmt: skip
    rows = []
    for n in MAGDEBURG_NODES:
        base = 901_000_000 + (n - 9_010_100) * 100  # как в справочнике набора: узел 9010106 — каналы 9010006xx
        rows.append((base, n, "node"))
        rows += [(base + 1 + j, n, m) for j, m in enumerate(MEASURES)]
    channels = pd.DataFrame(rows, columns=["channel_id", "object_id", "measure"])
    times = pd.date_range(T0, T0 + pd.Timedelta(hours=2), freq="10s", inclusive="left")
    ramp = np.clip((times - (T0 + pd.Timedelta(minutes=90))).total_seconds() / 600, 0, 1)  # за 10 мин — до полной силы
    events = []
    for c, n, m in channels[channels.measure != "node"].itertuples(index=False):
        x = level[m] * (1 + rng.normal(0, 0.02, len(times))) + fire.get(m, 0) * ramp * (1.0 if n == 9010108 else 0.6)
        events.append(pd.DataFrame({"channel_id": c, "ts": times, "value": [f"{v:.4g}" for v in x]}))
    return pd.concat(events, ignore_index=True), channels


def vast_world() -> dict:
    """Датчики, прошлый проезд мотоцикла, классы и допуски: пост 1 за воротами — только для рейнджеров."""
    channels = pd.DataFrame(
        [(902001100, 9020011, "entrance0"), (902001101, 9020011, "entrance1"), (902001201, 9020012, "general-gate1")]
        + [(902001601, 9020016, "ranger-stop1")],
        columns=["channel_id", "object_id", "measure"],
    )
    past = pd.DataFrame(
        {"channel_id": [902001101, 902001100], "ts": pd.to_datetime(["2015-07-10 07:28:03", "2015-07-10 07:02:10"],
         utc=True), "value": ["20152810102803-808", "20150210100210-101"]}
    )  # fmt: skip
    permits = pd.DataFrame(
        {"credential_id": ["ranger-2P"], "object_id": [9020016], "valid_from": [None], "valid_to": [None]}
    )
    classes = {"20152810102803-808": "1", "20150210100210-101": "1"}
    return {"channels": channels, "past": past, "permits": permits, "classes": classes}


def pdm_world() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Три агрегата: у 101 — сутки с лишним телеметрии, ошибки и ТО; у 102 — телеметрия уходит от нормы и ошибки перед
    отказом; у 103 — телеметрии только 6 ч (short_history)."""
    units = pd.DataFrame({"object_id": [9040101, 9040102, 9040103], "model": ["model3", "model4", "model1"],
                          "age_years": [18.0, 7.0, 2.0]})  # fmt: skip
    tags = ["machine", *TELEMETRY, "errors", "maint", "failures"]
    rows = [(904000000 + (u - 9040100) * 100 + k, u, t) for u in units.object_id for k, t in enumerate(tags)]
    channels = pd.DataFrame(rows, columns=["channel_id", "object_id", "measure"])
    ch = channels.set_index(["object_id", "measure"]).channel_id
    t0 = pd.Timestamp("2015-11-25 03:00", tz="UTC")
    rng = np.random.default_rng(3)
    normal = {"volt": 170, "rotate": 450, "pressure": 100, "vibration": 40}
    ev = []
    for u, hours in ((9040101, 30), (9040102, 30), (9040103, 6)):
        for h in range(30 - hours, 30):
            for t in TELEMETRY:
                x = normal[t] * (1.12 if u == 9040102 and h >= 6 and t == "volt" else 1) + rng.normal(0, 3)
                ev.append((ch[(u, t)], t0 + pd.Timedelta(hours=h), f"{x:.4f}"))
    ev += [(ch[(9040102, "errors")], t0 + pd.Timedelta(hours=7), "error1"),
           (ch[(9040101, "maint")], t0 - pd.Timedelta(days=12), "comp2")]  # fmt: skip
    return pd.DataFrame(ev, columns=["channel_id", "ts", "value"]), channels, units


SMOKE_BASE = pd.Timestamp("2025-03-03")  # понедельник


def smoke_object() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Объект 1 с четырьмя дымовыми каналами за 41 сутки, как tests/synthetic.py: каждый день «Норма»; тревога 11
    с неисправностью через час, повторная тревога 11, тревоги 12–14 почти разом, неисправность и тревога 12, ранняя
    тревога 14 — решения и пропуски по правилам допустимости."""
    channels = pd.DataFrame(
        {
            "channel_id": [11, 12, 13, 14],
            "object_id": [1] * 4,
            "system_type": ["Пожарная охрана"] * 4,
            "sensor_type": ["Датчик дыма"] * 4,
            "tag": ["1-1.1.1.1.", "1-1.1.1.2.", "1-1.1.1.3.", "1-1.1.2.1."],
            "name": ["ДД ПК10", "ДД ПК11", "ДД ПК12", "ДД ПК50"],
            "picket_number": [10, 11, 12, 50],
        }
    )

    def at(day: int, hh: int = 0, mm: int = 0) -> pd.Timestamp:
        return SMOKE_BASE + pd.Timedelta(days=day, hours=hh, minutes=mm)

    rows = [(c, at(d, 6), "Норма") for c in (11, 12, 13, 14) for d in range(0, 41)]
    smoke, fault = "Обнаружен дым", "Неисправен"
    rows += [(11, at(20, 10), smoke), (11, at(20, 10, 30), smoke), (11, at(20, 11), fault), (12, at(30, 9), smoke),
             (13, at(30, 9, 1), smoke), (14, at(30, 9, 2), smoke), (12, at(35, 12), fault), (12, at(35, 12, 30), smoke),
             (14, at(3, 10), smoke)]  # fmt: skip
    events = pd.DataFrame(rows, columns=["channel_id", "ts", "value"])
    events["event_id"] = range(len(events))
    return events.sort_values(["channel_id", "ts"]).reset_index(drop=True), channels


DAILY_DAY = pd.Timestamp("2026-03-02")
DAILY_REQUEST = {"run_id": 7, "date": "2026-03-02", "channel_ids": [1001, 1004, 1005, 1006, 1007, 99]}


def daily_world() -> tuple[tuple, object]:
    """Ежедневный прогноз по типам (15.4) на объекте 100 (родитель 10), как в тестовой базе backend: дым 1001 — v0,
    ИБП 1004 — ансамбль, извещатель 1005 — своя модель, 1006 в неисправности, тепловой 1007 — тип без модели, 99 —
    канала нет. 200 суток событий «Норма», у ИБП — эпизоды «Неисправен»."""
    from ml.daily_routing.online import Inputs
    from ml.each_channel.features import normalize_inputs

    types = {1001: "Датчик дыма", 1004: "ИБП", 1005: "Ручной извещатель", 1006: "ИБП", 1007: "Тепловой датчик"}
    raw = pd.DataFrame(
        {
            "ид_канала_данных": list(types),
            "ид_объект": [100] * len(types),
            "тип_датчика": list(types.values()),
            "тег_инженерной_системы": [f"15-11.1.{i}.1." for i in range(len(types))],
            "название_датчика": ["ДД ПК1", "ИБП 1", "ИПР ПК2", "ИБП 2", "ТД ПК3"],
        }
    )
    days = pd.date_range(DAILY_DAY - pd.Timedelta(days=200), DAILY_DAY - pd.Timedelta(days=1))
    n = {1004: 4, 1005: 2, 1006: 3}
    daily = pd.DataFrame(
        [
            {"sensor_id": c, "date": d, "n_events": k, "n_alarm": 0, "n_fault": int(c == 1004 and d.day == 5),
             "n_unknown": 0, "n_disabled": 0, "n_normal": k, "n_smoke": 0, "n_trigger": 0, "n_open": 0,
             "n_numeric": 0, "numeric_sum": 0.0, "numeric_sumsq": 0.0, "numeric_min": np.nan, "numeric_max": np.nan,
             "last_value": "Норма", "last_ts": d + pd.Timedelta(hours=6)}
            for c, k in n.items() for d in days
        ]
    )  # fmt: skip
    starts = [d + pd.Timedelta(hours=7) for d in days if d.day == 5]
    episodes = pd.DataFrame(
        {
            "sensor_id": [1004] * len(starts) + [1006],
            "start": starts + [DAILY_DAY - pd.Timedelta(days=3)],
            "end": [s + pd.Timedelta(hours=5) for s in starts] + [pd.NaT],
            "start_is_observed": True,
            "coverage_uncertain": False,
        }
    )
    objects = pd.DataFrame({"object_id": [1, 10, 100], "level": [1, 2, 3], "parent_id": [None, 1, 10],
                            "object_kind": ["district", "controlHouse", "controlHouse"]})  # fmt: skip
    channels, daily, episodes = normalize_inputs(raw, daily, episodes)
    x = Inputs(channels, daily, episodes, objects, DAILY_DAY, [1004, 1005, 1006])
    # как history.daily_inputs: каналы восьми типов v0 на объекте, в том числе ИБП и извещатель — их ведут другие модели
    v0 = pd.DataFrame({"channel_id": [1001, 1004, 1005, 1006], "object_id": [100] * 4,
                       "sensor_type": [types[c] for c in (1001, 1004, 1005, 1006)]})  # fmt: skip
    v0_daily = pd.DataFrame(
        [{"channel_id": 1001, "date": d.date(), "n_events": 3, "n_alarm": 0, "n_fault": 0, "n_num": 0, "mean": None,
          "max": None} for d in days[-60:]]
    )  # fmt: skip
    v0_eps = pd.DataFrame({"channel_id": pd.Series(dtype="int64"), "start": pd.Series(dtype="datetime64[ns]"),
                           "end": pd.Series(dtype="datetime64[ns]")})  # fmt: skip
    return (v0, v0_daily, v0_eps), x


ALARMS = [  # решение, повтор за сутки, неисправность за сутки, короткая история, канал вне справочника
    {"channel_id": 11, "ts": "2025-03-23T10:00:00+03:00"},
    {"channel_id": 11, "ts": "2025-03-23T10:30:00+03:00"},
    {"channel_id": 13, "ts": "2025-04-02T09:01:00+03:00"},
    {"channel_id": 12, "ts": "2025-04-07T12:30:00+03:00"},
    {"channel_id": 14, "ts": "2025-03-06T10:00:00+03:00"},
    {"channel_id": 99, "ts": "2025-04-02T09:00:00+03:00"},
]
# Модель доступа: две точки решения одного подобъекта — днём у «своей» двери и ночью у люка (access.md §4)
ACCESS_REQUEST = {
    "run_id": 7,
    "points": [
        {"sub_object_id": 500, "channel_id": 5001, "start_ts": "2026-03-02T10:05:00+03:00", "is_alarm": False,
         "hatch": False, "armed_since": "2026-03-02T09:40:00+03:00", "guard_faulty": False,
         "guard_channel_id": 5000, "opener_ids": [5001, 5002]},
        {"sub_object_id": 500, "channel_id": 5002, "start_ts": "2026-03-02T23:30:00+03:00", "is_alarm": True,
         "hatch": True, "armed_since": "2026-03-02T19:10:00+03:00", "guard_faulty": False,
         "guard_channel_id": 5000, "opener_ids": [5001, 5002]},
    ],
}  # fmt: skip


def access_world() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Месяц подобъекта 500: снятия охраны по будням в 9–10, открытия двери 5001 в рабочие часы, прошлые кандидаты —
    дверь: охрану сняли за 5 мин (0), люк ночью: не сняли (1)."""
    days = pd.date_range("2026-02-02", "2026-03-01", freq="B", tz="Europe/Moscow")
    guard = pd.DataFrame({"sub_object_id": 500, "channel_id": 5000, "ts": days + pd.Timedelta(hours=9, minutes=50),
                          "value": "Снято с охраны"})  # fmt: skip
    openers = pd.DataFrame({"sub_object_id": 500, "channel_id": 5001, "ts": days + pd.Timedelta(hours=10),
                            "value": "Не замкнут"})  # fmt: skip
    past = pd.DataFrame({"sub_object_id": 500, "channel_id": [5001, 5001, 5002],
                         "start_ts": pd.to_datetime(["2026-02-10T09:48:00+03:00", "2026-02-17T09:47:00+03:00",
                                                     "2026-02-20T23:10:00+03:00"]),
                         "label": [0, 0, 1]})  # fmt: skip
    frames = ((past, "start_ts"), (guard, "ts"), (openers, "ts"))
    return tuple(f.assign(**{c: f[c].dt.tz_convert("UTC")}) for f, c in frames)


ALARM_REQUESTS = {  # теневой скоринг (15.2): одна пачка тревог — запрос на каждую версию
    "alarm_score_v3": {"run_id": 7, "alarms": ALARMS, "explain": True, "model_version": "v3"},
    "alarm_score_baseline": {"run_id": 7, "alarms": ALARMS, "explain": True, "model_version": "baseline-v0"},
}

REQUESTS = {
    "external_score_magdeburg": {
        "run_id": 28,
        "model": "magdeburg_fire",
        "decision_time": "2023-07-13T12:45:00+03:00",
        "targets": None,
        "explain": True,
    },
    "external_score_magdeburg_quiet": {  # до горения: тревог нет; цель вне набора — пропуск, факторы без explain — []
        "run_id": 28,
        "model": "magdeburg_fire",
        "decision_time": "2023-07-13T12:25:00+03:00",
        "targets": [9010106, 9010108, 1],
        "explain": False,
    },
    "external_score_pdm": {  # hourly, агрегаты; у одного — short_history
        "run_id": 33,
        "model": "pdm_failure_24h",
        "decision_time": "2015-11-26T12:00:00+03:00",
        "targets": None,
        "explain": True,
    },
    "external_score_vast_gate": {
        "run_id": 31,
        "model": "vast_gate",
        "decision_time": "2015-07-10T10:40:00+03:00",
        "events": [
            {"channel_id": 902001601, "ts": "2015-07-10T10:35:47+03:00", "value": "20152810102803-808"},
            {"channel_id": 902001201, "ts": "2015-07-10T10:12:40+03:00", "value": "20150210100210-101"},
            {"channel_id": 1, "ts": "2015-07-10T10:36:00+03:00", "value": "20152810102803-808"},
            {"channel_id": 902001100, "ts": "2015-07-10T10:45:00+03:00", "value": "20150210100210-101"},
        ],
    },
}


def snapshots() -> dict[str, dict]:
    events, channels = magdeburg_journal()
    vast = vast_world()
    pdm_events, pdm_channels, pdm_units = pdm_world()
    smoke, smoke_channels = smoke_object()
    journal = pd.concat([events, pdm_events], ignore_index=True)
    v0_inputs, routed = daily_world()

    def external_channels(dataset: str) -> pd.DataFrame:
        return {"magdeburg": channels, "azure_pdm": pdm_channels}.get(dataset, vast["channels"])

    def external_events(run_id, ids, since, until) -> pd.DataFrame:
        return journal[journal.channel_id.isin(ids) & (journal.ts >= since) & (journal.ts < until)]

    def external_passes(run_id, ids, values, since, until) -> pd.DataFrame:
        p = vast["past"]
        return p[p.channel_id.isin(ids) & p.value.isin(values) & (p.ts >= since) & (p.ts < until)]

    fakes = {
        "external_channels": external_channels,
        "external_events": external_events,
        "external_passes": external_passes,
        "external_classes": lambda dataset, ids: {k: v for k, v in vast["classes"].items() if k in ids},
        "external_permits": lambda dataset: vast["permits"],
        "external_units": lambda dataset: pdm_units,
        "objects_of": lambda ids: {i: 1 for i in ids if i in set(smoke_channels.channel_id)},
        "smoke_channels": lambda obj: smoke_channels,
        "smoke_events": lambda run, obj, ids, since, until: smoke[smoke.ts <= pd.Timestamp(until)],
        "coverage": lambda run: set(pd.date_range(SMOKE_BASE, periods=41)),
        "v3_rows": lambda run, obj: AlarmRows(),
        "daily_inputs": lambda run, date, types, ids, days: v0_inputs,
        "routed_inputs": lambda run, date, types: routed,
        "known_channels": lambda ids: {i for i in ids if i == 1007},
        "access_inputs": lambda run, points: access_world(),
    }
    out = {}
    with ExitStack() as stack:
        for name, fake in fakes.items():
            stack.enter_context(patch.object(history, name, fake))
        with TestClient(app) as client:
            out["models"] = client.get("/v1/models").json()
            out["models_compare"] = client.get("/v1/models/compare", params={"model": "smoke_alarm"}).json()
            for name, body in REQUESTS.items():
                response = client.post("/v1/external/score", json=body).json()
                out[name] = response | {"elapsed_ms": 0}
                out[f"{name}_request"] = body
            for name, body in ALARM_REQUESTS.items():
                out[name] = client.post("/v1/alarm/score", json=body).json() | {"elapsed_ms": 0}
                out[f"{name}_request"] = body
            out["daily_score"] = client.post("/v1/daily/score", json=DAILY_REQUEST).json() | {"elapsed_ms": 0}
            out["daily_score_request"] = DAILY_REQUEST
            out["access_score"] = client.post("/v1/access/score", json=ACCESS_REQUEST).json() | {"elapsed_ms": 0}
            out["access_score_request"] = ACCESS_REQUEST
    return out


def render(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in snapshots().items():
        path = OUT / f"{name}.json"
        path.write_text(render(payload), encoding="utf-8")
        print(path.relative_to(OUT.parents[3]))


if __name__ == "__main__":
    main()
