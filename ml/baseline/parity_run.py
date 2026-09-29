"""Сверка прогона с обучением: оценки ml-service в `predictions` против офлайн-оценки той же модели по архиву.

Событийная модель: решения прогона (`predictions`, model = smoke_alarm) против решений, которые строит код обучения
по архиву журнала — тем же данным, на которых модель обучалась. Совпадать должны набор допустимых решений в окне
прогона, оценки (сервис округляет их до 6 знаков) и тревоги. Пропуски сервиса (`skipped`: одно решение на канал
в сутки, неисправность за 24 ч и т. п.) офлайн не считает решениями. Сверка сквозная: проверяет и эмулятор со шлюзом —
события прогона должны повторять журнал.

Версии (`--version`, теневой скоринг 15.2): `baseline-v0` — код обучения `ml.baseline.features.build`; `v3` — код
ML-разработчика целиком (`build_alarm_rows` + `build_extended` из ml/v3, как их prepare_features.py). `--score URL` —
не читать `predictions`, а оценить дымовые тревоги прогона через ml-service по этому адресу, по объектам и пачкам,
как worker: так сверяется версия, которую backend ещё не пишет в журнал.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.baseline.parity_run --run 3 [--version v3]
[--score http://localhost:18010]`. Код выхода 1 — расхождение.
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from catboost import CatBoostClassifier
from sqlalchemy import create_engine, text

from ml.baseline.dataset import SMOKE, TZ, load_channels, load_events
from ml.baseline.features import build
from ml.baseline.train import GAPS, HISTORY_START

ROOT = Path(__file__).resolve().parents[2]
KEY = ["channel_id", "decision_time"]


def _local(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(TZ).dt.tz_localize(None).astype("datetime64[ns]")


def load_run(db: str, run_id: int) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Прогон, прогнозы событийной модели и тревоги прогона; время — наивное московское."""
    with create_engine(db).connect() as c:
        run = (
            c.execute(
                text(
                    "SELECT id, scenario, status, sim_start, coalesce(sim_now, sim_end) AS sim_until "
                    "FROM runs WHERE id = :r"
                ),
                {"r": run_id},
            )
            .mappings()
            .first()
        )
        if run is None:
            raise SystemExit(f"прогона {run_id} нет")
        pred = pd.read_sql(
            text(
                "SELECT p.channel_id, c.object_id, p.decision_time, p.status, p.skip_reason, p.score, p.alert, "
                "p.model_version FROM predictions p JOIN channels c ON c.id = p.channel_id "
                "WHERE p.run_id = :r AND p.model = 'smoke_alarm'"
            ),
            c,
            params={"r": run_id},
        )
        alarms = pd.read_sql(
            text(
                "SELECT a.channel_id, c.object_id, a.ts AS alarm_ts FROM alarms a "
                "JOIN channels c ON c.id = a.channel_id WHERE a.run_id = :r AND c.sensor_type = :smoke"
            ),
            c,
            params={"r": run_id, "smoke": SMOKE},
        )
    pred["decision_time"] = _local(pred["decision_time"])
    alarms["alarm_ts"] = _local(alarms["alarm_ts"])
    run = dict(run)
    for k in ("sim_start", "sim_until"):
        run[k] = pd.Timestamp(run[k]).tz_convert(TZ).tz_localize(None)
    return run, pred, alarms


def offline(archive: Path, model_dir: Path, objects, start, until) -> tuple[pd.DataFrame, float]:
    """Допустимые решения по дымовым каналам объектов в окне [start, until] и их оценки — кодом обучения."""
    cfg = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    model = CatBoostClassifier()
    model.load_model(str(model_dir / "smoke_alarm.cbm"))
    ch = load_channels(archive)
    ch = ch[(ch.sensor_type == SMOKE) & ch.object_id.isin(objects)]
    ev = load_events(archive, ch.channel_id, since=HISTORY_START, until=until + pd.Timedelta(seconds=1))
    d = build(ev, ch, data_end=until + pd.Timedelta(seconds=1), gaps=GAPS)
    d = d[(d.decision_time >= start) & (d.decision_time <= until)].copy()
    d["offline_score"] = model.predict_proba(d[cfg["features"]])[:, 1] if len(d) else []
    d["offline_alert"] = d["offline_score"] >= cfg["threshold"]
    return d[[*KEY, "offline_score", "offline_alert"]], cfg["threshold"]


