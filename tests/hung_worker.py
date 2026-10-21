"""Test-only launcher: a real worker whose email handler hangs without cooperating (EC-18).

The hang ignores ctx.sleep, so the cooperative timeout cannot interrupt it; only the
heartbeat backstop (stop renewing, then exit the process) can. Run as
`python -m tests.hung_worker`.
"""

import time
from typing import Any

from app import handlers, worker
from app.models import JobType


def hang(_payload: dict[str, Any], _ctx: handlers.HandlerContext) -> dict[str, Any]:
    time.sleep(1000)
    return {}


if __name__ == "__main__":
    handlers.HANDLERS[JobType.EMAIL] = hang  # same dict object the worker dispatches from
    worker.main()
