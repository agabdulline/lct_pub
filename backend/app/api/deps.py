"""Общие зависимости эндпоинтов: сессия БД и выбранный прогон `?run=` (docs/api.md, «Прогон»)."""

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Query
from sqlalchemy import select, text
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
    # «Все данные» (`?run=all`): база хакатона и прогоны-основы всех наборов — каждый со своим временем, не смешиваются
    all_data: bool = False
    dataset_runs: tuple[Run, ...] = ()

    @property
    def run_id(self) -> int | None:
        return self.run.id if self.run else None


ALL = "all"
RunQuery = Annotated[
    int | Literal["all"] | None,
    Query(
        description="прогон; без параметра — последний, 0 — только базовая история, all — все данные: база хакатона "
        "и прогоны-основы всех наборов"
    ),
]


async def dataset_base_runs(session: AsyncSession) -> list[Run]:
    """Прогоны-основы внешних наборов (`kind = dataset`) — их история целиком."""
    return list((await session.scalars(select(Run).where(Run.kind == "dataset").order_by(Run.id))).all())


async def resolve_context(session: AsyncSession, run: int | Literal["all"] | None) -> RunContext:
    if run == ALL:
        return RunContext(None, await base_now(session), HACKATHON, True, tuple(await dataset_base_runs(session)))
    try:
        found = await resolve_run(session, run)
    except RunError as e:
        raise ApiError(404, "run_not_found", str(e)) from e
    if found is None:
        return RunContext(None, await base_now(session))
    return RunContext(found, found.sim_now or found.sim_start, await run_source(session, found))


async def for_channel(session: AsyncSession, ctx: RunContext, channel_id: int) -> RunContext:
    """«Все данные»: канал внешнего набора смотрится в прогоне-основе своего набора, канал хакатона — в базе."""
    if not ctx.all_data:
        return ctx
    source = await session.scalar(text("SELECT source FROM channels WHERE id = :id"), {"id": channel_id})
    run = next((r for r in ctx.dataset_runs if (r.params or {}).get("dataset") == source), None)
    if run is None:
        return ctx
    return RunContext(run, run.sim_now or run.sim_end or run.sim_start, source)


async def run_context(session: Session, run: RunQuery = None) -> RunContext:
    return await resolve_context(session, run)


# Сессия зависимости с `yield` живёт до конца ответа. Для обычных эндпоинтов это доли секунды, а долгим ответам
# (SSE) нельзя брать `RunCtx`: соединение висело бы «idle in transaction» часами и держало блокировки (инцидент
# 26.09 — миграция ждала `ALTER TABLE runs`, за ней встал весь API). Им — `resolve_context` в своей короткой сессии
RunCtx = Annotated[RunContext, Depends(run_context)]
