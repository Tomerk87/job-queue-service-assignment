"""Real worker processes: graceful stop, forced kill, hung handler and JSON logs (EC-17, EC-18).

Workers run as `python -m app.worker` subprocesses against the test database with
short lease/heartbeat/recovery settings, so each scenario takes a few seconds.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import db_row, log_events, recover, submit

FAST_PROCESS_ENV = {
    "LEASE_SECONDS": "2",
    "HEARTBEAT_INTERVAL_SECONDS": "0.3",
    "RECOVERY_INTERVAL_SECONDS": "0.3",
    "WORKER_POLL_INTERVAL_SECONDS": "0.2",
    "BATCH_ITEM_DELAY_SECONDS": "0.2",
    "WEBHOOK_FAILURE_RATE": "0",
}
SECRET = "secret-item-7f3a"  # payload value that must never reach the logs


def start_worker(worker_id: str, module: str = "app.worker", **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env=os.environ | FAST_PROCESS_ENV | {"WORKER_ID": worker_id} | env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_for(condition: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def submit_batch(client: TestClient, items: int) -> str:
    return submit(client, "batch", payload={"items": [SECRET] * items})


def test_sigterm_finishes_running_job_then_exits_cleanly(client: TestClient) -> None:
    """EC-17: graceful stop drains the active job while its heartbeat keeps the lease alive."""
    job_id = submit_batch(client, items=15)  # ~3s at 0.2s/item, longer than one 2s lease
    worker = start_worker("proc-graceful")
    try:
        wait_for(lambda: db_row(job_id).progress > 0, 10, "the batch to start")
        worker.send_signal(signal.SIGTERM)
        output, _ = worker.communicate(timeout=15)
    finally:
        worker.kill()

    assert worker.returncode == 0
    row = db_row(job_id)
    assert (row.status, row.progress, row.attempts) == ("COMPLETED", 100, 1)
    events = [line.get("event") for line in json_lines(output)]
    assert events.index("stopping") < events.index("completed") < events.index("stopped")


def test_sigkill_mid_job_is_recovered_and_completed_by_another_worker(client: TestClient) -> None:
    """EC-17: a forced kill leaves the job PROCESSING until its lease expires and it is recovered."""
    job_id = submit_batch(client, items=15)
    killed = start_worker("proc-killed")
    try:
        wait_for(lambda: db_row(job_id).progress > 0, 10, "the batch to start")
        killed.kill()
        killed.wait(timeout=5)
    finally:
        killed.kill()
    assert db_row(job_id).status == "PROCESSING"

    survivor = start_worker("proc-survivor", RETRY_DELAYS_SECONDS="0,0")
    try:
        wait_for(lambda: db_row(job_id).status == "COMPLETED", 20, "recovery and completion")
    finally:
        survivor.send_signal(signal.SIGTERM)
        survivor.communicate(timeout=15)

    row = db_row(job_id)
    assert (row.attempts, row.worker_id, row.last_error) == (2, "proc-survivor", None)
    assert log_events(job_id) == [
        "created", "claimed", "lease_expired", "retry_scheduled", "claimed", "completed",
    ]


def test_hung_handler_exits_worker_and_job_is_recovered(client: TestClient) -> None:
    """EC-18: a non-cooperative hang cannot renew forever; the process exits and the lease lapses."""
    job_id = submit(client, "email")
    hung = start_worker("proc-hung", module="tests.hung_worker", JOB_TIMEOUT_SECONDS="1")
    started = time.monotonic()
    try:
        output, _ = hung.communicate(timeout=15)
    finally:
        hung.kill()
    elapsed = time.monotonic() - started

    assert hung.returncode == 1
    assert elapsed < 10  # about timeout (1s) + lease (2s) after the claim, not 1000s
    critical = [line for line in json_lines(output) if line.get("event") == "handler_hung"]
    assert critical and critical[0]["level"] == "CRITICAL" and critical[0]["job_id"] == job_id

    wait_for(lambda: recover() == 1, 5, "the lapsed lease to be recovered")
    row = db_row(job_id)
    assert (row.status, row.attempts, row.last_error) == (
        "PENDING", 1, "lease expired (worker proc-hung)",
    )


def test_worker_logs_are_json_with_job_context_and_no_payload(client: TestClient) -> None:
    job_id = submit_batch(client, items=2)
    worker = start_worker("proc-logs")
    try:
        wait_for(lambda: db_row(job_id).status == "COMPLETED", 10, "the job to complete")
        worker.send_signal(signal.SIGTERM)
        output, _ = worker.communicate(timeout=15)
    finally:
        worker.kill()

    lines = json_lines(output)  # raises if any line is not JSON
    assert all({"ts", "level", "logger", "message"} <= line.keys() for line in lines)
    job_lines = [line for line in lines if line.get("job_id") == job_id]
    assert {line["event"] for line in job_lines} >= {"claimed", "completed"}
    assert all(line["worker_id"] == "proc-logs" for line in job_lines)
    assert SECRET not in output
