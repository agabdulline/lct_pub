"""Асинхронный доменный код из задач Celery: своё соединение без пула на каждый вызов — у `asyncio.run` новый цикл
событий, соединения прошлого цикла использовать нельзя."""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


def run_with_session[T](fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())
