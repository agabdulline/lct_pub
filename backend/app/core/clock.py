"""Системные часы: в режиме потока — SimulatedTime эмулятора, иначе реальное время (D10)."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core import keys
from app.core.config import settings
from app.core.redis import async_client


async def current() -> dict:
    """Часы источника. В режиме `stream` — и `run_id`: прогон, к которому `SimulatedTime` относится по метке записи
    (шлюз). Часы чужого прогона своему не показываются — у только что созданного прогона это конец прошлого."""
    try:
        sim, received_at, run = await async_client().mget(keys.CLOCK_SIM, keys.CLOCK_SIM_RECEIVED_AT, keys.CLOCK_RUN)
    except Exception:
        sim, received_at, run = None, None, None
    if sim and received_at and time.time() - float(received_at) < settings.clock_stale_seconds:
        return {"mode": "stream", "now": sim, "run_id": int(run) if run else None}
    return {"mode": "real", "now": datetime.now(ZoneInfo(settings.timezone)).isoformat()}
