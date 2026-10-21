"""Job persistence and all state transitions (API side and worker side).

Every state change is a conditional statement in a short transaction; database
time (now()) is authoritative for scheduling, leases and idempotency expiry.
Worker writes are fenced by (status='PROCESSING', claim_token) so a worker that
no longer owns a job cannot change it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import IdempotencyKey, Job, JobLog, JobStatus, JobType
from app.schemas import JobCreate

CANCELLABLE_STATUSES = (JobStatus.PENDING, JobStatus.SCHEDULED)
MAX_ERROR_LENGTH = 2000


class ScheduleTooOldError(ValueError):
    """scheduled_at lies further in the past than the configured tolerance."""


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
    scheduled_at: datetime | None, db_now: datetime, tolerance: timedelta
) -> tuple[JobStatus, datetime]:
    """Return the initial (status, next_run_at) for a new job."""
    if scheduled_at is None:
        return JobStatus.PENDING, db_now
    scheduled_at = scheduled_at.astimezone(timezone.utc)
    if scheduled_at > db_now:
        return JobStatus.SCHEDULED, scheduled_at
    if db_now - scheduled_at <= tolerance:
        return JobStatus.PENDING, db_now
    raise ScheduleTooOldError(
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
            request.scheduled_at, db_now, settings.schedule_past_tolerance
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
    with session.begin():
        job = session.scalars(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(CANCELLABLE_STATUSES))
            .values(status=JobStatus.CANCELLED, updated_at=func.now())
            .returning(Job),
            execution_options={"populate_existing": True},
        ).one_or_none()
        if job is not None:
            session.add(
                JobLog(job_id=job.id, level="info", event="cancelled", message="job cancelled")
            )
            return job

        job = session.get(Job, job_id, populate_existing=True)
        if job is None:
            raise JobNotFoundError(str(job_id))
        if job.status == JobStatus.CANCELLED:
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
    return _fenced_update(session, job_id, token, {"progress": progress})


def complete_job(
    session: Session, job_id: uuid.UUID, token: uuid.UUID, result: dict[str, Any]
) -> bool:
    return _fenced_update(
        session,
        job_id,
        token,
        {
            "status": JobStatus.COMPLETED,
            "result": result,
            "progress": 100,
            "completed_at": func.now(),
            "claim_token": None,
            "lease_expires_at": None,
        },
        log=("info", "completed", "job completed"),
    )


def record_failure(session: Session, job_id: uuid.UUID, token: uuid.UUID, error: str) -> bool:
    """Record a failed attempt.

    Interim rule until Phase 3: every failure is terminal (FAILED). Phase 3 replaces
    this function with the shared retry policy (retry after 30s/120s, FAILED after 3 attempts).
    """
    return _fenced_update(
        session,
        job_id,
        token,
        {
            "status": JobStatus.FAILED,
            "last_error": error[:MAX_ERROR_LENGTH],
            "claim_token": None,
            "lease_expires_at": None,
        },
        log=("error", "failed", "job failed"),
    )


def _fenced_update(
    session: Session,
    job_id: uuid.UUID,
    token: uuid.UUID,
    values: dict[str, Any],
    log: tuple[str, str, str] | None = None,
) -> bool:
    """Apply values only while the caller still owns the job; False means ownership was lost."""
    with session.begin():
        result = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.PROCESSING,
                Job.claim_token == token,
            )
            .values(**values, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        if log is not None:
            level, event, message = log
            session.add(JobLog(job_id=job_id, level=level, event=event, message=message))
    return True
