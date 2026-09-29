import time

from app.core import keys
from app.core.redis import sync_client
from app.tasks.celery_app import celery_app


@celery_app.task(name="system.heartbeat")
def heartbeat() -> float:
    """Пульс worker: beat ставит задачу, worker пишет время в Redis — health видит, что цепочка жива."""
    now = time.time()
    sync_client().set(keys.WORKER_HEARTBEAT, now)
    return now


@celery_app.task(name="system.ping")
def ping() -> str:
    return "pong"
