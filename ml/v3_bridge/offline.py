"""Событийная v3 в контуре сервиса: эталон их кодом, путь сервиса по всем тревогам 2026, метрики и сравнение с v0.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.v3_bridge.offline [--work ../data/interim/v3] [--jobs 8]`.
Код выхода 1 — расхождение с их отчётом или пути сервиса с эталоном.

1. Входы из архива, как в бандле v3 (`ml/v3/prepare_features.py`): события дымовых каналов с 2022 года, дни с данными
   журнала, справочник — исходный CSV. Кэшируются в --work.
2. Эталон — их код целиком: `build_alarm_rows` + `build_extended` по всем каналам. Два расчёта: отчётный — исходы
   решений до `--label-end` 2026-06-30, как в их prepare_features.py; калибровка IV кв. 2025 и проверка 2026 должны
   совпасть с их `alarm_evaluation.json` до единицы. Живой — исходы до конца данных, как видит их predict_alarms.py
   (`label_end = until`): у решений 30.06 признаки истории объекта и шлейфа учитывают исходы, закрывшиеся 30.06 до T,
   которые отчётный расчёт скрывает. На проверку 2026 (решения до 29.06) это не влияет.
3. Путь сервиса — все дымовые тревоги I полугодия 2026 по объектам, пачками, как их присылает worker (тревоги объекта
   не дальше DEBOUNCE друг от друга), через `AlarmRows` и `decide`; справочник — каталог архива, как таблица
   `channels`. Против живого эталона совпадать должны набор решений, признаки (побитово) и оценки; у тревог без
   решения — причина пропуска.
4. `ml/models/v3/config.json` и `offline.json` — конфиг модели для ml-service и офлайн-метрики для страницы модели
   в формате v0 (`ml.baseline.offline`).
5. `ml/models/compare_smoke_alarm.json` — v0 и v3 на общих решениях проверки (GET /v1/models/compare).

Тест ml-service (`tests/test_v3.py`) повторяет шаги 2–3 на синтетическом журнале: данных хакатона в тестах нет.
"""

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
from catboost import CatBoostClassifier

from ml.baseline import offline as v0off
from ml.v3.src.event_model import build_alarm_rows
from ml.v3.src.neighbors import build_extended
from ml.v3_bridge.alarm import (
    FAULT_WASHOUT_HOURS,
    HISTORY_START,
    SECOND,
    SMOKE,
    AlarmRows,
    decide,
    registry,
    skip_reason,
)

ROOT = Path(__file__).resolve().parents[2]
V3 = ROOT / "ml" / "v3" / "artifacts"
MODEL_DIR = ROOT / "ml" / "models" / "v3"
MODEL_FILE = "alarm_all_d7.cbm"  # = smoke_alarm_fault.cbm их бандла: оценки совпадают, файл пересохранён
COMPARE = ROOT / "ml" / "models" / "compare_smoke_alarm.json"
TZ = "Europe/Moscow"
LABEL_END = "2026-06-30"  # их --label-end: отчётный расчёт
LIVE_END = "2026-07-01"  # данные — до 30.06 включительно: исходы до T не скрыты, как у их predict_alarms.py
SMOKE_SENSOR = "Датчик дыма"
DEBOUNCE = pd.Timedelta(seconds=5)  # worker скорит тревоги объекта одним запросом через 5 с после последней
TEST = ("2026-01-01", LABEL_END)
CALIBRATION = ("2025-10-01", "2026-01-01")
SEEN = ("2023-01-01", "2025-07-01")  # их seen_in_training: каналы с решениями в обучении после дообучения
PERIODS = {  # train_alarm.py: обучение 2023–2024 + early stopping на I пол. 2025, дообучение до июля 2025,
    "train": ["2023-01-01", "2025-07-01"],  # выбор признаков по III кв. 2025, порог — IV кв. 2025, проверка — 2026
    "early_stop": ["2025-01-01", "2025-07-01"],
    "selection": ["2025-07-01", "2025-10-01"],
    "threshold": list(CALIBRATION),
    "test": list(TEST),
}
QUARTER_ROLE = {  # роли кварталов для страницы модели: early_stop | threshold | test (web OfflineReportCard)
    "2025Q1": "early_stop",
    "2025Q2": "early_stop",
    "2025Q3": "early_stop",  # выбор набора признаков — тоже не независимая проверка
    "2025Q4": "threshold",
    "2026Q1": "test",
    "2026Q2": "test",
}


