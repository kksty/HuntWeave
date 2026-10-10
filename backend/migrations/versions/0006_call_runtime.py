"""Keep what the execution side decided about where a call acted.

The console has to show the command, the working directory, the user, the instance and the tool
environment a call really ran under, and it has to keep showing them after the deployment's profile
has moved on. Reconstructing that from the current profile would rewrite history, so it is stored on
the call, written once when the execution side said so.

Nullable on purpose: calls recorded before this migration have no such statement, and inventing one
for them would attribute to an old call facts nobody wrote down.

Revision ID: 0006_call_runtime
Revises: 0005_run_execution_profile
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_call_runtime"
down_revision = "0005_run_execution_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("runtime", JSONB(), nullable=True), schema="huntweave")
    op.add_column("tool_calls", sa.Column("progress", JSONB(), nullable=True), schema="huntweave")


def downgrade() -> None:
    op.drop_column("tool_calls", "progress", schema="huntweave")
    op.drop_column("tool_calls", "runtime", schema="huntweave")
