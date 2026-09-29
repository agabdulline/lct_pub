"""Эталон ежедневных моделей ML-разработчика на наших данных: его офлайн-код, решения на каждые сутки проверки.

Задача B (docs/PLAN.md 15.9) — его ежедневные модели в сервисе по типам. Эталон нужен дважды: для сравнения с нашей
v0 на одних строках (`ml.daily_routing.compare`) и для сверки онлайн-пути с офлайн-кодом (AGENTS.md §5).

Входы собирает его код на наших CSV (`data/raw`), логика не менялась — только чтение CSV вместо 7z:
`ml.v3_inputs` (sensor_daily, fault_episodes, global_coverage, channels) и `ml.each_channel.prepare` (типы вне v3
поверх того же набора). Окружение — его закреплённое (`ml/each_channel/pyproject.toml`: pandas 2.3, catboost 1.2.10).

- `v3` — признаки `ml.v3.src.inference.snapshot_features` на каждую дату T, модель `ml/v3/artifacts/models`.
  `predict_daily` ещё добавляет столбцы `DailyContext`, но в 164 признака модели они не входят (признаки модели =
  `ml.v3.src.features.FEATURES`, проверяется) — вход модели и оценки те же.
- `ups` — ансамбль `0.75 × old + 0.25 × mean_3d(new)` (`ml/models/each-channel/ups`): признаки `build_features` +
  `add_hierarchy_features`, как в `six_channel_study` / `six_channel_ensemble`.
- `manual` — ручной извещатель: `build_features` + производственный календарь, порог помесячный причинный
  (`hard_channel_study.monthly_scores`: окно 90 суток, готовность — 1 500 созревших положительных).

У ИБП и извещателя две оценки: `*_protocol` — как в его отчёте (сглаживание и порог по строкам его оценочной таблицы:
только решения с известной меткой), и рабочая — как будет в сервисе (по всем суткам, где канал оценён). Совпадение
первой с его отчётом — проверка, что эталон воспроизводит его расчёт; вторая идёт в сравнение.

Запуск из ml/each_channel (его окружение): `PYTHONPATH=../.. uv run python -m ml.daily_routing.reference v3`,
затем `ups`, `manual`. Результат — `data/interim/daily-routing/reference/`.
"""

import argparse
import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier

ROOT = Path(__file__).resolve().parents[2]
# Журналы 2019–2026: так собран его набор — с ним эталон повторяет его отчёт ежедневной v3 до единицы, с 2022 года —
# нет (признаки давности: days_since_smoke_day, history_days и др.)
REF = ROOT / "data" / "interim" / "v3-ref19"
BUNDLE = REF / "bundle" / "data"  # ml.v3_inputs: ingest 2019–2026 + prepare_dataset + prepare_episodes
EACH = REF / "each-channel"  # ml.each_channel.prepare поверх BUNDLE
OUT = ROOT / "data" / "interim" / "daily-routing" / "reference"
CALENDAR = ROOT / "data" / "interim" / "calendar"
V3_MODEL = ROOT / "ml" / "v3" / "artifacts" / "models"
EACH_MODELS = ROOT / "ml" / "models" / "each-channel"
# Проверка: T, у которых горизонт 30 суток закрывается до дня без журнала 1.06.2026 (как тест его отдельных моделей)
TEST = ("2026-01-01", "2026-05-02")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cbm(path: Path, sha: str, features: list[str]) -> CatBoostClassifier:
    if sha256(path) != sha:
        raise ValueError(f"SHA256 не совпал: {path}")
    model = CatBoostClassifier().load_model(str(path))
    if model.feature_names_ != features:
        raise ValueError(f"Признаки модели не совпали с конфигом: {path}")
    return model


# ---------- v3

_V3: dict = {}


def _v3_init() -> None:
    from ml.v3.src.features import FEATURES

    cfg = json.loads((V3_MODEL / "model_config.json").read_text(encoding="utf-8"))
    if cfg["features"] != FEATURES:
        raise ValueError("признаки ежедневной v3 не равны ml.v3.src.features.FEATURES — нужен DailyContext")
    model = CatBoostClassifier()
    model.load_model(str(V3_MODEL / cfg["model_file"]))
    # Чтение — как в ml/v3/predict_daily.py
    _V3.update(
        cfg=cfg,
        model=model,
        daily=pd.read_parquet(BUNDLE / "sensor_daily.parquet"),
        episodes=pd.read_parquet(BUNDLE / "fault_episodes.parquet"),
        channels=pd.read_csv(BUNDLE / "channels.csv"),
        coverage=pd.read_csv(BUNDLE / "global_coverage.csv", parse_dates=["date"]).date,
    )


