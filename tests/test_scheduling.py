"""Priority ordering and scheduled-job release against real PostgreSQL (EC-07, EC-08)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import jobs
from app.db import SessionLocal, engine
from tests.helpers import claim, db_row, fast_forward, log_events, submit, work_once

THREADS = 10


def in_an_hour() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def make_due(*job_ids: str) -> None:
    """Move scheduled_at (and next_run_at) to now(), the inclusive release boundary."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET scheduled_at = now(), next_run_at = now() WHERE id = ANY(:ids)"),
            {"ids": list(job_ids)},
        )


def release(limit: int = jobs.RELEASE_BATCH_SIZE) -> int:
    with SessionLocal() as session:
        return jobs.release_due_jobs(session, limit)


def claim_order() -> list[str]:
    order = []
    while (job := claim()) is not None:
        order.append(str(job.id))
    return order


# --- priority (EC-07) ----------------------------------------------------------------------------


def test_higher_priority_is_claimed_first(client: TestClient) -> None:
    low = submit(client, priority=1)
    high = submit(client, priority=9)
    medium = submit(client, priority=5)

    assert claim_order() == [high, medium, low]


def test_equal_priority_is_first_in_first_out(client: TestClient) -> None:
    first, second, third = (submit(client, priority=5) for _ in range(3))

    assert claim_order() == [first, second, third]


def test_running_job_is_not_preempted_by_higher_priority(client: TestClient) -> None:
    low = submit(client, priority=0)
    running = claim()
    assert running is not None and str(running.id) == low

    high = submit(client, priority=10)

    row = db_row(low)
    assert (row.status, row.claim_token) == ("PROCESSING", running.claim_token)
    assert claim_order() == [high]


def test_high_priority_job_waiting_for_retry_does_not_block_lower_priority(
    client: TestClient,
) -> None:
    high = submit(client, priority=10)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET next_run_at = now() + interval '30 seconds' WHERE id = :id"),
            {"id": high},
        )
    low = submit(client, priority=0)

    assert claim_order() == [low]
    fast_forward(high)
    assert claim_order() == [high]


# --- scheduling (EC-08) ---------------------------------------------------------------------------


def test_future_job_is_not_released_or_claimed_early(client: TestClient) -> None:
    job_id = submit(client, scheduled_at=in_an_hour())

    assert release() == 0
    assert claim() is None
    assert work_once() is False
    assert db_row(job_id).status == "SCHEDULED"


def test_job_due_exactly_now_is_released_and_claimed(client: TestClient) -> None:
    job_id = submit(client, scheduled_at=in_an_hour())
    make_due(job_id)

    assert release() == 1
    assert db_row(job_id).status == "PENDING"
    claimed = claim()
    assert claimed is not None and str(claimed.id) == job_id
    assert log_events(job_id) == ["created", "released", "claimed"]


def test_worker_releases_and_runs_due_job(client: TestClient) -> None:
    job_id = submit(client, scheduled_at=in_an_hour())
    make_due(job_id)

    assert work_once() is True

    assert db_row(job_id).status == "COMPLETED"


def test_cancelled_scheduled_job_is_never_released(client: TestClient) -> None:
    job_id = submit(client, scheduled_at=in_an_hour())
    client.post(f"/jobs/{job_id}/cancel")
    make_due(job_id)

    assert release() == 0
    assert db_row(job_id).status == "CANCELLED"


def test_release_is_bounded_per_call(client: TestClient) -> None:
    job_ids = [submit(client, scheduled_at=in_an_hour()) for _ in range(5)]
    make_due(*job_ids)

    assert [release(limit=2) for _ in range(4)] == [2, 2, 1, 0]


def test_concurrent_release_releases_each_job_once(client: TestClient) -> None:
    job_ids = [submit(client, scheduled_at=in_an_hour()) for _ in range(20)]
    make_due(*job_ids)
    barrier = threading.Barrier(THREADS)

    def race(_: int) -> int:
        barrier.wait()
        return release()

    with ThreadPoolExecutor(THREADS) as pool:
        released = list(pool.map(race, range(THREADS)))

    assert sum(released) == 20
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM job_logs WHERE event = 'released'")) == 20
        assert conn.scalar(text("SELECT count(*) FROM jobs WHERE status = 'PENDING'")) == 20
