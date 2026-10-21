"""Worker integration tests against real PostgreSQL and Redis (EC-05, EC-06, EC-14, EC-16).

Handlers run with an injected no-op sleep, so tests are fast and deterministic.
"""

import random
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import jobs, notifier
from app import worker as worker_module
from app.config import settings
from app.db import SessionLocal, engine
from app.jobs import ClaimedJob
from app.worker import Wakeup, run, run_once

THREADS = 10
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
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


def work_once(config=FAST, sleep: Callable[[float], None] = no_sleep) -> bool:
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


# --- happy path and failure path -------------------------------------------------------------


@pytest.mark.parametrize("job_type", list(JOBS_BY_TYPE))
def test_submitted_job_is_claimed_executed_and_completed(client: TestClient, job_type: str) -> None:
    job_id = submit(client, job_type)

    assert work_once() is True

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "COMPLETED"
    assert (job["attempts"], job["progress"], job["worker_id"]) == (1, 100, "test-worker")
    assert job["result"] and job["last_error"] is None
    assert job["started_at"] is not None and job["completed_at"] is not None
    row = db_row(job_id)
    assert row.claim_token is None and row.lease_expires_at is None
    assert log_events(job_id) == ["created", "claimed", "completed"]


def test_result_shapes_per_type(client: TestClient) -> None:
    ids = {job_type: submit(client, job_type) for job_type in JOBS_BY_TYPE}
    while work_once():
        pass

    results = {t: client.get(f"/jobs/{i}").json()["result"] for t, i in ids.items()}
    assert results["email"]["message_id"].startswith("msg-")
    assert results["webhook"] == {"status_code": 200}
    assert results["report"] == {"report_url": f"https://reports.example.com/{ids['report']}.pdf"}
    assert results["batch"] == {"total": 3, "processed": 3}


def test_failed_attempt_is_recorded_as_failed(client: TestClient) -> None:
    job_id = submit(client, "webhook")

    assert work_once(replace(FAST, webhook_failure_rate=1.0)) is True

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "FAILED"
    assert job["attempts"] == 1
    assert job["last_error"].startswith("SimulatedFailure: ")
    assert job["result"] is None
    assert db_row(job_id).claim_token is None
    assert log_events(job_id) == ["created", "claimed", "failed"]


def test_unexpected_handler_error_fails_job_without_crashing_worker(client: TestClient) -> None:
    job_id = submit(client, "email")

    def broken_sleep(_seconds: float) -> None:
        raise RuntimeError("disk on fire")

    assert work_once(sleep=broken_sleep) is True
    job = client.get(f"/jobs/{job_id}").json()
    assert (job["status"], job["last_error"]) == ("FAILED", "RuntimeError: disk on fire")


# --- eligibility -------------------------------------------------------------------------------


def test_empty_queue_returns_false() -> None:
    assert work_once() is False


def test_ineligible_jobs_are_not_claimed(client: TestClient) -> None:
    in_an_hour = datetime.now(timezone.utc) + timedelta(hours=1)
    scheduled = submit(client, scheduled_at=in_an_hour.isoformat())
    cancelled = submit(client)
    client.post(f"/jobs/{cancelled}/cancel")
    not_yet_due = submit(client)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET next_run_at = now() + interval '1 hour' WHERE id = :id"),
            {"id": not_yet_due},
        )

    assert work_once() is False
    assert [db_row(i).status for i in (scheduled, cancelled, not_yet_due)] == [
        "SCHEDULED", "CANCELLED", "PENDING",
    ]


def test_claim_sets_token_worker_and_lease(client: TestClient) -> None:
    job_id = submit(client)

    claimed = claim("worker-a")

    assert claimed is not None and str(claimed.id) == job_id
    row = db_row(job_id)
    assert row.status == "PROCESSING"
    assert (row.claim_token, row.worker_id, row.attempts) == (claimed.claim_token, "worker-a", 1)
    with engine.connect() as conn:
        lease_left = conn.scalar(
            text("SELECT lease_expires_at - now() FROM jobs WHERE id = :id"), {"id": job_id}
        )
    assert timedelta(seconds=29) <= lease_left <= timedelta(seconds=30)
    assert log_events(job_id) == ["created", "claimed"]


# --- concurrency (EC-06, EC-05) ---------------------------------------------------------------


