"""Shared fixtures. Tests run against a real PostgreSQL database whose name must end in _test."""

from collections.abc import Iterator

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from app.config import settings
from app.db import engine
from app.main import app


def _ensure_test_database() -> None:
    url = make_url(settings.database_url)
    if not (url.database or "").endswith("_test"):
        raise RuntimeError(f"refusing to run tests against non-test database {url.database!r}")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Rebuild the schema from scratch, exercising both migration directions."""
    _ensure_test_database()
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def clean_tables(migrated_database: None) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE job_logs, idempotency_keys, jobs"))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def set_status(job_id: str, status: str) -> None:
    """Force a job into a state the API cannot set directly (e.g. PROCESSING)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET status = :status WHERE id = :id"), {"status": status, "id": job_id}
        )


def count_rows(table: str) -> int:
    with engine.connect() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {table}"))
