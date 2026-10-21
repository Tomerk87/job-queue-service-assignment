"""Typed runtime configuration read from environment variables."""

import os
from dataclasses import dataclass
from datetime import timedelta

MIN_IDEMPOTENCY_TTL_HOURS = 24


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    idempotency_ttl_hours: int
    schedule_past_tolerance_seconds: int
    redis_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.idempotency_ttl_hours < MIN_IDEMPOTENCY_TTL_HOURS:
            raise ValueError(f"IDEMPOTENCY_TTL_HOURS must be >= {MIN_IDEMPOTENCY_TTL_HOURS}")
        if self.schedule_past_tolerance_seconds < 0:
            raise ValueError("SCHEDULE_PAST_TOLERANCE_SECONDS must be >= 0")

    @property
    def idempotency_ttl(self) -> timedelta:
        return timedelta(hours=self.idempotency_ttl_hours)

    @property
    def schedule_past_tolerance(self) -> timedelta:
        return timedelta(seconds=self.schedule_past_tolerance_seconds)


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://jobqueue:jobqueue@localhost:5432/jobqueue"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        idempotency_ttl_hours=int(os.environ.get("IDEMPOTENCY_TTL_HOURS", "24")),
        schedule_past_tolerance_seconds=int(os.environ.get("SCHEDULE_PAST_TOLERANCE_SECONDS", "30")),
        redis_timeout_seconds=float(os.environ.get("REDIS_TIMEOUT_SECONDS", "0.5")),
    )


settings = load_settings()