def _v3_day(day: pd.Timestamp) -> tuple[str, int, int, float]:
    from ml.v3.src.inference import snapshot_features

    tick = time.perf_counter()
    s = _V3
    f = snapshot_features(s["daily"], s["episodes"], s["channels"], s["coverage"], day)
    f["score"] = np.nan
    eligible = f.eligible
    if eligible.any():
        f.loc[eligible, "score"] = s["model"].predict_proba(f.loc[eligible, s["cfg"]["features"]])[:, 1]
    f["threshold"] = s["cfg"]["threshold"]
    f["alert"] = f.score.ge(s["cfg"]["threshold"]) & eligible
    path = OUT / "v3" / f"{day:%Y-%m-%d}.parquet"
    f.to_parquet(path, index=False)
    return f"{day:%Y-%m-%d}", len(f), int(eligible.sum()), time.perf_counter() - tick


def v3(workers: int) -> None:
    (OUT / "v3").mkdir(parents=True, exist_ok=True)
    days = [d for d in pd.date_range(*TEST) if not (OUT / "v3" / f"{d:%Y-%m-%d}.parquet").exists()]
    with ProcessPoolExecutor(workers, initializer=_v3_init) as pool:
        for day, rows, eligible, seconds in pool.map(_v3_day, days):
            print(json.dumps({"v3": day, "rows": rows, "eligible": eligible, "seconds": round(seconds, 1)}), flush=True)


# ---------- отдельные модели типов


def mean_3d(frame: pd.DataFrame, scores: np.ndarray) -> np.ndarray:
    from ml.each_channel.six_channel_study import transform_scores

    return transform_scores(frame, scores, "mean_3d")


def ups() -> None:
    from ml.each_channel.features import build_features, load_inputs
    from ml.each_channel.six_channel_study import add_hierarchy_features, load_objects, subset

    cfg = json.loads((EACH_MODELS / "ups" / "config.json").read_text(encoding="utf-8"))
    parts = []
    for comp in cfg["components"]:
        c = json.loads((EACH_MODELS / "ups" / comp["config"]).read_text(encoding="utf-8"))
        model = load_cbm(EACH_MODELS / "ups" / comp["path"], comp["sha256"], c["features"])
        parts.append((comp, c, model))
    channels, daily, episodes = load_inputs(EACH)
    objects = load_objects(BUNDLE / "objects.csv")
    ids = channels.loc[channels.sensor_type.eq(cfg["type"]), "channel_id"].tolist()
    # С начала периода порога: его оценочная таблица — калибровка (IV кв. 2025) + проверка
    frame = build_features(daily, episodes, channels, pd.date_range("2025-10-01", TEST[1]), ids)
    frame = add_hierarchy_features(frame, episodes, channels, objects)

    def blend(rows: pd.DataFrame) -> tuple[np.ndarray, dict]:
        total, comps = np.zeros(len(rows)), {}
        for comp, c, model in parts:
            raw = model.predict_proba(rows[c["features"]])[:, 1]
            value = mean_3d(rows, raw) if comp.get("postprocess") == "mean_3d" else raw
            comps[c["version"]] = value
            total += comp["weight"] * value
        return total, comps

    # Как в его отчёте: сглаживание по строкам оценочной таблицы (калибровка + проверка, только известные метки)
    protocol = pd.concat([subset(frame, "calibration"), subset(frame, "test")], ignore_index=True).sort_values(
        ["date", "channel_id"], kind="stable"
    )
    protocol["score_protocol"], _ = blend(protocol)
    # Как в сервисе: все сутки, где канал оценён
    scored = frame.loc[frame.status.eq("scored")].sort_values(["date", "channel_id"], kind="stable").copy()
    scored["score"], comps = blend(scored)
    for name, value in comps.items():
        scored[f"score_{name}"] = value
    keys = ["channel_id", "date"]
    out = frame.merge(scored[[*keys, "score", *[f"score_{n}" for n in comps]]], on=keys, how="left")
    out = out.merge(protocol[[*keys, "score_protocol"]], on=keys, how="left")
    out["threshold"] = cfg["threshold"]
    out["alert"] = out.score.ge(cfg["threshold"])
    save(out, "ups", check_protocol(out, cfg["threshold"], cfg["metrics"]["test"]))


