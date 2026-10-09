"""Durable P0 research, execution intents, evidence and ordered events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_orchestration"
down_revision = "0002_identity_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "runs",
        sa.Column(
            "demonstration_scenario", sa.String(20), nullable=False, server_default="positive"
        ),
        schema="huntweave",
    )
    op.add_column(
        "runs",
        sa.Column("demonstration_duration_ms", sa.Integer(), nullable=False, server_default="1500"),
        schema="huntweave",
    )
    op.add_column("runs", sa.Column("started_at", sa.DateTime(timezone=True)), schema="huntweave")
    op.add_column("runs", sa.Column("reason_code", sa.String(64)), schema="huntweave")
    tables = {
        "research_tasks": [
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("step", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("lease_generation", sa.Integer(), nullable=False),
            sa.Column("lease_owner", sa.Uuid()),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        ],
        "agent_sessions": [
            sa.Column(
                "task_id", sa.Uuid(), sa.ForeignKey("huntweave.research_tasks.id"), nullable=False
            ),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("context", JSONB(), nullable=False),
        ],
        "decisions": [
            sa.Column(
                "session_id",
                sa.Uuid(),
                sa.ForeignKey("huntweave.agent_sessions.id"),
                nullable=False,
            ),
            sa.Column("step", sa.Integer(), nullable=False),
            sa.Column("content", JSONB(), nullable=False),
        ],
        "tool_calls": [
            sa.Column(
                "session_id",
                sa.Uuid(),
                sa.ForeignKey("huntweave.agent_sessions.id"),
                nullable=False,
            ),
            sa.Column(
                "decision_id",
                sa.Uuid(),
                sa.ForeignKey("huntweave.decisions.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("ticket", JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        "budget_reservations": [
            sa.Column(
                "call_id",
                sa.Uuid(),
                sa.ForeignKey("huntweave.tool_calls.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("settled", sa.Boolean(), nullable=False),
            sa.Column("output_bytes", sa.Integer(), nullable=False),
        ],
        "evidence": [
            sa.Column(
                "call_id", sa.Uuid(), sa.ForeignKey("huntweave.tool_calls.id"), nullable=False
            ),
            sa.Column("metadata_json", JSONB(), nullable=False),
        ],
        "interruption_records": [
            sa.Column("reason_code", sa.String(64), nullable=False),
            sa.Column("recovery_condition", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
    }
    for name, columns in tables.items():
        op.create_table(
            name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("run_id", sa.Uuid(), sa.ForeignKey("huntweave.runs.id"), nullable=False),
            *columns,
            schema="huntweave",
        )
        op.create_index("ix_huntweave_" + name + "_run_id", name, ["run_id"], schema="huntweave")
    op.create_table(
        "tool_results",
        sa.Column("call_id", sa.Uuid(), sa.ForeignKey("huntweave.tool_calls.id"), primary_key=True),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "execution_outbox",
        sa.Column("call_id", sa.Uuid(), sa.ForeignKey("huntweave.tool_calls.id"), primary_key=True),
        sa.Column("acknowledged", sa.Boolean(), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "event_cursors",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("huntweave.runs.id"), primary_key=True),
        sa.Column("cursor", sa.Integer(), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "audit_events",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("huntweave.runs.id"), primary_key=True),
        sa.Column("cursor", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("source_event_id", sa.String(160)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "source_event_id"),
        schema="huntweave",
    )


def downgrade():
    for name in (
        "audit_events",
        "event_cursors",
        "execution_outbox",
        "tool_results",
        "interruption_records",
        "evidence",
        "budget_reservations",
        "tool_calls",
        "decisions",
        "agent_sessions",
        "research_tasks",
    ):
        op.drop_table(name, schema="huntweave")
    for column in (
        "reason_code",
        "started_at",
        "demonstration_duration_ms",
        "demonstration_scenario",
    ):
        op.drop_column("runs", column, schema="huntweave")