def test_concurrent_workers_cannot_claim_the_same_job(client: TestClient) -> None:
    """EC-06: ten workers race for one eligible job; exactly one wins."""
    job_id = submit(client)
    barrier = threading.Barrier(THREADS)

    def race(index: int) -> ClaimedJob | None:
        barrier.wait()
        return claim(f"worker-{index}")

    with ThreadPoolExecutor(THREADS) as pool:
        results = list(pool.map(race, range(THREADS)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert str(winners[0].id) == job_id
    row = db_row(job_id)
    assert (row.attempts, row.claim_token) == (1, winners[0].claim_token)


def test_concurrent_workers_drain_queue_without_duplicates(client: TestClient) -> None:
    """EC-06: ten workers drain 30 jobs; every job is claimed exactly once."""
    job_ids = {submit(client) for _ in range(30)}
    barrier = threading.Barrier(THREADS)

    def drain(index: int) -> list[uuid.UUID]:
        barrier.wait()
        claimed: list[uuid.UUID] = []
        while (job := claim(f"worker-{index}")) is not None:
            claimed.append(job.id)
        return claimed

    with ThreadPoolExecutor(THREADS) as pool:
        per_worker = list(pool.map(drain, range(THREADS)))

    all_claims = [str(job_id) for claims in per_worker for job_id in claims]
    assert len(all_claims) == 30
    assert set(all_claims) == job_ids
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM jobs WHERE status='PROCESSING' AND attempts=1")) == 30


def test_cancel_and_claim_race_has_exactly_one_winner(client: TestClient) -> None:
    """EC-05: a cancel and a claim racing on one PENDING job never both succeed."""
    cancel_wins = claim_wins = 0
    for _ in range(20):
        job_id = uuid.UUID(submit(client))
        barrier = threading.Barrier(2)

        def cancel() -> bool:
            barrier.wait()
            with SessionLocal() as session:
                try:
                    return jobs.cancel_job(session, job_id).status == "CANCELLED"
                except jobs.JobStateConflictError:
                    return False

        def take() -> bool:
            barrier.wait()
            return claim() is not None

        with ThreadPoolExecutor(2) as pool:
            cancelled_future, claimed_future = pool.submit(cancel), pool.submit(take)
            cancelled, claimed = cancelled_future.result(), claimed_future.result()

        assert cancelled != claimed, "exactly one of cancel/claim must win"
        assert db_row(job_id).status == ("CANCELLED" if cancelled else "PROCESSING")
        cancel_wins += cancelled
        claim_wins += claimed
    assert cancel_wins + claim_wins == 20


# --- fencing and lost ownership -----------------------------------------------------------------


def test_writes_with_wrong_token_are_rejected(client: TestClient) -> None:
    job_id = submit(client)
    claimed = claim()
    assert claimed is not None
    wrong = uuid.uuid4()

    with SessionLocal() as session:
        assert jobs.update_progress(session, claimed.id, wrong, 50) is False
        assert jobs.complete_job(session, claimed.id, wrong, {"x": 1}) is False
        assert jobs.record_failure(session, claimed.id, wrong, "boom") is False
    row = db_row(job_id)
    assert (row.status, row.progress, row.result, row.last_error) == ("PROCESSING", 0, None, None)

    with SessionLocal() as session:
        assert jobs.update_progress(session, claimed.id, claimed.claim_token, 50) is True
    assert db_row(job_id).progress == 50


def test_worker_that_lost_ownership_does_not_complete_job(client: TestClient) -> None:
    job_id = submit(client, "email")

    assert work_once(sleep=lambda _s: steal_job(job_id)) is True

    row = db_row(job_id)
    assert (row.status, row.result, row.completed_at) == ("PROCESSING", None, None)
    assert log_events(job_id) == ["created", "claimed"]


def test_batch_stops_writing_progress_after_losing_ownership(client: TestClient) -> None:
    job_id = submit(client, "batch")
    calls = 0

    def steal_on_second_item(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            steal_job(job_id)

    assert work_once(sleep=steal_on_second_item) is True

    row = db_row(job_id)
    assert (row.status, row.progress, row.result) == ("PROCESSING", 33, None)
    assert calls == 2  # the handler stopped instead of processing the third item


def test_database_error_during_progress_is_not_recorded_as_job_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An infrastructure error propagates to the worker loop; the job is left for lease recovery."""
    job_id = submit(client, "batch")

    def database_down(*_args: Any) -> bool:
        raise OperationalError("UPDATE jobs ...", {}, Exception("connection lost"))

    monkeypatch.setattr(jobs, "update_progress", database_down)
    with pytest.raises(OperationalError):
        work_once()

    row = db_row(job_id)
    assert (row.status, row.last_error) == ("PROCESSING", None)
    assert log_events(job_id) == ["created", "claimed"]


# --- batch progress persisted (EC-16) -----------------------------------------------------------


def test_batch_progress_is_persisted_monotonically(client: TestClient) -> None:
    job_id = submit(client, "batch", payload={"items": list(range(5))})
    observed: list[int] = []

    def observe(_seconds: float) -> None:
        observed.append(db_row(job_id).progress)

    work_once(sleep=observe)

    assert observed == [0, 20, 40, 60, 80]  # read before each item is processed
    job = client.get(f"/jobs/{job_id}").json()
    assert (job["status"], job["progress"], job["result"]) == (
        "COMPLETED", 100, {"total": 5, "processed": 5},
    )


# --- Redis wake-ups and polling fallback (EC-14) ------------------------------------------------


def test_job_submitted_while_redis_is_down_is_processed_by_polling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EC-14: no notification is ever delivered, yet the worker loop completes the job."""
    monkeypatch.setattr(
        notifier, "_client", redis.Redis.from_url(UNREACHABLE_REDIS, socket_connect_timeout=0.2)
    )
    # run() uses real time.sleep; a batch with zero item delay completes without waiting.
    job_id = submit(client, "batch")
    config = replace(FAST, redis_url=UNREACHABLE_REDIS, worker_poll_interval_seconds=0.1)
    stop = threading.Event()
    worker = threading.Thread(target=run, args=(stop, config))
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and db_row(job_id).status != "COMPLETED":
            time.sleep(0.1)
    finally:
        stop.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert db_row(job_id).status == "COMPLETED"


def test_wakeup_returns_early_on_notification() -> None:
    stop = threading.Event()
    wakeup = Wakeup(settings.redis_url, settings.redis_timeout_seconds, stop)
    try:
        wakeup.wait(0.05)  # subscribes
        notifier.notify_job_created(uuid.uuid4())
        started = time.monotonic()
        wakeup.wait(5)
        assert time.monotonic() - started < 1
    finally:
        wakeup.close()


def test_wakeup_drains_stale_hints_so_a_burst_wakes_only_once() -> None:
    stop = threading.Event()
    wakeup = Wakeup(settings.redis_url, settings.redis_timeout_seconds, stop)
    try:
        wakeup.wait(0.05)  # subscribes
        for _ in range(5):
            notifier.notify_job_created(uuid.uuid4())
        time.sleep(0.2)  # let all five hints arrive in the subscriber's buffer
        wakeup.wait(5)  # returns at once on the first hint and drains the rest

        started = time.monotonic()
        wakeup.wait(0.3)
        assert time.monotonic() - started >= 0.25  # no stale hint left to wake on
    finally:
        wakeup.close()


def test_wakeup_falls_back_to_timed_wait_when_redis_is_down() -> None:
    stop = threading.Event()
    wakeup = Wakeup(UNREACHABLE_REDIS, 0.2, stop)

    started = time.monotonic()
    wakeup.wait(0.5)
    elapsed = time.monotonic() - started

    assert 0.4 <= elapsed < 1.5


def test_run_exits_immediately_when_stop_is_already_set() -> None:
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    run(stop, FAST)
    assert time.monotonic() - started < 1


def test_run_survives_database_outage_and_keeps_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB outage is logged and retried after the poll interval; the loop neither crashes nor spins."""
    dead = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    attempts = 0
    real_factory = sessionmaker(bind=dead)

    def counting_factory():
        nonlocal attempts
        attempts += 1
        return real_factory()

    monkeypatch.setattr(worker_module, "SessionLocal", counting_factory)
    stop = threading.Event()
    loop = threading.Thread(target=run, args=(stop, replace(FAST, worker_poll_interval_seconds=0.2)))
    loop.start()
    time.sleep(1.0)
    stop.set()
    loop.join(timeout=5)
    dead.dispose()

    assert not loop.is_alive()
    assert 2 <= attempts <= 8  # retried roughly every 0.2s, not in a busy loop