def offline_v3(archive: Path, work: Path, objects, start, until) -> tuple[pd.DataFrame, float]:
    """Решения v3 по дымовым каналам объектов в окне [start, until] — их кодом целиком по архиву (ml/v3)."""
    from ml.v3.src.event_model import build_alarm_rows
    from ml.v3.src.neighbors import build_extended
    from ml.v3_bridge.alarm import CATALOG_COLUMNS, FAULT_WASHOUT_HOURS
    from ml.v3_bridge.alarm import HISTORY_START as V3_START
    from ml.v3_bridge.offline import MODEL_DIR, MODEL_FILE, archive_coverage

    cfg = json.loads((MODEL_DIR / "config.json").read_text(encoding="utf-8"))
    model = CatBoostClassifier()
    model.load_model(str(MODEL_DIR / MODEL_FILE))
    ch = load_channels(archive)
    ch = ch[(ch.sensor_type == SMOKE) & ch.object_id.isin(objects)]
    end = until + pd.Timedelta(seconds=1)
    # год раздела архива — по UTC: первые часы 2022 года по Москве лежат в разделе 2021
    ev = load_events(archive, ch.channel_id, since="2021-12-31", until=end)
    ev = ev[ev.ts >= V3_START].rename(columns={"channel_id": "sensor_id"})
    rows = build_alarm_rows(ev, archive_coverage(archive, work), end, FAULT_WASHOUT_HOURS)
    f = build_extended(rows, ev, ch.rename(columns=CATALOG_COLUMNS)) if len(rows) else rows
    d = f[(f.decision_time >= start) & (f.decision_time <= until)].rename(columns={"sensor_id": "channel_id"})
    d["offline_score"] = model.predict_proba(d[cfg["features"]])[:, 1] if len(d) else []
    d["offline_alert"] = d["offline_score"] >= cfg["threshold"]
    return d[[*KEY, "offline_score", "offline_alert"]], cfg["threshold"]


def score_run(url: str, run_id: int, version: str, alarms: pd.DataFrame, debounce_s: float = 5.0) -> pd.DataFrame:
    """Дымовые тревоги прогона через ml-service: по объекту, пачками (тревоги не дальше debounce_s друг от друга),
    в порядке времени — как их присылает worker. Ответ — в формате строк `predictions`."""
    import httpx  # только для --score; в образе ml-service его нет

    out = []
    with httpx.Client(base_url=url, timeout=120) as client:
        for obj, a in alarms.sort_values("alarm_ts").groupby("object_id", sort=False):
            gap = a.alarm_ts.diff().dt.total_seconds().gt(debounce_s).cumsum()
            for _, batch in a.groupby(gap):
                body = {"run_id": run_id, "model_version": version, "explain": False,
                        "alarms": [{"channel_id": int(c), "ts": ts.isoformat()} for c, ts in
                                   zip(batch.channel_id, batch.alarm_ts, strict=True)]}  # fmt: skip
                r = client.post("/v1/alarm/score", json=body)
                r.raise_for_status()
                for x in r.json()["results"]:
                    out.append({"channel_id": x["channel_id"], "object_id": obj, "decision_time": x["decision_time"],
                                "status": x["status"], "skip_reason": x["skip_reason"], "score": x["score"],
                                "alert": x["alert"], "model_version": r.json()["model"]})  # fmt: skip
    pred = pd.DataFrame(out)
    pred["decision_time"] = pd.to_datetime(pred["decision_time"]).dt.tz_localize(None).astype("datetime64[ns]")
    return pred.drop_duplicates(KEY)


