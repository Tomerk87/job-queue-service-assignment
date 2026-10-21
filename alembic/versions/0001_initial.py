"""Initial schema: jobs, idempotency_keys, job_logs.

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("scheduled_at", TIMESTAMPTZ),
        sa.Column("next_run_at", TIMESTAMPTZ, nullable=False),
        sa.Column("lease_expires_at", TIMESTAMPTZ),
        sa.Column("claim_token", sa.Uuid),
        sa.Column("worker_id", sa.String(255)),
        # Not unique: an expired key may be reused by a newer job; uniqueness of
        # *active* keys is enforced by idempotency_keys.key.
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("result", JSONB),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TIMESTAMPTZ),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.CheckConstraint(
            "status IN ('SCHEDULED','PENDING','PROCESSING','COMPLETED','FAILED','CANCELLED')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("type IN ('email','webhook','report','batch')", name="ck_jobs_type"),
        sa.CheckConstraint("priority BETWEEN 0 AND 10", name="ck_jobs_priority"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_jobs_progress"),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts"),
    )
    # Claim eligibility: highest priority first, FIFO within a priority.
    op.create_index(
        "ix_jobs_pending_priority",
        "jobs",
        [sa.text("priority DESC"), sa.text("created_at ASC")],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_jobs_processing_lease",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.create_index(
        "ix_jobs_scheduled_at",
        "jobs",
        ["scheduled_at"],
        postgresql_where=sa.text("status = 'SCHEDULED'"),
    )
    op.create_index("ix_jobs_status_created", "jobs", ["status", sa.text("created_at DESC")])
    op.create_index("ix_jobs_type_created", "jobs", ["type", sa.text("created_at DESC")])

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("job_id", sa.Uuid, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
    )

    op.create_table(
        "job_logs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.Uuid, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("level IN ('info','warning','error')", name="ck_job_logs_level"),
    )
    op.create_index("ix_job_logs_job_created", "job_logs", ["job_id", "created_at"])


def downgrade() -> None:
    op.drop_table("job_logs")
    op.drop_table("idempotency_keys")
    op.drop_table("jobs")
