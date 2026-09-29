"""HTTP-управление эмулятором — docs/api.md §3. Доступно только `api` внутри сети Docker."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.player import Player, PlayerError
from app.scenario_data import DEFAULT_LIMIT, DataError, ScenarioData, SummaryWorker
from app.scenarios import Scenario


class StartRequest(BaseModel):
    scenario: str
    speed: float | None = Field(default=None, description="По умолчанию — скорость сценария")
    run_id: int | None = Field(default=None, description="Прогон backend; публикуется в узле Emulator/RunId")


class SpeedRequest(BaseModel):
    speed: float


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_control(player: Player) -> FastAPI:
    catalog = player.emulator.catalog
    worker = SummaryWorker(catalog) if settings.summary_worker else None
    data = ScenarioData(catalog, worker)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if worker is not None:
            worker.close()

    app = FastAPI(title="LCT · эмулятор СМВУ", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(PlayerError)
    @app.exception_handler(DataError)
    async def player_error(request: Request, exc: PlayerError | DataError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        parts = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
        return _error(422, "validation_error", "; ".join(parts) or "Некорректный запрос")

    def state() -> dict:
        return {
            **player.describe(),
            "opcua_endpoint": settings.opcua_endpoint,
            "objects": len(player.emulator.catalog.objects),
            "channels": len(player.emulator.catalog.channels),
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", **state()}

    @app.get("/state")
    def get_state() -> dict:
        """Состояние проигрывателя: сценарий, прогон, целевая и фактическая скорость, симуляционное время."""
        return state()

    @app.get("/scenarios")
    def scenarios() -> dict:
        """Сценарии; `ready` — срез подготовлен (`make emulator-slices`)."""
        return {"scenarios": [s.describe(player.slices_dir) for s in player.scenarios().values()]}

    def scenario_slice(name: str) -> tuple[Scenario, Path]:
        scenario = player.scenarios().get(name)
        if scenario is None:
            raise PlayerError(404, "scenario_not_found", f"Сценария {name!r} нет")
        return scenario, scenario.slice_path(player.slices_dir)

    # Данные сценария — синхронные обработчики в пуле потоков. Сводка к тому же считается в своём процессе: в этом
    # её Python держал бы GIL, и публикация OPC UA замирала (scenario_data.SummaryWorker)

    @app.get("/scenarios/{name}/summary")
    def scenario_summary(name: str) -> dict:
        """Что в данных сценария: объекты, каналы по типам и подсистемам, значения, плотность, ключевые моменты."""
        return data.summary(*scenario_slice(name))

    @app.get("/scenarios/{name}/events")
    def scenario_events(
        name: str,
        cursor: Annotated[str | None, Query(description="`next_cursor` прошлой страницы")] = None,
        limit: Annotated[int, Query(description="До 1000")] = DEFAULT_LIMIT,
        from_: Annotated[str | None, Query(alias="from", description="ISO; без пояса — Москва; включается")] = None,
        to: Annotated[str | None, Query(description="ISO; не включается")] = None,
        object_id: Annotated[list[str] | None, Query(description="Любой уровень, вложенные включаются")] = None,
        channel_id: Annotated[list[str] | None, Query()] = None,
        sensor_type: Annotated[list[str] | None, Query()] = None,
        value: Annotated[list[str] | None, Query(description="«числовые показания» — все числа")] = None,
        alarm: bool | None = None,
        q: Annotated[str | None, Query(description="Подстрока названия канала")] = None,
    ) -> dict:
        """Сигналы среза в порядке передачи. Списки — повтором параметра или через запятую."""
        return data.events(
            *scenario_slice(name),
            cursor=cursor,
            limit=limit,
            from_=from_,
            to=to,
            object_id=object_id,
            channel_id=channel_id,
            sensor_type=sensor_type,
            value=value,
            alarm=alarm,
            q=q,
        )

    @app.post("/start")
    async def start(body: StartRequest) -> dict:
        """Запуск сценария с начала. Идущий сценарий останавливается."""
        await player.start(body.scenario, body.speed, body.run_id)
        return state()

    @app.post("/pause")
    async def pause() -> dict:
        await player.pause()
        return state()

    @app.post("/resume")
    async def resume() -> dict:
        await player.resume()
        return state()

    @app.post("/stop")
    async def stop() -> dict:
        await player.stop()
        return state()

    @app.post("/speed")
    async def speed(body: SpeedRequest) -> dict:
        """Целевая скорость от 1 до `max_speed` сценария (по умолчанию 60)."""
        await player.set_speed(body.speed)
        return state()

    return app
