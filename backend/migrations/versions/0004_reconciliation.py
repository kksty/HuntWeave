"""Operator verdicts on calls whose outcome the ledger never confirmed."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_reconciliation"
down_revision = "0003_orchestration"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tool_calls", sa.Column("replaces_call_id", sa.Uuid()), schema="huntweave")
    op.create_foreign_key(
        "fk_tool_calls_replaces_call_id",
        "tool_calls",
        "tool_calls",
        ["replaces_call_id"],
        ["id"],
        source_schema="huntweave",
        referent_schema="huntweave",
    )
    op.add_column("tool_calls", sa.Column("observation", JSONB()), schema="huntweave")
    op.create_table(
        "reconciliation_decisions",
        sa.Column(
            "call_id",
            sa.Uuid(),
            sa.ForeignKey("huntweave.tool_calls.id"),
            primary_key=True,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("huntweave.runs.id"), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        # Kept by value: expired sessions are deleted by the access layer, and a verdict must
        # still name who decided it after that.
        sa.Column("operator_session_id", sa.Uuid(), nullable=False),
        sa.Column("scope_version", sa.Integer(), nullable=False),
        sa.Column("evidence_ids", JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("observation", JSONB()),
        sa.Column("redispatch_authorized", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="huntweave",
    )
    op.create_index(
        "ix_huntweave_reconciliation_decisions_run_id",
        "reconciliation_decisions",
        ["run_id"],
        schema="huntweave",
    )


def downgrade():
    op.drop_index(
        "ix_huntweave_reconciliation_decisions_run_id",
        table_name="reconciliation_decisions",
        schema="huntweave",
    )
    op.drop_table("reconciliation_decisions", schema="huntweave")
    op.drop_column("tool_calls", "observation", schema="huntweave")
    op.drop_constraint(
        "fk_tool_calls_replaces_call_id", "tool_calls", type_="foreignkey", schema="huntweave"
    )
    op.drop_column("tool_calls", "replaces_call_id", schema="huntweave")
