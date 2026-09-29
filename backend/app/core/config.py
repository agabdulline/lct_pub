from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из переменных окружения. Значения по умолчанию — для запуска на хосте против infra/docker-compose."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://lct:lct@localhost:15432/lct"
    redis_url: str = "redis://localhost:16379/0"
    ml_service_url: str = "http://localhost:18010"
    # основная версия событийной модели новых прогонов (15.2); остальные готовые версии скорят рядом, теневыми.
    # Связка «v3, где оценила, иначе v0» (`model_versions.CHAIN_SEP`); её частей нет среди готовых — последняя часть
    primary_alarm_model: str = "v3>baseline-v0"
    opcua_endpoint: str = "opc.tcp://localhost:14840/lct/smvu"
    # HTTP-управление эмулятором — только внутри сети Docker (docs/api.md §3); на хосте задать EMULATOR_URL
    emulator_url: str = "http://emulator:8000"
    opcua_namespace: str = "urn:lct-burrgers:smvu"
    # сколько источник может молчать, прежде чем шлюз сочтёт связь потерянной (`gateway/opcua.py`, WATCHDOG)
    opcua_watchdog_seconds: float = 60.0
    timezone: str = "Europe/Moscow"
    auto_migrate: bool = False
    clock_stale_seconds: float = 5.0
    heartbeat_stale_seconds: float = 30.0
    # Данные: исходники, архив Parquet, промежуточные файлы (пути от backend/ или из окружения контейнера)
    raw_dir: Path = Path("../data/raw")
    archive_dir: Path = Path("../data/archive")
    interim_dir: Path = Path("../data/interim")
    external_dir: Path = Path("../data/external")  # внешние наборы: <набор>/prepared/*.parquet (этап 14)
    # Загруженные журналы и сценарии из них (docs/api.md, «Сценарий из своего журнала»): оригиналы — в uploads_dir,
    # срез и метаданные — в emulator_dir/uploaded, откуда их читает эмулятор
    uploads_dir: Path = Path("../data/uploads")
    emulator_dir: Path = Path("../data/processed/emulator")
    # Внешние открытые наборы (docs/architecture/external-data.md §4.1): raw/ и prepared/ по ключу набора
    external_dir: Path = Path("../data/external")
    upload_max_bytes: int = 300 * 1024 * 1024
    upload_duckdb_memory: str = "256MB"  # разбор загрузки в процессе api; лишнее — на диск
    enqueue_tasks: bool = True  # ставить задачи Celery (скоринг, архив прогона); в тестах выключено
    db_null_pool: bool = False  # без пула соединений — для тестов, где у каждого запроса свой цикл событий


settings = Settings()
