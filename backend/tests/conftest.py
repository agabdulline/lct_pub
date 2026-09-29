"""Общие фикстуры. Тесты с БД работают на отдельной базе `<имя>_test` того же PostgreSQL, что и стенд
этого worktree (порты из infra/.env), и пропускаются, если сервер недоступен.

Адреса выставляются в окружение до импорта приложения: API в тестах ходит в тестовую базу и Redis своего стенда,
без пула соединений и без постановки задач Celery.
"""

import os
import zlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND = Path(__file__).resolve().parents[1]
INFRA_ENV = BACKEND.parent / "infra" / ".env"


def _stand_env() -> dict[str, str]:
    env = {
        "POSTGRES_USER": "lct",
        "POSTGRES_PASSWORD": "lct",
        "POSTGRES_DB": "lct",
        "POSTGRES_PORT": "15432",
        "REDIS_PORT": "16379",
    }
    if INFRA_ENV.exists():
        for line in INFRA_ENV.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in env:
                env[key.strip()] = value.strip()
    return env


def _server_url() -> URL:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url:
        return make_url(url)
    env = _stand_env()
    return URL.create(
        "postgresql+psycopg",
        username=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        host="localhost",
        port=int(env["POSTGRES_PORT"]),
        database=env["POSTGRES_DB"],
    )


SERVER_URL = _server_url()
TEST_URL = SERVER_URL.set(database=f"{SERVER_URL.database.removesuffix('_test')}_test")
os.environ["DATABASE_URL"] = TEST_URL.render_as_string(hide_password=False)
# Redis стенда: база 0 — его рабочая (блокировки, часы, очередь Celery), тесты туда не пишут. По умолчанию — база по
# имени тестовой базы (1–15): параллельные сессии с разными TEST_DATABASE_URL реже делят ключи. Каналы потока
# разделены именем тестовой базы (`notify.stream_channel`)
os.environ.setdefault(
    "REDIS_URL", f"redis://localhost:{_stand_env()['REDIS_PORT']}/{1 + zlib.crc32(TEST_URL.database.encode()) % 15}"
)
os.environ["ENQUEUE_TASKS"] = "false"
os.environ["DB_NULL_POOL"] = "true"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def db_url() -> Iterator[URL]:
    server = SERVER_URL.set(database="postgres")
    test_url = TEST_URL
    admin = create_engine(server, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 10})
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{test_url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    except OperationalError as e:
        pytest.skip(f"PostgreSQL стенда недоступен: {e.orig}")
    finally:
        admin.dispose()

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", test_url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")
    yield test_url


TABLES = (
    "access_incident_openings, access_incidents, "
    "work_orders, alarms, predictions, alarm_episodes, fault_episodes, channel_state, channel_daily, "
    "channel_hourly, events_raw, events, model_versions, external_labels, external_permits, external_credentials, "
    "channels, objects, runs"
)
SEED = [
    "INSERT INTO objects (id, parent_id, level, kind, name) VALUES "
    "(1, NULL, 1, 'district', 'Район'), (10, 1, 2, 'controlHouse', 'Объект А'), "
    "(100, 10, 3, 'controlHouse', 'Подобъект А1')",
    "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, stream, deadband) VALUES "
    "(1001, 100, 'Пожарная охрана', 'Датчик дыма', '15-11.1.1.1.', 'ДД ПК1', 'significant', NULL), "
    "(1002, 100, 'Газовая охрана', 'Газовый датчик', '15-11.1.2.1.', 'ГАЗ Д1 ПК2', 'raw', 0.02), "
    "(1003, 100, 'Температурная подсистема', 'Датчик температуры', '15-11.1.3.1.', 'ТЕМП ПК3', 'significant', NULL)",
]


@pytest.fixture
def seeded(db_url: URL) -> URL:
    """Чистая тестовая база: данные всех таблиц удалены, справочник — минимальный."""
    engine = create_engine(db_url, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        for sql in SEED:
            conn.execute(text(sql))
    engine.dispose()
    return db_url


@pytest.fixture
async def session(seeded: URL) -> AsyncIterator[AsyncSession]:
    """Асинхронная сессия на чистой базе с минимальным справочником."""
    engine = create_async_engine(seeded, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Кэши процесса (справочник каналов, опорные значения зоны нечувствительности) — пустые в каждом тесте."""
    from app.domain import channels, ingest

    channels.directory.channels.clear()
    channels.directory.lever_channels.clear()
    ingest._deadbands.clear()
    ingest._deadband_ready.clear()
    yield


@pytest.fixture
def client(seeded):
    """Клиент API поверх чистой тестовой базы с минимальным справочником."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
