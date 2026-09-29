"""Клиент ML-сервиса (docs/api.md §2). Недоступность сервиса — не ошибка приёма: прогнозы пишутся пропущенными
с причиной `ml_unavailable`, вердикты остаются по правилам без модели."""

import datetime as dt
import logging

import httpx

from app.core.config import settings

log = logging.getLogger("ml_client")
TIMEOUT = httpx.Timeout(120.0, connect=5.0)  # пересчёт признаков по объекту — 1–2 с, запас на полную историю
# Ежедневный прогноз по типам (15.4): на стенде ~25–30 с на все каналы (первый запрос прогона), на проде до 5 раз
# медленнее. Меньше блокировки такта (tasks/runs.py TICK_LOCK_SECONDS = 300): второй такт его не повторит
DAILY_TIMEOUT = httpx.Timeout(240.0, connect=5.0)


class MlUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MlClient:
    def __init__(self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url or settings.ml_service_url
        self.transport = transport

    async def _post(self, path: str, body: dict, timeout: httpx.Timeout = TIMEOUT) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self.transport) as c:
                r = await c.post(path, json=body)
        except httpx.HTTPError as e:
            raise MlUnavailable(f"ml-service недоступен: {e}") from e
        if r.status_code != 200:
            try:
                code = r.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = f"http_{r.status_code}"
            raise MlUnavailable(code)
        return r.json()

    daily_routing: dict | None = None  # таблица маршрутизации ежедневного прогноза из последнего `models()`

    async def models(self) -> list[dict]:
        """`GET /v1/models` — загруженные модели: версия, семейство, порог, признаки, sha256, офлайн-метрики.
        Таблица маршрутизации ежедневного прогноза из того же ответа — в `daily_routing`."""
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, transport=self.transport) as c:
                r = await c.get("/v1/models")
        except httpx.HTTPError as e:
            raise MlUnavailable(f"ml-service недоступен: {e}") from e
        if r.status_code != 200:
            raise MlUnavailable(f"http_{r.status_code}")
        body = r.json()
        self.daily_routing = body.get("daily_routing")
        return body.get("models", [])

    async def models_compare(self, model: str) -> dict | None:
        """`GET /v1/models/compare` — офлайн-сравнение версий модели на одних решениях (15.2); не посчитано — None."""
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0, transport=self.transport) as c:
                r = await c.get("/v1/models/compare", params={"model": model})
        except httpx.HTTPError as e:
            raise MlUnavailable(f"ml-service недоступен: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise MlUnavailable(f"http_{r.status_code}")
        return r.json()

    async def score_alarms(
        self, run_id: int | None, alarms: list[tuple[int, dt.datetime]], model_version: str | None = None
    ) -> dict:
        """`{model, results: [...] в порядке запроса, elapsed_ms}`. `model_version` — версия событийной модели
        (теневой скоринг, 15.2); без неё — активная."""
        body = {
            "run_id": run_id,
            "alarms": [{"channel_id": c, "ts": ts.isoformat()} for c, ts in alarms],
            "explain": True,
        }
        if model_version is not None:
            body["model_version"] = model_version
        return await self._post("/v1/alarm/score", body)

    async def score_daily(self, run_id: int | None, date: dt.date) -> dict:
        body = {"run_id": run_id, "date": date.isoformat(), "channel_ids": None}
        return await self._post("/v1/daily/score", body, timeout=DAILY_TIMEOUT)

    async def score_external(self, run_id: int, model: str, decision_time: dt.datetime) -> dict:
        """Внешняя модель (этап 14) на все цели набора: `{model, dataset, decision_time, results, elapsed_ms}`."""
        body = {
            "run_id": run_id,
            "model": model,
            "decision_time": decision_time.isoformat(),
            "targets": None,
            "explain": False,
        }
        return await self._post("/v1/external/score", body)

    async def score_external_events(
        self, run_id: int, model: str, decision_time: dt.datetime, events: list[dict]
    ) -> dict:
        """Событийная внешняя модель (`trigger = on_event`): по результату на событие `{channel_id, ts, value}`."""
        body = {"run_id": run_id, "model": model, "decision_time": decision_time.isoformat(), "events": events}
        return await self._post("/v1/external/score", body)
