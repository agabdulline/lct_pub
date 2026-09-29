"""Управление эмулятором СМВУ из интерфейса — прокси в HTTP эмулятора (docs/api.md §1, §3).

`web` к эмулятору напрямую не ходит (D6). Ответы эмулятора (§3) приводятся к схемам `api` (§1): сценарий
с названиями объектов, состояние с `sim_start`, `sim_end`, `events_sent`. Сводка и сигналы сценария до запуска
(13.2) проходят как есть. Запуск сценария создаёт прогон (D21):
текущий останавливается, данные не удаляются; `ml-service` сбрасывает кэш нового прогона; эмулятор стартует
с `run_id` и публикует его в `Emulator/RunId`, по нему шлюз помечает события.
"""

import asyncio
import datetime as dt
import logging
import time
from functools import cache
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.params import csv_list
from app.core.config import settings
from app.db.models import Object, Run
from app.db.session import SessionLocal
from app.domain import model_versions
from app.domain import runs as runs_domain
from app.domain.ml_client import MlClient
from app.external.registry import load_registry

log = logging.getLogger("api.emulator")
router = APIRouter(prefix="/emulator", tags=["эмулятор"])

# Для тестов: транспорт httpx вместо сети
transport: httpx.AsyncBaseTransport | None = None
# Старт без ответа (таймаут): эмулятор ставит run_id, только загрузив срез, — столько ждём его в /state
START_SETTLE_SECONDS = 60.0
START_POLL_SECONDS = 1.0
_settling: set[asyncio.Task] = set()  # фоновые проверки старта — держим ссылки, иначе их соберёт GC


class EmulatorError(Exception):
    """Ошибка эмулятора. `delivered`: True — эмулятор ответил ошибкой; False — запрос до него не дошёл (нет
    соединения); None — неизвестно (таймаут, обрыв): эмулятор мог его выполнить."""

    def __init__(self, status: int, code: str, message: str, delivered: bool | None = True) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.delivered = delivered

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status, content={"error": {"code": self.code, "message": self.message}})


class StartBody(BaseModel):
    scenario: str
    speed: float | None = Field(default=None, description="По умолчанию — скорость сценария")
    primary_model: str | None = Field(
        default=None,
        description="Основная версия событийной модели прогона (15.2): baseline-v0, v3 или связка v3>baseline-v0 — "
        "v3, где оценила, иначе v0; по умолчанию — связка, если v3 и v0 готовы, иначе baseline-v0",
    )
    shadow_models: list[str] | None = Field(
        default=None, description="Теневые версии (15.2): по умолчанию — все остальные готовые, [] — без теневых"
    )


class SpeedBody(BaseModel):
    speed: float


async def _call(
    method: str, path: str, body: dict | None = None, params: list[tuple[str, str]] | None = None, timeout: float = 10
) -> dict:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(base_url=settings.emulator_url, timeout=timeout, transport=transport) as client:
            resp = await client.request(method, path, json=body, params=params)
    except httpx.HTTPError as e:
        delivered = False if isinstance(e, httpx.ConnectError | httpx.ConnectTimeout) else None
        # причина — в лог: у таймаутов httpx текст пустой, а по одному 503 не понять, кто не ответил
        elapsed = time.monotonic() - started
        name = type(e).__name__
        log.warning("эмулятор %s %s: %s за %.1f с (таймаут %s с) %s", method, path, name, elapsed, timeout, e)
        raise EmulatorError(503, "emulator_unavailable", f"Эмулятор недоступен: {e}", delivered) from e
    if resp.is_error:
        try:
            error = resp.json()["error"]
        except (ValueError, KeyError, TypeError):
            error = {"code": "emulator_error", "message": resp.text[:200]}
        raise EmulatorError(resp.status_code, error["code"], error["message"])
    return resp.json()


# --- схемы §1 -----------------------------------------------------------------------------------------------------


def _state(raw: dict) -> dict:
    period = raw.get("period") or {}
    progress = raw.get("progress") or {}
    return {
        "state": raw["state"],
        "scenario": raw.get("scenario"),
        "run_id": raw.get("run_id"),
        "target_speed": raw.get("target_speed"),
        "effective_speed": raw.get("effective_speed"),
        "sim_time": raw.get("sim_time"),
        "sim_start": period.get("start"),
        "sim_end": period.get("end"),
        "events_sent": progress.get("emitted", 0),
        "events_total": progress.get("total", 0),
        "max_speed": raw.get("max_speed"),
    }


