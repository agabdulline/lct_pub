"""Проигрыватель сценариев: симуляционное время, скорость, ограничение выхода (docs/architecture/emulator.md §6).

Симуляционное время идёт со скоростью `target_speed` × реальное. Выход ограничен `max_rate` событий в секунду
реального времени (ведро токенов). Во всплеске, когда событий больше, чем позволяет предел, время не уходит
дальше последнего отправленного события — всплеск растягивается, события не теряются, порядок сохраняется (D18).
Фактическая скорость — `effective_speed`, сглаженная за ~1 с.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from app.scenarios import DEFAULT_MAX_SPEED, MOSCOW, SLICE_ORDER, Scenario, all_scenarios
from app.server import Emulator

log = logging.getLogger("emulator.player")

SMOOTHING_SECONDS = 1.0
IDLE, RUNNING, PAUSED, FINISHED, STOPPED = "idle", "running", "paused", "finished", "stopped"


class PlayerError(Exception):
    """Ошибка управления: `code` — машинный код, `status` — HTTP-статус."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Slice:
    """Срез сценария в памяти: параллельные списки, отсортированные по времени."""

    ts: list[float]  # исходное время, unix-секунды
    source: list[datetime]  # SourceTimestamp: исходное время в UTC + порядковый номер в секунде канала, мс
    channel: list[int]
    value: list[str]
    alarm: list[bool]

    def __len__(self) -> int:
        return len(self.ts)


def load_slice(path: Path, known_channels: set[int] | None = None) -> Slice:
    """Срез из Parquet. Несколько событий канала в одну секунду (4.5% событий без газа) получают метки
    +0, +1, +2 мс в исходном порядке: у OPC UA метка источника — ключ склейки `Value` и `Alarm`, а повтор
    той же метки на том же узле подписка не сообщила бы. Шлюз отбрасывает миллисекунды."""
    # своё соединение: общее по умолчанию (`duckdb.sql`) в это время читают потоки HTTP — список сценариев
    with duckdb.connect() as con:
        rows = con.execute(
            "SELECT channel_id, ts, is_alarm, value FROM read_parquet($path, file_row_number = true) "
            f"ORDER BY {SLICE_ORDER}",
            {"path": str(path)},
        ).fetchall()
    data = Slice([], [], [], [], [])
    seq: dict[int, int] = {}
    current = None
    for channel, ts, alarm, value in rows:
        if known_channels is not None and channel not in known_channels:
            continue
        if ts != current:
            current, seq = ts, {}
        k = seq.get(channel, 0)
        seq[channel] = k + 1
        utc = ts.replace(tzinfo=MOSCOW).astimezone(UTC)
        data.ts.append(utc.timestamp())
        data.source.append(utc + timedelta(milliseconds=k))
        data.channel.append(channel)
        data.value.append(value)
        data.alarm.append(bool(alarm))
    return data


def _epoch(naive_moscow: datetime) -> float:
    return naive_moscow.replace(tzinfo=MOSCOW).timestamp()


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, MOSCOW).isoformat()


