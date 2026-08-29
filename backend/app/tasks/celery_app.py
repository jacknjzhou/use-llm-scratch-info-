from celery import Celery

from app.config import settings

celery_app = Celery(
    "extract",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.extract_run"],
)
celery_app.conf.update(
    task_default_queue="extract",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_create_missing_queues=True,
)

# 双队列优先级：加急(≥8)走 extract_high，worker 同时订阅两者且优先消费前者
HIGH_QUEUE = "extract_high"


def queue_for(priority: int) -> str:
    return HIGH_QUEUE if (priority or 0) >= 8 else "extract"
