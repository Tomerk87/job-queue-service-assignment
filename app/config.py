"""Typed runtime configuration read from environment variables."""

import os
import socket
from dataclasses import dataclass
from datetime import timedelta

MIN_IDEMPOTENCY_TTL_HOURS = 24
MAX_POLL_INTERVAL_SECONDS = 2.0
# Delays before automatic attempts 2 and 3 (jobs have 3 attempts per cycle).
RETRY_DELAY_COUNT = 2


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    idempotency_ttl_hours: int
    schedule_past_tolerance_seconds: int
    schedule_max_ahead_days: int
    redis_timeout_seconds: float
    worker_id: str
    worker_poll_interval_seconds: float
    lease_seconds: float
    webhook_failure_rate: float
    batch_item_delay_seconds: float
    retry_delays_seconds: tuple[int, ...]
    heartbeat_interval_seconds: float
    recovery_interval_seconds: float
    job_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.idempotency_ttl_hours < MIN_IDEMPOTENCY_TTL_HOURS:
            raise ValueError(f"IDEMPOTENCY_TTL_HOURS must be >= {MIN_IDEMPOTENCY_TTL_HOURS}")
        if self.schedule_past_tolerance_seconds < 0:
            raise ValueError("SCHEDULE_PAST_TOLERANCE_SECONDS must be >= 0")
        if self.schedule_max_ahead_days <= 0:
            raise ValueError("SCHEDULE_MAX_AHEAD_DAYS must be > 0")
        if not 0 < self.worker_poll_interval_seconds <= MAX_POLL_INTERVAL_SECONDS:
            raise ValueError(f"WORKER_POLL_INTERVAL_SECONDS must be in (0, {MAX_POLL_INTERVAL_SECONDS}]")
        if self.lease_seconds <= 0:
            raise ValueError("LEASE_SECONDS must be > 0")
        if not 0 <= self.webhook_failure_rate <= 1:
            raise ValueError("WEBHOOK_FAILURE_RATE must be in [0, 1]")
        if self.batch_item_delay_seconds < 0:
            raise ValueError("BATCH_ITEM_DELAY_SECONDS must be >= 0")
        if not 0 < self.heartbeat_interval_seconds < self.lease_seconds:
            raise ValueError("HEARTBEAT_INTERVAL_SECONDS must be > 0 and < LEASE_SECONDS")
        if self.recovery_interval_seconds <= 0:
            raise ValueError("RECOVERY_INTERVAL_SECONDS must be > 0")
        if self.job_timeout_seconds <= 0:
            raise ValueError("JOB_TIMEOUT_SECONDS must be > 0")
        if len(self.retry_delays_seconds) != RETRY_DELAY_COUNT or min(self.retry_delays_seconds) < 0:
            raise ValueError(f"RETRY_DELAYS_SECONDS must be {RETRY_DELAY_COUNT} non-negative integers")

    @property
    def idempotency_ttl(self) -> timedelta:
        return timedelta(hours=self.idempotency_ttl_hours)

    @property
    def schedule_past_tolerance(self) -> timedelta:
        return timedelta(seconds=self.schedule_past_tolerance_seconds)

    @property
    def schedule_max_ahead(self) -> timedelta:
        return timedelta(days=self.schedule_max_ahead_days)

    @property
    def lease(self) -> timedelta:
        return timedelta(seconds=self.lease_seconds)

    @property
    def retry_delays(self) -> tuple[timedelta, ...]:
        return tuple(timedelta(seconds=seconds) for seconds in self.retry_delays_seconds)


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://jobqueue:jobqueue@localhost:5432/jobqueue"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        idempotency_ttl_hours=int(os.environ.get("IDEMPOTENCY_TTL_HOURS", "24")),
        schedule_past_tolerance_seconds=int(os.environ.get("SCHEDULE_PAST_TOLERANCE_SECONDS", "30")),
        schedule_max_ahead_days=int(os.environ.get("SCHEDULE_MAX_AHEAD_DAYS", "365")),
        redis_timeout_seconds=float(os.environ.get("REDIS_TIMEOUT_SECONDS", "0.5")),
        worker_id=os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
        worker_poll_interval_seconds=float(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "2")),
        lease_seconds=float(os.environ.get("LEASE_SECONDS", "30")),
        webhook_failure_rate=float(os.environ.get("WEBHOOK_FAILURE_RATE", "0.2")),
        batch_item_delay_seconds=float(os.environ.get("BATCH_ITEM_DELAY_SECONDS", "0.2")),
        heartbeat_interval_seconds=float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "5")),
        recovery_interval_seconds=float(os.environ.get("RECOVERY_INTERVAL_SECONDS", "5")),
        job_timeout_seconds=float(os.environ.get("JOB_TIMEOUT_SECONDS", "60")),
        retry_delays_seconds=tuple(
            int(part) for part in os.environ.get("RETRY_DELAYS_SECONDS", "30,120").split(",")
        ),
    )


settings = load_settings()
