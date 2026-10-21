"""Unit tests for initial scheduling rules (EC-01) and configuration bounds."""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.jobs import InvalidScheduleError, resolve_schedule
from app.models import JobStatus

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
TOLERANCE = timedelta(seconds=30)
MAX_AHEAD = timedelta(days=365)


def test_missing_scheduled_at_is_pending_now() -> None:
    assert resolve_schedule(None, NOW, TOLERANCE, MAX_AHEAD) == (JobStatus.PENDING, NOW)


def test_future_scheduled_at_is_scheduled_for_that_time() -> None:
    future = NOW + timedelta(microseconds=1)
    assert resolve_schedule(future, NOW, TOLERANCE, MAX_AHEAD) == (JobStatus.SCHEDULED, future)


def test_scheduled_at_equal_to_now_is_pending() -> None:
    assert resolve_schedule(NOW, NOW, TOLERANCE, MAX_AHEAD) == (JobStatus.PENDING, NOW)


def test_exactly_tolerance_in_past_is_accepted_as_pending() -> None:
    assert resolve_schedule(NOW - TOLERANCE, NOW, TOLERANCE, MAX_AHEAD) == (JobStatus.PENDING, NOW)


def test_just_beyond_tolerance_is_rejected() -> None:
    with pytest.raises(InvalidScheduleError):
        resolve_schedule(NOW - TOLERANCE - timedelta(microseconds=1), NOW, TOLERANCE, MAX_AHEAD)


def test_exactly_max_ahead_is_scheduled() -> None:
    limit = NOW + MAX_AHEAD
    assert resolve_schedule(limit, NOW, TOLERANCE, MAX_AHEAD) == (JobStatus.SCHEDULED, limit)


def test_beyond_max_ahead_is_rejected() -> None:
    with pytest.raises(InvalidScheduleError, match="in the future"):
        resolve_schedule(NOW + MAX_AHEAD + timedelta(microseconds=1), NOW, TOLERANCE, MAX_AHEAD)


def test_far_past_is_rejected() -> None:
    with pytest.raises(InvalidScheduleError):
        resolve_schedule(datetime(2000, 1, 1, tzinfo=timezone.utc), NOW, TOLERANCE, MAX_AHEAD)


def test_zero_tolerance_accepts_now_and_rejects_any_past() -> None:
    assert resolve_schedule(NOW, NOW, timedelta(0), MAX_AHEAD)[0] == JobStatus.PENDING
    with pytest.raises(InvalidScheduleError):
        resolve_schedule(NOW - timedelta(microseconds=1), NOW, timedelta(0), MAX_AHEAD)


def test_offset_timestamp_is_normalized_to_utc() -> None:
    plus_three = timezone(timedelta(hours=3))
    scheduled = datetime(2026, 10, 21, 18, 0, 0, tzinfo=plus_three)  # 15:00 UTC

    status, next_run_at = resolve_schedule(scheduled, NOW, TOLERANCE, MAX_AHEAD)

    assert status == JobStatus.SCHEDULED
    assert next_run_at.utcoffset() == timedelta(0)
    assert next_run_at == datetime(2026, 10, 21, 15, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://u:p@h/db_test",
        "redis_url": "redis://h:6379/0",
        "idempotency_ttl_hours": 24,
        "schedule_past_tolerance_seconds": 30,
        "schedule_max_ahead_days": 365,
        "redis_timeout_seconds": 0.5,
        "worker_id": "test-worker",
        "worker_poll_interval_seconds": 2.0,
        "lease_seconds": 30,
        "webhook_failure_rate": 0.2,
        "batch_item_delay_seconds": 0.2,
        "retry_delays_seconds": (30, 120),
    }
    return Settings(**(values | overrides))  # type: ignore[arg-type]


def test_settings_reject_idempotency_ttl_below_24_hours() -> None:
    with pytest.raises(ValueError):
        _settings(idempotency_ttl_hours=23)


def test_settings_reject_non_positive_max_ahead() -> None:
    with pytest.raises(ValueError):
        _settings(schedule_max_ahead_days=0)


def test_settings_reject_negative_schedule_tolerance() -> None:
    with pytest.raises(ValueError):
        _settings(schedule_past_tolerance_seconds=-1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"worker_poll_interval_seconds": 0},
        {"worker_poll_interval_seconds": 2.5},
        {"lease_seconds": 0},
        {"webhook_failure_rate": -0.1},
        {"webhook_failure_rate": 1.1},
        {"batch_item_delay_seconds": -1},
        {"retry_delays_seconds": (30,)},
        {"retry_delays_seconds": (30, 120, 300)},
        {"retry_delays_seconds": (-1, 120)},
    ],
    ids=[
        "poll-0", "poll-above-2", "lease-0", "rate-negative", "rate-above-1", "delay-negative",
        "retry-delays-too-few", "retry-delays-too-many", "retry-delay-negative",
    ],
)
def test_settings_reject_invalid_worker_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _settings(**overrides)


def test_settings_accept_boundary_worker_values() -> None:
    config = _settings(worker_poll_interval_seconds=2, webhook_failure_rate=1, batch_item_delay_seconds=0)
    assert config.lease.total_seconds() == 30
    assert [d.total_seconds() for d in config.retry_delays] == [30, 120]
