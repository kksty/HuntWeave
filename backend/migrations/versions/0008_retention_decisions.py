"""Record the operator's retention decisions business-side.

Selective retention is a deployment-level policy, but its decisions concern specific Runs: the
private workspaces being kept, pinned or removed are those Runs' reproduction material. The
execution side already keeps what it really removed; this table is the control plane's own trace of
who asked for what, which is what the Run timeline and the operator's review read afterwards.

The identifier is the execution side's decision id rather than a fresh one, so a retried request
cannot become a second decision, and nothing here can invent a deletion the execution side did not
report.

Revision ID: 0008_retention_decisions
Revises: 0007_drop_login_throttle
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_retention_decisions"
down_revision = "0007_drop_login_throttle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retention_decisions",
        sa.Column("decision_id", sa.Uuid(), primary_key=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("target_kind", sa.String(length=20), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("operator_session_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("affected_runs", JSONB(), nullable=False),
        sa.Column("deleted_artifacts", JSONB(), nullable=False),
        sa.Column("failed", JSONB(), nullable=False),
        sa.Column("freed_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="huntweave",
    )


def downgrade() -> None:
    op.drop_table("retention_decisions", schema="huntweave")
