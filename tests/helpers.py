"""Shared helpers for worker, retry and scheduling tests (real PostgreSQL)."""

import random
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import jobs
from app.config import Settings, settings
from app.db import SessionLocal, engine
from app.jobs import ClaimedJob
from app.worker import run_once

FAST = replace(
    settings, worker_id="test-worker", webhook_failure_rate=0.0, batch_item_delay_seconds=0.0
)

JOBS_BY_TYPE = {
    "email": {"to": "user@example.com", "subject": "Hi"},
    "webhook": {"url": "https://example.com/hook"},
    "report": {"report_name": "monthly"},
    "batch": {"items": [1, 2, 3]},
}


def no_sleep(_seconds: float) -> None:
    return None


def submit(client: TestClient, job_type: str = "email", **extra: Any) -> str:
    response = client.post("/jobs", json={"type": job_type, "payload": JOBS_BY_TYPE[job_type]} | extra)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def work_once(config: Settings = FAST, sleep: Callable[[float], None] = no_sleep) -> bool:
    return run_once(config, random.Random(0), sleep)


def db_row(job_id: str | uuid.UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": str(job_id)}).one()


def log_events(job_id: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.scalars(
                text("SELECT event FROM job_logs WHERE job_id = :id ORDER BY id"), {"id": job_id}
            )
        )


def claim(worker_id: str = "test-worker") -> ClaimedJob | None:
    with SessionLocal() as session:
        return jobs.claim_next_job(session, worker_id, settings.lease)


def steal_job(job_id: str | uuid.UUID) -> None:
    """Simulate another worker taking over the job by replacing its claim token."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET claim_token = :token WHERE id = :id"),
            {"token": str(uuid.uuid4()), "id": str(job_id)},
        )


def fast_forward(job_id: str | uuid.UUID) -> None:
    """Make a job waiting for its retry delay eligible now, instead of sleeping in the test."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET next_run_at = now() WHERE id = :id"), {"id": str(job_id)})


def time_until_next_run(job_id: str | uuid.UUID) -> timedelta:
    with engine.connect() as conn:
        return conn.scalar(
            text("SELECT next_run_at - now() FROM jobs WHERE id = :id"), {"id": str(job_id)}
        )
