"""Best-effort Redis wake-up notifications.

Redis is only a hint that new work exists. PostgreSQL is the queue, and workers poll
it, so a failed publish delays pickup at most until the next poll and never loses a job.
"""

import logging
import uuid

import redis

from app.config import settings

JOB_CREATED_CHANNEL = "jobs:created"

logger = logging.getLogger(__name__)

_client = redis.Redis.from_url(
    settings.redis_url,
    socket_timeout=settings.redis_timeout_seconds,
    socket_connect_timeout=settings.redis_timeout_seconds,
)


def notify_job_created(job_id: uuid.UUID) -> None:
    """Publish a wake-up hint. Call only after the job row is committed."""
    try:
        _client.publish(JOB_CREATED_CHANNEL, str(job_id))
    except redis.RedisError as exc:
        logger.warning(
            "redis notification failed (job_id=%s, error=%s); workers will find the job by polling",
            job_id,
            type(exc).__name__,
        )


def redis_ok() -> bool:
    try:
        return bool(_client.ping())
    except redis.RedisError:
        return False
