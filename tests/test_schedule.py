"""Unit tests for initial scheduling rules and configuration bounds (EC-01)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.jobs import ScheduleTooOldError, resolve_schedule
from app.models import JobStatus

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
TOLERANCE = timedelta(seconds=30)


def test_missing_scheduled_at_is_pending_now() -> None:
    assert resolve_schedule(None, NOW, TOLERANCE) == (JobStatus.PENDING, NOW)


def test_future_scheduled_at_is_scheduled_for_that_time() -> None:
    future = NOW + timedelta(microseconds=1)
    assert resolve_schedule(future, NOW, TOLERANCE) == (JobStatus.SCHEDULED, future)


def test_scheduled_at_equal_to_now_is_pending() -> None:
    assert resolve_schedule(NOW, NOW, TOLERANCE) == (JobStatus.PENDING, NOW)


def test_exactly_tolerance_in_past_is_accepted_as_pending() -> None:
    assert resolve_schedule(NOW - TOLERANCE, NOW, TOLERANCE) == (JobStatus.PENDING, NOW)


def test_just_beyond_tolerance_is_rejected() -> None:
    with pytest.raises(ScheduleTooOldError):
        resolve_schedule(NOW - TOLERANCE - timedelta(microseconds=1), NOW, TOLERANCE)


def test_far_past_is_rejected() -> None:
    with pytest.raises(ScheduleTooOldError):
        resolve_schedule(datetime(2000, 1, 1, tzinfo=timezone.utc), NOW, TOLERANCE)


def test_zero_tolerance_accepts_now_and_rejects_any_past() -> None:
    assert resolve_schedule(NOW, NOW, timedelta(0))[0] == JobStatus.PENDING
    with pytest.raises(ScheduleTooOldError):
        resolve_schedule(NOW - timedelta(microseconds=1), NOW, timedelta(0))


def test_offset_timestamp_is_normalized_to_utc() -> None:
    plus_three = timezone(timedelta(hours=3))
    scheduled = datetime(2026, 10, 21, 18, 0, 0, tzinfo=plus_three)  # 15:00 UTC

    status, next_run_at = resolve_schedule(scheduled, NOW, TOLERANCE)

    assert status == JobStatus.SCHEDULED
    assert next_run_at.utcoffset() == timedelta(0)
    assert next_run_at == datetime(2026, 10, 21, 15, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://u:p@h/db_test",
        "redis_url": "redis://h:6379/0",
        "idempotency_ttl_hours": 24,
        "schedule_past_tolerance_seconds": 30,
        "redis_timeout_seconds": 0.5,
    }
    return Settings(**(values | overrides))  # type: ignore[arg-type]


def test_settings_reject_idempotency_ttl_below_24_hours() -> None:
    with pytest.raises(ValueError):
        _settings(idempotency_ttl_hours=23)


def test_settings_reject_negative_schedule_tolerance() -> None:
    with pytest.raises(ValueError):
        _settings(schedule_past_tolerance_seconds=-1)
