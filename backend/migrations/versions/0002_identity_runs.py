"""Server-side sessions and immutable authorization / demonstration Run records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_identity_runs"
down_revision = "0001_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_key_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "login_buckets",
        sa.Column("bucket", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="huntweave",
    )
    op.create_table(
        "authorization_scopes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("huntweave.projects.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="huntweave",
    )
    op.create_index(
        "ix_huntweave_authorization_scopes_project_id",
        "authorization_scopes",
        ["project_id"],
        schema="huntweave",
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("huntweave.projects.id"), nullable=False),
        sa.Column(
            "scope_id",
            sa.Uuid(),
            sa.ForeignKey("huntweave.authorization_scopes.id"),
            nullable=False,
        ),
        sa.Column("scope_version", sa.Integer(), nullable=False),
        sa.Column("scope_snapshot", JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("phase", sa.String(20)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0 AND scope_version > 0", name="ck_runs_versions"),
        sa.CheckConstraint(
            "status IN ('draft','queued','running','waiting','pausing','paused',"
            "'recovering','cancelling','closed','cancelled','failed')",
            name="ck_runs_status",
        ),
        schema="huntweave",
    )
    op.create_index("ix_huntweave_runs_project_id", "runs", ["project_id"], schema="huntweave")


def downgrade() -> None:
    for name in (
        "runs",
        "authorization_scopes",
        "projects",
        "login_buckets",
        "web_sessions",
        "access_key_state",
    ):
        op.drop_table(name, schema="huntweave")
