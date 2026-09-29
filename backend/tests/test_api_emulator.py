"""`/api/v1/emulator/*`: прокси в эмулятор, прогон при старте, статусы прогона по командам."""

import asyncio
import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import emulator as api
from app.main import app

pytestmark = pytest.mark.anyio

SCENARIOS = [
    {
        "name": "ppr",
        "title": "Плановая проверка",
        "period": {"start": "2026-01-16T08:30:00+03:00", "end": "2026-01-16T14:00:00+03:00"},
        "objects": [10],
        "include_gas": False,
        "speed": 60.0,
        "max_speed": 60.0,
        "ready": True,
    },
    {**{"name": "no-slice", "ready": False}, **{k: None for k in ("title", "period", "objects", "include_gas")}},
    {
        "name": "ext-magdeburg-cable",
        "title": "Тлеющий кабель",
        "period": {"start": "2023-07-12T16:35:00+03:00", "end": "2023-07-12T17:20:00+03:00"},
        "objects": [9010002],
        "speed": 10.0,
        "max_speed": 15.0,
        "include_gas": False,
        "dataset": "magdeburg",
        "timeline": [
            {
                "at": "2023-07-12T16:49:00+03:00",
                "until": "2023-07-12T17:10:47+03:00",
                "text": "Тление кабеля (опыт 24)",
                "source": "dataset",
            }
        ],
        "ready": True,
    },
    {"name": "ext-unknown", "dataset": "нет-в-реестре", "ready": False},
]


EMULATOR_STATE = {  # формат эмулятора — docs/api.md §3
    "state": "running",
    "scenario": "ppr",
    "run_id": 3,
    "target_speed": 60.0,
    "effective_speed": 58.9,
    "max_speed": 60.0,
    "sim_time": "2026-01-16T09:41:12+03:00",
    "period": {"start": "2026-01-16T08:30:00+03:00", "end": "2026-01-16T14:00:00+03:00"},
    "progress": {"emitted": 812, "total": 2937},
    "max_rate": 200.0,
}


