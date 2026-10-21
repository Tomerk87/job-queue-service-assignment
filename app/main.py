"""FastAPI application: HTTP routes only. Jobs are never executed in a request."""

import logging
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import jobs, notifier
from app.config import settings
from app.db import get_session
from app.logging_config import configure_logging
from app.models import Job, JobStatus, JobType
from app.schemas import HealthOut, JobCreate, JobList, JobOut

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Job Queue Service")

SessionDep = Annotated[Session, Depends(get_session)]


@app.exception_handler(OperationalError)
def database_unavailable(_request: Request, _exc: OperationalError) -> JSONResponse:
    """PostgreSQL unreachable (or connection lost mid-transaction): nothing was committed.

    503 tells clients to retry; with an idempotency key a retry can never create a duplicate.
    """
    logger.error("database unavailable", extra={"event": "db_unavailable"}, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "database unavailable"}
    )


@app.post(
    "/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": JobOut, "description": "Existing job for an active idempotency key"}},
)
def submit_job(request: JobCreate, session: SessionDep, response: Response) -> Job:
    try:
        result = jobs.create_job(session, request, settings)
    except jobs.InvalidScheduleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if result.created:
        notifier.notify_job_created(result.job.id)
    else:
        response.status_code = status.HTTP_200_OK
    return result.job


@app.get("/jobs/{job_id}", response_model=JobOut)
def read_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    try:
        return jobs.get_job(session, job_id)
    except jobs.JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found") from exc


@app.get("/jobs", response_model=JobList)
def list_jobs(
    session: SessionDep,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    job_type: Annotated[JobType | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    items = jobs.list_jobs(session, job_status, job_type, limit, offset)
    return JobList(items=[JobOut.model_validate(job) for job in items], limit=limit, offset=offset)


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    try:
        return jobs.cancel_job(session, job_id)
    except jobs.JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found") from exc
    except jobs.JobStateConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"cannot cancel a job in status {exc.status}"
        ) from exc


@app.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    """Re-open a FAILED job as a new cycle of attempts."""
    try:
        job = jobs.retry_job(session, job_id)
    except jobs.JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found") from exc
    except jobs.JobStateConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"only FAILED jobs can be retried; job is {exc.status}"
        ) from exc
    notifier.notify_job_created(job.id)
    return job


@app.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
def health(session: SessionDep, response: Response) -> HealthOut:
    redis_state = "ok" if notifier.redis_ok() else "down"
    try:
        counts = jobs.count_by_status(session)
    except SQLAlchemyError:
        logger.exception("health check: database unavailable", extra={"event": "health_db_down"})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthOut(status="unhealthy", database="down", redis=redis_state, jobs=None)
    overall = "ok" if redis_state == "ok" else "degraded"
    return HealthOut(status=overall, database="ok", redis=redis_state, jobs=counts)
