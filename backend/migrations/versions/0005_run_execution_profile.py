"""add the Run's execution profile

A Run fixes whether it runs in demonstration mode or for real. Existing Runs were all
demonstrations, and the server default keeps that true for rows created before this migration.

Revision ID: 0005_run_execution_profile
Revises: 0004_reconciliation
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_run_execution_profile"
down_revision = "0004_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "execution_profile",
            sa.String(length=20),
            nullable=False,
            server_default="fake-p0-v1",
        ),
        schema="huntweave",
    )


def downgrade() -> None:
    op.drop_column("runs", "execution_profile", schema="huntweave")
