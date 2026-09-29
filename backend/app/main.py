import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.api import errors
from app.api.router import api_router
from app.core.loop_watch import watch_loop


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    watch = asyncio.create_task(watch_loop())  # простои цикла событий — в лог (app/core/loop_watch.py)
    yield
    watch.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watch


app = FastAPI(
    title="LCT · сервис диспетчеризации",
    version="0.1.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    lifespan=lifespan,
)
errors.install(app)
app.include_router(api_router, prefix="/api/v1")
