import os
import re

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db import models  # noqa: F401 — регистрирует таблицы в metadata
from app.db.base import Base

target_metadata = Base.metadata
# Адрес из alembic.ini / Config (тесты) важнее настроек окружения
url = context.config.get_main_option("sqlalchemy.url") or settings.database_url

# Сколько миграция ждёт блокировку таблицы. Без предела `ALTER TABLE` ждёт, пока держат таблицу, а за ним в очередь
# встают все запросы к ней — API перестаёт отвечать (инцидент на проде 26.09: 21 минута). С пределом миграция
# падает понятной ошибкой, и сервисы работают дальше
LOCK_TIMEOUT = os.environ.get("MIGRATION_LOCK_TIMEOUT", "10s")
if not re.fullmatch(r"\d+(ms|s|min)?", LOCK_TIMEOUT):
    raise SystemExit(f"MIGRATION_LOCK_TIMEOUT: ожидается число с ms, s или min, получено {LOCK_TIMEOUT!r}")


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, connect_args={"options": f"-c lock_timeout={LOCK_TIMEOUT}"})
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    except OperationalError as e:
        if getattr(e.orig, "sqlstate", None) != "55P03":  # lock_not_available
            raise
        raise SystemExit(
            f"Миграция не получила блокировку за {LOCK_TIMEOUT}: таблицу держат другие сессии — обычно api, gateway, "
            "worker или beat. Остановите их (scripts/deploy_prod.sh up делает это сам) или найдите сессию в "
            f"pg_stat_activity. Ничего не изменено. {e.orig}"
        ) from e
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
