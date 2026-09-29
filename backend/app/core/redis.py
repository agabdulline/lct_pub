import redis
import redis.asyncio as aioredis

from app.core.config import settings


def async_client() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


def sync_client() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


async def try_lock(key: str, seconds: int) -> bool:
    """Блокировка на ключ с истечением: повтор задачи не выполняет ту же работу параллельно. Без Redis — без
    блокировки."""
    try:
        client = async_client()
        got = await client.set(key, "1", nx=True, ex=seconds)
        await client.aclose()
        return bool(got)
    except Exception:  # noqa: BLE001
        return True


async def unlock(key: str) -> None:
    try:
        client = async_client()
        await client.delete(key)
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass
