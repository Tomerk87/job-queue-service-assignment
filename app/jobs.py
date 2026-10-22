"""Job persistence and all state transitions (API side and worker side).

Every state change is a conditional statement in a short transaction; database
time (now()) is authoritative for scheduling, leases and idempotency expiry.
Worker writes are fenced by (status='PROCESSING', claim_token, unexpired lease):
a worker that no longer owns a job, or whose lease has lapsed, cannot change it.
Once a lease lapses only recovery may touch the row.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Row, case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import IdempotencyKey, Job, JobLog, JobStatus, JobType
from app.schemas import JobCreate

CANCELLABLE_STATUSES = (JobStatus.PENDING, JobStatus.SCHEDULED)
MAX_ERROR_LENGTH = 2000
RELEASE_BATCH_SIZE = 100
RECOVERY_BATCH_SIZE = 100


class InvalidScheduleError(ValueError):
    """scheduled_at is too far in the past (beyond the tolerance) or too far in the future."""


class JobNotFoundError(LookupError):
    pass


class JobStateConflictError(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(f"job is {status}")
        self.status = status


class _ActiveKeyConflict(Exception):
    """Internal: another job holds the active idempotency key; abort this transaction."""


@dataclass(frozen=True)
class CreateResult:
    job: Job
    created: bool


@dataclass(frozen=True)
class ClaimedJob:
    """Plain data handed to the worker; no ORM object outlives the claim transaction."""

    id: uuid.UUID
    type: JobType
    payload: dict[str, Any]
    claim_token: uuid.UUID
    attempts: int


def resolve_schedule(
    scheduled_at: datetime | None, db_now: datetime, tolerance: timedelta, max_ahead: timedelta
) -> tuple[JobStatus, datetime]:
    """Return the initial (status, next_run_at) for a new job. Both bounds are inclusive."""
    if scheduled_at is None:
        return JobStatus.PENDING, db_now
    scheduled_at = scheduled_at.astimezone(timezone.utc)
    if scheduled_at > db_now + max_ahead:
        raise InvalidScheduleError(f"scheduled_at is more than {max_ahead.days} days in the future")
    if scheduled_at > db_now:
        return JobStatus.SCHEDULED, scheduled_at
    if db_now - scheduled_at <= tolerance:
        return JobStatus.PENDING, db_now
    raise InvalidScheduleError(
        f"scheduled_at is more than {int(tolerance.total_seconds())} seconds in the past"
    )


def create_job(session: Session, request: JobCreate, settings: Settings) -> CreateResult:
    """Create a job, or return the job that already holds the active idempotency key.

    The key upsert below inserts a new key or takes over an *expired* one; it does
    nothing when the key is active. PostgreSQL locks the conflicting key row, so
    concurrent submissions with the same key serialize and exactly one creates a job.
    """
    if request.idempotency_key is None:
        return _insert_job(session, request, settings)

    # Bounded: a second pass is needed only if the winner's key expired between
    # our failed upsert and our read, which requires a TTL shorter than one request.
    for _ in range(2):
        try:
            return _insert_job(session, request, settings)
        except _ActiveKeyConflict:
            pass  # the transaction was rolled back; our new job row is discarded
        with session.begin():
            existing = session.scalar(
                select(Job)
                .join(IdempotencyKey, IdempotencyKey.job_id == Job.id)
                .where(
                    IdempotencyKey.key == request.idempotency_key,
                    IdempotencyKey.expires_at > func.now(),
                )
            )
        if existing is not None:
            return CreateResult(job=existing, created=False)
    raise RuntimeError("idempotency key changed concurrently; retry the request")


def _insert_job(session: Session, request: JobCreate, settings: Settings) -> CreateResult:
    with session.begin():
        db_now: datetime = session.scalar(select(func.now()))
        status, next_run_at = resolve_schedule(
            request.scheduled_at, db_now, settings.schedule_past_tolerance, settings.schedule_max_ahead
        )
        job = Job(
            id=uuid.uuid4(),
            type=request.type,
            payload=request.payload,
            status=status,
            priority=request.priority,
            scheduled_at=request.scheduled_at,
            next_run_at=next_run_at,
            idempotency_key=request.idempotency_key,
            created_at=db_now,
            updated_at=db_now,
        )
        session.add(job)
        session.flush()

        if request.idempotency_key is not None:
            upsert = insert(IdempotencyKey).values(
                key=request.idempotency_key,
                job_id=job.id,
                expires_at=func.now() + settings.idempotency_ttl,
            )
            upsert = upsert.on_conflict_do_update(
                index_elements=[IdempotencyKey.key],
                set_={"job_id": upsert.excluded.job_id, "expires_at": upsert.excluded.expires_at},
                where=IdempotencyKey.expires_at <= func.now(),
            ).returning(IdempotencyKey.job_id)
            if session.execute(upsert).scalar_one_or_none() is None:
                raise _ActiveKeyConflict

        session.add(
            JobLog(
                job_id=job.id,
                level="info",
                event="created",
                message="job created",
                metadata_={"status": status, "priority": request.priority},
            )
        )
    return CreateResult(job=job, created=True)


def get_job(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    return job


def list_jobs(
    session: Session,
    status: JobStatus | None,
    job_type: JobType | None,
    limit: int,
    offset: int,
) -> list[Job]:
    query = select(Job)
    if status is not None:
        query = query.where(Job.status == status)
    if job_type is not None:
        query = query.where(Job.type == job_type)
    query = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit).offset(offset)
    return list(session.scalars(query))


def cancel_job(session: Session, job_id: uuid.UUID) -> Job:
    """Cancel a PENDING/SCHEDULED job atomically; cancelling twice is a no-op.

    The conditional UPDATE is the single arbiter against a concurrent worker claim:
    whichever statement commits first changes the status, the other matches no row.
    """
    return _transition(
        session,
        job_id,
        from_statuses=CANCELLABLE_STATUSES,
        values={"status": JobStatus.CANCELLED},
        event="cancelled",
        message="job cancelled",
        already_done=JobStatus.CANCELLED,
    )


def retry_job(session: Session, job_id: uuid.UUID) -> Job:
    """Manually re-open a FAILED job as a new cycle; earlier attempts stay in job_logs."""
    return _transition(
        session,
        job_id,
        from_statuses=(JobStatus.FAILED,),
        values={
            "status": JobStatus.PENDING,
            "attempts": 0,
            "next_run_at": func.now(),
            "last_error": None,
            "result": None,
            "progress": 0,
            "started_at": None,
            "completed_at": None,
            "worker_id": None,
        },
        event="manual_retry",
        message="manual retry: attempts reset",
    )


def _transition(
    session: Session,
    job_id: uuid.UUID,
    from_statuses: tuple[JobStatus, ...],
    values: dict[str, Any],
    event: str,
    message: str,
    already_done: JobStatus | None = None,
) -> Job:
    """Conditionally move a job out of `from_statuses`; 404/409 semantics when it can't.

    A job already in `already_done` is returned unchanged (idempotent repeat).
    """
    with session.begin():
        job = session.scalars(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(from_statuses))
            .values(**values, updated_at=func.now())
            .returning(Job),
            execution_options={"populate_existing": True},
        ).one_or_none()
        if job is not None:
            session.add(JobLog(job_id=job.id, level="info", event=event, message=message))
            return job

        job = session.get(Job, job_id, populate_existing=True)
        if job is None:
            raise JobNotFoundError(str(job_id))
        if job.status == already_done:
            return job
        raise JobStateConflictError(job.status)


def count_by_status(session: Session) -> dict[JobStatus, int]:
    counts = {status: 0 for status in JobStatus}
    for status, count in session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ):
        counts[JobStatus(status)] = count
    return counts


# --- worker side --------------------------------------------------------------------------


def release_due_jobs(session: Session, limit: int = RELEASE_BATCH_SIZE) -> int:
    """Move SCHEDULED jobs whose time has come to PENDING; returns how many were released.

    Run by every worker before it claims. SKIP LOCKED lets concurrent workers release
    disjoint rows, so each job is released exactly once and never before scheduled_at.
    next_run_at already equals scheduled_at, so the job is immediately claimable.
    """
    due = (
        select(Job.id)
        .where(Job.status == JobStatus.SCHEDULED, Job.scheduled_at <= func.now())
        .order_by(Job.scheduled_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    with session.begin():
        released = session.scalars(
            update(Job)
            .where(Job.id.in_(due), Job.status == JobStatus.SCHEDULED)
            .values(status=JobStatus.PENDING, updated_at=func.now())
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        ).all()
        session.add_all(
            JobLog(job_id=job_id, level="info", event="released", message="scheduled time reached")
            for job_id in released
        )
    return len(released)


def claim_next_job(session: Session, worker_id: str, lease: timedelta) -> ClaimedJob | None:
    """Atomically claim the most urgent eligible job, or return None if there is none.

    FOR UPDATE SKIP LOCKED makes concurrent workers pass over rows another
    transaction has locked, so each eligible row is claimed by at most one worker.
    A fresh claim_token fences every later write of this attempt.
    """
    token = uuid.uuid4()
    next_job_id = (
        select(Job.id)
        .where(Job.status == JobStatus.PENDING, Job.next_run_at <= func.now())
        .order_by(Job.priority.desc(), Job.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    with session.begin():
        row = session.execute(
            update(Job)
            .where(Job.id == next_job_id)
            .values(
                status=JobStatus.PROCESSING,
                attempts=Job.attempts + 1,
                claim_token=token,
                worker_id=worker_id,
                started_at=func.now(),
                lease_expires_at=func.now() + lease,
                progress=0,
                updated_at=func.now(),
            )
            .returning(Job.id, Job.type, Job.payload, Job.attempts)
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if row is None:
            return None
        session.add(
            JobLog(
                job_id=row.id,
                level="info",
                event="claimed",
                message="job claimed",
                metadata_={"worker_id": worker_id, "attempt": row.attempts},
            )
        )
    return ClaimedJob(
        id=row.id,
        type=JobType(row.type),
        payload=row.payload,
        claim_token=token,
        attempts=row.attempts,
    )


def update_progress(session: Session, job_id: uuid.UUID, token: uuid.UUID, progress: int) -> bool:
    return _fenced_update(session, job_id, token, {"progress": progress}) is not None


def renew_lease(session: Session, job_id: uuid.UUID, token: uuid.UUID, lease: timedelta) -> bool:
    """Heartbeat: extend a live lease. False means ownership is gone; stop working."""
    values = {"lease_expires_at": func.now() + lease}
    return _fenced_update(session, job_id, token, values) is not None


def recover_expired_leases(
    session: Session, retry_delays: tuple[timedelta, ...], limit: int = RECOVERY_BATCH_SIZE
) -> int:
    """Treat jobs whose lease expired (crashed, killed or hung worker) as a failed attempt.

    Applies the same rule as a handler error (after_failed_attempt), so attempts and
    backoff are counted identically. A live heartbeat needs lease_expires_at > now()
    and this needs lease_expires_at < now(); the row lock serializes the two, so a job
    is either renewed or recovered, never both. Clearing claim_token fences the old worker.
    """
    expired = (
        select(Job.id)
        .where(Job.status == JobStatus.PROCESSING, Job.lease_expires_at < func.now())
        .order_by(Job.lease_expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    with session.begin():
        rows = session.execute(
            update(Job)
            .where(
                Job.id.in_(expired),
                Job.status == JobStatus.PROCESSING,
                Job.lease_expires_at < func.now(),
            )
            .values(
                **after_failed_attempt(retry_delays),
                last_error=func.concat("lease expired (worker ", Job.worker_id, ")"),
                updated_at=func.now(),
            )
            .returning(Job.id, Job.status, Job.attempts, Job.next_run_at)
            .execution_options(synchronize_session=False)
        ).all()
        for row in rows:
            session.add(
                JobLog(
                    job_id=row.id,
                    level="warning",
                    event="lease_expired",
                    message="lease expired; attempt counted as failed",
                    metadata_={"attempt": row.attempts},
                )
            )
            session.add(_failure_log(row))
    return len(rows)


def complete_job(
    session: Session, job_id: uuid.UUID, token: uuid.UUID, result: dict[str, Any]
) -> bool:
    row = _fenced_update(
        session,
        job_id,
        token,
        {
            "status": JobStatus.COMPLETED,
            "result": result,
            "last_error": None,
            "progress": 100,
            "completed_at": func.now(),
            "claim_token": None,
            "lease_expires_at": None,
        },
        log=lambda row: JobLog(job_id=row.id, level="info", event="completed", message="job completed"),
    )
    return row is not None


def after_failed_attempt(retry_delays: tuple[timedelta, ...]) -> dict[str, Any]:
    """The single retry rule for a failed attempt (handler error or expired lease).

    attempts was incremented at claim time. While attempts < max_attempts the job goes
    back to PENDING, eligible again after retry_delays[attempts - 1] (30s, then 120s);
    after the last attempt it is FAILED and next_run_at is left as it was.
    """
    return {
        "status": case(
            (Job.attempts < Job.max_attempts, JobStatus.PENDING), else_=JobStatus.FAILED
        ),
        "next_run_at": case(
            {attempt: func.now() + delay for attempt, delay in enumerate(retry_delays, start=1)},
            value=Job.attempts,
            else_=Job.next_run_at,
        ),
        "claim_token": None,
        "lease_expires_at": None,
    }


def record_failure(
    session: Session,
    job_id: uuid.UUID,
    token: uuid.UUID,
    error: str,
    retry_delays: tuple[timedelta, ...],
) -> Row[Any] | None:
    """Record a failed attempt; returns (id, status, attempts, next_run_at) or None if not owned."""
    return _fenced_update(
        session,
        job_id,
        token,
        after_failed_attempt(retry_delays) | {"last_error": error[:MAX_ERROR_LENGTH]},
        log=_failure_log,
    )


def _failure_log(row: Row[Any]) -> JobLog:
    if row.status == JobStatus.FAILED:
        return JobLog(
            job_id=row.id,
            level="error",
            event="failed",
            message="job failed permanently",
            metadata_={"attempt": row.attempts},
        )
    return JobLog(
        job_id=row.id,
        level="warning",
        event="retry_scheduled",
        message="attempt failed; retry scheduled",
        metadata_={"attempt": row.attempts, "next_run_at": row.next_run_at.isoformat()},
    )


def _fenced_update(
    session: Session,
    job_id: uuid.UUID,
    token: uuid.UUID,
    values: dict[str, Any],
    log: Callable[[Row[Any]], JobLog] | None = None,
) -> Row[Any] | None:
    """Apply values only while the caller still owns the job and its lease is live.

    None means ownership was lost (recovered, re-claimed, or lease expired).
    """
    with session.begin():
        row = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.PROCESSING,
                Job.claim_token == token,
                Job.lease_expires_at > func.now(),
            )
            .values(**values, updated_at=func.now())
            .returning(Job.id, Job.status, Job.attempts, Job.next_run_at)
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if row is not None and log is not None:
            session.add(log(row))
    return row
