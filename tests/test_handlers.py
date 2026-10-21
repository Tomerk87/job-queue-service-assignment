"""Unit tests for the mock handlers with deterministic time and randomness (EC-16 baseline)."""

import random
import uuid
from collections.abc import Callable

import pytest

from app.handlers import (
    HANDLERS,
    HandlerContext,
    LostOwnership,
    SimulatedFailure,
    handle_batch,
    handle_email,
    handle_report,
    handle_webhook,
)
from app.models import JobType

JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class FixedRandom(random.Random):
    """random() always returns `value`, so uniform(a, b) == a + (b - a) * value."""

    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


def make_context(
    rng: random.Random | None = None,
    failure_rate: float = 0.2,
    report_progress: Callable[[int], bool] | None = None,
    sleeps: list[float] | None = None,
) -> HandlerContext:
    recorded = sleeps if sleeps is not None else []
    return HandlerContext(
        job_id=JOB_ID,
        report_progress=report_progress or (lambda _progress: True),
        sleep=recorded.append,
        rng=rng or random.Random(0),
        webhook_failure_rate=failure_rate,
        batch_item_delay=0.2,
    )


def test_every_job_type_has_a_handler() -> None:
    assert set(HANDLERS) == set(JobType)


def test_email_sleeps_one_to_three_seconds_and_returns_message_id() -> None:
    sleeps: list[float] = []

    result = handle_email({"to": "a@b.c", "subject": "s"}, make_context(FixedRandom(0.5), sleeps=sleeps))

    assert sleeps == [2.0]
    assert result["message_id"].startswith("msg-")


@pytest.mark.parametrize("value, expected_sleep", [(0.0, 1.0), (0.999, 1.999)])
def test_webhook_sleeps_one_to_two_seconds(value: float, expected_sleep: float) -> None:
    sleeps: list[float] = []

    handle_webhook({"url": "https://x.test"}, make_context(FixedRandom(value), 0.0, sleeps=sleeps))

    assert sleeps == [pytest.approx(expected_sleep)]


def test_webhook_succeeds_when_failure_rate_is_zero() -> None:
    result = handle_webhook({"url": "https://x.test"}, make_context(FixedRandom(0.0), failure_rate=0.0))
    assert result == {"status_code": 200}


def test_webhook_always_fails_when_failure_rate_is_one() -> None:
    with pytest.raises(SimulatedFailure):
        handle_webhook({"url": "https://x.test"}, make_context(FixedRandom(0.999), failure_rate=1.0))


def test_webhook_failure_boundary() -> None:
    """Fails when random() < rate; a draw equal to the rate succeeds (so P(fail) == rate)."""
    with pytest.raises(SimulatedFailure):
        handle_webhook({"url": "https://x.test"}, make_context(FixedRandom(0.19), failure_rate=0.2))
    result = handle_webhook({"url": "https://x.test"}, make_context(FixedRandom(0.2), failure_rate=0.2))
    assert result == {"status_code": 200}


def test_webhook_failure_rate_is_roughly_twenty_percent() -> None:
    rng = random.Random(42)
    failures = 0
    for _ in range(1000):
        try:
            handle_webhook({"url": "https://x.test"}, make_context(rng, failure_rate=0.2))
        except SimulatedFailure:
            failures += 1
    assert 150 <= failures <= 250  # seeded, so deterministic; sanity-checks the rate wiring


def test_report_sleeps_three_to_five_seconds_and_returns_url() -> None:
    sleeps: list[float] = []

    result = handle_report({"report_name": "r"}, make_context(FixedRandom(1.0), sleeps=sleeps))

    assert sleeps == [5.0]
    assert result == {"report_url": f"https://reports.example.com/{JOB_ID}.pdf"}


def test_batch_reports_monotonic_progress_and_summary() -> None:
    progress: list[int] = []
    sleeps: list[float] = []
    context = make_context(report_progress=lambda p: progress.append(p) or True, sleeps=sleeps)

    result = handle_batch({"items": ["a", "b", "c"]}, context)

    assert progress == [33, 66, 100]
    assert sleeps == [0.2, 0.2, 0.2]
    assert result == {"total": 3, "processed": 3}


@pytest.mark.parametrize("count", [1, 7, 100])
def test_batch_progress_is_monotonic_and_ends_at_100(count: int) -> None:
    progress: list[int] = []
    context = make_context(report_progress=lambda p: progress.append(p) or True)

    handle_batch({"items": list(range(count))}, context)

    assert len(progress) == count
    assert progress == sorted(progress)
    assert progress[-1] == 100


def test_batch_stops_when_progress_write_is_rejected() -> None:
    calls: list[int] = []

    def reject_second(progress: int) -> bool:
        calls.append(progress)
        return len(calls) < 2

    with pytest.raises(LostOwnership):
        handle_batch({"items": [1, 2, 3, 4]}, make_context(report_progress=reject_second))
    assert calls == [25, 50]
