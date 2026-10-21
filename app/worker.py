"""Worker process entrypoint.

Phase 1: an idle process that starts with the stack and exits cleanly on SIGTERM/SIGINT.
Claiming and executing jobs are added in Phase 2.
"""

import logging
import signal
import threading
from types import FrameType

logger = logging.getLogger("app.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s, stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info("worker started (idle: job execution is not implemented yet)")
    stop.wait()
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
