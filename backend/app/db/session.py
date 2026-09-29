from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

_pool = {"poolclass": NullPool} if settings.db_null_pool else {"pool_pre_ping": True}
engine = create_async_engine(settings.database_url, connect_args={"connect_timeout": 5}, **_pool)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
