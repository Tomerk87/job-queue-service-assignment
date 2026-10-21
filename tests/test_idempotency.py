"""Idempotency against real PostgreSQL, including concurrent races (EC-03, EC-04)."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from app.jobs import create_job
from app.schemas import JobCreate
from tests.conftest import count_rows

THREADS = 10


def email_job(key: str, subject: str = "Hello") -> dict:
    return {
        "type": "email",
        "payload": {"to": "user@example.com", "subject": subject},
        "idempotency_key": key,
    }


def submit_concurrently(request: JobCreate) -> list[tuple[bool, uuid.UUID]]:
    """Release THREADS submissions at once, each with its own session and connection."""
    barrier = threading.Barrier(THREADS)

    def submit_one() -> tuple[bool, uuid.UUID]:
        with SessionLocal() as session:
            barrier.wait()
            result = create_job(session, request, settings)
            return result.created, result.job.id

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(submit_one) for _ in range(THREADS)]
        return [future.result() for future in futures]


def expire_key(key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE key = :k"),
            {"k": key},
        )


def mapped_job_id(key: str) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(text("SELECT job_id FROM idempotency_keys WHERE key = :k"), {"k": key}))


def test_same_key_returns_original_job(client: TestClient) -> None:
    first = client.post("/jobs", json=email_job("order-1"))
    second = client.post("/jobs", json=email_job("order-1"))

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    assert count_rows("jobs") == 1


def test_same_key_with_different_payload_returns_original(client: TestClient) -> None:
    original = client.post("/jobs", json=email_job("order-2", subject="first")).json()

    response = client.post("/jobs", json=email_job("order-2", subject="second"))

    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    assert response.json()["payload"]["subject"] == "first"
    assert count_rows("jobs") == 1


def test_different_keys_create_different_jobs(client: TestClient) -> None:
    a = client.post("/jobs", json=email_job("key-a")).json()
    b = client.post("/jobs", json=email_job("key-b")).json()

    assert a["id"] != b["id"]
    assert count_rows("jobs") == 2


def test_key_is_retained_for_at_least_24_hours(client: TestClient) -> None:
    client.post("/jobs", json=email_job("retained"))

    with engine.connect() as conn:
        remaining = conn.scalar(
            text("SELECT expires_at - now() FROM idempotency_keys WHERE key = 'retained'")
        )
    assert remaining >= timedelta(hours=24) - timedelta(minutes=1)


def test_concurrent_same_key_creates_exactly_one_job() -> None:
    """EC-03: simultaneous submissions with one active key yield a single job."""
    request = JobCreate.model_validate(email_job("race-1"))

    outcomes = submit_concurrently(request)

    created = [job_id for was_created, job_id in outcomes if was_created]
    assert len(created) == 1
    assert {job_id for _, job_id in outcomes} == {created[0]}
    assert count_rows("jobs") == 1
    assert count_rows("job_logs") == 1  # losers' rows and logs were rolled back
    assert mapped_job_id("race-1") == str(created[0])


def test_expired_key_creates_new_job_and_keeps_old_one(client: TestClient) -> None:
    """EC-04: an expired key maps to a new job; the old job stays queryable."""
    old = client.post("/jobs", json=email_job("reuse-1")).json()
    expire_key("reuse-1")

    response = client.post("/jobs", json=email_job("reuse-1"))

    assert response.status_code == 201
    new = response.json()
    assert new["id"] != old["id"]
    assert client.get(f"/jobs/{old['id']}").status_code == 200
    assert mapped_job_id("reuse-1") == new["id"]
    assert count_rows("jobs") == 2

    # The renewed key is active again: a further submission returns the new job.
    again = client.post("/jobs", json=email_job("reuse-1"))
    assert (again.status_code, again.json()["id"]) == (200, new["id"])


def test_concurrent_reuse_of_expired_key_creates_exactly_one_new_job(client: TestClient) -> None:
    """EC-04 race: competing submissions on an expired key replace it exactly once."""
    old = client.post("/jobs", json=email_job("reuse-race")).json()
    expire_key("reuse-race")

    outcomes = submit_concurrently(JobCreate.model_validate(email_job("reuse-race")))

    created = [job_id for was_created, job_id in outcomes if was_created]
    assert len(created) == 1
    assert {job_id for _, job_id in outcomes} == {created[0]}
    assert str(created[0]) != old["id"]
    assert mapped_job_id("reuse-race") == str(created[0])
    assert count_rows("jobs") == 2
