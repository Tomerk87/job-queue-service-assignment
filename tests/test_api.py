"""API contract tests against real PostgreSQL and Redis (EC-01, EC-02)."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import notifier
from app.config import settings
from app.db import engine, get_session
from app.main import app
from app.models import JobStatus
from tests.conftest import count_rows, set_status

EMAIL_JOB = {"type": "email", "payload": {"to": "user@example.com", "subject": "Hello"}}

VALID_JOBS = [
    {"type": "email", "payload": {"to": "user@example.com", "subject": "Hi", "body": "Text"}},
    {"type": "webhook", "payload": {"url": "https://example.com/hook", "body": {"event": "x"}}},
    {"type": "report", "payload": {"report_name": "monthly-sales"}},
    {"type": "batch", "payload": {"items": [1, 2, 3]}},
]


def submit(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/jobs", json=EMAIL_JOB | overrides)
    assert response.status_code == 201, response.text
    return response.json()


# --- create / get (EC-01 positive path) ---------------------------------------------------


@pytest.mark.parametrize("body", VALID_JOBS, ids=[job["type"] for job in VALID_JOBS])
def test_submit_and_get_each_job_type(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/jobs", json=body)

    assert response.status_code == 201, response.text
    job = response.json()
    assert job["type"] == body["type"]
    assert job["payload"] == body["payload"]
    assert job["status"] == "PENDING"
    assert (job["priority"], job["attempts"], job["max_attempts"], job["progress"]) == (0, 0, 3, 0)
    assert job["result"] is None and job["last_error"] is None
    assert "claim_token" not in job

    fetched = client.get(f"/jobs/{job['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == job


def test_submit_records_created_log(client: TestClient) -> None:
    job = submit(client, priority=7)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT event, metadata FROM job_logs WHERE job_id = :id"), {"id": job["id"]}
        ).all()
    assert [(row.event, row.metadata) for row in rows] == [
        ("created", {"status": "PENDING", "priority": 7})
    ]


# --- validation (EC-01 negative paths) ------------------------------------------------------

INVALID_BODIES = {
    "unknown type": {"type": "sms", "payload": {}},
    "missing payload": {"type": "email"},
    "email missing to": {"type": "email", "payload": {"subject": "x"}},
    "email bad address": {"type": "email", "payload": {"to": "not-an-email", "subject": "x"}},
    "email unknown field": {"type": "email", "payload": {"to": "a@b.c", "subject": "x", "cc": 1}},
    "webhook bad url": {"type": "webhook", "payload": {"url": "not a url"}},
    "report empty name": {"type": "report", "payload": {"report_name": ""}},
    "batch empty items": {"type": "batch", "payload": {"items": []}},
    "batch items not list": {"type": "batch", "payload": {"items": "abc"}},
    "batch over 100 items": {"type": "batch", "payload": {"items": list(range(101))}},
    "priority below 0": EMAIL_JOB | {"priority": -1},
    "priority above 10": EMAIL_JOB | {"priority": 11},
    "priority not int": EMAIL_JOB | {"priority": "high"},
    "max_attempts not accepted": EMAIL_JOB | {"max_attempts": 5},
    "empty idempotency key": EMAIL_JOB | {"idempotency_key": ""},
    "idempotency key too long": EMAIL_JOB | {"idempotency_key": "k" * 256},
    "naive scheduled_at": EMAIL_JOB | {"scheduled_at": "2030-01-01T00:00:00"},
    "unparseable scheduled_at": EMAIL_JOB | {"scheduled_at": "tomorrow"},
    "far past scheduled_at": EMAIL_JOB | {"scheduled_at": "2000-01-01T00:00:00Z"},
}


@pytest.mark.parametrize("body", INVALID_BODIES.values(), ids=INVALID_BODIES.keys())
def test_invalid_submission_returns_422_and_creates_nothing(
    client: TestClient, body: dict[str, Any]
) -> None:
    response = client.post("/jobs", json=body)

    assert response.status_code == 422, response.text
    assert count_rows("jobs") == 0


def test_batch_of_max_size_is_accepted(client: TestClient) -> None:
    response = client.post("/jobs", json={"type": "batch", "payload": {"items": list(range(100))}})
    assert response.status_code == 201


def test_idempotency_key_of_max_length_is_accepted(client: TestClient) -> None:
    assert submit(client, idempotency_key="k" * 255)["idempotency_key"] == "k" * 255


# --- scheduled_at rules via the API (EC-01) ------------------------------------------------


def test_future_scheduled_at_creates_scheduled_job(client: TestClient) -> None:
    run_at = datetime.now(timezone.utc) + timedelta(hours=1)

    job = submit(client, scheduled_at=run_at.isoformat())

    assert job["status"] == "SCHEDULED"
    assert datetime.fromisoformat(job["scheduled_at"]) == run_at
    assert datetime.fromisoformat(job["next_run_at"]) == run_at


def test_offset_scheduled_at_is_stored_as_same_instant(client: TestClient) -> None:
    plus_three = timezone(timedelta(hours=3))
    run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone(plus_three)

    job = submit(client, scheduled_at=run_at.isoformat())

    stored = datetime.fromisoformat(job["scheduled_at"])
    assert stored == run_at
    assert stored.utcoffset() == timedelta(0)


def test_recent_past_scheduled_at_within_tolerance_is_pending(client: TestClient) -> None:
    job = submit(client, scheduled_at=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat())

    assert job["status"] == "PENDING"


def test_scheduled_at_beyond_tolerance_is_rejected(client: TestClient) -> None:
    too_old = datetime.now(timezone.utc) - timedelta(seconds=settings.schedule_past_tolerance_seconds + 5)

    response = client.post("/jobs", json=EMAIL_JOB | {"scheduled_at": too_old.isoformat()})

    assert response.status_code == 422
    assert "in the past" in response.json()["detail"]
    assert count_rows("jobs") == 0


def test_scheduled_at_beyond_max_ahead_is_rejected(client: TestClient) -> None:
    too_far = datetime.now(timezone.utc) + timedelta(days=2 * 365)

    response = client.post("/jobs", json=EMAIL_JOB | {"scheduled_at": too_far.isoformat()})

    assert response.status_code == 422
    assert "in the future" in response.json()["detail"]
    assert count_rows("jobs") == 0


def test_listing_index_exists() -> None:
    with engine.connect() as conn:
        definition = conn.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_created'")
        )
    assert definition is not None and "created_at DESC, id DESC" in definition


# --- get / list ------------------------------------------------------------------------------


def test_get_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_get_malformed_job_id_returns_422(client: TestClient) -> None:
    assert client.get("/jobs/not-a-uuid").status_code == 422


def test_list_is_newest_first_and_paginated(client: TestClient) -> None:
    ids = [submit(client)["id"] for _ in range(3)]

    first_page = client.get("/jobs", params={"limit": 2}).json()
    second_page = client.get("/jobs", params={"limit": 2, "offset": 2}).json()

    assert [job["id"] for job in first_page["items"]] == [ids[2], ids[1]]
    assert (first_page["limit"], first_page["offset"]) == (2, 0)
    assert [job["id"] for job in second_page["items"]] == [ids[0]]


def test_list_filters_by_status_and_type(client: TestClient) -> None:
    email = submit(client)
    report = client.post("/jobs", json=VALID_JOBS[2]).json()
    cancelled_report = client.post("/jobs", json=VALID_JOBS[2]).json()
    client.post(f"/jobs/{cancelled_report['id']}/cancel")

    by_type = client.get("/jobs", params={"type": "report"}).json()["items"]
    by_status = client.get("/jobs", params={"status": "PENDING"}).json()["items"]
    combined = client.get("/jobs", params={"status": "CANCELLED", "type": "report"}).json()["items"]

    assert {job["id"] for job in by_type} == {report["id"], cancelled_report["id"]}
    assert {job["id"] for job in by_status} == {email["id"], report["id"]}
    assert [job["id"] for job in combined] == [cancelled_report["id"]]


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "BOGUS"}, {"type": "sms"}],
    ids=["limit-0", "limit-101", "offset-negative", "bad-status", "bad-type"],
)
def test_list_rejects_invalid_parameters(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/jobs", params=params).status_code == 422


def test_list_accepts_limit_bounds(client: TestClient) -> None:
    assert client.get("/jobs", params={"limit": 1}).status_code == 200
    assert client.get("/jobs", params={"limit": 100}).status_code == 200


# --- cancel (EC-02) -------------------------------------------------------------------------


def test_cancel_pending_job(client: TestClient) -> None:
    job = submit(client)

    response = client.post(f"/jobs/{job['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert client.get(f"/jobs/{job['id']}").json()["status"] == "CANCELLED"
    with engine.connect() as conn:
        events = conn.scalars(
            text("SELECT event FROM job_logs WHERE job_id = :id ORDER BY id"), {"id": job["id"]}
        ).all()
    assert events == ["created", "cancelled"]


def test_cancel_scheduled_job(client: TestClient) -> None:
    run_at = datetime.now(timezone.utc) + timedelta(hours=1)
    job = submit(client, scheduled_at=run_at.isoformat())

    response = client.post(f"/jobs/{job['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_cancel_is_idempotent_for_cancelled_job(client: TestClient) -> None:
    job = submit(client)
    client.post(f"/jobs/{job['id']}/cancel")

    response = client.post(f"/jobs/{job['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert count_rows("job_logs") == 2  # created + one cancelled; the repeat logs nothing


@pytest.mark.parametrize("status", ["PROCESSING", "COMPLETED", "FAILED"])
def test_cancel_in_non_cancellable_state_returns_409(client: TestClient, status: str) -> None:
    job = submit(client)
    set_status(job["id"], status)

    response = client.post(f"/jobs/{job['id']}/cancel")

    assert response.status_code == 409
    assert client.get(f"/jobs/{job['id']}").json()["status"] == status


def test_cancel_unknown_job_returns_404(client: TestClient) -> None:
    response = client.post("/jobs/00000000-0000-0000-0000-000000000000/cancel")
    assert response.status_code == 404


# --- health and Redis independence ----------------------------------------------------------


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    unreachable = redis.Redis.from_url(
        "redis://127.0.0.1:1/0", socket_timeout=0.2, socket_connect_timeout=0.2
    )
    monkeypatch.setattr(notifier, "_client", unreachable)


def test_health_ok_reports_counts_per_status(client: TestClient) -> None:
    submit(client)
    cancelled = submit(client)
    client.post(f"/jobs/{cancelled['id']}/cancel")

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["database"], body["redis"]) == ("ok", "ok", "ok")
    assert body["jobs"] == {status.value: 0 for status in JobStatus} | {"PENDING": 1, "CANCELLED": 1}


def test_health_degraded_when_redis_down(client: TestClient, redis_down: None) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["database"], body["redis"]) == ("degraded", "ok", "down")


def test_submit_succeeds_when_redis_down(client: TestClient, redis_down: None) -> None:
    job = submit(client)

    assert client.get(f"/jobs/{job['id']}").json()["status"] == "PENDING"


def test_health_unhealthy_when_database_down(client: TestClient) -> None:
    dead_engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")

    def dead_session() -> Iterator[Session]:
        with Session(dead_engine) as session:
            yield session

    app.dependency_overrides[get_session] = dead_session
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()
        dead_engine.dispose()

    assert response.status_code == 503
    body = response.json()
    assert (body["status"], body["database"], body["jobs"]) == ("unhealthy", "down", None)


def test_new_job_publishes_wakeup_after_commit(client: TestClient) -> None:
    subscriber = redis.Redis.from_url(settings.redis_url).pubsub()
    subscriber.subscribe(notifier.JOB_CREATED_CHANNEL)
    assert subscriber.get_message(timeout=2)["type"] == "subscribe"
    try:
        job = submit(client)
        message = subscriber.get_message(timeout=2)
    finally:
        subscriber.close()

    assert message is not None and message["data"].decode() == job["id"]
