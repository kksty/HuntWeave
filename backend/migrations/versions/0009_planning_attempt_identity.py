"""Independent task identity and the durable record of one model request.

Two assumptions from P0 stop being implementable with remote models, and this revision is where the
schema stops reproducing them (issue #43, `docs/specs/0003-agent-research.md` section 5.1):

1. A task used to be identified by `run_id + role`, so a Run could never hold two Workers of the
   same role, nor ask a role for a second round of evidence, without the two sharing a session,
   a decision and a call. `research_tasks.ordinal` makes "the Nth task of this role in this Run"
   part of the key, and the unique constraint refuses a collision instead of overwriting.
2. Nothing recorded that a model request had been *intended*. `decisions` becomes the planning
   attempt itself: the intent, the frozen input, its watermark, the versions it was prepared
   against, and the answer — including an answer that arrived too late to apply, and including the
   provider's accounting, or the fact that no accounting ever arrived.

The existing rows keep their meaning. A decision row could only be written by the previous code
path after the model answered and the call was committed, so it becomes `applied`; its task binding
is backfilled from its session, which is what already linked the two; and its model usage stays
`unknown`, because no receipt was ever recorded and zero would be a number nobody measured.

The downgrade cannot be complete, and says so: dropping `ordinal` merges two same-role tasks of one
Run into rows nothing can tell apart, so `downgrade()` refuses on a database that actually uses the
new identity rather than losing the distinction. The planning-attempt columns are dropped with their
history either way.

Revision ID: 0009_planning_attempt_identity
Revises: 0008_retention_decisions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_planning_attempt_identity"
down_revision = "0008_retention_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_tasks",
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema="huntweave",
    )
    op.create_unique_constraint(
        "uq_research_tasks_run_role_ordinal",
        "research_tasks",
        ["run_id", "role", "ordinal"],
        schema="huntweave",
    )

    # The task binding first, so the backfill can read the column it fills.
    op.add_column("decisions", sa.Column("task_id", sa.Uuid(), nullable=True), schema="huntweave")
    op.add_column(
        "decisions",
        sa.Column("attempt_ordinal", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema="huntweave",
    )
    op.add_column(
        "decisions",
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'applied'")),
        schema="huntweave",
    )
    op.add_column("decisions", sa.Column("input_snapshot", JSONB(), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("input_hash", sa.String(64), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("input_watermark", sa.Integer(), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("budget_snapshot", JSONB(), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("run_version", sa.Integer(), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("task_version", sa.Integer(), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("scope_version", sa.Integer(), nullable=True), schema="huntweave")
    op.add_column(
        "decisions", sa.Column("lease_generation", sa.Integer(), nullable=True), schema="huntweave"
    )
    op.add_column(
        "decisions", sa.Column("prompt_version", sa.String(32), nullable=True), schema="huntweave"
    )
    op.add_column("decisions", sa.Column("provider", sa.String(64), nullable=True), schema="huntweave")
    op.add_column("decisions", sa.Column("model_name", sa.String(128), nullable=True), schema="huntweave")
    op.add_column(
        "decisions",
        sa.Column("request_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema="huntweave",
    )
    op.add_column("decisions", sa.Column("usage", JSONB(), nullable=True), schema="huntweave")
    op.add_column(
        "decisions",
        sa.Column("usage_state", sa.String(16), nullable=False, server_default=sa.text("'unknown'")),
        schema="huntweave",
    )
    op.add_column("decisions", sa.Column("reason_code", sa.String(64), nullable=True), schema="huntweave")
    op.add_column(
        "decisions",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        schema="huntweave",
    )
    op.add_column(
        "decisions",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        schema="huntweave",
    )
    op.add_column("decisions", sa.Column("supersedes_id", sa.Uuid(), nullable=True), schema="huntweave")

    # A session already names the task it belongs to, so the existing rows are backfilled from the
    # record they actually carry rather than from a guess.
    op.execute(
        "UPDATE huntweave.decisions AS d SET task_id = s.task_id "
        "FROM huntweave.agent_sessions AS s WHERE d.session_id = s.id"
    )
    op.alter_column("decisions", "task_id", nullable=False, schema="huntweave")
    op.create_foreign_key(
        "fk_decisions_task_id",
        "decisions",
        "research_tasks",
        ["task_id"],
        ["id"],
        source_schema="huntweave",
        referent_schema="huntweave",
    )
    op.create_foreign_key(
        "fk_decisions_supersedes_id",
        "decisions",
        "decisions",
        ["supersedes_id"],
        ["id"],
        source_schema="huntweave",
        referent_schema="huntweave",
    )
    op.create_index(
        "ix_huntweave_decisions_task_id", "decisions", ["task_id"], schema="huntweave"
    )


def downgrade() -> None:
    # Dropping `ordinal` merges two same-role tasks of one Run back into rows nothing can tell
    # apart, and re-upgrading then cannot restore the constraint over them. A database that uses
    # the new identity therefore refuses the downgrade instead of quietly losing the distinction;
    # a database that never used it (a fresh one) round-trips. The planning attempts stored in
    # `decisions` are dropped with their columns, which is the other half of the loss.
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM (SELECT run_id, role FROM huntweave.research_tasks "
            "GROUP BY run_id, role HAVING count(*) > 1) AS duplicated"
        )
    ).scalar()
    if duplicates:
        raise RuntimeError(
            "This database holds more than one task of the same role in a Run, so dropping "
            "research_tasks.ordinal would merge them irreversibly; the downgrade is refused."
        )
    op.drop_index("ix_huntweave_decisions_task_id", table_name="decisions", schema="huntweave")
    op.drop_constraint("fk_decisions_supersedes_id", "decisions", type_="foreignkey", schema="huntweave")
    op.drop_constraint("fk_decisions_task_id", "decisions", type_="foreignkey", schema="huntweave")
    for column in (
        "supersedes_id",
        "finished_at",
        "created_at",
        "reason_code",
        "usage_state",
        "usage",
        "request_count",
        "model_name",
        "provider",
        "prompt_version",
        "lease_generation",
        "scope_version",
        "task_version",
        "run_version",
        "budget_snapshot",
        "input_watermark",
        "input_hash",
        "input_snapshot",
        "status",
        "attempt_ordinal",
        "task_id",
    ):
        op.drop_column("decisions", column, schema="huntweave")
    op.drop_constraint(
        "uq_research_tasks_run_role_ordinal", "research_tasks", type_="unique", schema="huntweave"
    )
    op.drop_column("research_tasks", "ordinal", schema="huntweave")
