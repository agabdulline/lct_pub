"""Живой поток: события для SSE публикуются в Redis pub/sub, `api` транслирует их в `/stream` (D5).

Канал Redis — на прогон: `stream:<рабочая база>:<run_id>`, база — `stream:<рабочая база>:base`. Каналы pub/sub общие
для всего сервера Redis — номер базы Redis их не разделяет, — поэтому в имени рабочая база: тесты и соседние стенды на
том же Redis иначе слали события в ленту стенда (мини-стенд получал `run.updated` тестов на своём прогоне 1).
Публикуется только после коммита, иначе интерфейс запросит данные, которых ещё не видно.
"""

import json
import logging
from dataclasses import dataclass
from functools import cache

from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.redis import async_client

log = logging.getLogger("notify")


@cache
def _namespace(database_url: str) -> str:
    return make_url(database_url).database or "lct"


def stream_channel(run_id: int | None) -> str:
    return f"stream:{_namespace(settings.database_url)}:{run_id if run_id is not None else 'base'}"


@dataclass(frozen=True, slots=True)
class Notification:
    run_id: int | None
    type: str  # alarm.created, alarm.scored, alarm_episode.updated, prediction.created, …
    data: dict

    def encode(self) -> str:
        return json.dumps({"type": self.type, "data": self.data}, ensure_ascii=False, default=json_default)


def json_default(value):
    """Время — ISO 8601 с «T», как в REST: `str(datetime)` даёт пробел, Safari такое не разбирает."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def publish(notifications: list[Notification]) -> int:
    """Опубликовать события; ошибки Redis не ломают приём — лента догонит данные запросом."""
    if not notifications:
        return 0
    try:
        redis = async_client()
        async with redis.pipeline(transaction=False) as pipe:
            for n in notifications:
                pipe.publish(stream_channel(n.run_id), n.encode())
            await pipe.execute()
        await redis.aclose()
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось опубликовать %d событий: %s", len(notifications), e)
        return 0
    return len(notifications)
