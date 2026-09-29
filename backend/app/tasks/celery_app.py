from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "lct",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.system", "app.tasks.scoring", "app.tasks.runs", "app.tasks.rules", "app.tasks.access"],
)
celery_app.conf.update(
    timezone=settings.timezone,
    task_default_queue="default",
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    beat_schedule={
        "heartbeat": {"task": "system.heartbeat", "schedule": 10.0},
        # часы прогона идут быстрее реальных — такт частый, работа в нём дешёвая
        "runs-tick": {"task": "runs.tick", "schedule": 15.0},
        "trim-raw-window": {"task": "runs.trim_raw_window", "schedule": crontab(hour=3, minute=30)},
        # правила заявок без ML смотрят на часы прогона — тоже часто (docs/architecture/backend.md §5)
        "apply-rules": {"task": "rules.apply_rules", "schedule": 30.0},
    },
)
