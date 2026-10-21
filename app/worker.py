"""Worker process: claim one job at a time from PostgreSQL and execute it.

PostgreSQL is the queue. Redis pub/sub only shortens the idle wait; when Redis is
down or a message is lost, the bounded poll interval still finds every committed job.
No database transaction or connection is held while a handler runs.
"""

import logging
import random
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType

import redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import jobs
from app.config import Settings, settings
from app.db import SessionLocal
from app.handlers import HANDLERS, HandlerContext, LostOwnership, SimulatedFailure
from app.notifier import JOB_CREATED_CHANNEL

logger = logging.getLogger("app.worker")


def run_once(
    config: Settings, rng: random.Random, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Claim and execute at most one job. Returns False when no job was eligible.

    One session serves the whole job on this thread. Each database write is its own
    short transaction, so no transaction or connection is held while the handler runs.
    """
    with SessionLocal() as session:
        claimed = jobs.claim_next_job(session, config.worker_id, config.lease)
        if claimed is None:
            return False
        logger.info(
            "job claimed (job_id=%s, type=%s, attempt=%d, worker_id=%s)",
            claimed.id, claimed.type, claimed.attempts, config.worker_id,
        )
        if not _execute(session, claimed, config, rng, sleep):
            logger.warning(
                "lost ownership; result not written (job_id=%s, worker_id=%s)",
                claimed.id, config.worker_id,
            )
    return True


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
            "job attempt failed (job_id=%s, worker_id=%s, error=%s)",
            claimed.id, config.worker_id, type(exc).__name__,
            exc_info=not isinstance(exc, SimulatedFailure),
        )
        return jobs.record_failure(
            session, claimed.id, claimed.claim_token, f"{type(exc).__name__}: {exc}"
        )
    if not jobs.complete_job(session, claimed.id, claimed.claim_token, result):
        return False
    logger.info("job completed (job_id=%s, worker_id=%s)", claimed.id, config.worker_id)
    return True


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
            logger.info("redis wake-ups active")
        else:
            logger.warning(
                "redis wake-ups unavailable (%s); relying on database polling", type(exc).__name__
            )
        self._available = available


def run(stop: threading.Event, config: Settings) -> None:
    """Process jobs until `stop` is set. The current job always finishes first."""
    wakeup = Wakeup(config.redis_url, config.redis_timeout_seconds, stop)
    rng = random.Random()
    try:
        while not stop.is_set():
            try:
                processed = run_once(config, rng)
            except SQLAlchemyError:
                logger.exception("database error in worker loop; retrying after poll interval")
                stop.wait(config.worker_poll_interval_seconds)
                continue
            if not processed:
                wakeup.wait(config.worker_poll_interval_seconds)
    finally:
        wakeup.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s; finishing current job, then stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info("worker started (worker_id=%s)", settings.worker_id)
    run(stop, settings)
    logger.info("worker stopped (worker_id=%s)", settings.worker_id)


if __name__ == "__main__":
    main()
