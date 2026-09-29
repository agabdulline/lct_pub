"""Живой поток `GET /stream?run=` — SSE выбранного прогона (D5, docs/api.md «Живой поток»).

События приходят из Redis pub/sub (`stream:<run_id>`), их публикуют приём событий и задачи после коммита.
Раз в секунду в режиме потока — `clock.tick`; раз в 15 с — комментарий-пинг, чтобы прокси не закрывали соединение.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import RunQuery, resolve_context
from app.core import clock, keys
from app.core.redis import async_client
from app.db.session import SessionLocal
from app.domain.notify import json_default, stream_channel

router = APIRouter(tags=["тревоги"])

TICK_SECONDS = 1.0
PING_SECONDS = 15.0


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=json_default)}\n\n"


async def _clock_tick(redis, run_id: int | None, speed: float | None, status: str | None) -> dict | None:
    now = await clock.current()
    # часы и состояние источника — только своего прогона: у нового прогона до его первого `SimulatedTime` там ещё
    # конец прошлого (гонка смены сценария, как у событий — backend.md §3)
    if now["mode"] != "stream" or now["run_id"] != run_id:
        return None
    state = status
    try:
        stats = json.loads(await redis.get(keys.GATEWAY_STATS) or "{}")
        if stats.get("state_run_id") == run_id and stats.get("state"):
            state = stats["state"]
    except (ValueError, OSError):
        pass
    return {**now, "run_id": run_id, "effective_speed": speed, "state": state}


async def events(request: Request, run_id: int | None, speed: float | None, status: str | None) -> AsyncIterator[str]:
    redis = async_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(stream_channel(run_id))
    last_tick = last_ping = 0.0
    try:
        yield ": connected\n\n"
        while not await request.is_disconnected():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if message is not None:
                payload = json.loads(message["data"])
                yield sse(payload["type"], payload["data"])
                continue
            now = time.monotonic()
            if now - last_tick >= TICK_SECONDS:
                last_tick = now
                tick = await _clock_tick(redis, run_id, speed, status)
                if tick:
                    yield sse("clock.tick", tick)
            if now - last_ping >= PING_SECONDS:
                last_ping = now
                yield ": ping\n\n"
    except (asyncio.CancelledError, ConnectionError):
        pass
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
        await redis.aclose()


@router.get("/stream")
async def stream(request: Request, run: RunQuery = None) -> StreamingResponse:
    """SSE: `alarm.created`, `alarm.scored`, `alarm_episode.updated`, `prediction.created`, `prediction.updated`,
    `daily_forecast.completed`, `work_order.created`, `channel.status`, `clock.tick`, `equipment_episode.created`,
    `equipment_episode.updated`."""
    # Прогон — в своей короткой сессии, закрытой до начала стрима: сессия зависимости жила бы, пока открыт стрим,
    # с открытой транзакцией (app/api/deps.py, RunCtx). Дальше стрим в БД не ходит — только Redis
    async with SessionLocal() as session:
        ctx = await resolve_context(session, run)
    found = ctx.run
    return StreamingResponse(
        events(request, ctx.run_id, found.speed if found else None, found.status if found else None),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
