"""Процесс эмулятора: OPC UA сервер, проигрыватель и HTTP-управление в одном цикле asyncio."""

import asyncio
import logging

import uvicorn

from app.catalog import Catalog, emulator_catalog
from app.config import settings
from app.control import create_control
from app.player import Player
from app.scenarios import EXTERNAL_DIR
from app.server import Emulator

log = logging.getLogger("emulator")


def read_catalog() -> Catalog:
    """Справочник хакатона и внешних наборов из `make emulator-slices` (external-data.md §5.1)."""
    return emulator_catalog(settings.objects_csv, settings.channels_csv, settings.slices_dir / EXTERNAL_DIR)


async def main() -> None:
    emulator = Emulator(read_catalog())
    await emulator.init()
    player = Player(emulator, settings.scenarios_dir, settings.slices_dir, settings.max_rate, settings.tick_seconds)
    control = create_control(player)
    http = uvicorn.Server(uvicorn.Config(control, host="0.0.0.0", port=settings.control_port, log_level="info"))
    async with emulator.server:
        await asyncio.gather(player.run(), http.serve())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    asyncio.run(main())