def compare(pred: pd.DataFrame, off: pd.DataFrame, alarms: pd.DataFrame, tol: float = 1e-6) -> dict:
    """Сверка решений сервиса с офлайн-решениями по (channel_id, decision_time); решение — через 1 с после тревоги.

    Офлайн-решение без прогноза сервиса делится на два случая: тревога в прогоне есть, но не оценена (очередь
    скоринга, остановка прогона) — или тревоги в прогоне нет вовсе (эмулятор или шлюз не передали событие).
    """
    x = pred.merge(off, on=KEY, how="outer", indicator=True)
    scored = x[x["status"].eq("scored")]
    both = scored[scored["_merge"] == "both"]
    delta = (both["score"] - both["offline_score"]).abs()
    score_bad = both[delta > tol]
    alert_bad = both[both["alert"].astype(bool) != both["offline_alert"].astype(bool)]
    service_only = scored[scored["_merge"] == "left_only"]  # сервис оценил, а офлайн решения не видит
    offline_only = x[x["_merge"] == "right_only"]  # офлайн-решение, которого у сервиса нет вовсе
    in_run = (
        offline_only.merge(
            alarms.assign(decision_time=alarms["alarm_ts"] + pd.Timedelta(seconds=1))[KEY],
            on=KEY,
            how="left",
            indicator="run",
        )["run"]
        .eq("both")
        .to_numpy()
    )
    skipped_but_offline = x[x["status"].eq("skipped") & (x["_merge"] == "both")]  # сервис пропустил допустимое
    ok = score_bad.empty and alert_bad.empty and service_only.empty and offline_only.empty and skipped_but_offline.empty
    cols = [*KEY, "offline_score"]
    return {
        "ok": bool(ok),
        "service_scored": int(len(scored)),
        "service_skipped": pred[pred["status"] == "skipped"]["skip_reason"].value_counts().to_dict(),
        "offline_decisions": int(len(off)),
        "matched": int(len(both)),
        "max_abs_delta": float(delta.max()) if len(delta) else None,
        "score_mismatch": score_bad[[*KEY, "score", "offline_score"]],
        "alert_mismatch": alert_bad[[*KEY, "score", "offline_score"]],
        "service_only": service_only[[*KEY, "score"]],
        "unscored": offline_only[in_run][cols],
        "missing_in_run": offline_only[~in_run][cols],
        "skipped_but_offline": skipped_but_offline[[*KEY, "skip_reason", "offline_score"]],
        "service_alerts": int(both["alert"].astype(bool).sum()),
        "offline_alerts": int(off["offline_alert"].sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--db", default="postgresql+psycopg://lct:lct@localhost:15432/lct")
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    ap.add_argument("--model-dir", type=Path, default=ROOT / "ml" / "models" / "baseline-v0")
    ap.add_argument("--version", default="baseline-v0", help="версия событийной модели: baseline-v0 или v3")
    ap.add_argument("--score", metavar="URL", help="оценить тревоги прогона через ml-service, а не читать predictions")
    ap.add_argument("--work", type=Path, default=ROOT / "data" / "interim" / "v3", help="кэш дней покрытия для v3")
    ap.add_argument("--tol", type=float, default=1e-6, help="допуск по оценке: сервис округляет до 6 знаков")
    ap.add_argument("--show", type=int, default=10, help="сколько расхождений каждого вида печатать")
    a = ap.parse_args()

    run, pred, alarms = load_run(a.db, a.run)
    print(f"прогон {run['id']} «{run['scenario']}», {run['status']}: {run['sim_start']} — {run['sim_until']}")
    if a.score:
        pred = score_run(a.score, a.run, a.version, alarms)
        print(f"оценено через {a.score}: {len(pred)} тревог")
    versions = sorted(pred["model_version"].unique()) if len(pred) else []
    pred = pred[pred["model_version"] == a.version] if len(pred) else pred
    if pred.empty:
        print(f"прогнозов событийной модели {a.version} нет (версии в журнале: {versions}) — сверять нечего")
        return
    objects = set(pred["object_id"]) | set(alarms["object_id"])
    print(f"прогнозов {a.version}: {len(pred)}, дымовых тревог: {len(alarms)}, версии в журнале: {versions}, "
          f"объектов: {len(objects)}")  # fmt: skip
    if a.version == "v3":
        off, thr = offline_v3(a.archive, a.work, objects, run["sim_start"], run["sim_until"])
    else:
        off, thr = offline(a.archive, a.model_dir, objects, run["sim_start"], run["sim_until"])
    r = compare(pred, off, alarms, a.tol)
    print(
        f"сервис: оценено {r['service_scored']}, пропущено {r['service_skipped']}; "
        f"офлайн-решений {r['offline_decisions']}; "
        f"совпало пар {r['matched']}, max |Δ оценки| {r['max_abs_delta']}; тревог: сервис {r['service_alerts']}, "
        f"офлайн {r['offline_alerts']} (порог {thr})"
    )
    pd.set_option("display.width", 200)
    for key, title in (
        ("score_mismatch", f"оценка расходится больше {a.tol}"),
        ("alert_mismatch", "тревога расходится"),
        ("service_only", "сервис оценил, офлайн такого решения нет"),
        ("unscored", "тревога в прогоне есть, но не оценена сервисом — очередь скоринга или остановка прогона"),
        ("missing_in_run", "офлайн-решение, тревоги которого в прогоне нет — эмулятор или шлюз"),
        ("skipped_but_offline", "сервис пропустил решение, допустимое офлайн"),
    ):
        if len(r[key]):
            print(f"\n{title}: {len(r[key])}")
            print(r[key].head(a.show).to_string(index=False))
    print("\nСОВПАДАЕТ" if r["ok"] else "\nРАСХОЖДЕНИЕ")
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