class FakeServices:
    """Эмулятор и ml-service за httpx.MockTransport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.run_id: int | None = None
        self.down = False
        self.models: list[dict] | None = None  # GET /v1/models ml-service; None — 503
        self.state = EMULATOR_STATE  # GET /state эмулятора
        # POST /start: None — стартует; "error" — 500; "connect" — нет соединения; "timeout" — нет ответа;
        # "timeout_started" — нет ответа, но эмулятор стартовал: /state покажет прогон
        self.start_failure: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path.startswith("/scenarios/ppr/"):
            # данные сценария (13.2) — эмулятор отвечает, api отдаёт как есть; эхо параметров для проверки
            return httpx.Response(200, json={"path": path, "params": list(request.url.params.multi_items())})
        if path.startswith("/scenarios/no-slice/"):
            error = {"code": "slice_not_ready", "message": "Срез не подготовлен"}
            return httpx.Response(409, json={"error": error})
        if path == "/scenarios":
            return httpx.Response(200, json={"scenarios": SCENARIOS})
        if path == "/state":
            return httpx.Response(200, json=self.state)
        if path == "/v1/models":
            if self.models is None:
                return httpx.Response(503, json={"error": {"code": "unavailable", "message": "нет"}})
            return httpx.Response(200, json={"models": self.models})
        if path == "/v1/cache/reload":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/start":
            if self.start_failure == "error":
                return httpx.Response(500, text="Internal Server Error")
            if self.start_failure == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            if self.start_failure == "timeout_started":
                self.state = {**EMULATOR_STATE, "state": "running", "run_id": body["run_id"]}
            if self.start_failure in ("timeout", "timeout_started"):
                raise httpx.ReadTimeout("timed out", request=request)
            self.run_id = body["run_id"]
            return httpx.Response(200, json={"state": "running", "run_id": self.run_id, "target_speed": body["speed"]})
        if path in ("/pause", "/stop"):
            return httpx.Response(
                200,
                json={
                    "state": path.strip("/").replace("pause", "paused").replace("stop", "stopped"),
                    "run_id": self.run_id,
                },
            )
        if path == "/speed":
            error = {"code": "speed_out_of_range", "message": "Скорость 100 вне диапазона 1–60"}
            return httpx.Response(422, json={"error": error})
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture
async def http(session, db_url, monkeypatch):
    fake = FakeServices()
    monkeypatch.setattr(api, "transport", httpx.MockTransport(fake))
    engine = create_async_engine(db_url, poolclass=NullPool)
    monkeypatch.setattr(api, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        yield client, fake
    await engine.dispose()


async def runs(session) -> list[tuple]:
    await session.rollback()  # увидеть то, что закоммитил api
    rows = await session.execute(
        text("SELECT id, scenario, status, speed, params->>'title', finished_at IS NOT NULL FROM runs ORDER BY id")
    )
    return rows.all()


async def test_scenarios_and_state_follow_api_schemas(http):
    client, fake = http
    items = (await client.get("/api/v1/emulator/scenarios")).json()["items"]
    assert items[0] == {
        "id": "ppr",
        "source": "builtin",
        "title": "Плановая проверка",
        "description": None,
        "period_start": "2026-01-16T08:30:00+03:00",
        "period_end": "2026-01-16T14:00:00+03:00",
        "objects": [{"id": 10, "name": "Объект А"}],  # название — из справочника рабочей базы
        "default_speed": 60.0,
        "max_speed": 60.0,
        "include_gas": False,
        "timeline": [],
        "ready": True,
        "source_file": None,
        "uploaded_at": None,
        "warnings": [],
        "events": None,
        "channels": None,
        "dataset": None,
    }
    external = {i["id"]: i for i in items}
    assert external["ext-magdeburg-cable"]["dataset"] == {
        "key": "magdeburg",
        "title": "Indoor Fire Dataset with Distributed Multi-Sensor Nodes (Industrial Hall)",
        "short": "Magdeburg · цех",
        "url": "https://data.mendeley.com/datasets/yghykzm4km/1",
        "license": "CC BY 4.0",
        "synthetic": False,
    }
    assert external["ext-magdeburg-cable"]["timeline"][0]["source"] == "dataset"
    assert external["ext-unknown"]["dataset"]["key"] == "нет-в-реестре"
    state = (await client.get("/api/v1/emulator/state")).json()
    assert state == {
        "state": "running",
        "scenario": "ppr",
        "run_id": 3,
        "target_speed": 60.0,
        "effective_speed": 58.9,
        "sim_time": "2026-01-16T09:41:12+03:00",
        "sim_start": "2026-01-16T08:30:00+03:00",
        "sim_end": "2026-01-16T14:00:00+03:00",
        "events_sent": 812,
        "events_total": 2937,
        "max_speed": 60.0,
    }


async def test_start_creates_run_and_passes_it_to_emulator(http, session):
    client, fake = http
    r = await client.post("/api/v1/emulator/start", json={"scenario": "no-slice"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "slice_not_ready"
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr", "speed": 61})
    assert r.status_code == 422 and r.json()["error"]["code"] == "speed_out_of_range"
    r = await client.post("/api/v1/emulator/start", json={"scenario": "nope"})
    assert r.status_code == 404

    assert await runs(session) == []

    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    assert r.status_code == 200 and r.json()["run_id"] == 1
    assert ("POST", "/v1/cache/reload", {"run_id": 1}) in fake.calls
    assert ("POST", "/start", {"scenario": "ppr", "speed": 60.0, "run_id": 1}) in fake.calls
    assert await runs(session) == [(1, "ppr", "running", 60.0, "Плановая проверка", False)]

    # новый запуск останавливает текущий прогон, данные не удаляются
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr", "speed": 30})
    assert r.json()["run_id"] == 2
    assert [(i, status) for i, _, status, *_ in await runs(session)] == [(1, "stopped"), (2, "running")]


async def test_commands_update_run_status_and_errors_pass_through(http, session):
    client, fake = http
    await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    assert (await client.post("/api/v1/emulator/pause")).json()["state"] == "paused"
    assert (await runs(session))[0][2] == "paused"
    assert (await client.post("/api/v1/emulator/stop")).json()["state"] == "stopped"
    assert (await runs(session))[0][2:] == ("stopped", 60.0, "Плановая проверка", True)

    r = await client.post("/api/v1/emulator/speed", json={"speed": 100})
    assert r.status_code == 422 and r.json()["error"]["code"] == "speed_out_of_range"

    fake.down = True
    r = await client.get("/api/v1/emulator/state")
    assert r.status_code == 503 and r.json()["error"]["code"] == "emulator_unavailable"


async def test_scenario_data_pass_through(http):
    client, fake = http
    r = await client.get("/api/v1/emulator/scenarios/ppr/summary")
    assert r.json() == {"path": "/scenarios/ppr/summary", "params": []}

    # списки — через запятую и повтором параметра; эмулятору уходят повтором
    r = await client.get(
        "/api/v1/emulator/scenarios/ppr/events",
        params=[
            ("sensor_type", "Датчик дыма,ИБП"),
            ("sensor_type", "Ручной извещатель"),
            ("value", "числовые показания"),
            ("from", "2026-01-16T09:00:00+03:00"),
            ("alarm", "true"),
            ("cursor", "200"),
        ],
    )
    assert r.json()["params"] == [
        ["cursor", "200"],
        ["from", "2026-01-16T09:00:00+03:00"],
        ["limit", "200"],
        ["alarm", "true"],
        ["sensor_type", "Датчик дыма"],
        ["sensor_type", "ИБП"],
        ["sensor_type", "Ручной извещатель"],
        ["value", "числовые показания"],
    ]

    r = await client.get("/api/v1/emulator/scenarios/no-slice/events")
    assert r.status_code == 409 and r.json()["error"]["code"] == "slice_not_ready"


async def test_start_dataset_scenario_runs_on_dataset_base(http, session):
    """Сценарий внешнего набора — прогон на основе набора (D28): `base_run_id`, ошибки домена — 409 с кодом."""
    client, fake = http
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ext-magdeburg-cable"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dataset_not_loaded"
    assert await runs(session) == []

    await session.execute(
        text(
            "INSERT INTO runs (id, scenario, status, kind, sim_start, sim_end, params) VALUES (50, 'magdeburg', "
            "'finished', 'dataset', '2023-07-13 00:00+03', '2023-07-14 00:00+03', '{\"dataset\": \"magdeburg\"}')"
        )
    )
    await session.commit()
    # сценарий 12.07 — до начала журнала основы
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ext-magdeburg-cable"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dataset_period_mismatch"

    await session.execute(text("UPDATE runs SET sim_start = '2023-07-10 10:30+03' WHERE id = 50"))
    await session.commit()
    # версия событийной модели у набора не применяется: ML-сервис (здесь недоступен) не спрашивается (15.2)
    r = await client.post(
        "/api/v1/emulator/start", json={"scenario": "ext-magdeburg-cable", "speed": 15, "primary_model": "v3"}
    )
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    await session.rollback()
    row = (
        await session.execute(
            text("SELECT kind, base_run_id, params->>'dataset', primary_model, shadow_models FROM runs WHERE id = :id"),
            {"id": run_id},
        )
    ).one()
    assert tuple(row) == ("scenario", 50, "magdeburg", None, [])
    assert ("POST", "/start", {"scenario": "ext-magdeburg-cable", "speed": 15, "run_id": run_id}) in fake.calls


ALARM_MODELS = [  # формат /v1/models ml-service: событийная v0, ежедневная v0, событийная v3
    {"name": "smoke_alarm", "label": "baseline-v0", "family": "baseline", "scoring": "available"},
    {"name": "daily", "label": "baseline-v0", "family": "baseline", "scoring": "available"},
    {"name": "alarm_all_d7", "label": "v3", "family": "v3", "scoring": "available", "active": True},
]


async def run_models(session) -> list[tuple]:
    await session.rollback()
    rows = await session.execute(text("SELECT id, status, primary_model, shadow_models FROM runs ORDER BY id"))
    return [tuple(r) for r in rows]


async def test_start_selects_primary_and_shadow_models(http, session):
    """15.2: основная версия событийной модели — по умолчанию связка «v3, где оценила, иначе v0», когда обе готовы
    (её части — не теневые); явная версия — основная, остальные готовые — теневые."""
    client, fake = http
    fake.models = ALARM_MODELS
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    assert r.status_code == 200
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr", "primary_model": "v3"})
    assert r.status_code == 200
    assert await run_models(session) == [(1, "stopped", "v3>baseline-v0", []), (2, "running", "v3", ["baseline-v0"])]

    # неизвестная версия — 422, текущий прогон не останавливается
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr", "primary_model": "v9"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_version"
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr", "shadow_models": ["v9"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_version"
    # ML-сервис недоступен: запрошенную версию не проверить — 503; без запроса — одна активная модель, как до 15.2
    fake.models = None
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr", "primary_model": "v3"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ml_unavailable"
    assert [status for _, status, *_ in await run_models(session)] == ["stopped", "running"]
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    assert r.status_code == 200
    assert (await run_models(session))[-1] == (3, "running", None, [])

    # без теневых версий — только основная: замер нагрузки, прогон без v3 (связка оценивает обеими — v0 явно)
    fake.models = ALARM_MODELS
    r = await client.post(
        "/api/v1/emulator/start", json={"scenario": "ppr", "primary_model": "baseline-v0", "shadow_models": []}
    )
    assert r.status_code == 200
    assert (await run_models(session))[-1] == (4, "running", "baseline-v0", [])


async def test_start_after_played_scenario_closes_it_as_finished(http, session):
    """Эмулятор доиграл сценарий, а шлюз ещё не перенёс `State = finished` (раз в шаг): новый старт закрывает прошлый
    прогон как `finished`, а не `stopped`. Прерванный посреди сценария — `stopped`, как раньше."""
    client, fake = http
    first = (await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})).json()["run_id"]
    fake.state = {**EMULATOR_STATE, "state": "finished", "run_id": first}
    second = (await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})).json()["run_id"]
    fake.state = {**EMULATOR_STATE, "state": "running", "run_id": second}
    await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    await session.rollback()
    rows = await session.execute(
        text("SELECT id, status, finished_at IS NOT NULL, sim_now = sim_end FROM runs ORDER BY id")
    )
    assert [tuple(r) for r in rows] == [
        (first, "finished", True, True),  # часы — на конце сценария, как при finished от шлюза
        (second, "stopped", True, False),
        (second + 1, "running", False, False),
    ]


class ClockRedis:
    """Часы шлюза в Redis — без настоящего Redis: тест не трогает часы стенда."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def mget(self, *names):
        return [self.data.get(n) for n in names]

    async def get(self, name):
        return self.data.get(name)


