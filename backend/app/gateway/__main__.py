"""Шлюз источника событий: `python -m app.gateway` (docs/architecture/emulator.md §7).

Подписывается на эмулятор СМВУ (или реальную систему с тем же адресным пространством) по OPC UA, склеивает
`Value` и `Alarm` в события журнала и передаёт их в приём событий с `run_id` прогона; `SimulatedTime` задаёт
системные часы backend.
"""

import asyncio
import logging

from app.core.config import settings
from app.core.redis import async_client
from app.gateway.opcua import OpcUaSource
from app.gateway.service import Gateway


async def run() -> None:
    source = OpcUaSource(
        settings.opcua_endpoint, settings.opcua_namespace, watchdog_seconds=settings.opcua_watchdog_seconds
    )
    gateway = Gateway(redis=async_client(), pairer=source.pairer)
    await asyncio.gather(source.run(gateway), gateway.run())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    asyncio.run(run())
