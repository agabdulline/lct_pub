import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app import access, history
from app.config import settings
from app.models import load_all, load_compare, load_routing
from app.scoring import (
    feature_module,
    score_daily,
    score_external,
    score_external_events,
    score_object,
    score_object_v3,
    target_channels,
)

state: dict = {"models": [], "compare": {}, "routing": None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["models"] = load_all(settings.model_dir)
    state["compare"] = load_compare(settings.model_dir, state["models"])
    state["routing"] = load_routing(settings.model_dir, state["models"])
    yield


app = FastAPI(title="LCT · ML-сервис", version="0.2.0", lifespan=lifespan)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def scoring_model(kind: str = "smoke_alarm", version: str | None = None):
    """Модель, которая скорит задачу kind: версия `version` (label: baseline-v0, v3) или активная задачи."""
    ready = [m for m in state["models"] if m.scoring and m.task == kind]
    if version is not None:
        return next((m for m in ready if m.label == version), None)
    return next((m for m in ready if m.active), ready[0] if ready else None)


class Alarm(BaseModel):
    channel_id: int
    ts: datetime


class AlarmScoreRequest(BaseModel):
    run_id: int | None = None
    alarms: list[Alarm]
    explain: bool = True
    model_version: str | None = None  # label версии (baseline-v0, v3) для теневого скоринга; None — активная


class DailyScoreRequest(BaseModel):
    run_id: int | None = None
    date: date_type
    channel_ids: list[int] | None = None
    explain: bool = False


class ExternalEvent(BaseModel):
    channel_id: int
    ts: datetime
    value: str


class ExternalScoreRequest(BaseModel):
    run_id: int | None = None
    model: str  # задача: magdeburg_fire, vast_gate, vast_badge, pdm_failure_24h
    decision_time: datetime
    targets: list[int] | None = None  # узлы, агрегаты или каналы; None — все цели набора
    events: list[ExternalEvent] | None = None  # у моделей с target = event (vast_gate): принятые события
    explain: bool = False


class AccessPoint(BaseModel):
    """Точка решения модели доступа — первое открытие кандидата правила (backend, access.md §4)."""

    sub_object_id: int
    channel_id: int
    start_ts: datetime
    is_alarm: bool
    hatch: bool
    armed_since: datetime | None = None
    guard_faulty: bool = False
    guard_channel_id: int
    opener_ids: list[int]


class AccessScoreRequest(BaseModel):
    run_id: int | None = None
    points: list[AccessPoint]


class ReloadRequest(BaseModel):
    run_id: int | None = None


@app.get("/health")
def health() -> dict:
    model, daily = scoring_model(), scoring_model("daily")
    return {
        "models_loaded": len(state["models"]),
        "scoring": model.label if model else "unavailable",
        "alarm_versions": [m.label for m in state["models"] if m.scoring and m.task == "smoke_alarm"],
        "daily_scoring": daily.label if daily else "unavailable",
    }


@app.get("/v1/models")
def models() -> dict:
    routing = state["routing"]
    return {"models": [m.describe() for m in state["models"]], "daily_routing": routing.describe() if routing else None}


@app.get("/v1/models/compare", response_model=None)
def models_compare(model: str = "smoke_alarm") -> dict | JSONResponse:
    """Версии задачи на общих решениях проверки: метрики каждой на своём пороге и где они расходятся."""
    compare = state["compare"].get(model)
    if compare is None:
        return error(404, "compare_not_found", f"Сравнения версий для {model} нет или оно посчитано для других моделей")
    return compare


@app.post("/v1/alarm/score", response_model=None)
def alarm_score(req: AlarmScoreRequest) -> dict | JSONResponse:
    model = scoring_model(version=req.model_version)
    if model is None and req.model_version is not None:
        ready = [m.label for m in state["models"] if m.scoring and m.task == "smoke_alarm"]
        return error(422, "unknown_version", f"Версии {req.model_version} нет среди готовых: {', '.join(ready)}")
    if model is None:
        return error(503, "model_missing", "Нет модели со скорингом тревог: ни v3, ни базовой")
    tz = ZoneInfo(settings.timezone)
    started = time.perf_counter()

    def local(ts: datetime) -> pd.Timestamp:
        return pd.Timestamp(ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)).tz_localize(None)

    alarms = [(a.channel_id, local(a.ts)) for a in req.alarms]
    try:
        object_of = history.objects_of(sorted({cid for cid, _ in alarms}))
        by_object: dict[int, list] = defaultdict(list)
        results: dict[tuple, dict] = {}
        for cid, ts in alarms:
            if cid in object_of:
                by_object[object_of[cid]].append((cid, ts))
            else:
                results[(cid, ts)] = {
                    "channel_id": cid,
                    "alarm_ts": ts,
                    "status": "skipped",
                    "skip_reason": "unknown_channel",
                }
        for obj, items in by_object.items():
            channels = history.smoke_channels(obj)
            smoke = set(channels["channel_id"])
            for cid, ts in items:
                if cid not in smoke:
                    results[(cid, ts)] = {
                        "channel_id": cid,
                        "alarm_ts": ts,
                        "status": "skipped",
                        "skip_reason": "not_smoke_channel",
                    }
            wanted = [(c, t) for c, t in items if c in smoke]
            if not wanted:
                continue
            until = max(t for _, t in wanted)
            events = history.smoke_events(req.run_id, obj, sorted(smoke), model.config["history_start"], until)
            if model.family == "v3":
                coverage = history.coverage(req.run_id) | {t.normalize() for _, t in wanted}
                rows = history.v3_rows(req.run_id, obj)
                scored = score_object_v3(model, events, channels, coverage, rows, wanted, req.explain)
            else:
                scored = score_object(model, events, channels, wanted, req.explain)
            for r in scored:
                results[(r["channel_id"], r["alarm_ts"])] = r
    except (SQLAlchemyError, OSError) as e:
        return error(503, "db_unavailable", f"Рабочая база недоступна: {str(e)[:200]}")

    out = []
    for cid, ts in alarms:
        r = dict(results[(cid, ts)])
        for key in ("alarm_ts", "decision_time"):
            if r.get(key) is not None:
                r[key] = r[key].tz_localize(tz).isoformat()
        out.append(r)
    return {
        "model": model.label,
        "results": out,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


@app.post("/v1/daily/score", response_model=None)
def daily_score(req: DailyScoreRequest) -> dict | JSONResponse:
    """Ежедневный прогноз по типам: канал решает модель своего типа по таблице маршрутизации (15.4), по умолчанию —
    активная ежедневная (v0). `results[].model` — id модели строки, `routing` — версия таблицы."""
    model = scoring_model("daily")
    if model is None:
        return error(503, "daily_model_missing", "Нет ежедневной модели со скорингом")
    started = time.perf_counter()
    cfg = model.config
    routing = state["routing"]
    wanted = None if req.channel_ids is None else list(dict.fromkeys(req.channel_ids))
    day = pd.Timestamp(req.date)
    routes = routing.routes if routing else dict.fromkeys(cfg["types"], model.model_id)
    others = {mid for mid in routes.values() if mid != model.model_id}
    try:
        # признаки объекта v0 — по каналам всех её восьми типов, как при обучении; решает она только по своим типам
        channels, daily, episodes = history.daily_inputs(
            req.run_id, req.date, cfg["types"], wanted, cfg["history_days"]
        )
        type_of = dict(zip(channels["channel_id"], channels["sensor_type"], strict=True))
        asked = wanted if wanted is not None else list(type_of)
        mine = [c for c in asked if routes.get(type_of.get(c)) == model.model_id]
        results = score_daily(model, daily, episodes, channels, day, req.explain, only=mine)
        routed = {mid: [c for c, t in type_of.items() if routes.get(t) == mid] for mid in others}
        if wanted is not None:
            routed = {mid: [c for c in ids if c in set(wanted)] for mid, ids in routed.items()}
        routed = {mid: ids for mid, ids in routed.items() if ids}
        if routed:
            by_id = {m.model_id: m for m in state["models"] if m.task == "daily"}
            types = [by_id[mid].artifact.type for mid in routed]
            x = history.routed_inputs(req.run_id, day, types)
            for mid, ids in routed.items():
                results += routed_results(by_id[mid], x, ids, day, req.run_id)
        supported = {c for c, t in type_of.items() if t in routes}
        missing = [c for c in wanted or [] if c not in supported]
        known = history.known_channels(missing) if missing else set()
    except (SQLAlchemyError, OSError) as e:
        return error(503, "db_unavailable", f"Рабочая база недоступна: {str(e)[:200]}")
    for c in missing:
        results.append(
            {
                "channel_id": c,
                "target_start": day + pd.Timedelta(days=1),
                "target_end": day + pd.Timedelta(days=30),
                "threshold": model.threshold,
                "seen_channel": None,
                "object_history": None,
                "model": model.label,
                "status": "skipped",
                "skip_reason": "unsupported_type" if c in known else "unknown_channel",
                "score": None,
                "alert": None,
                "factors": [],
            }
        )
    if wanted is not None:  # порядок — как в запросе
        pos = {c: k for k, c in enumerate(wanted)}
        results.sort(key=lambda r: pos[r["channel_id"]])
    tz = ZoneInfo(settings.timezone)
    for r in results:
        for key in ("target_start", "target_end"):
            r[key] = r[key].tz_localize(tz).isoformat()
    return {
        "model": model.label,
        "routing": routing.version if routing else None,
        "date": req.date.isoformat(),
        "results": results,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def routed_results(model, x, ids: list[int], day: pd.Timestamp, run_id: int | None) -> list[dict]:
    """Строки ответа отдельной модели типа — в формате score_daily; пропуск — статус его кода (in_fault, no_coverage).
    У извещателя порог — месяца, готовность ниже 1 500 созревших положительных гасит тревоги, оценка остаётся."""
    rows = model.artifact.score(x, only=ids, key=run_id)
    out = []
    for r in rows.itertuples(index=False):
        scored = r.status == "scored" and not pd.isna(r.score)
        out.append(
            {
                "channel_id": int(r.channel_id),
                "target_start": day + pd.Timedelta(days=1),
                "target_end": day + pd.Timedelta(days=30),
                "threshold": round(float(r.threshold), 6) if not pd.isna(r.threshold) else None,
                "seen_channel": None,
                "object_history": None,
                "model": model.model_id,
                "status": "scored" if scored else "skipped",
                "skip_reason": None if scored else r.status,
                "score": round(float(r.score), 6) if scored else None,
                "alert": bool(r.alert) if scored else None,
                "factors": [],
            }
        )
    return out


@app.post("/v1/external/score", response_model=None)
def external_score(req: ExternalScoreRequest) -> dict | JSONResponse:
    """Модели на внешних данных (docs/architecture/external-data.md): отдельное семейство, свой набор каналов."""
    model = scoring_model(req.model)
    if model is None or model.family != "external":
        return error(404, "model_not_found", f"Нет внешней модели со скорингом: {req.model}")
    started = time.perf_counter()
    decision_time = pd.Timestamp(req.decision_time)
    decision_time = (
        decision_time.tz_convert("UTC")
        if decision_time.tzinfo
        else decision_time.tz_localize(settings.timezone).tz_convert("UTC")
    )
    history_span = pd.Timedelta(minutes=model.config.get("history_minutes", 0))
    if model.config.get("target") == "event":
        return _external_events(model, req, decision_time, history_span, started)
    try:
        channels = history.external_channels(model.config["dataset"])
        # по умолчанию — все объекты с каналом цели, у старых конфигов без target_tag — все объекты с каналами
        default = sorted(target_channels(model, channels)) or sorted(set(channels["object_id"]))
        targets = req.targets if req.targets is not None else default
        # все каналы набора: признаки узла сравнивают его с остальными узлами того же помещения; у сигналов со своим
        # окном (history_minutes_by_tag: телеметрия — сутки, журнал ТО — 90 суток) — по окну на сигнал
        by_tag = model.config.get("history_minutes_by_tag") or {}
        windows: dict[int, list[int]] = {}
        for tag, ids in channels.groupby("measure")["channel_id"]:
            if by_tag and tag not in by_tag:
                continue  # сигнал модели не нужен — из базы не читаем
            windows.setdefault(by_tag.get(tag, model.config.get("history_minutes", 0)), []).extend(ids.tolist())
        events = pd.concat(
            [
                history.external_events(req.run_id, ids, decision_time - pd.Timedelta(minutes=m), decision_time)
                for m, ids in sorted(windows.items())
            ],
            ignore_index=True,
        )
        need_units = getattr(feature_module(model), "UNITS", False)
        units = history.external_units(model.config["dataset"]) if need_units else None
    except (SQLAlchemyError, OSError) as e:
        return error(503, "db_unavailable", f"Рабочая база недоступна: {str(e)[:200]}")
    targets = list(dict.fromkeys(targets))
    results = score_external(model, events, channels, targets, decision_time, req.explain, units=units)
    return {
        "model": model.label,
        "dataset": model.config["dataset"],
        "decision_time": decision_time.tz_convert(settings.timezone).isoformat(),
        "results": results,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def _external_events(model, req: ExternalScoreRequest, decision_time, history_span, started) -> dict | JSONResponse:
    """Событийная внешняя модель: решение по каждому переданному событию, история — прошлые события того же пропуска."""
    if not req.events:
        return error(422, "events_required", f"Модели {req.model} нужны события: поле events")
    dataset = model.config["dataset"]
    targets = pd.DataFrame([e.model_dump() for e in req.events])
    ts = pd.to_datetime(targets["ts"])
    targets["ts"] = (
        ts.dt.tz_convert("UTC") if ts.dt.tz is not None else ts.dt.tz_localize(settings.timezone).dt.tz_convert("UTC")
    )
    values = sorted(set(targets["value"]))
    try:
        channels = history.external_channels(dataset)
        past = history.external_passes(
            req.run_id, channels["channel_id"].tolist(), values, targets["ts"].min() - history_span, targets["ts"].max()
        )
        classes = history.external_classes(dataset, values)
        permits = history.external_permits(dataset)
    except (SQLAlchemyError, OSError) as e:
        return error(503, "db_unavailable", f"Рабочая база недоступна: {str(e)[:200]}")
    results = score_external_events(model, targets, past, channels, classes, permits, decision_time)
    tz = settings.timezone
    for r in results:
        r["event_ts"] = pd.Timestamp(r["event_ts"]).tz_convert(tz).isoformat()
        r["decision_time"] = pd.Timestamp(r["decision_time"]).tz_convert(tz).isoformat()
    return {
        "model": model.label,
        "dataset": dataset,
        "decision_time": decision_time.tz_convert(tz).isoformat(),
        "results": results,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


@app.post("/v1/access/score", response_model=None)
def access_score(req: AccessScoreRequest) -> dict | JSONResponse:
    """Модель доступа: «нужна реакция» (охрану не снимут за 5 мин) или «вероятно свой» при открытии под охраной —
    прокси «свой или чужой» по тому, снимут ли охрану, а не обнаружение проникновения. Признаки — `ml.access.features`
    по истории представления прогона."""
    model = scoring_model("access_open")
    if model is None:
        return error(503, "access_model_missing", "Нет модели доступа со скорингом")
    if not req.points:
        return {"model": model.label, "threshold": model.threshold, "results": [], "elapsed_ms": 0}
    started = time.perf_counter()
    points = pd.DataFrame([p.model_dump() for p in req.points])
    for col in ("start_ts", "armed_since"):
        points[col] = pd.to_datetime(points[col], utc=True)
    try:
        past, guard, openers = history.access_inputs(req.run_id, points)
    except (SQLAlchemyError, OSError) as e:
        return error(503, "db_unavailable", f"Рабочая база недоступна: {str(e)[:200]}")
    x = access.features(points, past, guard, openers)
    scores = access.score(model, x)
    tz = ZoneInfo(settings.timezone)
    results = [
        {
            "sub_object_id": int(p.sub_object_id),
            "start_ts": p.start_ts.tz_convert(tz).isoformat(),
            "status": "scored",
            "score": s,
            "threshold": model.threshold,
            "alert": s >= model.threshold,
        }
        for p, s in zip(points.itertuples(), scores, strict=True)
    ]
    return {
        "model": model.label,
        "threshold": model.threshold,
        "results": results,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


@app.post("/v1/cache/reload")
def cache_reload(req: ReloadRequest) -> dict:
    return {"run_id": req.run_id, "dropped": history.reload(req.run_id)}
