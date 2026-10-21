"""Mock job handlers. They simulate work only: no email is sent and no HTTP call is made.

Handlers are plain functions. Time and randomness come from the context so tests can
make them instant and deterministic.
"""

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.models import JobType


class SimulatedFailure(Exception):
    """An expected, simulated job failure (e.g. the webhook's 20% error rate)."""


class LostOwnership(Exception):
    """This worker no longer owns the job (fenced write rejected or lease lost)."""


class JobTimeout(Exception):
    """The job ran past JOB_TIMEOUT_SECONDS; the attempt fails and is retried."""


@dataclass(frozen=True)
class HandlerContext:
    job_id: uuid.UUID
    report_progress: Callable[[int], bool]
    sleep: Callable[[float], None]
    rng: random.Random
    webhook_failure_rate: float
    batch_item_delay: float


Handler = Callable[[dict[str, Any], HandlerContext], dict[str, Any]]


def handle_email(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    ctx.sleep(ctx.rng.uniform(1, 3))
    return {"message_id": f"msg-{uuid.uuid4().hex[:16]}"}


def handle_webhook(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    ctx.sleep(ctx.rng.uniform(1, 2))
    if ctx.rng.random() < ctx.webhook_failure_rate:
        raise SimulatedFailure("simulated webhook failure: endpoint returned HTTP 503")
    return {"status_code": 200}


def handle_report(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    ctx.sleep(ctx.rng.uniform(3, 5))
    return {"report_url": f"https://reports.example.com/{ctx.job_id}.pdf"}


def handle_batch(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    """Process items one by one, reporting monotonic progress after each item.

    A re-executed batch (after a crash) starts again from the first item; mock items
    have no side effects, so reprocessing is harmless here.
    """
    items = payload["items"]
    total = len(items)
    for done, _item in enumerate(items, start=1):
        ctx.sleep(ctx.batch_item_delay)
        if not ctx.report_progress(done * 100 // total):
            raise LostOwnership(f"progress rejected after {done}/{total} items")
    return {"total": total, "processed": total}


HANDLERS: dict[JobType, Handler] = {
    JobType.EMAIL: handle_email,
    JobType.WEBHOOK: handle_webhook,
    JobType.REPORT: handle_report,
    JobType.BATCH: handle_batch,
}
