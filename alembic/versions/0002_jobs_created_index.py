"""Index for newest-first job listing.

Matches GET /jobs ordering (created_at DESC, id DESC), so an unfiltered page is
read from the index instead of sorting the whole jobs table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_created", "jobs", [sa.text("created_at DESC"), sa.text("id DESC")])


def downgrade() -> None:
    op.drop_index("ix_jobs_created", table_name="jobs")
