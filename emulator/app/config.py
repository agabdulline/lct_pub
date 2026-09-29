from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Значения по умолчанию — для запуска на хосте из папки emulator/; в контейнере пути задаёт docker-compose."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    opcua_endpoint: str = "opc.tcp://0.0.0.0:4840/lct/smvu"
    opcua_namespace: str = "urn:lct-burrgers:smvu"
    control_port: int = 8000
    # Проигрыватель (§6): шаг и предел выхода, событий в секунду реального времени
    tick_seconds: float = 0.1
    max_rate: float = 200.0
    # Справочники монтируются только на чтение (docs/architecture/emulator.md §3)
    raw_dir: Path = Path("../data/raw")
    objects_csv: Path = Path("../data/raw/справочник_объектов_диспетчер.csv")
    channels_csv: Path = Path("../data/raw/справочник_каналов_датчиков.csv")
    # Свойства каналов SensorType, Subsystem, Tag, Picket — 46 тыс. из 80 тыс. узлов, ~310 из ~660 МБ памяти.
    # Шлюз их не читает; на машине разработчика, где стенды делят память Docker, их выключают (infra/.env)
    channel_properties: bool = True
    # Сценарии и их срезы (§4)
    scenarios_dir: Path = Path("scenarios")
    slices_dir: Path = Path("../data/processed/emulator")
    # Сводка сценария — в отдельном процессе (scenario_data.SummaryWorker), чтобы не останавливать публикацию OPC UA
    summary_worker: bool = True
    # Внешние наборы: prepared/ читает только подготовка срезов, в контейнер не монтируется (external-data.md §5)
    external_dir: Path = Path("../data/external")


settings = Settings()