def period(f: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Их period() из train_alarm.py: решение в [start, end), окно исхода закрылось до end, исход известен."""
    t = f.decision_time
    return f.loc[t.ge(start) & (t + pd.Timedelta(days=1)).le(end) & f.target.notna()]


# ---------- 1. входы


def archive_coverage(archive: Path, work: Path) -> pd.Series:
    """Дни с данными журнала с 2021 года — день, в который есть хоть одно событие любого канала (их global_coverage).
    Полный проход по времени событий архива — один раз, дальше из --work."""
    path = work / "global_coverage.csv"
    if not path.exists():
        work.mkdir(parents=True, exist_ok=True)
        events = ds.dataset(archive / "events", format="parquet", partitioning="hive")
        dates = set()
        shift = 3 * 3600 * 10**6  # Москва — UTC+3 без перехода с 2014 года
        for frag in events.get_fragments(filter=ds.field("year") >= 2021):
            us = frag.to_table(columns=["ts"]).column("ts").cast("int64").to_numpy() + shift
            dates.update(np.unique(us // (86400 * 10**6)).tolist())
        pd.DataFrame({"date": pd.to_datetime(sorted(dates), unit="D")}).to_csv(path, index=False)
    return pd.read_csv(path, parse_dates=["date"]).date


def build_inputs(archive: Path, raw_registry: Path, work: Path) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """События дымовых каналов с 2022 года (sensor_id, event_id, ts — наивное московское, value), дни с данными
    журнала и справочник — исходный CSV. Год раздела архива — по UTC, поэтому читаем с 2021 и режем по Москве."""
    ev_path = work / "smoke_events_2022_2026.parquet"
    channels = pd.read_csv(raw_registry)
    if not ev_path.exists():
        work.mkdir(parents=True, exist_ok=True)
        events = ds.dataset(archive / "events", format="parquet", partitioning="hive")
        smoke = channels.loc[channels["тип_датчика"] == SMOKE_SENSOR, "ид_канала_данных"].astype(int).tolist()
        flt = ds.field("channel_id").isin(smoke) & (ds.field("year") >= 2021)
        t = events.to_table(columns=["event_id", "channel_id", "ts", "value_raw"], filter=flt).to_pandas()
        t["ts"] = t.ts.dt.tz_convert(TZ).dt.tz_localize(None).astype("datetime64[ns]")
        t = t[t.ts >= HISTORY_START].rename(columns={"channel_id": "sensor_id", "value_raw": "value"})
        t["sensor_id"] = t.sensor_id.astype("int64")
        t = t.sort_values(["sensor_id", "ts", "event_id"], kind="stable")
        t[["event_id", "sensor_id", "ts", "value"]].to_parquet(ev_path, index=False)
    return pd.read_parquet(ev_path), archive_coverage(archive, work), channels


# ---------- 2. эталон


def reference(events, coverage, channels, work: Path, label_end: str = LABEL_END) -> pd.DataFrame:
    """Признаки всех решений их кодом, как prepare_features.py (кэш в --work: alarm_features_v3.parquet — отчётный,
    alarm_features_v3_live.parquet — с исходами до конца данных)."""
    path = work / ("alarm_features_v3.parquet" if label_end == LABEL_END else "alarm_features_v3_live.parquet")
    if path.exists():
        return pd.read_parquet(path)
    rows = build_alarm_rows(events, coverage, label_end, FAULT_WASHOUT_HOURS)
    f = build_extended(rows, events, channels)
    f.to_parquet(path, index=False, compression="zstd")
    return f


def load_model() -> tuple[CatBoostClassifier, dict]:
    model = CatBoostClassifier()
    model.load_model(str(MODEL_DIR / MODEL_FILE))
    cfg = json.loads((V3 / "models" / "smoke_alarm_config.json").read_text(encoding="utf-8"))
    return model, cfg


def counts(y: np.ndarray, s: np.ndarray, thr: float) -> dict:
    a, y = s >= thr, y == 1
    return {"rows": len(y), "positives": int(y.sum()), "alerts": int(a.sum()), "tp": int((a & y).sum()),
            "fp": int((a & ~y).sum())}  # fmt: skip


def check_report(f: pd.DataFrame, model, cfg) -> bool:
    """Калибровка и проверка против их alarm_evaluation.json: решения, положительные, тревоги, TP, FP."""
    ev = json.loads((V3 / "reports" / "alarm_evaluation.json").read_text(encoding="utf-8"))["modes"]["balanced"]
    ok = True
    for name, (lo, hi) in (("calibration", CALIBRATION), ("test", TEST)):
        g = period(f, lo, hi)
        got = counts(g.target.to_numpy(), model.predict_proba(g[cfg["features"]])[:, 1], cfg["threshold"])
        want = {k: ev[name][k] for k in got}
        ok &= got == want
        print(f"эталон, {name}: {got} — {'как в отчёте' if got == want else f'в отчёте {want}'}")
    return ok


# ---------- 3. путь сервиса


def batches(alarms: pd.DataFrame) -> list[list[tuple[int, pd.Timestamp]]]:
    """Тревоги объекта пачками: следующая тревога не дальше DEBOUNCE от предыдущей — тот же запрос скоринга."""
    out, cur, last = [], [], None
    for sid, ts in alarms.sort_values(["alarm_ts", "sensor_id"]).itertuples(index=False):
        if last is not None and ts - last > DEBOUNCE:
            out.append(cur)
            cur = []
        cur.append((int(sid), ts))
        last = ts
    return out + ([cur] if cur else [])


def service_object(args) -> tuple[pd.DataFrame, pd.DataFrame, float, int]:
    """Все тревоги объекта через путь сервиса: AlarmRows + decide по пачкам в порядке времени."""
    events, coverage, catalog, alarms, features = args
    reg = registry(catalog)
    cache = AlarmRows()
    decided, skipped, started, n = [], [], time.perf_counter(), 0
    for batch in batches(alarms):
        until = max(ts for _, ts in batch) + SECOND
        rows = cache.get(events, coverage, until)
        f = decide(events, reg, batch, rows)
        got = set(zip(f.sensor_id, f.alarm_ts, strict=True)) if len(f) else set()
        if len(f):
            decided.append(f[["sensor_id", "decision_time", *features]])
        for sid, ts in batch:
            if (sid, ts) not in got:
                skipped.append((sid, ts, skip_reason(events[events.ts < until], coverage, sid, ts)))
        n += 1
    dec = pd.concat(decided, ignore_index=True) if decided else pd.DataFrame(columns=["sensor_id", "decision_time"])
    skip = pd.DataFrame(skipped, columns=["sensor_id", "alarm_ts", "skip_reason"])
    return dec, skip, time.perf_counter() - started, n


def service_path(events, coverage, catalog, ref, model, cfg, jobs: int) -> tuple[bool, pd.DataFrame]:
    """Все дымовые тревоги I полугодия 2026 через путь сервиса против живого эталона."""
    feats = cfg["features"]
    smoke = catalog[catalog.sensor_type == SMOKE_SENSOR]
    obj_of = smoke.set_index("channel_id").object_id
    al = events[(events.value == SMOKE) & (events.ts >= "2026-01-01") & (events.ts < "2026-07-01")]
    al = al[["sensor_id", "ts"]].drop_duplicates().rename(columns={"ts": "alarm_ts"})
    al["object_id"] = al.sensor_id.map(obj_of)
    ev = events.assign(object_id=events.sensor_id.map(obj_of))
    tasks = []
    for obj, a in al.groupby("object_id"):
        tasks.append((ev[ev.object_id == obj].drop(columns="object_id"), coverage, smoke[smoke.object_id == obj],
                      a[["sensor_id", "alarm_ts"]], feats))  # fmt: skip
    tasks.sort(key=lambda t: -len(t[0]))  # крупные объекты — первыми
    started = time.perf_counter()
    with ProcessPoolExecutor(jobs) as pool:
        parts = list(pool.map(service_object, tasks))
    dec = pd.concat([p[0] for p in parts if len(p[0])], ignore_index=True)
    skip = pd.concat([p[1] for p in parts], ignore_index=True)
    slowest = max(parts, key=lambda p: p[2] / max(p[3], 1))
    print(
        f"путь сервиса: тревог {len(al)}, объектов {len(tasks)}, запросов {sum(p[3] for p in parts)}, "
        f"{time.perf_counter() - started:.0f} с; решений {len(dec)}, пропусков {len(skip)} "
        f"{skip.skip_reason.value_counts().to_dict()}"
    )
    print(
        f"  самый медленный объект: {slowest[2] / max(slowest[3], 1):.3f} с на запрос в среднем, запросов {slowest[3]}"
    )

    want = ref[(ref.decision_time >= "2026-01-01") & (ref.decision_time < "2026-07-01")]
    x = dec.merge(want[["sensor_id", "decision_time", *feats]], on=["sensor_id", "decision_time"], how="outer",
                  suffixes=("", "_ref"), indicator=True)  # fmt: skip
    only_service, only_ref = int((x._merge == "left_only").sum()), int((x._merge == "right_only").sum())
    both = x[x._merge == "both"]
    bad_features = []
    for c in feats:
        a, b = both[c], both[f"{c}_ref"]
        same = (a == b) | (a.isna() & b.isna())
        if not same.all():
            bad_features.append((c, int((~same).sum())))
    s = model.predict_proba(both[feats])[:, 1]
    r = model.predict_proba(both[[f"{c}_ref" for c in feats]].set_axis(feats, axis=1))[:, 1]
    delta = float(np.abs(s - r).max()) if len(s) else 0.0
    print(
        f"  против эталона: общих {len(both)}, только у сервиса {only_service}, только в эталоне {only_ref}, "
        f"признаков с расхождением {len(bad_features)} {bad_features[:5]}, max |Δ оценки| {delta:.1e}"
    )
    ok = only_service == 0 and only_ref == 0 and not bad_features and delta <= 1e-12
    return ok, skip


# ---------- 4–5. конфиг, офлайн-метрики, сравнение с v0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(g: pd.DataFrame, s: np.ndarray, thr: float) -> dict:
    """Метрики блока в формате config.json v0."""
    b = v0off.block(g.target.to_numpy(), s, thr)
    keys = ("decisions", "positives", "tp", "fp", "fn", "precision", "recall", "f1")
    return {**{k: b[k] for k in keys}, "average_precision": b["ap"]}


def write_config(f: pd.DataFrame, model, cfg) -> dict:
    """ml/models/v3/config.json — всё, что нужно ml-service; содержимое детерминировано (без времени расчёта)."""
    thr = cfg["threshold"]
    seen = set(period(f, *SEEN).sensor_id)
    te, cal = period(f, *TEST), period(f, *CALIBRATION)
    s_te = model.predict_proba(te[cfg["features"]])[:, 1]
    is_seen = te.sensor_id.isin(seen).to_numpy()
    out = {
        "model": "smoke_alarm",
        "family": "v3",
        "version": "v3",
        "label": "v3",
        "title": "Событийная v3 ML-разработчика: неисправность в 24 ч после дымовой тревоги",
        "source": "PR #114 (Answers/v3 → ml/v3); модель — smoke_alarm_fault.cbm = alarm_all_d7.cbm",
        "module": "ml.v3_bridge.alarm",
        "feature_version": cfg["feature_version"],
        "features": cfg["features"],
        "categorical": cfg["categorical_features"],
        "threshold": thr,
        "thresholds": {"balanced": thr, "recall": cfg["recall_mode_threshold"]},
        "trees": model.tree_count_,
        "history_start": HISTORY_START,
        "horizon_hours": cfg["horizon_hours"],
        "fault_washout_hours": FAULT_WASHOUT_HOURS,
        "periods": PERIODS,
        "sha256": sha256(MODEL_DIR / MODEL_FILE),
        "metrics": {
            "threshold": metrics(cal, model.predict_proba(cal[cfg["features"]])[:, 1], thr),
            "test": metrics(te, s_te, thr),
            "test_seen": metrics(te[is_seen], s_te[is_seen], thr),
            "test_unseen": metrics(te[~is_seen], s_te[~is_seen], thr),
        },
        "seen_rule": "каналы с решениями в обучении 2023-01 — 2025-07 (их alarm_seen_unseen.csv)",
        "seen_channels": sorted(int(c) for c in seen),
    }
    (MODEL_DIR / "config.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"config.json: seen-каналов {len(seen)}; тест unseen: {out['metrics']['test_unseen']}")
    return out


def v3_decisions(f: pd.DataFrame, model, cfg, db: str) -> pd.DataFrame:
    """Размеченные решения v3 с оценкой, кварталом и видом эпизода тревоги (как smoke_decisions у v0)."""
    d = f[f.target.notna()].copy()
    d["score"] = model.predict_proba(d[cfg["features"]])[:, 1]
    d["quarter"] = v0off.quarter_of(d.decision_time)
    d["channel_id"] = d.sensor_id
    d["alarm_ts"] = d.decision_time - SECOND
    d = d.merge(v0off.episode_kinds(db, "2025-01-01"), on=["channel_id", "alarm_ts"], how="left")
    d.loc[d.decision_time >= "2025-01-01", "kind"] = d["kind"].fillna("unknown")
    return d


def offline_json(d: pd.DataFrame, conf: dict) -> dict:
    """ml/models/v3/offline.json — тот же формат, что у v0 (docs/api.md, GET /v1/models → offline)."""
    thr = conf["threshold"]
    te = period(d, *TEST)
    lead = (te.lead_hours * 3600).to_numpy()
    seen = te.sensor_id.isin(set(conf["seen_channels"])).to_numpy()
    y, s, kind = te.target.to_numpy(), te.score.to_numpy(), te["kind"].to_numpy()
    rule = te.object_isolated_3600s.fillna(0).to_numpy()
    q = d[d.quarter.isin(v0off.QUARTERS) & d.decision_time.add(pd.Timedelta(days=1)).le(LABEL_END)]
    payload = {
        "model": "smoke_alarm",
        "version": "v3",
        "period": {"from": TEST[0], "to": TEST[1], "label": v0off.TEST_LABEL},
        "threshold": thr,
        "test": v0off.block(y, s, thr, lead),
        "seen": v0off.block(y[seen], s[seen], thr, lead[seen]),
        "unseen": v0off.block(y[~seen], s[~seen], thr, lead[~seen]),
        "seen_rule": "каналы с решениями в обучении 2023-01 — 2025-07",
        "lead_time": v0off.lead_time(lead[(s >= thr) & (y == 1)]),
        "pr_curve": v0off.pr_curve(y, s),
        "by_quarter": [
            {"quarter": k, "role": QUARTER_ROLE[k], **v0off.block(g.target, g.score, thr, g.lead_hours * 3600)}
            for k, g in q.groupby("quarter")
        ],
        "by_type": None,
        "by_episode_kind": [
            {"kind": k, **v0off.block(y[kind == k], s[kind == k], thr, lead[kind == k])}
            for k in [*v0off.EPISODE_KINDS, "unknown"]
            if (kind == k).any()
        ],
        "rule": v0off.rule_block("одиночная тревога на объекте за час → неисправность", y, rule),
    }
    v0off.write("v3", conf["sha256"], payload)
    t = payload["test"]
    print(f"offline.json: тест P {t['precision']} R {t['recall']} AP {t['ap']}, решений {t['decisions']}")
    return payload


def compare(d3: pd.DataFrame, conf: dict, archive: Path, v0_cache: Path | None, db: str) -> dict:
    """v0 и v3 на общих решениях проверки: каждая — на своём пороге; где расходятся — кто прав."""
    d0, cfg0, sha0 = v0off.smoke_decisions(archive, v0_cache, db)
    t3 = period(d3, *TEST)
    lo0, hi0 = cfg0["periods"]["test"]
    t0 = d0[(d0.decision_time >= lo0) & (d0.decision_time < hi0)]
    both = t3[["channel_id", "decision_time", "target", "lead_hours", "score"]].merge(
        t0[["channel_id", "decision_time", "target", "score"]], on=["channel_id", "decision_time"],
        suffixes=("_v3", "_v0"),
    )  # fmt: skip
    label_mismatch = int((both.target_v3 != both.target_v0).sum())
    y, lead = both.target_v3.to_numpy(), (both.lead_hours * 3600).to_numpy()
    thr = {"baseline-v0": cfg0["threshold"], "v3": conf["threshold"]}
    score = {"baseline-v0": both.score_v0.to_numpy(), "v3": both.score_v3.to_numpy()}
    alert = {v: score[v] >= thr[v] for v in thr}
    differ = alert["baseline-v0"] != alert["v3"]
    out = {
        "model": "smoke_alarm",
        "sha256": {"baseline-v0": sha0, "v3": conf["sha256"]},
        "period": {"from": TEST[0], "to": TEST[1], "label": "I полугодие 2026, решения, допустимые для обеих моделей"},
        "decisions": len(both),
        "positives": int(y.sum()),
        "versions": {v: v0off.block(y, score[v], thr[v], lead) for v in thr},
        "disagree": {
            "decisions": int(differ.sum()),
            "alert_only": {
                v: int((alert[v] & ~alert[o]).sum()) for v, o in (("baseline-v0", "v3"), ("v3", "baseline-v0"))
            },
            "right": {v: int((differ & (alert[v] == (y == 1))).sum()) for v in thr},
        },
        "only": {"baseline-v0": int(len(t0) - len(both)), "v3": int(len(t3) - len(both))},
        "label_mismatch": label_mismatch,
    }
    COMPARE.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"сравнение: общих {len(both)} (только v0 {out['only']['baseline-v0']}, только v3 {out['only']['v3']}), "
        f"метки разошлись {label_mismatch}; расходятся тревогой {out['disagree']}"
    )
    for v, b in out["versions"].items():
        print(f"  {v}: P {b['precision']} R {b['recall']} AP {b['ap']} TP {b['tp']} FP {b['fp']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, default=ROOT / "data" / "interim" / "v3")
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    ap.add_argument("--raw-registry", type=Path, default=ROOT / "data" / "raw" / "справочник_каналов_датчиков.csv")
    ap.add_argument("--db", default="postgresql+psycopg://lct:lct@localhost:15432/lct")
    ap.add_argument("--v0-cache", type=Path, help="parquet с решениями v0 (ml.baseline.offline --cache)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--skip-service", action="store_true", help="без пути сервиса по всем тревогам 2026")
    a = ap.parse_args()

    started = time.perf_counter()
    events, coverage, channels = build_inputs(a.archive, a.raw_registry, a.work)
    print(f"входы: событий {len(events)}, каналов {events.sensor_id.nunique()}, дней покрытия {len(coverage)}")
    ref = reference(events, coverage, channels, a.work)
    model, cfg = load_model()
    print(f"эталон: решений {len(ref)}, признаков {len(cfg['features'])}, {time.perf_counter() - started:.0f} с")
    ok = check_report(ref, model, cfg)
    catalog = pd.read_parquet(a.archive / "catalog" / "channels.parquet").rename(columns={"id": "channel_id"})
    live = reference(events, coverage, channels, a.work, LIVE_END)
    if not a.skip_service:
        same, _ = service_path(events, coverage, catalog, live, model, cfg, a.jobs)
        ok &= same
    conf = write_config(ref, model, cfg)
    d3 = v3_decisions(ref, model, cfg, a.db)
    offline_json(d3, conf)
    compare(d3, conf, a.archive, a.v0_cache, a.db)
    print("СОВПАДАЕТ" if ok else "РАСХОЖДЕНИЕ")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
