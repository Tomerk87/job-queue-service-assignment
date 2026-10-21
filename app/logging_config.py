"""Structured JSON logging for the API and the worker.

Each record is one JSON object per line: ts, level, logger, message, plus any
context passed with `extra=` (event, job_id, worker_id, attempt, ...). Callers must
never put job payloads or other user data into log records.
"""

import logging
import sys

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.json import JsonFormatter

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
# uvicorn attaches an ANSI-colored duplicate of each message; keep it out of the JSON.
EXCLUDED_FIELDS = [*RESERVED_ATTRS, "color_message"]


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            reserved_attrs=EXCLUDED_FIELDS,
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own plain-text handlers; route its records through ours.
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
