"""Общие зависимости эндпоинтов: сессия БД и выбранный прогон `?run=` (docs/api.md, «Прогон»)."""

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.db.models import HACKATHON, Run
from app.db.session import SessionLocal
from app.domain.runs import RunError, resolve_run, run_source


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def base_now(session: AsyncSession) -> dt.datetime:
    """«Сейчас» базовой истории — последнее событие данных (30.06.2026), а не реальная дата: иначе окна дашборда
    за сутки были бы пустыми. Не кэшируется: база дополняется через `POST /ingest/events`."""
    found = await session.scalar(text("SELECT max(ts) FROM channel_state WHERE run_id IS NULL"))
    return found or dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class RunContext:
    """Выбранный прогон и его «сейчас»: у прогона — симуляционное время, у базы — конец данных."""

    run: Run | None
    now: dt.datetime
    source: str = HACKATHON  # набор данных прогона: справочник экранов — только его (этап 14)

    @property
    def run_id(self) -> int | None:
        return self.run.id if self.run else None


RunQuery = Annotated[int | None, Query(description="прогон; без параметра — последний, 0 — только базовая история")]


async def resolve_context(session: AsyncSession, run: int | None) -> RunContext:
    try:
        found = await resolve_run(session, run)
    except RunError as e:
        raise ApiError(404, "run_not_found", str(e)) from e
    if found is None:
        return RunContext(None, await base_now(session))
    return RunContext(found, found.sim_now or found.sim_start, await run_source(session, found))


async def run_context(session: Session, run: RunQuery = None) -> RunContext:
    return await resolve_context(session, run)


# Сессия зависимости с `yield` живёт до конца ответа. Для обычных эндпоинтов это доли секунды, а долгим ответам
# (SSE) нельзя брать `RunCtx`: соединение висело бы «idle in transaction» часами и держало блокировки (инцидент
# 26.09 — миграция ждала `ALTER TABLE runs`, за ней встал весь API). Им — `resolve_context` в своей короткой сессии
RunCtx = Annotated[RunContext, Depends(run_context)]