async def test_new_run_does_not_show_clock_of_previous_one(http, monkeypatch):
    """Гонка часов при смене сценария: эмулятор пишет `SimulatedTime` каждый шаг и в `finished`, и до первого
    значения нового сценария в часах Redis — конец прошлого. `/clock` и `clock.tick` показывают часы только их
    прогона (шлюз пишет прогон по метке записи): новый до своих часов стоит на начале, `clock.tick` молчит."""
    import time

    from app.api import stream
    from app.core import clock, keys

    client, fake = http
    redis = ClockRedis()
    monkeypatch.setattr(clock, "async_client", lambda: redis)

    def gateway(now: str, run: int, state: str) -> None:
        stats = {"run_id": run, "state": state, "state_run_id": run}
        redis.data.update(
            {
                keys.CLOCK_SIM: now,
                keys.CLOCK_SIM_RECEIVED_AT: str(time.time()),
                keys.CLOCK_RUN: str(run),
                keys.GATEWAY_STATS: json.dumps(stats),
            }
        )

    async def clock_of(run: int) -> dt.datetime:
        return dt.datetime.fromisoformat((await client.get("/api/v1/clock", params={"run": run})).json()["now"])

    async def tick(run: int) -> dict | None:
        return await stream._clock_tick(redis, run, 60.0, "running")

    first = (await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})).json()["run_id"]
    end, start = "2026-01-16T14:00:00+03:00", dt.datetime.fromisoformat("2026-01-16T08:30:00+03:00")
    gateway(end, first, "finished")  # доиграл: часы стоят на конце, эмулятор пишет их каждый шаг
    fake.state = {**EMULATOR_STATE, "state": "finished", "run_id": first}
    second = (await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})).json()["run_id"]

    assert await tick(second) is None and await clock_of(second) == start  # не 14:00 прошлого
    assert (await tick(first))["now"] == end and (await tick(first))["state"] == "finished"

    moved = "2026-01-16T08:31:00+03:00"
    gateway(moved, second, "running")  # первое значение нового сценария с его прогоном
    assert (await tick(second))["now"] == moved and (await tick(second))["run_id"] == second
    assert await clock_of(second) == dt.datetime.fromisoformat(moved)
    assert await tick(first) is None  # у закрытого — где остановился, из базы
    assert await clock_of(first) == dt.datetime.fromisoformat(end)


