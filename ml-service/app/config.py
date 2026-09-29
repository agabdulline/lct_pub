from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # По умолчанию — модели из репозитория (ml/models), в контейнере — /app/ml/models.
    model_dir: Path = Path(__file__).resolve().parents[2] / "ml" / "models"
    # Рабочая база, только чтение (D2). По умолчанию — стенд координатора.
    database_url: str = "postgresql+psycopg://lct:lct@localhost:15432/lct"
    timezone: str = "Europe/Moscow"


settings = Settings()
