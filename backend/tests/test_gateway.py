"""Шлюз: склейка `Value` и `Alarm`, пачки в приём событий, прогон по `RunId`."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from asyncua import ua

from app.domain.ingest import IngestResult, RawEvent
from app.gateway.opcua import Pairer
from app.gateway.service import Gateway

T = datetime(2026, 3, 28, 5, 0, 40, tzinfo=UTC)
MS = timedelta(milliseconds=1)


def value(v: str | None, ts: datetime = T, code: int = ua.StatusCodes.Good) -> ua.DataValue:
    return ua.DataValue(Value=ua.Variant(v, ua.VariantType.String), StatusCode=ua.StatusCode(code), SourceTimestamp=ts)


def alarm(a: bool, ts: datetime = T) -> ua.DataValue:
    return ua.DataValue(Value=ua.Variant(a, ua.VariantType.Boolean), SourceTimestamp=ts)


def test_pairer_glues_halves_in_any_order_and_drops_milliseconds():
    p = Pairer()
    assert p.value(1, value("Норма"), now=0) is None
    assert p.alarm(1, alarm(False), now=0) == RawEvent(1, T, "Норма", False)
    # та же секунда канала — метка +1 мс; сначала пришёл Alarm
    assert p.alarm(1, alarm(False, T + MS), now=0) is None
    assert p.value(1, value("Дыма нет", T + MS), now=0) == RawEvent(1, T, "Дыма нет", False)
    assert p.pending == 0


def test_pairer_restores_state_from_bad_status_and_skips_no_data():
    p = Pairer()
    p.value(1, value(None, code=ua.StatusCodes.BadDeviceFailure), now=0)
    assert p.alarm(1, alarm(True), now=0) == RawEvent(1, T, "Неисправен", True)
    p.value(2, value(None, code=ua.StatusCodes.BadOutOfService), now=0)
    assert p.alarm(2, alarm(False), now=0).value_raw == "Отключено устройство"
    p.value(3, value("Неопределен", code=ua.StatusCodes.Uncertain), now=0)
    assert p.alarm(3, alarm(False), now=0).value_raw == "Неопределен"
    # сброс канала перед прогоном — не событие
    no_data = ua.DataValue(StatusCode=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData), SourceTimestamp=T)
    assert p.value(4, no_data, now=0) is None and p.alarm(4, no_data, now=0) is None
    assert p.pending == 0


def test_pairer_keeps_write_time_of_the_pair():
    """Метка записи события — позднейшая из половин: по ней шлюз относит событие к прогону."""
    p = Pairer()
    w1, w2 = T + timedelta(hours=1), T + timedelta(hours=1, microseconds=3)
    p.value(1, ua.DataValue(Value=ua.Variant("Норма"), SourceTimestamp=T, ServerTimestamp=w1), now=0)
    event = p.alarm(1, ua.DataValue(Value=ua.Variant(False), SourceTimestamp=T, ServerTimestamp=w2), now=0)
    assert event == RawEvent(1, T, "Норма", False) and event.written == w2


def test_pairer_completes_lonely_half_from_last_known():
    p = Pairer(timeout=5)
    p.value(1, value("Норма"), now=0)
    p.alarm(1, alarm(False), now=0)
    later = T + timedelta(seconds=5)
    p.value(1, value("Обнаружен дым", later), now=10)
    assert p.expire(now=12) == []
    assert p.expire(now=16) == [RawEvent(1, later, "Обнаружен дым", False)]
    p.value(2, value("18", later), now=20)
    assert p.expire(now=30) == [] and p.skipped["unpaired"] == 1


class FakeIngest:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[int | None, list[RawEvent]]] = []
        self.fail_times = fail_times

    async def __call__(self, run_id, events):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("БД недоступна")
        self.calls.append((run_id, list(events)))
        return IngestResult(accepted=len(events))


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict = {}

    async def mset(self, mapping):
        self.data.update(mapping)

    async def set(self, key, value):
        self.data[key] = value


@pytest.mark.anyio
async def test_gateway_tags_events_with_run_at_receipt_and_retries():
    ingest = FakeIngest(fail_times=1)
    gw = Gateway(redis=FakeRedis(), ingest=ingest)
    e1, e2, e3 = RawEvent(1, T, "a", False), RawEvent(1, T, "b", False), RawEvent(2, T, "c", True)

    await gw.on_service("RunId", 0)
    gw.on_event(e1)  # без прогона — не пишется в базовую историю
    await gw.on_service("RunId", 7)
    gw.on_event(e2)
    await gw.on_service("RunId", 8)
    gw.on_event(e3)

    await gw.flush()  # БД недоступна — события остаются
    assert ingest.calls == [] and len(gw.buffer) == 2 and gw.stats.failed_flushes == 1
    await gw.flush()
    assert ingest.calls == [(7, [e2]), (8, [e3])]
    assert (gw.stats.received, gw.stats.without_run, gw.stats.accepted) == (3, 1, 2)

    await gw.on_service("SimulatedTime", T)
    await gw.on_connection(True)
    assert gw.redis.data["clock:sim"] == T.isoformat() and gw.redis.data["gateway:status"] == "connected"
    assert gw.redis.data["clock:run_id"] == 8  # источник без меток записи — прогон на момент приёма


def w(ms: int) -> datetime:
    """Метка записи у источника: реальное время эмулятора, строго растёт."""
    return datetime(2026, 9, 27, 1, 48, tzinfo=UTC) + timedelta(milliseconds=ms)


def ev(channel: int, second: int, written_ms: int) -> RawEvent:
    return RawEvent(channel, T + timedelta(seconds=second), str(second), False, written=w(written_ms))


@pytest.mark.anyio
async def test_events_of_new_run_before_its_run_id_go_to_new_run():
    """Гонка прогонов 30–33 стенда. Сервер собирает уведомления цикла публикации по элементам подписки: канал,
    который в цикле уже менялся (сброс перед запуском, хвост прошлого прогона), идёт в уведомлении раньше `RunId`,
    и первые события нового сценария приходили до его `RunId` — в прошлый прогон. Прогон — по меткам записи."""
    ingest = FakeIngest()
    gw = Gateway(ingest=ingest)
    await gw.on_service("RunId", 30, w(0))
    await gw.on_service("RunId", 30, w(100))  # пульс: RunId каждый шаг
    old1 = ev(1, 1, 50)
    gw.on_event(old1)

    # одно уведомление: элементы канала 1 (хвост 30, события 31), затем RunId (пульс 30, смена на 31, пульс 31)
    old2, new1, new2 = ev(1, 2, 150), ev(1, 900, 260), ev(1, 901, 310)
    for event in (old2, new1, new2):
        gw.on_event(event)
    await gw.flush()
    assert ingest.calls == [(30, [old1])] and gw.stats.waiting_run == 3  # прогон ещё не определить
    await gw.on_service("RunId", 30, w(200))
    await gw.on_service("RunId", 31, w(250))  # смена: всё, что записано раньше, — прогона 30
    await gw.flush()
    assert ingest.calls[-1] == (30, [old2]) and gw.waiting == [new1, new2]
    await gw.on_service("RunId", 31, w(350))
    await gw.flush()
    assert ingest.calls[-1] == (31, [new1, new2]) and gw.stats.waiting_run == 0

    # прогон закрыт, эмулятор в idle: события после смены на 0 не пишутся
    await gw.on_service("RunId", 0, w(400))
    gw.on_event(ev(1, 902, 450))
    await gw.on_service("RunId", 0, w(500))
    await gw.flush()
    assert gw.stats.without_run == 1 and len(ingest.calls) == 3


@pytest.mark.anyio
async def test_status_and_clock_go_to_run_of_their_write_time(monkeypatch):
    """`State` и `SimulatedTime` нового сценария тоже приходили раньше его `RunId`: часы прошлого прогона уходили
    к старту следующего. Статус и часы — прогону по метке записи, пока она не определена — не пишутся."""
    calls = []

    class Runs:
        async def set_run_status(self, session, run_id, status):
            calls.append(("status", run_id, status))

        async def update_run_clock(self, session, run_id, sim_now, speed=None):
            calls.append(("clock", run_id, sim_now))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr("app.gateway.service.runs_domain", Runs())
    monkeypatch.setattr("app.gateway.service.SessionLocal", Session)
    gw = Gateway(ingest=FakeIngest())
    await gw.on_service("RunId", 30, w(0))
    await gw.on_service("RunId", 30, w(100))
    await gw.on_service("State", "finished", w(110))
    await gw.sync_run()
    assert calls == []  # записан позже последнего пульса RunId — прогон ещё не определить
    await gw.on_service("RunId", 30, w(200))
    await gw.sync_run()
    assert calls == [("status", 30, "finished")]
    # State и SimulatedTime нового сценария — раньше его RunId
    await gw.on_service("State", "running", w(260))
    await gw.on_service("SimulatedTime", T, w(270))
    await gw.sync_run()
    assert len(calls) == 1
    await gw.on_service("RunId", 31, w(250))
    await gw.on_service("RunId", 31, w(350))
    await gw.sync_run()
    assert calls[1:] == [("clock", 31, T), ("status", 31, "running")]


@pytest.mark.anyio
async def test_clock_and_state_in_redis_go_to_run_of_their_write_time():
    """Гонка часов при смене сценария: эмулятор пишет `SimulatedTime` каждый шаг и в `finished`, и `clock.tick`
    только что созданного прогона ~1 с показывал конец прошлого. Часы и состояние в Redis — с прогоном по метке
    записи, как события (#173); пока прогон метки не определить, значение ждёт пульса `RunId`."""
    redis = FakeRedis()
    gw = Gateway(redis=redis, ingest=FakeIngest())
    end30, start31, next31 = T, T + timedelta(days=30), T + timedelta(days=30, seconds=6)

    def clock() -> tuple:
        return redis.data.get("clock:sim"), redis.data.get("clock:run_id")

    async def state() -> tuple:
        await gw.publish_stats()
        stats = json.loads(redis.data["gateway:stats"])
        return stats["state"], stats["state_run_id"]

    await gw.on_service("RunId", 30, w(0))
    await gw.on_service("RunId", 30, w(100))
    await gw.on_service("State", "finished", w(110))
    await gw.on_service("SimulatedTime", end30, w(120))
    assert clock() == (None, None)  # записано позже последнего пульса RunId — прогон ещё не определить
    await gw.on_service("RunId", 30, w(200))
    assert clock() == (end30.isoformat(), 30) and await state() == ("finished", 30)

    # старт 31: State и SimulatedTime нового сценария пришли раньше его RunId
    await gw.on_service("State", "running", w(260))
    await gw.on_service("SimulatedTime", start31, w(270))
    assert clock() == (end30.isoformat(), 30) and await state() == ("finished", 30)  # последнее отнесённое
    await gw.on_service("RunId", 31, w(250))
    assert clock() == (end30.isoformat(), 30)  # смена известна до 250, значения записаны в 260 и 270
    await gw.on_service("RunId", 31, w(350))
    assert clock() == (start31.isoformat(), 31) and await state() == ("running", 31)

    # обычный шаг: значение после пульса — ждёт следующего, пульс без нового значения часы не трогает
    await gw.on_service("SimulatedTime", next31, w(360))
    assert clock() == (start31.isoformat(), 31)
    await gw.on_service("RunId", 31, w(450))
    assert clock() == (next31.isoformat(), 31)
    redis.data["clock:sim"] = "стёрто"
    await gw.on_service("RunId", 31, w(550))
    assert clock() == ("стёрто", 31)


@pytest.mark.anyio
async def test_rejected_run_transition_is_not_retried(monkeypatch):
    calls = []

    class Runs:
        async def set_run_status(self, session, run_id, status):
            calls.append((run_id, status))
            raise ValueError(f"прогон {run_id}: переход finished → {status} невозможен")

        async def update_run_clock(self, session, run_id, sim_now, speed=None):
            pass

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr("app.gateway.service.runs_domain", Runs())
    monkeypatch.setattr("app.gateway.service.SessionLocal", Session)
    gw = Gateway(ingest=FakeIngest())
    await gw.on_service("RunId", 7)
    await gw.on_service("State", "running")
    await gw.sync_run()
    await gw.sync_run()
    assert calls == [(7, "running")]


@pytest.mark.anyio
async def test_interrupted_runs_are_closed(session):
    """Эмулятор перезапущен (idle, RunId 0) — прогон, который он вёл, закрывается как прерванный.
    Прогон, который источник ведёт, и только что созданный (эмулятор ещё не стартовал) — не трогаются."""
    from sqlalchemy import text

    old, fresh = "now() - interval '10 minutes'", "now() - interval '5 seconds'"
    for run_id, status, started in (
        (1, "stopped", old),
        (2, "running", old),
        (3, "paused", old),
        (4, "running", fresh),
    ):
        await session.execute(
            text(
                f"INSERT INTO runs (id, scenario, status, sim_start, started_at) "
                f"VALUES ({run_id}, 'ppr', '{status}', '2026-01-16 08:30+03', {started})"
            )
        )
    await session.commit()

    gw = Gateway(ingest=FakeIngest())
    # начальные значения подписки приходят до отметки «подключён»
    await gw.on_service("RunId", 3)
    await gw.on_service("State", "paused")
    assert await gw.reconcile_runs() == []  # без связи состояние источника не проверяется
    await gw.on_connection(True)
    assert await gw.reconcile_runs() == []  # после подключения ещё ничего не приходило
    await gw.on_service("SimulatedTime", T)  # идёт каждый шаг эмулятора, State при этом не меняется
    assert await gw.reconcile_runs() == [2]  # 3 ведёт источник, 4 моложе отсрочки

    gw._reconciled_at = 0
    await gw.on_service("RunId", 0)
    await gw.on_service("State", "idle")
    assert await gw.reconcile_runs() == [3]

    rows = await session.execute(
        text("SELECT id, status, finished_at IS NOT NULL, params->>'stop_reason' FROM runs ORDER BY id")
    )
    assert [tuple(r) for r in rows] == [
        (1, "stopped", False, None),
        (2, "stopped", True, "interrupted"),
        (3, "stopped", True, "interrupted"),
        (4, "running", False, None),
    ]


@pytest.mark.anyio
async def test_reconcile_follows_current_run_not_state_stamp(session):
    """Прогон 58 стенда закрыт как прерванный посреди проигрывания: его запустили из `running` прогона 57, эмулятор
    `State` не переписал (пишет только при смене), и метка `State` осталась от 57. Ведёт источник текущий `RunId`.
    Прогон, созданный при этом шлюзе и ни разу не пришедший в `RunId`, — не прерван: ждёт эмулятора (прогон 57
    эмулятор взял через 46 с после создания). Созданный до старта шлюза и не ведомый — прерван, как раньше."""
    from sqlalchemy import text

    gw = Gateway(ingest=FakeIngest())
    gw._started_at = datetime.now(UTC) - timedelta(minutes=5)
    for run_id, started in ((57, "10 minutes"), (58, "4 minutes"), (59, "1 minute"), (60, "8 minutes")):
        await session.execute(
            text(
                "INSERT INTO runs (id, scenario, status, sim_start, started_at) "
                f"VALUES ({run_id}, 'ppr', 'running', '2026-01-16 08:30+03', now() - interval '{started}')"
            )
        )
    await session.commit()
    await gw.on_connection(True)
    await gw.on_service("RunId", 57, w(0))
    await gw.on_service("State", "running", w(10))
    await gw.on_service("RunId", 57, w(100))
    await gw.on_service("RunId", 58, w(200))  # новый прогон из running: State не переписан
    await gw.on_service("RunId", 58, w(300))
    await gw.on_service("SimulatedTime", T, w(310))
    assert gw.settled["State"] == (57, "running")
    # 57 — вёл и больше не ведёт; 59 — создан при шлюзе, источник его ещё не брал; 60 — до старта шлюза, не ведом
    assert sorted(await gw.reconcile_runs()) == [57, 60]
    rows = await session.execute(text("SELECT id, status FROM runs ORDER BY id"))
    assert [tuple(r) for r in rows] == [(57, "stopped"), (58, "running"), (59, "running"), (60, "stopped")]


@pytest.mark.anyio
async def test_run_finishes_after_its_last_events(monkeypatch):
    """`finished` — только после последних событий прогона: события, записанные до `State = finished`, приходят
    в том же уведомлении и ждут пачки. Закрытие раньше них — архив и окончательные пересчёты без хвоста; в тесте
    шлюза с эмулятором это давало 2 события из 7 (test_gateway_ingests_scenario_from_emulator, 1 раз из 6)."""
    calls = []

    class Runs:
        async def set_run_status(self, session, run_id, status):
            calls.append(("status", run_id, status))

        async def update_run_clock(self, session, run_id, sim_now, speed=None):
            pass

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr("app.gateway.service.runs_domain", Runs())
    monkeypatch.setattr("app.gateway.service.SessionLocal", Session)
    ingest = FakeIngest()
    gw = Gateway(ingest=ingest)
    await gw.on_service("RunId", 7, w(0))
    await gw.on_service("RunId", 7, w(100))
    gw.on_event(ev(1, 50, 150))  # последнее событие сценария
    await gw.on_service("State", "finished", w(160))
    await gw.on_service("RunId", 7, w(200))  # пульс: и событие, и State отнесены к прогону 7
    assert gw.buffer and gw.settled["State"] == (7, "finished")
    await gw.sync_run()
    assert calls == []  # событие ещё в пачке
    await gw.flush()
    await gw.sync_run()
    assert ingest.calls == [(7, [ev(1, 50, 150)])] and calls == [("status", 7, "finished")]