class Player:
    def __init__(
        self, emulator: Emulator, scenarios_dir: Path, slices_dir: Path, max_rate: float, tick: float = 0.1
    ) -> None:
        self.emulator = emulator
        self.scenarios_dir = scenarios_dir
        self.slices_dir = slices_dir
        self.max_rate = max_rate
        self.tick = tick
        self.state = IDLE
        self.scenario: Scenario | None = None
        self.run_id: int | None = None
        self.target_speed = 1.0
        self.effective_speed = 0.0
        self.sim_now: float | None = None
        self.data = Slice([], [], [], [], [])
        self.cursor = 0
        self._budget = 0.0
        self._touched: set[int] = set()
        self._lock = asyncio.Lock()

    # --- управление -------------------------------------------------------------------------------------------

    def scenarios(self) -> dict[str, Scenario]:
        """Встроенные и загруженные сценарии; загруженные появляются без перезапуска."""
        return all_scenarios(self.scenarios_dir, self.slices_dir, self.max_rate)

    async def start(self, name: str, speed: float | None = None, run_id: int | None = None) -> dict:
        scenario = self.scenarios().get(name)
        if scenario is None:
            raise PlayerError(404, "scenario_not_found", f"Сценария {name!r} нет")
        path = scenario.slice_path(self.slices_dir)
        if not path.is_file():
            raise PlayerError(409, "slice_not_ready", f"Срез сценария {name!r} не подготовлен: make emulator-slices")
        speed = scenario.speed if speed is None else float(speed)
        self._check_speed(speed, scenario.max_speed)
        known = set(self.emulator.catalog.channels)
        data = await asyncio.to_thread(load_slice, path, known)
        async with self._lock:
            if self.state in (RUNNING, PAUSED):
                log.info("прогон %s остановлен новым запуском", self.run_id)
            # значения прошлого прогона не должны выглядеть текущими (emulator.md §3)
            await self.emulator.reset_channels(self._touched)
            self._touched = set()
            self.scenario, self.run_id, self.data, self.cursor = scenario, run_id, data, 0
            self.sim_now = _epoch(scenario.start)
            self.target_speed, self.effective_speed, self._budget = speed, 0.0, 0.0
            self.state = RUNNING
            await self._publish_service()
        log.info("старт %s: %d событий, ×%g, прогон %s", name, len(data), speed, run_id)
        return self.describe()

    async def pause(self) -> dict:
        return await self._transition({RUNNING}, PAUSED)

    async def resume(self) -> dict:
        return await self._transition({PAUSED}, RUNNING)

    async def stop(self) -> dict:
        return await self._transition({RUNNING, PAUSED}, STOPPED)

    async def set_speed(self, speed: float) -> dict:
        max_speed = self.scenario.max_speed if self.scenario else DEFAULT_MAX_SPEED
        self._check_speed(float(speed), max_speed)
        async with self._lock:
            self.target_speed = float(speed)
            await self._publish_service()
        return self.describe()

    async def _transition(self, allowed: set[str], target: str) -> dict:
        async with self._lock:
            if self.state not in allowed:
                raise PlayerError(409, "invalid_state", f"Из состояния {self.state!r} нельзя перейти в {target!r}")
            self.state = target
            if target == RUNNING:
                self._budget = 0.0
            else:
                self.effective_speed = 0.0
            await self._publish_service()
        return self.describe()

    @staticmethod
    def _check_speed(speed: float, max_speed: float) -> None:
        if not 1 <= speed <= max_speed:
            raise PlayerError(422, "speed_out_of_range", f"Скорость {speed:g} вне диапазона 1–{max_speed:g}")

    # --- проигрывание -----------------------------------------------------------------------------------------

    async def run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.tick)
            now = time.monotonic()
            dt, last = now - last, now
            async with self._lock:
                await self.step(dt)
                await self._publish_service()

    async def step(self, dt: float) -> None:
        """Шаг проигрывания за `dt` секунд реального времени."""
        if self.state != RUNNING or self.scenario is None or self.sim_now is None or dt <= 0:
            return
        before = self.sim_now
        end = _epoch(self.scenario.end)
        target = min(self.sim_now + self.target_speed * dt, end)
        # ведро токенов: копится не больше, чем на один шаг, — после тихих участков нет залпа
        self._budget = min(self._budget + self.max_rate * dt, self.max_rate * max(dt, self.tick))
        data = self.data
        while self.cursor < len(data) and data.ts[self.cursor] <= target:
            if self._budget < 1:
                # предел выхода: время стоит на последнем отправленном событии, всплеск растягивается
                target = max(self.sim_now, data.ts[self.cursor - 1]) if self.cursor else self.sim_now
                break
            i = self.cursor
            await self.emulator.publish(data.channel[i], data.source[i], data.value[i], data.alarm[i])
            self._touched.add(data.channel[i])
            self.cursor += 1
            self._budget -= 1
        self.sim_now = max(self.sim_now, target)
        if self.cursor >= len(data) and self.sim_now >= end:
            self.state = FINISHED
            self.effective_speed = 0.0
            log.info("сценарий %s доигран, прогон %s", self.scenario.name, self.run_id)
            return
        alpha = dt / (SMOOTHING_SECONDS + dt)
        self.effective_speed += alpha * ((self.sim_now - before) / dt - self.effective_speed)

    async def _publish_service(self) -> None:
        sim = datetime.now(UTC) if self.sim_now is None else datetime.fromtimestamp(self.sim_now, UTC)
        # RunId — первым: шлюз помечает события прогоном, и смена прогона должна прийти раньше нового состояния
        await self.emulator.set_service(
            RunId=int(self.run_id or 0),
            # после событий шага: всё, что сосчитано, записано раньше этой метки
            EventsSent=int(self.cursor),
            Scenario=self.scenario.name if self.scenario else "",
            State=self.state,
            TargetSpeed=float(self.target_speed),
            EffectiveSpeed=round(float(self.effective_speed), 2),
            SimulatedTime=sim,
        )

    def describe(self) -> dict:
        s = self.scenario
        return {
            "state": self.state,
            "scenario": s.name if s else None,
            "run_id": self.run_id,
            "target_speed": self.target_speed,
            "effective_speed": round(self.effective_speed, 2),
            "max_speed": s.max_speed if s else DEFAULT_MAX_SPEED,
            "sim_time": _iso(self.sim_now),
            "period": {"start": _iso(_epoch(s.start)), "end": _iso(_epoch(s.end))} if s else None,
            "progress": {"emitted": self.cursor, "total": len(self.data)},
            "max_rate": self.max_rate,
        }
