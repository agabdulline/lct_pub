"""Живой поток: каналы Redis pub/sub разделены рабочей базой (app/domain/notify.py)."""

import json

import pytest

from app.core.config import settings
from app.core.redis import async_client
from app.domain.notify import Notification, publish, stream_channel

pytestmark = pytest.mark.anyio


async def test_stream_channel_is_scoped_to_database(monkeypatch):
    """Каналы pub/sub общие для всего сервера Redis, номер базы Redis их не разделяет: тесты и соседний стенд на том же
    Redis слали события в ленту стенда — прогон 1 мини-стенда получал `run.updated` тестов про прогон 1. Канал несёт
    имя рабочей базы: событие другой базы про тот же прогон сюда не приходит."""
    own = stream_channel(1)
    assert own.startswith("stream:") and own.endswith(":1") and own != "stream:1"

    redis = async_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(own)
    try:
        await pubsub.get_message(timeout=1)  # подтверждение подписки
        other = settings.database_url.rsplit("/", 1)[0] + "/another_stand"
        monkeypatch.setattr(settings, "database_url", other)
        assert stream_channel(1) != own
        await publish([Notification(1, "run.updated", {"id": 1, "from": "another_stand"})])
        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5) is None

        monkeypatch.undo()
        await publish([Notification(1, "run.updated", {"id": 1, "from": "own"})])
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
        assert message is not None and json.loads(message["data"])["data"]["from"] == "own"
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
        await redis.aclose()
