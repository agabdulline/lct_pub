"""Журнал прогнозов, метрики и модели (docs/api.md, «Прогнозы и метрики»)."""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.api.deps import RunCtx, Session
from app.api.errors import ApiError
from app.api.params import csv_list, page_params
from app.domain.metrics import metrics_now, model_metrics
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.model_versions import (
    TASK,
    chain_of,
    chains,
    default_version,
    other_disagrees_sql,
    paired_sql,
    version_of,
    version_sql,
)
from app.domain.records import PREDICTION_SELECT, RUN, prediction, row_dict
from app.domain.views import object_tree
from app.schemas.api import Metrics, ModelCompare, ModelName, ModelsResponse, Prediction, PredictionPage

router = APIRouter(tags=["прогнозы"])

# чья модель — по семейству (`ModelInfo.author`); у внешних моделей — null
AUTHORS = {
    "baseline": "команда",
    "each_channel": "ML-разработчик",  # ML-инженер и ML-разработчик — один человек (NewDayOK)
    "v3": "ML-разработчик",
    "chain": "ML-разработчик и команда",
}


@router.get("/predictions", response_model=PredictionPage)
async def predictions(
    ctx: RunCtx,
    session: Session,
    model: ModelName | None = None,
    from_: Annotated[dt.datetime | None, Query(alias="from")] = None,
    to: dt.datetime | None = None,
    alert: bool | None = None,
    outcome: Annotated[str | None, Query(description="fault, no_fault, open")] = None,
    status: Annotated[str | None, Query(description="scored, skipped")] = None,
    object_id: int | None = None,
    channel_id: int | None = None,
    seen_channel: bool | None = None,
    object_history: bool | None = None,
    model_version: Annotated[
        str | None, Query(description="версия модели: baseline-v0, v3 (15.2); связка v3>baseline-v0 — её решения")
    ] = None,
    disagree: Annotated[
        bool | None, Query(description="эта и другая версия оценили ту же тревогу, тревога модели разная (15.2)")
    ] = None,
    paired: Annotated[bool | None, Query(description="тревогу оценили без пропуска все версии прогона (15.2)")] = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    """Журнал прогнозов прогона, новые сверху; `from`, `to` — по времени решения."""
    page, size = page_params(page, size)
    where, params = [f"p.{RUN}", "p.decision_time <= :now"], {"run": ctx.run_id, "now": ctx.now}
    if disagree is not None:
        where.append(other_disagrees_sql("p.") if disagree else f"NOT {other_disagrees_sql('p.')}")
    if paired is not None:
        where.append(paired_sql("p.") if paired else f"NOT {paired_sql('p.')}")
    if model_version is not None:  # у связки — её решения тревог, по одному на тревогу
        where.append(version_sql("p.", "model_version"))
        params["model_version"] = model_version
    for column, value in (
        ("p.model", model),
        ("p.alert", alert),
        ("p.channel_id", channel_id),
        ("p.seen_channel", seen_channel),
        ("p.object_history", object_history),
    ):
        if value is not None:
            key = column.split(".")[1]
            where.append(f"{column} = :{key}")
            params[key] = value
    if from_ is not None:
        where.append("p.decision_time >= :from")
        params["from"] = from_
    if to is not None:
        where.append("p.decision_time < :to")
        params["to"] = to
    if statuses := csv_list(status):
        where.append("p.status = ANY(:statuses)")
        params["statuses"] = statuses
    if outcomes := csv_list(outcome):
        # как карточка метрик: исход засчитан, когда горизонт истёк к «сейчас» метрик; раньше решение открыто, даже
        # если исход уже известен (отказ начался, досчёт по разметке набора после конца прогона)
        params["closed_at"] = await metrics_now(session, ctx.run_id, ctx.now)
        parts = [f"(p.outcome = '{o}' AND p.horizon_end <= :closed_at)" for o in outcomes if o in ("fault", "no_fault")]
        if "open" in outcomes:
            parts.append("(p.outcome IS NULL OR p.horizon_end > :closed_at)")
        # the outcome belongs to a scored decision only: a skipped one («без оценки») is neither open nor closed
        where.append(f"p.status = 'scored' AND ({' OR '.join(parts) or 'false'})")
    if object_id is not None:
        tree = await object_tree(session)
        if object_id not in tree.objects:
            raise ApiError(404, "object_not_found", f"объект {object_id} не найден")
        where.append("c.object_id = ANY(:subs)")
        params["subs"] = sorted(tree.sub_objects_under(object_id))
    condition = " AND ".join(where)
    total = await session.scalar(
        text(f"SELECT count(*) FROM predictions p JOIN channels c ON c.id = p.channel_id WHERE {condition}"), params
    )
    rows = await session.execute(
        text(
            f"{PREDICTION_SELECT} WHERE {condition} ORDER BY p.decision_time DESC, p.id DESC "
            f"LIMIT {size} OFFSET {(page - 1) * size}"
        ),
        params,
    )
    return {"items": [row_dict(r) for r in rows], "total": total, "page": page, "size": size}


@router.get("/predictions/{prediction_id}", response_model=Prediction)
async def prediction_detail(prediction_id: int, ctx: RunCtx, session: Session) -> dict:
    found = await prediction(session, prediction_id)
    if found is None or found["run_id"] != ctx.run_id:
        raise ApiError(404, "prediction_not_found", f"прогноз {prediction_id} не найден в прогоне")
    return found


@router.get("/metrics", response_model=Metrics, response_model_by_alias=True)
async def metrics(
    ctx: RunCtx,
    session: Session,
    model: ModelName,
    from_: Annotated[dt.datetime | None, Query(alias="from")] = None,
    to: dt.datetime | None = None,
    model_version: Annotated[
        str | None,
        Query(
            description="версия событийной модели; по умолчанию основная версия прогона (15.2), у связки — её решения"
        ),
    ] = None,
    paired: Annotated[bool, Query(description="только решения, оценённые всеми версиями прогона (15.2)")] = False,
    sensor_type: Annotated[str | None, Query(description="только каналы этого типа датчика (задача C)")] = None,
) -> dict:
    """Метрики по закрытым прогнозам (горизонт истёк относительно «сейчас» прогона): решения, положительные,
    TP/FP/FN/TN, P/R/F1, квантили упреждения; раздельно каналы с историей и без. Открытые — числом `open`.
    Версии событийной модели не смешиваются: одна версия, по умолчанию основная. Ежедневная — смесь моделей по таблице
    маршрутизации, `by_version` и `by_sensor_type` — по моделям и типам датчиков."""
    return await model_metrics(session, ctx.run_id, model, ctx.now, from_, to, model_version, paired, sensor_type)


# Горизонт внешней модели из её конфига (`horizon`): `now` — класс в момент решения, иначе длительность от T
_EXTERNAL_HORIZON = {"now": "класс в момент T", None: "в момент T"}


def _external_model_info(m: dict, active: bool) -> dict:
    """Внешняя модель (этап 14, семейство `external`): имя задачи, набор, отчёт по опытам и классам."""
    report = m.get("offline")
    offline = None
    if report and report.get("test"):
        t = report["test"]
        offline = {"period": report["period"]["label"], "ap": t.get("ap")}
        offline |= {k: t[k] for k in ("precision", "recall", "f1", "tp", "fp", "decisions", "positives")}
    dataset = m.get("dataset")
    horizon = m.get("horizon")
    return {
        "model": m.get("name") or "unknown",
        "version": m.get("label") or m.get("version") or "unknown",
        "family": "external",
        "title": m.get("title") or f"Внешние данные: {dataset} — {m.get('name')}",
        "loaded": True,
        "active": active,
        "threshold": m.get("threshold"),
        "features": m.get("features_used") or 0,
        "trained_on": report["period"]["label"] if report else "",
        "horizon": _EXTERNAL_HORIZON.get(horizon, f"[T, T+{horizon})"),
        "sensor_types": m.get("sensor_types") or [],
        "artifact_sha256": m.get("sha256"),
        "offline_metrics": offline,
        "offline": report,
        "dataset": dataset,
        "synthetic": m.get("synthetic"),
        "trigger": m.get("trigger"),
        "eval_scheme": m.get("eval_scheme"),
        # интерфейс отличает обнаружение от прогноза и подписывает исход (docs/api.md, поля внешних данных)
        "outcome": m.get("outcome"),
        "positive_labels": m.get("positive_labels"),
    }


def _model_info(m: dict, active: bool) -> dict:
    if m.get("family") == "external":
        return _external_model_info(m, active)
    name = m.get("name", "")
    each_channel = m.get("family") == "each_channel"
    # task — с 15.1 в /v1/models; без него отдельные модели типов (each_channel, #243) — ежедневные, старые v3 —
    # по имени. Модель без задачи не должна становиться событийной: иначе она попадает в версии smoke_alarm
    model = m.get("task") or ("daily" if each_channel or "daily" in name else "smoke_alarm")
    family = m.get("family") if m.get("family") in ("v3", "baseline", "each_channel") else "v3"
    periods = m.get("periods") or {}
    train = periods.get("train")
    test = (m.get("metrics") or {}).get("test")
    kind = "Ежедневная" if model == "daily" else "Событийная"
    title = f"{kind} v3" if family == "v3" else f"{kind} — базовая модель команды"
    if each_channel:
        title = f"{kind} — {m.get('label') or name}"
    report = m.get("offline")  # офлайн-отчёт lct-50: период с подписью, рабочая точка на тесте, кривые
    offline = None
    if report:
        t = report["test"]
        offline = {"period": report["period"]["label"], "ap": t.get("ap")}
        offline |= {k: t[k] for k in ("precision", "recall", "f1", "tp", "fp", "decisions", "positives")}
    elif test:
        period = periods.get("test")
        offline = {
            "period": " — ".join(period) if period else "тест",
            "precision": test["precision"],
            "recall": test["recall"],
            "f1": test["f1"],
            "ap": test.get("average_precision"),
            "tp": test["tp"],
            "fp": test["fp"],
            "decisions": test["decisions"],
            "positives": test["positives"],
        }
    return {
        "model": model,
        # метка версии, как в predictions.model_version, runs.primary_model (15.2) и маршрутах ежедневного прогноза;
        # у отдельных моделей типов — имя модели: версии каталогов у ИБП — «old» и «new»
        "version": name if each_channel else version_of(m),
        "family": family,
        "author": AUTHORS.get(family),
        "routes": m.get("routes") if model == "daily" else None,
        "title": title,
        "loaded": True,
        "active": active,
        "threshold": m.get("threshold"),
        "features": m.get("features") or 0,
        "trained_on": " — ".join(train) if train else "",
        "horizon": "[T+24ч, T+30д)" if model == "daily" else "[T, T+24ч)",
        "sensor_types": m.get("sensor_types")
        or ([t["sensor_type"] for t in (report or {}).get("by_type") or []])
        or (["Датчик дыма"] if model == "smoke_alarm" else []),
        "artifact_sha256": m.get("sha256"),
        "offline_metrics": offline,
        "offline": report,
    }


@router.get("/models", response_model=ModelsResponse)
async def models() -> dict:
    """Модели ML-сервиса: версии, пороги, офлайн-метрики. Активная — та, что скорит задачу по умолчанию: у событийной
    — основная версия новых прогонов (15.2), остальные готовые версии скорят тенью; `scoring = false` — не готова.
    Связка событийных версий (`family: chain`) — отдельной записью, когда все её части готовы. `daily_routing` —
    таблица маршрутизации ежедневного прогноза по типам датчиков (задача C)."""
    ml = MlClient()
    try:
        loaded = await ml.models()
    except MlUnavailable as e:
        raise ApiError(503, "ml_unavailable", e.reason) from e
    alarm_default = default_version(loaded)
    items, taken = [], set()
    for m in loaded:
        info = _model_info(m, active=False)
        info["scoring"] = m.get("scoring") == "available"
        if info["model"] == TASK and info["family"] != "external":
            info["active"] = info["scoring"] and info["version"] == alarm_default
        elif info["scoring"] and info["model"] not in taken:
            info["active"] = True
            taken.add(info["model"])
        items.append(info)
    ready = [i["version"] for i in items if i["model"] == TASK and i["family"] != "external" and i["scoring"]]
    for version in chains(ready):
        items.append(await _chain_info(ml, version, items, active=version == alarm_default))
    return {"items": items, "daily_routing": ml.daily_routing}


async def _chain_info(ml: MlClient, version: str, items: list[dict], active: bool) -> dict:
    """Запись связки в `/models`: части — из готовых версий, офлайн — итог связки ml-service (`fallback` сравнения
    версий, `ml.v3_bridge.fallback`), пока его нет — null."""
    parts = [next(i for i in items if i["model"] == TASK and i["version"] == v) for v in chain_of(version)]
    offline = None
    try:
        compare = await ml.models_compare(TASK) or {}
    except MlUnavailable:
        compare = {}
    combo = ((compare.get("fallback") or {}).get("half_year") or {}).get("combo")
    if combo and combo.get("precision") is not None and combo.get("recall") is not None:
        p, r = combo["precision"], combo["recall"]
        base = ((compare.get("period") or {}).get("label") or "офлайн").split(",")[0]
        offline = {
            # у связки своё множество решений: общие для обеих версий и неделя, где v3 молчит по покрытию
            "period": f"{base}, общие решения и неделя после пропуска журнала",
            "precision": p,
            "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0.0,
            "ap": None,
            "tp": combo["tp"],
            "fp": combo["fp"],
            "decisions": combo["decisions"],
            "positives": combo["positives"],
        }
    first, *rest = chain_of(version)
    return {
        "model": TASK,
        "version": version,
        "family": "chain",
        "author": AUTHORS["chain"],
        "chain": chain_of(version),
        "title": f"Связка: {first}, где оценила, иначе {' → '.join(rest)}",
        "loaded": True,
        "active": active,
        "scoring": True,
        "threshold": None,
        "features": 0,
        "trained_on": "",
        "horizon": parts[0]["horizon"],
        "sensor_types": sorted({t for part in parts for t in part["sensor_types"]}),
        "artifact_sha256": None,
        "offline_metrics": offline,
        "offline": None,
    }


@router.get("/models/compare", response_model=ModelCompare, response_model_by_alias=True)
async def models_compare(model: ModelName = TASK) -> dict:
    """Офлайн-сравнение версий событийной модели на одних решениях — считает ml-service (15.2): решения, где версии
    разошлись, кто прав. Не посчитано — 404 `compare_not_found`."""
    try:
        found = await MlClient().models_compare(model)
    except MlUnavailable as e:
        raise ApiError(503, "ml_unavailable", e.reason) from e
    if found is None:
        raise ApiError(404, "compare_not_found", f"сравнение версий модели {model} не посчитано")
    return found
