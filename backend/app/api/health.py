import time

import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app.core import keys
from app.core.config import settings
from app.core.redis import async_client
from app.db.session import engine

router = APIRouter(tags=["служебное"])


@router.get("/health/live")
async def live() -> dict:
    """Процесс api жив — для healthcheck контейнера."""
    return {"status": "ok"}


@router.get("/health")
async def health() -> dict:
    """Состояние всех частей системы: БД, Redis, worker, шлюз OPC UA, ML-сервис."""
    components: dict[str, dict] = {}

    try:
        async with engine.connect() as conn:
            ts = await conn.scalar(text("select extversion from pg_extension where extname = 'timescaledb'"))
        components["db"] = {"status": "ok", "timescaledb": ts}
    except Exception as e:  # noqa: BLE001 — health показывает любую причину
        components["db"] = {"status": "down", "error": str(e)[:200]}

    r = async_client()
    try:
        await r.ping()
        components["redis"] = {"status": "ok"}
        heartbeat, gateway, received_at = await r.mget(
            keys.WORKER_HEARTBEAT, keys.GATEWAY_STATUS, keys.CLOCK_SIM_RECEIVED_AT
        )
        age = time.time() - float(heartbeat) if heartbeat else None
        components["worker"] = {
            "status": "ok" if age is not None and age < settings.heartbeat_stale_seconds else "down",
            "heartbeat_age_s": round(age, 1) if age is not None else None,
        }
        clock_age = time.time() - float(received_at) if received_at else None
        components["gateway"] = {
            "status": "ok" if gateway == "connected" else "down",
            "source": settings.opcua_endpoint,
            "clock_age_s": round(clock_age, 1) if clock_age is not None else None,
        }
    except Exception as e:  # noqa: BLE001
        components["redis"] = {"status": "down", "error": str(e)[:200]}

    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{settings.ml_service_url}/health")
        components["ml_service"] = {"status": "ok" if resp.status_code == 200 else "down", **resp.json()}
    except Exception as e:  # noqa: BLE001
        components["ml_service"] = {"status": "down", "error": str(e)[:200]}

    overall = "ok" if all(c.get("status") == "ok" for c in components.values()) else "degraded"
    return {"status": overall, "components": components}
