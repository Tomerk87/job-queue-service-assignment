"""Request/response schemas and type-specific payload validation."""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from app.models import JobStatus, JobType

MAX_BATCH_ITEMS = 100


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailPayload(_Payload):
    to: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    subject: str = Field(min_length=1, max_length=255)
    body: str | None = Field(default=None, max_length=10_000)


class WebhookPayload(_Payload):
    # Simulation only: the URL is validated and stored, never called.
    url: HttpUrl
    body: dict[str, Any] | None = None


class ReportPayload(_Payload):
    report_name: str = Field(min_length=1, max_length=255)


class BatchPayload(_Payload):
    items: list[Any] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)


PAYLOAD_MODELS: dict[JobType, type[_Payload]] = {
    JobType.EMAIL: EmailPayload,
    JobType.WEBHOOK: WebhookPayload,
    JobType.REPORT: ReportPayload,
    JobType.BATCH: BatchPayload,
}


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: JobType
    payload: dict[str, Any]
    priority: int = Field(default=0, ge=0, le=10)
    scheduled_at: AwareDatetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("scheduled_at")
    @classmethod
    def normalize_to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        try:
            payload = PAYLOAD_MODELS[self.type].model_validate(self.payload)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise ValueError(f"invalid {self.type} payload: {details}") from None
        self.payload = payload.model_dump(mode="json", exclude_none=True)
        return self


class JobOut(BaseModel):
    """Public job representation; internal lease fields such as claim_token are never exposed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: JobType
    status: JobStatus
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    progress: int
    result: dict[str, Any] | None
    last_error: str | None
    idempotency_key: str | None
    worker_id: str | None
    scheduled_at: datetime | None
    next_run_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobList(BaseModel):
    items: list[JobOut]
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    database: Literal["ok", "down"]
    redis: Literal["ok", "down"]
    jobs: dict[JobStatus, int] | None