def manual() -> None:
    from ml.each_channel.calendar_study import calendar_features
    from ml.each_channel.features import build_features, load_inputs
    from ml.each_channel.hard_channel_study import READINESS_MINIMUM, monthly_scores

    cfg = json.loads((EACH_MODELS / "manual-alarm" / "config.json").read_text(encoding="utf-8"))
    model = load_cbm(EACH_MODELS / "manual-alarm" / cfg["artifact"], cfg["sha256"], cfg["features"])
    protocol_file = ROOT / "ml" / "each_channel" / "hard_channel_study" / "protocol.json"
    sources = json.loads(protocol_file.read_text(encoding="utf-8"))["calendar_sources"]
    calendar, ours = calendar_features(CALENDAR, download=True)
    if ours != sources:
        raise ValueError(f"календарь не совпал с его источниками: {ours}")
    window = cfg["threshold_policy"]["calibration_window_days"]
    channels, daily, episodes = load_inputs(EACH)
    ids = channels.loc[channels.sensor_type.eq(cfg["type"]), "channel_id"].tolist()
    # Порог января считается по решениям с 2025-09-03 (окно 90 суток, кончается за 30 суток до месяца)
    frame = build_features(daily, episodes, channels, pd.date_range("2025-08-01", TEST[1]), ids)
    frame = frame.sort_values(["date", "channel_id"], kind="stable")
    frame = frame.merge(calendar, on="date", how="left", validate="many_to_one")
    frame["score"] = np.where(frame.status.eq("scored"), model.predict_proba(frame[cfg["features"]])[:, 1], np.nan)
    labelled = frame.loc[frame.status.eq("scored") & frame.target.notna()].copy()
    metrics, rows = monthly_scores(labelled, TEST[0], "2026-05-01", window, READINESS_MINIMUM)
    months = rows.groupby(rows.date.dt.to_period("M")).agg(
        threshold=("threshold", "first"), matured_positives=("matured_positives", "first")
    )
    frame["month"] = frame.date.dt.to_period("M")
    frame = frame.merge(months, left_on="month", right_index=True, how="left").drop(columns="month")
    frame["ready"] = frame.matured_positives.ge(READINESS_MINIMUM)
    frame["alert"] = frame.ready & frame.score.ge(frame.threshold)
    frame["score_protocol"] = frame.score
    checked = {"metrics_protocol": _round(metrics), "expected": cfg["metrics"]["retrospective_test"]}
    checked["reproduced"] = all(metrics[k] == checked["expected"][k] for k in ("decisions", "positives", "tp", "fp"))
    checked["months"] = {str(k): v for k, v in months.to_dict("index").items()}
    save(frame, "manual_alarm", checked)


def check_protocol(out: pd.DataFrame, threshold: float, expected: dict) -> dict:
    from ml.each_channel.train import assess

    rows = out.loc[out.score_protocol.notna() & out.date.between(*TEST)]
    got = assess(rows, rows.score_protocol.to_numpy(), threshold)
    same = all(got[k] == expected[k] for k in ("decisions", "positives", "tp", "fp"))
    return {"metrics_protocol": _round(got), "expected": expected, "reproduced": same}


def _round(m: dict) -> dict:
    return {k: v for k, v in m.items() if not isinstance(v, dict)}


def save(frame: pd.DataFrame, name: str, check: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(OUT / f"{name}.parquet", index=False)
    (OUT / f"{name}_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=1, default=str), "utf-8")
    print(name, json.dumps({k: check[k] for k in ("reproduced", "metrics_protocol")}, ensure_ascii=False, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", choices=("v3", "ups", "manual"))
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    tick = time.perf_counter()
    {"v3": lambda: v3(a.workers), "ups": ups, "manual": manual}[a.model]()
    print(json.dumps({a.model: "done", "seconds": round(time.perf_counter() - tick)}), flush=True)


if __name__ == "__main__":
    main()
