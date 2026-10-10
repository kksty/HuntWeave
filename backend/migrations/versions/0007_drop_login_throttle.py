"""Drop the key-submission throttle.

The table backed a per-address and global counter on `POST /auth/login`. That throttle protected
nothing the deployment needs: the value being submitted is the access key this deployment itself
generated (at least 32 bytes of cryptographic randomness), so a per-address attempt limit does not
address guessing it — while it does lock the operator out of their own platform for a minute after a
few typos, and it makes any scripted use of the console depend on a timer that has nothing to do with
what it is testing. Request-level abuse protection for a public entry point belongs at the reverse
proxy, which sees real client addresses and covers the whole surface.

Dropping the table loses only counters, which expire within a minute by design. The migration's
history keeps the record of why they existed.

Revision ID: 0007_drop_login_throttle
Revises: 0006_call_runtime
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_drop_login_throttle"
down_revision = "0006_call_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("login_buckets", schema="huntweave")


def downgrade() -> None:
    op.create_table(
        "login_buckets",
        sa.Column("bucket", sa.String(length=64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        schema="huntweave",
    )
