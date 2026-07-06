from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "bts",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "renew-graph-subscription-every-2d": {
            "task": "app.tasks.tasks.renew_graph_subscription",
            "schedule": 2 * 24 * 3600,  # every 2 days
        },
    },
)
