"""Persistent heartbeat for the supervised agentd process."""

import sqlalchemy as sa
from alembic import op

revision = "0001_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_processes",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        schema="huntweave",
    )


def downgrade() -> None:
    op.drop_table("runtime_processes", schema="huntweave")
