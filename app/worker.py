"""Worker process: claim one job at a time from PostgreSQL and execute it.

PostgreSQL is the queue. Redis pub/sub only shortens the idle wait; when Redis is
down or a message is lost, the bounded poll interval still finds every committed job.
No database transaction or connection is held while a handler runs.

Reliability: a heartbeat thread renews the job's lease while the handler runs; every
worker periodically recovers jobs whose lease expired (crashed, killed or hung workers).
"""

import logging
import os
import random
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import Any

import redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import jobs
from app.config import Settings, settings
from app.db import SessionLocal
from app.handlers import (
    HANDLERS,
    HandlerContext,
    JobTimeout,
    LostOwnership,
    SimulatedFailure,
)
from app.logging_config import configure_logging
from app.models import JobStatus
from app.notifier import JOB_CREATED_CHANNEL

logger = logging.getLogger("app.worker")

# Expected failures are logged without a traceback; anything else gets one.
EXPECTED_FAILURES = (SimulatedFailure, JobTimeout)


def _context(claimed: jobs.ClaimedJob, config: Settings, event: str, **fields: Any) -> dict[str, Any]:
    return {"event": event, "job_id": str(claimed.id), "worker_id": config.worker_id} | fields


def run_once(
    config: Settings, rng: random.Random, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Claim and execute at most one job. Returns False when no job was eligible.

    One session serves the whole job on this thread. Each database write is its own
    short transaction, so no transaction or connection is held while the handler runs.
    The heartbeat thread uses its own sessions.
    """
    with SessionLocal() as session:
        if released := jobs.release_due_jobs(session):
            logger.info(
                "released due scheduled jobs",
                extra={"event": "released", "count": released, "worker_id": config.worker_id},
            )
        claimed = jobs.claim_next_job(session, config.worker_id, config.lease)
        if claimed is None:
            return False
        logger.info(
            "job claimed",
            extra=_context(claimed, config, "claimed", type=claimed.type, attempt=claimed.attempts),
        )
        deadline = time.monotonic() + config.job_timeout_seconds
        heartbeat = Heartbeat(claimed, config, deadline)
        heartbeat.start()
        try:
            guarded = _guarded_sleep(sleep, heartbeat.lost, deadline, config.job_timeout_seconds)
            owned = _execute(session, claimed, config, rng, guarded)
        finally:
            heartbeat.done.set()
            heartbeat.join()
        if not owned:
            logger.warning(
                "lost ownership; result not written", extra=_context(claimed, config, "lost_ownership")
            )
    return True


def _guarded_sleep(
    sleep: Callable[[float], None], lost: threading.Event, deadline: float, timeout: float
) -> Callable[[float], None]:
    """Handlers wait only through this: it enforces the job timeout and stops on lost ownership."""

    def check() -> None:
        if lost.is_set():
            raise LostOwnership("lease lost")
        if time.monotonic() >= deadline:
            raise JobTimeout(f"job exceeded {timeout:g}s timeout")

    def guarded(seconds: float) -> None:
        check()
        sleep(max(0.0, min(seconds, deadline - time.monotonic())))
        check()

    return guarded


def _execute(
    session: Session,
    claimed: jobs.ClaimedJob,
    config: Settings,
    rng: random.Random,
    sleep: Callable[[float], None],
) -> bool:
    """Run the handler and record its outcome. Returns False if ownership was lost."""
    context = HandlerContext(
        job_id=claimed.id,
        report_progress=lambda progress: jobs.update_progress(
            session, claimed.id, claimed.claim_token, progress
        ),
        sleep=sleep,
        rng=rng,
        webhook_failure_rate=config.webhook_failure_rate,
        batch_item_delay=config.batch_item_delay_seconds,
    )
    try:
        result = HANDLERS[claimed.type](claimed.payload, context)
    except LostOwnership:
        return False
    except SQLAlchemyError:
        raise  # infrastructure failure, not a job failure: leave the job to lease recovery
    except Exception as exc:  # any handler error fails the attempt; never crash the worker
        logger.warning(
            "job attempt failed",
            extra=_context(claimed, config, "attempt_failed", error=type(exc).__name__),
            exc_info=not isinstance(exc, EXPECTED_FAILURES),
        )
        outcome = jobs.record_failure(
            session,
            claimed.id,
            claimed.claim_token,
            f"{type(exc).__name__}: {exc}",
            config.retry_delays,
        )
        if outcome is None:
            return False
        if outcome.status == JobStatus.FAILED:
            logger.warning(
                "job failed permanently",
                extra=_context(claimed, config, "failed", attempt=outcome.attempts),
            )
        else:
            logger.info(
                "retry scheduled",
                extra=_context(
                    claimed, config, "retry_scheduled",
                    attempt=outcome.attempts, next_run_at=outcome.next_run_at.isoformat(),
                ),
            )
        return True
    if not jobs.complete_job(session, claimed.id, claimed.claim_token, result):
        return False
    logger.info("job completed", extra=_context(claimed, config, "completed"))
    return True


class Heartbeat(threading.Thread):
    """Renews one job's lease, on its own sessions, until the handler returns.

    Renewal stops at the job deadline so the lease can lapse. A handler that is still
    running a full lease after its deadline cannot be stopped from Python, so the
    process exits; Docker restarts the worker and lease recovery requeues the job.
    """

    def __init__(self, claimed: jobs.ClaimedJob, config: Settings, deadline: float) -> None:
        super().__init__(name=f"heartbeat-{claimed.id}", daemon=True)
        self.claimed = claimed
        self.config = config
        self.deadline = deadline
        self.done = threading.Event()  # set by the worker when the handler returns
        self.lost = threading.Event()  # set when this worker no longer owns the job

    def run(self) -> None:
        last_renewed = time.monotonic()
        while not self.done.wait(self.config.heartbeat_interval_seconds):
            now = time.monotonic()
            if now >= self.deadline + self.config.lease_seconds:
                logger.critical(
                    "handler hung past its timeout; exiting so the worker restarts",
                    extra=_context(self.claimed, self.config, "handler_hung"),
                )
                logging.shutdown()
                os._exit(1)
            if now >= self.deadline or self.lost.is_set():
                continue  # stop renewing; the lease lapses and recovery takes over
            if self._renew():
                last_renewed = now
            elif now - last_renewed >= self.config.lease_seconds:
                self._mark_lost("no successful renewal within one lease")

    def _renew(self) -> bool:
        try:
            with SessionLocal() as session:
                renewed = jobs.renew_lease(
                    session, self.claimed.id, self.claimed.claim_token, self.config.lease
                )
        except SQLAlchemyError:
            logger.warning(
                "heartbeat failed; will retry",
                extra=_context(self.claimed, self.config, "heartbeat_error"),
                exc_info=True,
            )
            return False
        if not renewed:
            self._mark_lost("lease renewal rejected")
        return renewed

    def _mark_lost(self, reason: str) -> None:
        if not self.lost.is_set():
            self.lost.set()
            logger.warning(
                "lease lost: %s", reason, extra=_context(self.claimed, self.config, "lease_lost")
            )


class Wakeup:
    """Waits for a Redis 'job created' hint, falling back to a plain timed wait."""

    def __init__(self, redis_url: str, socket_timeout: float, stop: threading.Event) -> None:
        self._client = redis.Redis.from_url(
            redis_url, socket_timeout=socket_timeout, socket_connect_timeout=socket_timeout
        )
        self._stop = stop
        self._pubsub: redis.client.PubSub | None = None
        self._available: bool | None = None

    def wait(self, timeout: float) -> None:
        """Return after `timeout` seconds, or earlier if a job-created hint arrives."""
        deadline = time.monotonic() + timeout
        try:
            if self._pubsub is None:
                pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(JOB_CREATED_CHANNEL)
                self._pubsub = pubsub
            self._set_available(True)
            while (remaining := deadline - time.monotonic()) > 0 and not self._stop.is_set():
                if self._pubsub.get_message(timeout=remaining) is not None:
                    # Hints that piled up while the worker was busy are stale: one claim
                    # attempt covers them all, so drain them instead of waking once each.
                    while self._pubsub.get_message(timeout=0) is not None:
                        pass
                    return
        except redis.RedisError as exc:
            self._set_available(False, exc)
            self.close()
            self._stop.wait(max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        if self._pubsub is not None:
            try:
                self._pubsub.close()
            except redis.RedisError:
                pass  # the connection is already broken; nothing left to release
            self._pubsub = None

    def _set_available(self, available: bool, exc: Exception | None = None) -> None:
        if available == self._available:
            return
        if available:
            logger.info("redis wake-ups active", extra={"event": "redis_up"})
        else:
            logger.warning(
                "redis wake-ups unavailable; relying on database polling",
                extra={"event": "redis_down", "error": type(exc).__name__},
            )
        self._available = available


def recover_once(config: Settings) -> int:
    """Requeue (or fail) jobs whose lease expired. Safe to run on every worker."""
    with SessionLocal() as session:
        recovered = jobs.recover_expired_leases(session, config.retry_delays)
    if recovered:
        logger.warning(
            "recovered jobs with expired leases",
            extra={"event": "leases_recovered", "count": recovered, "worker_id": config.worker_id},
        )
    return recovered


def run(stop: threading.Event, config: Settings) -> None:
    """Process jobs until `stop` is set. The current job always finishes first."""
    wakeup = Wakeup(config.redis_url, config.redis_timeout_seconds, stop)
    rng = random.Random()
    next_recovery = 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= next_recovery:
                    recover_once(config)
                    next_recovery = time.monotonic() + config.recovery_interval_seconds
                processed = run_once(config, rng)
            except SQLAlchemyError:
                logger.exception(
                    "database error in worker loop; retrying after poll interval",
                    extra={"event": "db_error", "worker_id": config.worker_id},
                )
                stop.wait(config.worker_poll_interval_seconds)
                continue
            if not processed:
                wakeup.wait(config.worker_poll_interval_seconds)
    finally:
        wakeup.close()


def main() -> None:
    configure_logging()
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "stop requested; finishing current job, then stopping",
            extra={"event": "stopping", "signal": signal.Signals(signum).name,
                   "worker_id": settings.worker_id},
        )
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info("worker started", extra={"event": "started", "worker_id": settings.worker_id})
    run(stop, settings)
    logger.info("worker stopped", extra={"event": "stopped", "worker_id": settings.worker_id})


if __name__ == "__main__":
    main()