async def _object_names(ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    try:
        async with SessionLocal() as session:
            rows = await session.execute(select(Object.id, Object.name).where(Object.id.in_(ids)))
            return dict(rows.all())
    except Exception as e:  # noqa: BLE001 — без БД список сценариев всё равно нужен
        log.warning("названия объектов недоступны: %s", e)
        return {}


@cache
def _registry() -> dict:
    try:
        return load_registry()
    except (OSError, ValueError, KeyError) as e:
        log.warning("реестр внешних наборов недоступен: %s", e)
        return {}


def _dataset(key: str | None) -> dict | None:
    """Внешний набор сценария с данными реестра — по ним интерфейс строит раздел «Внешние данные»
    (external-data.md §5.2). Набора нет в реестре — только ключ."""
    if not key:
        return None
    d = _registry().get(key)
    if d is None:
        return {"key": key, "title": None, "short": None, "url": None, "license": None, "synthetic": None}
    return {
        "key": key,
        "title": d.title,
        "short": d.short,
        "url": d.url,
        "license": d.license,
        "synthetic": d.synthetic,
    }


def _scenario(raw: dict, names: dict[int, str]) -> dict:
    period = raw.get("period") or {}
    return {
        "id": raw["name"],
        "source": raw.get("source", "builtin"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "period_start": period.get("start"),
        "period_end": period.get("end"),
        "objects": [{"id": i, "name": names.get(i, str(i))} for i in raw.get("objects") or []],
        "default_speed": raw.get("speed"),
        "max_speed": raw.get("max_speed"),
        "include_gas": raw.get("include_gas"),
        "timeline": raw.get("timeline") or [],
        "ready": raw.get("ready", False),
        # загруженные (13.1): файл, время загрузки, предупреждения разбора, события и каналы среза
        "source_file": raw.get("source_file"),
        "uploaded_at": raw.get("uploaded_at"),
        "warnings": raw.get("warnings") or [],
        "events": raw.get("events"),
        "channels": raw.get("channels"),
        # внешний набор (этап 14): ключ и реестр — лицензия, ссылка, синтетика
        "dataset": _dataset(raw.get("dataset")),
    }


# --- прогоны ------------------------------------------------------------------------------------------------------


# Коды ошибок выбора версий модели (15.2); остальные коды домена прогонов — 409
VERSION_ERRORS = {"unknown_version": 422, "ml_unavailable": 503}


async def _select_versions(
    scenario: dict, requested: str | None, shadows: list[str] | None = None
) -> tuple[str | None, list[str]]:
    """Основная и теневые версии событийной модели нового прогона — до остановки текущего: при ошибке ничего
    не меняется. ML-сервис недоступен и версия не запрошена — `(None, [])`, одна активная модель. У сценария
    внешнего набора дымовых тревог хакатона нет — версии не выбираются, `primary_model` не применяется."""
    if scenario.get("dataset"):
        return None, []
    try:
        return await model_versions.select_versions(requested, MlClient(transport=transport), shadows)
    except runs_domain.RunError as e:
        raise EmulatorError(VERSION_ERRORS.get(e.code, 409), e.code or "run_error", str(e)) from e


async def _played() -> list[int]:
    """Прогон, который эмулятор уже доиграл до конца (`State = finished`): новый старт закроет его как `finished`, а
    не `stopped`, даже если шлюз ещё не перенёс статус. Эмулятор недоступен — пусто: старт всё равно упадёт на нём."""
    try:
        raw = await _call("GET", "/state")
    except EmulatorError:
        return []
    return [raw["run_id"]] if raw.get("state") == "finished" and raw.get("run_id") else []


async def _create_run(
    scenario: dict, speed: float, primary: str | None = None, shadows: list[str] | None = None
) -> int:
    """Прогон со снимком базы на `sim_start`; текущий прогон домен останавливает сам. Сценарий внешнего набора —
    прогон на его основе (D28): снимок из основы, `base_run_id`; ошибки домена с кодом — 409 с этим кодом.
    `primary`, `shadows` — версии событийной модели (15.2)."""
    dataset = scenario.get("dataset")
    played = await _played()
    params = {k: scenario[k] for k in ("title", "objects", "include_gas")}
    if dataset:
        params["dataset"] = dataset
    async with SessionLocal() as session:
        try:
            run = await runs_domain.create_run(
                session,
                scenario=scenario["name"],
                sim_start=dt.datetime.fromisoformat(scenario["period"]["start"]),
                sim_end=dt.datetime.fromisoformat(scenario["period"]["end"]),
                speed=speed,
                params=params,
                dataset=dataset,
                primary_model=primary,
                shadow_models=shadows or [],
                played=played,
            )
        except runs_domain.RunError as e:
            if e.code is None:
                raise
            raise EmulatorError(409, e.code, str(e)) from e
        await session.commit()
        return run.id


async def _discard_run(run_id: int, reason: str) -> None:
    """Прогон, который эмулятор не начал: без событий — удалить (не оставлять пустые прогоны), иначе — остановить."""
    try:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            if run.events == 0:
                await runs_domain.delete_run(session, run_id)
                log.info("прогон %s удалён: %s", run_id, reason)
            else:
                await runs_domain.set_run_status(session, run_id, "stopped")
                log.warning("прогон %s остановлен: %s, событий уже %s", run_id, reason, run.events)
            await session.commit()
    except Exception as e:  # noqa: BLE001 — ответ на старт уже ушёл; прогон останется, как был
        log.warning("прогон %s: не удалось убрать после неудачного старта: %s", run_id, e)


async def _settle_start(run_id: int) -> None:
    """Старт без ответа: ждём наш `run_id` в `/state` до START_SETTLE_SECONDS. Появился — эмулятор стартовал,
    прогон остаётся `running`; нет — эмулятор его не начал."""
    deadline = time.monotonic() + START_SETTLE_SECONDS
    while True:
        try:
            if (await _call("GET", "/state")).get("run_id") == run_id:
                log.info("прогон %s: эмулятор стартовал после таймаута", run_id)
                return
        except EmulatorError:
            pass
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(START_POLL_SECONDS)
    await _discard_run(run_id, f"эмулятор не стартовал за {START_SETTLE_SECONDS:g} с после таймаута")


async def _set_run_status(run_id: int | None, status: str) -> None:
    """Статус прогона по команде; `finished` и часы прогона переносит шлюз из состояния эмулятора."""
    if not run_id:
        return
    try:
        async with SessionLocal() as session:
            await runs_domain.set_run_status(session, run_id, status)
            await session.commit()
    except Exception as e:  # noqa: BLE001 — команда эмулятору уже выполнена; статус догонит шлюз
        log.warning("прогон %s: не удалось поставить статус %s: %s", run_id, status, e)


async def _reload_ml_cache(run_id: int) -> None:
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport) as client:
            resp = await client.post(f"{settings.ml_service_url}/v1/cache/reload", json={"run_id": run_id})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("ml-service не сбросил кэш прогона %s: %s", run_id, e)


# --- эндпоинты ----------------------------------------------------------------------------------------------------


@router.get("/scenarios")
async def scenarios() -> dict:
    """`{items}` — сценарии: период, объекты с названиями, скорость по умолчанию и предел, готовность среза."""
    try:
        raw = (await _call("GET", "/scenarios"))["scenarios"]
    except EmulatorError as e:
        return e.response()
    names = await _object_names({i for s in raw for i in s.get("objects") or []})
    return {"items": [_scenario(s, names) for s in raw]}


# Сводка по большому загруженному журналу считается при первом запросе — дольше обычной команды
DATA_TIMEOUT = 60


@router.get("/scenarios/{scenario_id}/summary")
async def scenario_summary(scenario_id: str) -> dict:
    """Что в данных сценария до запуска: объекты и подобъекты, каналы по типам и подсистемам, значения, плотность
    потока, ключевые моменты. 404 `scenario_not_found`, 409 `slice_not_ready`."""
    try:
        return await _call("GET", f"/scenarios/{quote(scenario_id, safe='')}/summary", timeout=DATA_TIMEOUT)
    except EmulatorError as e:
        return e.response()


@router.get("/scenarios/{scenario_id}/events")
async def scenario_events(
    scenario_id: str,
    cursor: Annotated[str | None, Query(description="`next_cursor` прошлой страницы")] = None,
    limit: Annotated[int, Query(description="По умолчанию 200, до 1000")] = 200,
    from_: Annotated[str | None, Query(alias="from", description="ISO; без пояса — Москва; включается")] = None,
    to: Annotated[str | None, Query(description="ISO; не включается")] = None,
    object_id: Annotated[list[str] | None, Query(description="объект любого уровня, вложенные включаются")] = None,
    channel_id: Annotated[list[str] | None, Query()] = None,
    sensor_type: Annotated[list[str] | None, Query()] = None,
    value: Annotated[list[str] | None, Query(description="«числовые показания» — все числовые значения")] = None,
    alarm: bool | None = None,
    q: Annotated[str | None, Query(description="подстрока названия канала")] = None,
) -> dict:
    """Сигналы сценария в порядке передачи, страница по курсору; `total` — с фильтрами. Списки — через запятую
    или повтором параметра."""
    params = [(k, v) for k, v in (("cursor", cursor), ("from", from_), ("to", to), ("q", q)) if v is not None]
    params += [("limit", str(limit))] + ([("alarm", str(alarm).lower())] if alarm is not None else [])
    for name, values in (
        ("object_id", object_id),
        ("channel_id", channel_id),
        ("sensor_type", sensor_type),
        ("value", value),
    ):
        params += [(name, v) for item in values or [] for v in csv_list(item)]
    try:
        return await _call(
            "GET", f"/scenarios/{quote(scenario_id, safe='')}/events", params=params, timeout=DATA_TIMEOUT
        )
    except EmulatorError as e:
        return e.response()


@router.get("/state")
async def state() -> dict:
    """Сценарий, прогон, состояние, целевая и фактическая скорость, симуляционное время, отправлено событий."""
    try:
        return _state(await _call("GET", "/state"))
    except EmulatorError as e:
        return e.response()


@router.post("/start")
async def start(body: StartBody) -> dict:
    """Новый прогон сценария: состояние с `run_id`. Текущий прогон останавливается, данные не удаляются.
    `primary_model` — основная версия событийной модели, остальные готовые версии оценивают тревоги тенью (15.2);
    у сценария внешнего набора не применяется. Версии нет — 422 `unknown_version`; ML-сервис недоступен при
    запрошенной версии — 503 `ml_unavailable`."""
    try:
        listed = (await _call("GET", "/scenarios"))["scenarios"]
        scenario = next((s for s in listed if s["name"] == body.scenario), None)
        if scenario is None:
            raise EmulatorError(404, "scenario_not_found", f"Сценария {body.scenario!r} нет")
        if not scenario["ready"]:
            raise EmulatorError(409, "slice_not_ready", f"Срез сценария {body.scenario!r} не подготовлен")
        speed = scenario["speed"] if body.speed is None else body.speed
        if not 1 <= speed <= scenario["max_speed"]:
            raise EmulatorError(422, "speed_out_of_range", f"Скорость {speed:g} вне 1–{scenario['max_speed']:g}")
        primary, shadows = await _select_versions(scenario, body.primary_model, body.shadow_models)
        run_id = await _create_run(scenario, speed, primary, shadows)
        await _reload_ml_cache(run_id)
        try:
            raw = await _call("POST", "/start", {"scenario": body.scenario, "speed": speed, "run_id": run_id})
        except EmulatorError as e:
            if e.delivered is None:
                # ответа нет: эмулятор мог стартовать — большой срез грузится дольше таймаута
                task = asyncio.create_task(_settle_start(run_id))
                _settling.add(task)
                task.add_done_callback(_settling.discard)
            else:
                await _discard_run(run_id, f"эмулятор не стартовал: {e.code}")
            raise
    except EmulatorError as e:
        return e.response()
    log.info("прогон %s: сценарий %s, ×%g, модель %s, теневые %s", run_id, body.scenario, speed, primary, shadows)
    return {**_state(raw), "run_id": run_id}


async def _command(path: str, body: dict | None = None, status: str | None = None) -> dict:
    try:
        raw = await _call("POST", path, body)
    except EmulatorError as e:
        return e.response()
    if status is not None:
        await _set_run_status(raw.get("run_id"), status)
    return _state(raw)


@router.post("/pause")
async def pause() -> dict:
    return await _command("/pause", status="paused")


@router.post("/resume")
async def resume() -> dict:
    return await _command("/resume", status="running")


@router.post("/stop")
async def stop() -> dict:
    """Остановить прогон: данные остаются, прогон — `stopped`."""
    return await _command("/stop", status="stopped")


@router.post("/speed")
async def speed(body: SpeedBody) -> dict:
    """Целевая скорость от 1 до `max_speed` сценария (по умолчанию 60)."""
    return await _command("/speed", {"speed": body.speed})
