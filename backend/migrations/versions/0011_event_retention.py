"""Record how far a Run's event timeline has been pruned, so a gap is stated rather than implied.

Retention of events is a policy about *the timeline*, not about the business record: the authoritative
sources of what a Run did are its versioned business rows (`runs`, `tool_calls`, `tool_results`,
`evidence`), and the event log is the incremental projection and the cursor a reader catches up on
(PROJECT.md section 12.1, 0006 section 9). When old events are removed, a reader arriving with an old
`Last-Event-ID` must be told that the events below some cursor no longer exist — otherwise "nothing to
send" and "the beginning of time" read the same, and the console would call a truncated timeline a
complete one.

A deleted row cannot say what it was, so the watermark is stored beside the Run's event cursor: the
highest cursor whose events are gone. Everything at or below it is a gap; everything above it is
either present now or not yet committed. A separate table was deliberately not introduced: a
per-Run table with one row per Run would duplicate `event_cursors`' lifetime for no separation, and
there is exactly one writer of the cursor either way (`runs/events.py`).

Revision ID: 0011_event_retention
Revises: 0008_retention_decisions
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_event_retention"
down_revision = "0008_retention_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `server_default` is required: an existing Run's events were never pruned, and "0" says exactly
    # that. A NULL would have to be given a meaning at every read site, and the honest meaning here
    # is already a number.
    op.add_column(
        "event_cursors",
        sa.Column("retained_from_cursor", sa.Integer(), nullable=False, server_default="0"),
        schema="huntweave",
    )


def downgrade() -> None:
    op.drop_column("event_cursors", "retained_from_cursor", schema="huntweave")
