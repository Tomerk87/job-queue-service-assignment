"""Lease recovery, fencing and heartbeat against real PostgreSQL (EC-11, EC-12, EC-13)."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import jobs
from app import worker as worker_module
from app.config import settings
from app.db import SessionLocal, engine
from app.worker import Heartbeat
from tests.helpers import (
    FAST,
    claim,
    db_row,
    expire_lease,
    fast_forward,
    log_events,
    recover,
    submit,
    time_until_next_run,
)

THREADS = 10


def claimed_job(client: TestClient) -> tuple[str, jobs.ClaimedJob]:
    job_id = submit(client)
    claimed = claim()
    assert claimed is not None and str(claimed.id) == job_id
    return job_id, claimed


# --- recovery (EC-11) ----------------------------------------------------------------------------


def test_expired_lease_after_first_attempt_is_retried_after_30s(client: TestClient) -> None:
    job_id, _ = claimed_job(client)
    expire_lease(job_id)

    assert recover() == 1

    row = db_row(job_id)
    assert (row.status, row.attempts, row.claim_token, row.lease_expires_at) == (
        "PENDING", 1, None, None,
    )
    assert row.last_error == "lease expired (worker test-worker)"
    remaining = time_until_next_run(job_id)
    assert timedelta(seconds=28) <= remaining <= timedelta(seconds=30)
    assert log_events(job_id) == ["created", "claimed", "lease_expired", "retry_scheduled"]


def test_expired_lease_on_last_attempt_fails_the_job(client: TestClient) -> None:
    job_id = submit(client)
    for _ in range(3):
        fast_forward(job_id)
        assert claim() is not None
        expire_lease(job_id)
        recover()

    row = db_row(job_id)
    assert (row.status, row.attempts) == ("FAILED", 3)
    assert log_events(job_id)[-2:] == ["lease_expired", "failed"]


def test_live_lease_is_never_recovered(client: TestClient) -> None:
    job_id, claimed = claimed_job(client)

    assert recover() == 0
    row = db_row(job_id)
    assert (row.status, row.claim_token) == ("PROCESSING", claimed.claim_token)


def test_concurrent_recovery_recovers_each_job_once(client: TestClient) -> None:
    job_ids = [submit(client) for _ in range(20)]
    while claim() is not None:
        pass
    for job_id in job_ids:
        expire_lease(job_id)
    barrier = threading.Barrier(THREADS)

    def race(_: int) -> int:
        barrier.wait()
        return recover()

    with ThreadPoolExecutor(THREADS) as pool:
        assert sum(pool.map(race, range(THREADS))) == 20
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM job_logs WHERE event = 'lease_expired'")) == 20


def test_recovery_is_bounded_per_call(client: TestClient) -> None:
    job_ids = [submit(client) for _ in range(5)]
    while claim() is not None:
        pass
    for job_id in job_ids:
        expire_lease(job_id)

    with SessionLocal() as session:
        counts = [jobs.recover_expired_leases(session, settings.retry_delays, limit=2) for _ in range(4)]
    assert counts == [2, 2, 1, 0]


# --- fencing (EC-12) ------------------------------------------------------------------------------


def test_stale_worker_cannot_write_after_recovery_and_reclaim(client: TestClient) -> None:
    job_id, stale = claimed_job(client)
    expire_lease(job_id)
    recover()
    fast_forward(job_id)
    fresh = claim("worker-b")
    assert fresh is not None and fresh.claim_token != stale.claim_token

    with SessionLocal() as session:
        assert jobs.update_progress(session, stale.id, stale.claim_token, 50) is False
        assert jobs.renew_lease(session, stale.id, stale.claim_token, settings.lease) is False
        assert jobs.complete_job(session, stale.id, stale.claim_token, {"stale": True}) is False
        assert jobs.record_failure(session, stale.id, stale.claim_token, "x", settings.retry_delays) is None
    row = db_row(job_id)
    assert (row.status, row.claim_token, row.worker_id, row.result) == (
        "PROCESSING", fresh.claim_token, "worker-b", None,
    )

    with SessionLocal() as session:
        assert jobs.complete_job(session, fresh.id, fresh.claim_token, {"ok": True}) is True
    assert db_row(job_id).status == "COMPLETED"


def test_lapsed_lease_rejects_writes_even_before_recovery(client: TestClient) -> None:
    """Lease is authority: the token alone is not enough once the lease has expired."""
    job_id, claimed = claimed_job(client)
    expire_lease(job_id)

    with SessionLocal() as session:
        assert jobs.complete_job(session, claimed.id, claimed.claim_token, {"late": True}) is False
        assert jobs.update_progress(session, claimed.id, claimed.claim_token, 10) is False
        assert jobs.renew_lease(session, claimed.id, claimed.claim_token, settings.lease) is False
    assert db_row(job_id).status == "PROCESSING"
    assert recover() == 1


# --- heartbeat (EC-13) ----------------------------------------------------------------------------


def test_renew_lease_extends_a_live_lease(client: TestClient) -> None:
    job_id, claimed = claimed_job(client)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() + interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )

    with SessionLocal() as session:
        assert jobs.renew_lease(session, claimed.id, claimed.claim_token, timedelta(seconds=30))

    with engine.connect() as conn:
        left = conn.scalar(text("SELECT lease_expires_at - now() FROM jobs WHERE id = :id"), {"id": job_id})
    assert left > timedelta(seconds=28)


def test_live_heartbeat_prevents_recovery(client: TestClient) -> None:
    job_id = submit(client)
    config = replace(FAST, lease_seconds=1.0, heartbeat_interval_seconds=0.2)
    with SessionLocal() as session:
        claimed = jobs.claim_next_job(session, config.worker_id, config.lease)
    assert claimed is not None and str(claimed.id) == job_id
    heartbeat = Heartbeat(claimed, config, deadline=time.monotonic() + 60)
    heartbeat.start()
    try:
        recovered = 0
        stop_at = time.monotonic() + 3  # three full lease lengths
        while time.monotonic() < stop_at:
            recovered += recover()
            time.sleep(0.1)
    finally:
        heartbeat.done.set()
        heartbeat.join()

    assert recovered == 0
    assert not heartbeat.lost.is_set()
    with SessionLocal() as session:
        assert jobs.complete_job(session, claimed.id, claimed.claim_token, {"ok": True})


def test_heartbeat_stops_renewing_after_deadline(client: TestClient) -> None:
    """Past the job deadline the lease is no longer extended, so recovery can take over.

    The heartbeat is stopped well before deadline + lease, where it would exit the process.
    """
    job_id = submit(client)
    config = replace(FAST, lease_seconds=3.0, heartbeat_interval_seconds=0.1)
    with SessionLocal() as session:
        claimed = jobs.claim_next_job(session, config.worker_id, config.lease)
    assert claimed is not None
    heartbeat = Heartbeat(claimed, config, deadline=time.monotonic() + 0.3)
    heartbeat.start()
    try:
        time.sleep(0.6)
        lease_after_deadline = db_row(job_id).lease_expires_at
        time.sleep(0.6)
        assert db_row(job_id).lease_expires_at == lease_after_deadline  # no renewal since
    finally:
        heartbeat.done.set()
        heartbeat.join()
    assert not heartbeat.lost.is_set()


def test_heartbeat_marks_lost_when_renewal_is_rejected(client: TestClient) -> None:
    job_id = submit(client)
    config = replace(FAST, lease_seconds=1.0, heartbeat_interval_seconds=0.1)
    with SessionLocal() as session:
        claimed = jobs.claim_next_job(session, config.worker_id, config.lease)
    assert claimed is not None
    with engine.begin() as conn:  # another worker took over
        conn.execute(
            text("UPDATE jobs SET claim_token = :t WHERE id = :id"),
            {"t": str(uuid.uuid4()), "id": job_id},
        )
    heartbeat = Heartbeat(claimed, config, deadline=time.monotonic() + 60)
    heartbeat.start()
    try:
        assert heartbeat.lost.wait(2)
    finally:
        heartbeat.done.set()
        heartbeat.join()


def test_renewal_and_recovery_race_on_expired_lease(client: TestClient) -> None:
    """An expired lease can only be recovered, never renewed; exactly one outcome applies."""
    for _ in range(10):
        job_id, claimed = claimed_job(client)
        expire_lease(job_id)
        barrier = threading.Barrier(2)

        def renew() -> bool:
            barrier.wait()
            with SessionLocal() as session:
                return jobs.renew_lease(session, claimed.id, claimed.claim_token, settings.lease)

        def reap() -> int:
            barrier.wait()
            return recover()

        with ThreadPoolExecutor(2) as pool:
            renewed, recovered = pool.submit(renew), pool.submit(reap)
            assert (renewed.result(), recovered.result()) == (False, 1)
        assert db_row(job_id).status == "PENDING"
        with engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET status = 'CANCELLED' WHERE id = :id"), {"id": job_id})


def test_heartbeat_marks_lost_after_a_lease_of_failed_renewals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §7: if the heartbeat keeps failing, the worker must stop and not write completion."""
    job_id = submit(client)
    config = replace(FAST, lease_seconds=1.0, heartbeat_interval_seconds=0.1)
    with SessionLocal() as session:
        claimed = jobs.claim_next_job(session, config.worker_id, config.lease)
    assert claimed is not None and str(claimed.id) == job_id
    dead = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    monkeypatch.setattr(worker_module, "SessionLocal", sessionmaker(bind=dead))
    heartbeat = Heartbeat(claimed, config, deadline=time.monotonic() + 60)
    started = time.monotonic()
    heartbeat.start()
    try:
        assert heartbeat.lost.wait(3)
        assert time.monotonic() - started >= 1.0  # only after a full lease without renewal
    finally:
        heartbeat.done.set()
        heartbeat.join()
        dead.dispose()
