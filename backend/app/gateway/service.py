"""Приёмник шлюза: события источника → приём событий backend, служебные значения → часы и прогон.

- События помечаются прогоном, который шёл в момент их записи у источника (`RunTimeline`), и пачками раз
  в `flush_seconds` уходят в `app.domain.ingest.ingest_events` — тот же код, что у `POST /ingest/events`. Дубли
  отсекает приём. Прогон на момент приёма был ненадёжен: `RunId` и события — разные элементы подписки, и первые
  события нового сценария приходили раньше его `RunId` — в прошлый прогон (прогоны 30–33 стенда).
- Без прогона (`RunId = 0`) события не пишутся: базовая история (`run_id = NULL`) — только журнал (D21).
- `SimulatedTime` задаёт системные часы backend в режиме `stream` (D10) — вместе с прогоном по метке записи.
- Состояние источника переносится в статус прогона: `paused`, `running`, `finished`, `stopped`.
- Прогон, который источник больше не ведёт (эмулятор перезапущен и в `idle`, или публикует другой `RunId`),
  закрывается как `stopped` с причиной `interrupted` — иначе он висел бы активным до следующего запуска.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core import keys
from app.db.models import Run
from app.db.session import SessionLocal
from app.domain import runs as runs_domain
from app.domain.ingest import IngestResult, RawEvent, ingest_events
from app.domain.notify import Notification, publish
from app.domain.runs import FINAL_STATUSES
from app.gateway.audit import EventAudit

log = logging.getLogger("gateway")

Ingest = Callable[[int | None, list[RawEvent]], Awaitable[IngestResult]]
RUN_STATUSES = {"running", "paused", "finished", "stopped"}
# Служебные значения, которые относятся к прогону по метке записи (`Gateway.settled`)
SETTLED = ("State", "SimulatedTime")
# Прогон моложе — не прерванный: `api` создаёт его раньше, чем эмулятор начнёт публиковать его `RunId`
INTERRUPT_GRACE = timedelta(seconds=30)


class RunTimeline:
    """Хронология `RunId` источника по меткам записи (`ServerTimestamp`).

    Эмулятор пишет `RunId` каждый шаг (0.1 с), метки записи у него строго возрастают (emulator.md §3). Элемент
    подписки отдаёт значения по порядку, поэтому пришедшее значение `RunId` с меткой `W` значит: все смены прогона
    до `W` уже известны (`seen_until`). Событие с меткой записи `E ≤ seen_until` относится к прогону, который шёл
    в `E`, — в каком бы порядке ни пришли уведомления разных элементов. Событие позже `seen_until` ждёт следующего
    пульса. Источник без пульса (`RunId` пишется только при смене) или без меток — прогон на момент приёма, как
    раньше.
    """

    MAX_CHANGES = 64

    def __init__(self) -> None:
        self.changes: list[tuple[datetime | None, int | None]] = []  # (с какой метки, прогон); None — с начала
        self.seen_until: datetime | None = None
        self.heartbeat = False

    @property
    def current(self) -> int | None:
        return self.changes[-1][1] if self.changes else None

    def note(self, run: int | None, written: datetime | None) -> None:
        if written is None:  # источник без меток: прогон — с этого момента и до следующего значения
            self.changes, self.seen_until, self.heartbeat = [(None, run)], None, False
            return
        if not self.changes:
            self.changes = [(None, run)]  # когда начался прогон, который застали при подключении, неизвестно
        elif run == self.current:
            self.heartbeat = True
        else:
            self.changes = [*self.changes[-self.MAX_CHANGES + 1 :], (written, run)]
        if self.seen_until is None or written > self.seen_until:
            self.seen_until = written

    def known(self, written: datetime | None) -> bool:
        """Все смены прогона до этой метки записи известны — прогон события можно определить."""
        if written is None or not self.heartbeat:
            return True
        return self.seen_until is not None and written <= self.seen_until

    def run_at(self, written: datetime | None) -> int | None:
        if written is None:
            return self.current
        run = self.changes[0][1] if self.changes else None
        for since, changed in self.changes:
            if since is None or since <= written:
                run = changed
            else:
                break
        return run


@dataclass
class Stats:
    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    unknown_channels: int = 0
    without_run: int = 0
    waiting_run: int = 0  # ждут пульса RunId: прогон по метке записи ещё не определить
    failed_flushes: int = 0
    last_flush_ms: float = 0.0


class Gateway:
    def __init__(
        self,
        redis=None,
        ingest: Ingest = ingest_events,
        flush_seconds: float = 0.5,
        max_buffer: int = 200_000,
        pairer=None,
    ) -> None:
        self.redis = redis
        self.ingest = ingest
        self.flush_seconds = flush_seconds
        self.max_buffer = max_buffer
        self.pairer = pairer
        self.buffer: list[tuple[int, RawEvent]] = []
        self.waiting: list[RawEvent] = []  # события, чей прогон ещё не определить (`RunTimeline.known`)
        self.timeline = RunTimeline()
        self.run_id: int | None = None
        self.service: dict = {}
        self.written: dict[str, datetime | None] = {}  # метки записи служебных значений
        self.stats = Stats()
        self._synced: tuple[int | None, str | None] = (None, None)
        self._clock_synced_at = 0.0
        # `State` и `SimulatedTime`, чей прогон по метке записи уже известен: (прогон, значение). Последнее пришедшее
        # значение обычно ещё не определить: в цикле публикации оно записано после последнего пульса `RunId`
        self.settled: dict[str, tuple[int | None, object]] = {}
        self._unsettled: set[str] = set()
        # прогоны, которые источник вёл при этом процессе шлюза: «прерван» только тот, кого вели (сверка прогонов)
        self.seen_runs: set[int] = set()
        self._started_at = datetime.now(UTC)
        self._connected_at: float | None = None
        self._service_at: float | None = None
        self._reconciled_at = 0.0
        # сверка принятых событий со счётчиком источника `EventsSent` (audit.py): потери прогона и их окна
        self.audit = EventAudit()
        self._losses_checked_at = 0.0
        self._losses_dirty: set[int] = set()

    # --- EventSink ----------------------------------------------------------------------------------------------

    def on_event(self, event: RawEvent) -> None:
        self.stats.received += 1
        if len(self.waiting) >= self.max_buffer:
            log.error("без известного прогона %d событий — старое — к прогону на момент приёма", len(self.waiting))
            self._accept(self.timeline.current, self.waiting.pop(0))
        self.waiting.append(event)
        self._release()

    def _release(self) -> None:
        """События, чей прогон уже определён, — в буфер своего прогона. Порядок событий канала сохраняется: метки
        записи канала растут, и раньше пришедшее событие канала определяется не позже следующего."""
        if not self.waiting:
            return
        still = []
        for event in self.waiting:
            if self.timeline.known(event.written):
                self._accept(self.timeline.run_at(event.written), event)
            else:
                still.append(event)
        self.waiting = still
        self.stats.waiting_run = len(still)

    def _accept(self, run_id: int | None, event: RawEvent) -> None:
        if run_id is None:
            if self.stats.without_run == 0:
                log.warning("событие без прогона (RunId = 0) — не пишется: базовая история только из журнала")
            self.stats.without_run += 1
            return
        if len(self.buffer) >= self.max_buffer:
            log.error("буфер шлюза переполнен (%d), старое событие отброшено", self.max_buffer)
            self.buffer.pop(0)
        self.buffer.append((run_id, event))
        self.audit.received(run_id, event)

    async def on_service(self, name: str, value, written: datetime | None = None) -> None:
        self.service[name] = value
        self.written[name] = written
        self._service_at = time.monotonic()
        if name == "RunId":
            self.run_id = int(value) if value else None
            if self.run_id is not None:
                self.seen_runs.add(self.run_id)
            self.timeline.note(self.run_id, written)
            self._release()
        elif name == "EventsSent":
            self.audit.sent(value, written)
        if name == "SimulatedTime" and isinstance(value, datetime):
            self.audit.clock(value, written)
        if name in SETTLED:
            self._unsettled.add(name)
        if "SimulatedTime" in self._settle():
            await self._publish_clock()

    def _settle(self) -> set[str]:
        """`State` и `SimulatedTime`, чей прогон по метке записи стал известен, — в `settled`. Статус, часы и сверка
        прогонов берут значения оттуда, а не последние пришедшие: те записаны после последнего пульса `RunId`."""
        done = set()
        for name in sorted(self._unsettled):
            known, run = self._service_run(name)
            if known:
                self.settled[name] = (run, self.service[name])
                self._unsettled.discard(name)
                done.add(name)
        return done

    async def _publish_clock(self) -> None:
        """`SimulatedTime` — в часы Redis с прогоном по метке записи, как события и статус. Эмулятор пишет его каждый
        шаг и в `finished`: без прогона конец прошлого сценария показывался как время только что созданного."""
        run, value = self.settled["SimulatedTime"]
        if self.redis is None or not isinstance(value, datetime):
            return
        try:
            await self.redis.mset(
                {keys.CLOCK_SIM: value.isoformat(), keys.CLOCK_SIM_RECEIVED_AT: time.time(), keys.CLOCK_RUN: run or ""}
            )
        except Exception as e:  # noqa: BLE001 — часы в Redis не должны ронять приём событий
            log.warning("Redis недоступен: %s", e)

    async def on_connection(self, connected: bool) -> None:
        self._connected_at = time.monotonic() if connected else None
        if self.redis is None:
            return
        try:
            await self.redis.set(keys.GATEWAY_STATUS, "connected" if connected else "disconnected")
        except Exception as e:  # noqa: BLE001 — статус в Redis не должен ронять шлюз
            log.warning("Redis недоступен: %s", e)

    # --- пачки --------------------------------------------------------------------------------------------------

    async def flush(self) -> None:
        if self.pairer is not None:
            for event in self.pairer.expire():
                self.on_event(event)
        self._release()
        if not self.buffer:
            return
        batch, self.buffer = self.buffer, []
        started = time.perf_counter()
        by_run: dict[int, list[RawEvent]] = {}
        for run_id, event in batch:
            by_run.setdefault(run_id, []).append(event)
        runs = list(by_run.items())
        for i, (run_id, events) in enumerate(runs):
            try:
                result = await self.ingest(run_id, events)
            except Exception as e:  # noqa: BLE001 — при сбое БД события ждут следующей попытки
                self.stats.failed_flushes += 1
                self.buffer = [(r, ev) for r, evs in runs[i:] for ev in evs] + self.buffer
                log.warning("приём событий не удался, %d событий ждут повтора: %s", len(self.buffer), e)
                return
            self.stats.accepted += result.accepted
            self.stats.duplicates += result.duplicates
            self.stats.unknown_channels += result.unknown_channels
        self.stats.last_flush_ms = round(1000 * (time.perf_counter() - started), 1)

    def _service_run(self, name: str) -> tuple[bool, int | None]:
        """Прогон, к которому относится служебное значение, — по его метке записи, как у событий: `State`
        и `SimulatedTime` нового сценария приходили раньше его `RunId` и попадали на прошлый прогон."""
        written = self.written.get(name)
        return self.timeline.known(written), self.timeline.run_at(written)

    async def sync_run(self) -> None:
        """Статус и часы прогона из состояния источника — прогона, к которому они относятся по метке записи
        (`settled`). Раз в шаг, а не на каждое уведомление. Последнее пришедшее `SimulatedTime` почти всегда новее
        последнего пульса `RunId`: по нему часы в базу не шли, и на хвосте сценария без событий стояли (прогон 54)."""
        now = time.monotonic()
        state_run, state = self.settled.get("State", (None, None))
        clock_run, clock = self.settled.get("SimulatedTime", (None, None))
        need_status = state_run and state in RUN_STATUSES and (state_run, state) != self._synced
        if need_status and state in FINAL_STATUSES and self._events_pending():
            # закрытие прогона — после его последних событий: за `finished` идут архив и окончательные пересчёты,
            # а события, записанные до `State = finished`, могли прийти в том же уведомлении и ждать пачки
            need_status = False
        need_clock = clock_run and isinstance(clock, datetime) and now - self._clock_synced_at > 5
        if not (need_status or need_clock):
            return
        try:
            async with SessionLocal() as session:
                # часы — до статуса: у закрытого прогона они уже не идут
                if need_clock:
                    speed = self.service.get("EffectiveSpeed")
                    await runs_domain.update_run_clock(session, clock_run, clock, speed)
                if need_status:
                    await runs_domain.set_run_status(session, state_run, state)
                await session.commit()
        except ValueError as e:
            # домен прогонов отверг переход (RunError), например из закрытого прогона — повтор не поможет
            log.warning("прогон %s: %s", state_run, e)
            self._synced, self._clock_synced_at = (state_run, state), now
            return
        except Exception as e:  # noqa: BLE001 — статус прогона не должен останавливать приём событий
            log.warning("прогон %s: не удалось обновить статус или часы: %s", state_run or clock_run, e)
            return
        if need_status:
            self._synced = (state_run, state)
            log.info("прогон %s: %s", state_run, state)
        if need_clock:
            self._clock_synced_at = now

    def _events_pending(self) -> bool:
        """Есть принятые, но ещё не записанные события: в буфере пачки или половины без пары."""
        return bool(self.buffer) or bool(self.pairer is not None and self.pairer.pending)

    async def reconcile_runs(self) -> list[int]:
        """Закрыть активные прогоны, которые источник не ведёт. Только при живой связи и после служебного значения,
        пришедшего уже после подключения (`SimulatedTime` идёт каждые 0.1 с): иначе состояние источника могло
        устареть. `State` и `RunId` приходят начальными значениями подписки и дальше меняются только при смене.
        Раз в 5 с.

        - Пока последнее `State` не отнесено к прогону по метке записи — ждём: сырое `State = finished` приходит раньше
          пульса `RunId`, и доигранный прогон 54 стенда закрылся как прерванный.
        - Ведёт источник текущий `RunId`, а не прогон, при котором записано `State`: эмулятор пишет `State` только при
          смене, и у нового прогона, запущенного из `running`, метка `State` — от прошлого (прогон 58 закрылся как
          прерванный посреди проигрывания). Доигранный источником прогон не трогаем — его закроет `sync_run`.
        - Прерван только прогон, который источник вёл: созданный при этом шлюзе и ни разу не пришедший в `RunId` ждёт
          эмулятора (прогон 57: эмулятор взял его через 46 с после создания). Созданный до старта шлюза — как раньше."""
        now = time.monotonic()
        if (
            self._connected_at is None
            or self._service_at is None
            or self._service_at < self._connected_at
            or "State" in self._unsettled
            or now - self._reconciled_at < 5
        ):
            return []
        self._reconciled_at = now
        state = self.settled.get("State", (None, None))[1]
        source_run = self.run_id if state in ("running", "paused", "finished") else None
        closed = []
        try:
            async with SessionLocal() as session:
                active = await session.scalars(
                    select(Run).where(
                        Run.status.in_(runs_domain.ACTIVE_STATUSES),
                        Run.started_at < datetime.now(UTC) - INTERRUPT_GRACE,
                    )
                )
                for run in active.all():
                    if run.id == source_run or (run.id not in self.seen_runs and run.started_at > self._started_at):
                        continue
                    await runs_domain.set_run_status(session, run.id, "stopped")
                    run.params = {**(run.params or {}), "stop_reason": "interrupted"}
                    closed.append(run.id)
                if closed:
                    await session.commit()
        except Exception as e:  # noqa: BLE001 — сверка прогонов не должна останавливать приём событий
            log.warning("сверка прогонов не удалась: %s", e)
            return []
        if closed:
            log.warning("прогоны %s прерваны: источник в состоянии %s, RunId %s", closed, state, self.run_id)
        return closed

    async def check_losses(self) -> list[int]:
        """Раз в 2 с: сверить принятые события прогона со счётчиком источника (audit.py). Выросли потери — в прогон
        (`events_lost`, `loss_windows`), в лог и в поток: `run.updated`, лента показывает плашку."""
        now = time.monotonic()
        if now - self._losses_checked_at < 2:
            return []
        self._losses_checked_at = now
        self._losses_dirty.update(self.audit.check(self.timeline))
        if not self._losses_dirty:
            return []
        changed = sorted(self._losses_dirty)
        try:
            async with SessionLocal() as session:
                for run_id in changed:
                    loss = self.audit.runs[run_id]
                    await session.execute(
                        update(Run).where(Run.id == run_id).values(events_lost=loss.lost, loss_windows=loss.as_json())
                    )
                await session.commit()
        except Exception as e:  # noqa: BLE001 — потери запишутся со следующей сверкой, приём не останавливаем
            log.warning("потери событий не записаны: %s", e)
            return []
        self._losses_dirty.clear()
        notes = []
        for run_id in changed:
            loss = self.audit.runs[run_id]
            log.warning("прогон %s: шлюз потерял %d событий, окна %s", run_id, loss.lost, loss.as_json())
            payload = {"id": run_id, "events_lost": loss.lost, "loss_windows": loss.as_json()}
            notes.append(Notification(run_id, "run.updated", payload))
        await publish(notes)
        return changed

    async def publish_stats(self) -> None:
        if self.redis is None:
            return
        try:
            # состояние — с прогоном по метке записи: `State` нового сценария приходит раньше его `RunId` и наоборот
            state_run, state = self.settled.get("State", (None, None))
            stats = {**asdict(self.stats), "run_id": self.run_id, "state": state, "state_run_id": state_run}
            await self.redis.set(keys.GATEWAY_STATS, json.dumps(stats))
        except Exception as e:  # noqa: BLE001
            log.warning("Redis недоступен: %s", e)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.flush_seconds)
            await self.flush()
            await self.sync_run()
            await self.reconcile_runs()
            await self.check_losses()
            await self.publish_stats()
