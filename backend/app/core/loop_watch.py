"""Сторож цикла событий api: цикл простоял дольше порога — предупреждение в лог с процессорным временем процесса за
простой. Процессор ≈ простою — цикл занял синхронный код; около нуля — процесс не получал процессор (перегружена
VM Docker, хост). Так разбирается «эмулятор недоступен» на прогоне 73: `/health/live` без ожиданий отвечал 16 с,
а запрос к эмулятору до него не дошёл."""

import asyncio
import logging
import time
from collections.abc import Callable

log = logging.getLogger("api.loop")

INTERVAL = 0.5  # с между замерами
THRESHOLD = 2.0  # с сверх интервала — простой, о котором пишем


def cause(lag: float, busy: float) -> str:
    """Чем объяснить простой `lag` с при процессорном времени процесса `busy` с за него."""
    return "синхронный код в цикле" if busy >= lag / 2 else "процесс не получал процессор"


async def watch_loop(
    interval: float = INTERVAL,
    threshold: float = THRESHOLD,
    clock: Callable[[], float] | None = None,
    cpu: Callable[[], float] = time.process_time,
) -> None:
    """`clock` — часы цикла (`loop.time`), `cpu` — процессорное время процесса; в тестах — свои, чтобы проверка не
    зависела от загрузки машины."""
    clock = clock or asyncio.get_running_loop().time
    while True:
        started, used = clock(), cpu()
        await asyncio.sleep(interval)
        lag = clock() - started - interval
        if lag > threshold:
            busy = cpu() - used
            why = cause(lag, busy)
            log.warning("цикл событий api простоял %.1f с, процессор процесса за простой %.1f с — %s", lag, busy, why)
