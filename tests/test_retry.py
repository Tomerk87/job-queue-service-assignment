"""Automatic retry with backoff and manual retry against real PostgreSQL (EC-09, EC-10, EC-02).

Retry delays are asserted against database time and skipped with fast_forward(),
so no test sleeps for the 30s/120s production delays.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.jobs import JobStateConflictError, retry_job
from tests.conftest import set_status
from tests.helpers import (
    FAST,
    db_row,
    fast_forward,
    log_events,
    submit,
    time_until_next_run,
    work_once,
)

ALWAYS_FAIL = replace(FAST, webhook_failure_rate=1.0)
TOLERANCE = timedelta(seconds=2)


def assert_delay(job_id: str, expected: timedelta) -> None:
    remaining = time_until_next_run(job_id)
    assert expected - TOLERANCE <= remaining <= expected, remaining


# --- automatic retry (EC-09) --------------------------------------------------------------------


def test_three_attempts_with_30s_and_120s_backoff_then_failed(client: TestClient) -> None:
    job_id = submit(client, "webhook")

    assert work_once(ALWAYS_FAIL) is True
    row = db_row(job_id)
    assert (row.status, row.attempts) == ("PENDING", 1)
    assert row.last_error.startswith("SimulatedFailure: ")
    assert_delay(job_id, timedelta(seconds=30))
    assert work_once(ALWAYS_FAIL) is False  # not eligible before the delay has passed

    fast_forward(job_id)
    assert work_once(ALWAYS_FAIL) is True
    assert (db_row(job_id).status, db_row(job_id).attempts) == ("PENDING", 2)
    assert_delay(job_id, timedelta(seconds=120))
    assert work_once(ALWAYS_FAIL) is False

    fast_forward(job_id)
    next_run_before_last_attempt = db_row(job_id).next_run_at
    assert work_once(ALWAYS_FAIL) is True
    row = db_row(job_id)
    assert (row.status, row.attempts) == ("FAILED", 3)
    assert row.next_run_at == next_run_before_last_attempt  # terminal: not rescheduled
    assert (row.claim_token, row.lease_expires_at) == (None, None)
    assert work_once(ALWAYS_FAIL) is False  # FAILED is never retried automatically

    assert log_events(job_id) == [
        "created",
        "claimed", "retry_scheduled",
        "claimed", "retry_scheduled",
        "claimed", "failed",
    ]


def test_success_on_retry_completes_and_clears_last_error(client: TestClient) -> None:
    job_id = submit(client, "webhook")
    work_once(ALWAYS_FAIL)
    assert db_row(job_id).last_error is not None

    fast_forward(job_id)
    assert work_once(FAST) is True

    job = client.get(f"/jobs/{job_id}").json()
    assert (job["status"], job["attempts"], job["last_error"]) == ("COMPLETED", 2, None)
    assert job["result"] == {"status_code": 200}


def test_configured_delays_are_used(client: TestClient) -> None:
    job_id = submit(client, "webhook")

    work_once(replace(ALWAYS_FAIL, retry_delays_seconds=(5, 7)))

    assert_delay(job_id, timedelta(seconds=5))


def test_job_waiting_for_retry_can_be_cancelled(client: TestClient) -> None:
    job_id = submit(client, "webhook")
    work_once(ALWAYS_FAIL)

    response = client.post(f"/jobs/{job_id}/cancel")

    assert (response.status_code, response.json()["status"]) == (200, "CANCELLED")
    fast_forward(job_id)
    assert work_once(ALWAYS_FAIL) is False


# --- manual retry (EC-10) ---------------------------------------------------------------------


def fail_permanently(client: TestClient) -> str:
    job_id = submit(client, "webhook")
    for _ in range(3):
        fast_forward(job_id)
        work_once(ALWAYS_FAIL)
    assert db_row(job_id).status == "FAILED"
    return job_id


def test_manual_retry_resets_attempts_and_keeps_history(client: TestClient) -> None:
    job_id = fail_permanently(client)
    history = log_events(job_id)

    response = client.post(f"/jobs/{job_id}/retry")

    assert response.status_code == 200
    job = response.json()
    assert (job["status"], job["attempts"], job["progress"]) == ("PENDING", 0, 0)
    assert (job["last_error"], job["result"], job["started_at"], job["completed_at"]) == (
        None, None, None, None,
    )
    assert log_events(job_id) == history + ["manual_retry"]

    assert work_once(FAST) is True  # eligible immediately, attempt 1 of a new cycle
    job = client.get(f"/jobs/{job_id}").json()
    assert (job["status"], job["attempts"]) == ("COMPLETED", 1)


def test_manual_retry_starts_a_full_new_cycle_of_attempts(client: TestClient) -> None:
    job_id = fail_permanently(client)
    client.post(f"/jobs/{job_id}/retry")

    work_once(ALWAYS_FAIL)

    row = db_row(job_id)
    assert (row.status, row.attempts) == ("PENDING", 1)
    assert_delay(job_id, timedelta(seconds=30))


def test_concurrent_manual_retries_have_one_winner(client: TestClient) -> None:
    job_id = uuid.UUID(fail_permanently(client))
    barrier = threading.Barrier(2)

    def retry() -> bool:
        barrier.wait()
        with SessionLocal() as session:
            try:
                retry_job(session, job_id)
                return True
            except JobStateConflictError:
                return False

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: retry(), range(2)))

    assert sorted(outcomes) == [False, True]
    assert log_events(str(job_id)).count("manual_retry") == 1


# --- invalid retry states (EC-02 remainder) ------------------------------------------------------


@pytest.mark.parametrize("status", ["PENDING", "SCHEDULED", "PROCESSING", "COMPLETED", "CANCELLED"])
def test_retry_in_non_failed_state_returns_409(client: TestClient, status: str) -> None:
    job_id = submit(client)
    set_status(job_id, status)

    response = client.post(f"/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert status in response.json()["detail"]
    assert db_row(job_id).status == status


def test_retry_unknown_job_returns_404(client: TestClient) -> None:
    assert client.post(f"/jobs/{uuid.uuid4()}/retry").status_code == 404
