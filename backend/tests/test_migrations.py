"""Миграции не вешают API (инцидент на проде 26.09): предел ожидания блокировки и таймаут забытой транзакции."""

import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

BACKEND = Path(__file__).resolve().parents[1]


def alembic_config(db_url) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url.render_as_string(hide_password=False).replace("%", "%%"))
    return cfg


def test_migration_fails_fast_when_table_is_held(db_url, monkeypatch):
    cfg = alembic_config(db_url)
    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT", "1s")
    command.downgrade(cfg, "0006")  # 0007 — ALTER TABLE runs, как в инциденте
    engine = create_engine(db_url, poolclass=NullPool)
    holder = engine.connect()
    try:
        holder.execute(text("SELECT count(*) FROM runs"))  # открытая транзакция держит блокировку runs
        started = time.monotonic()
        with pytest.raises(SystemExit, match="не получила блокировку за 1s"):
            command.upgrade(cfg, "head")
        assert time.monotonic() - started < 10
        holder.rollback()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006"
    finally:
        holder.close()
        command.upgrade(cfg, "head")
        engine.dispose()


def test_idle_in_transaction_timeout(db_url):
    engine = create_engine(db_url, poolclass=NullPool)
    with engine.connect() as conn:
        assert conn.execute(text("SHOW idle_in_transaction_session_timeout")).scalar_one() == "5min"
    engine.dispose()


def test_role_migration_does_not_rewrite_shared_role(db_url, monkeypatch):
    """Роль `lct_ml` и её настройки — в общих каталогах сервера: миграции тестовых баз разных сессий pytest писали одну
    строку, и одновременная запись падала «tuple concurrently updated» — 112 ошибок на setup у одной из двух
    параллельных сессий. Уже настроенную роль миграция не трогает: чужая транзакция держит строку, миграция идёт."""
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0005")
    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT", "2s")
    engine = create_engine(db_url, poolclass=NullPool)
    holder = engine.connect()
    try:
        holder.execute(text("ALTER ROLE lct_ml SET default_transaction_read_only = on"))  # строка роли занята
        command.upgrade(cfg, "0006")
    finally:
        holder.rollback()
        holder.close()
        command.upgrade(cfg, "head")
        engine.dispose()