async def settled() -> None:
    """Дождаться фоновых проверок старта без ответа."""
    while api._settling:
        await asyncio.gather(*api._settling)


@pytest.mark.parametrize("failure", ["error", "connect"])
async def test_failed_start_leaves_no_empty_run(http, session, failure):
    """Эмулятор ответил ошибкой или запрос до него не дошёл — прогон без событий удаляется, пустых не остаётся."""
    client, fake = http
    fake.start_failure = failure
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    assert r.status_code == (500 if failure == "error" else 503)
    assert await runs(session) == []


async def test_start_timeout_waits_for_emulator(http, session, monkeypatch):
    """Старт без ответа: эмулятор мог стартовать — большой срез грузится дольше таймаута. Наш прогон появился в
    /state — прогон остаётся; не появился — удаляется; есть события — только останавливается."""
    client, fake = http
    monkeypatch.setattr(api, "START_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(api, "START_POLL_SECONDS", 0.05)

    fake.start_failure = "timeout_started"
    r = await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "emulator_unavailable"
    await settled()
    assert [(i, status) for i, _, status, *_ in await runs(session)] == [(1, "running")]

    fake.start_failure, fake.state = "timeout", {**EMULATOR_STATE, "run_id": 999}  # в /state — чужой прогон
    await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    await settled()
    assert [(i, status) for i, _, status, *_ in await runs(session)] == [(1, "stopped")]  # второй удалён

    await client.post("/api/v1/emulator/start", json={"scenario": "ppr"})
    await session.execute(text("UPDATE runs SET events = 5 WHERE id = 3"))  # события уже пришли
    await session.commit()
    await settled()
    assert [(i, status) for i, _, status, *_ in await runs(session)] == [(1, "stopped"), (3, "stopped")]
