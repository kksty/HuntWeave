"""Service facts, entry identity, append-only observations and cross-Run lineage.

Issue #44 needs four things the schema did not have: a host, a service and a web entry as
program-computed identities (issue #44 criterion 1), an observation record that cannot be
overwritten (criterion 2), ADR-0015's four separate records for one research relation (criterion 3),
and an explicit, frozen, read-only reference to an earlier Run (criterion 4).

Three details of the shape are load-bearing and are stated here so a later migration does not undo
them by accident:

* ``observations`` carries a trigger that refuses any statement changing what was observed. What a
  record *currently* amounts to is derived from the records pointing at it, so "矛盾、更正与撤回并存
  可查" is a property of the data rather than of a service method.
* ``web_endpoints.key`` contains the scheme, the Host/SNI name and the path, so two applications on
  one address and port are two rows. ``services.key`` stays the coarser ``IP + transport + port``.
* ``service_settlements.call_id`` is unique. A cross-service call is bound to several services and
  charged once; per-service cost lives in ``service_cost_shares`` and never enforces a budget.

Revision ID: 0010_service_facts
Revises: 0009_planning_attempt_identity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_service_facts"
down_revision = "0009_planning_attempt_identity"
branch_labels = None
depends_on = None

IMMUTABLE_OBSERVATION = """
CREATE FUNCTION huntweave.observations_are_append_only() RETURNS trigger AS $$
BEGIN
    IF to_jsonb(NEW) - 'recorded_at' IS DISTINCT FROM to_jsonb(OLD) - 'recorded_at' THEN
        RAISE EXCEPTION 'an observation cannot be rewritten';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER observations_append_only
    BEFORE UPDATE ON huntweave.observations
    FOR EACH ROW EXECUTE FUNCTION huntweave.observations_are_append_only();
"""


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("key", sa.String(length=200), primary_key=True),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("identity_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        schema="huntweave",
    )
    op.create_index("ix_hosts_project_id", "hosts", ["project_id"], schema="huntweave")

    op.create_table(
        "services",
        sa.Column("key", sa.String(length=200), primary_key=True),
        sa.Column("host_key", sa.String(length=200), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("transport", sa.String(length=8), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("identity_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["host_key"], ["huntweave.hosts.key"]),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        schema="huntweave",
    )
    op.create_index("ix_services_host_key", "services", ["host_key"], schema="huntweave")
    op.create_index("ix_services_project_id", "services", ["project_id"], schema="huntweave")

    op.create_table(
        "web_endpoints",
        sa.Column("key", sa.String(length=400), primary_key=True),
        sa.Column("service_key", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("sni", sa.String(length=255), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("identity_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["service_key"], ["huntweave.services.key"]),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        schema="huntweave",
    )
    op.create_index(
        "ix_web_endpoints_service_key", "web_endpoints", ["service_key"], schema="huntweave"
    )
    op.create_index(
        "ix_web_endpoints_project_id", "web_endpoints", ["project_id"], schema="huntweave"
    )

    op.create_table(
        "observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("host_key", sa.String(length=200), nullable=False),
        sa.Column("service_key", sa.String(length=200), nullable=False),
        sa.Column("entry_key", sa.String(length=400), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("facts", JSONB(), nullable=False),
        sa.Column("connection", JSONB(), nullable=False),
        sa.Column("access", JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("parser_version", sa.String(length=80), nullable=False),
        sa.Column("resolver_version", sa.Integer(), nullable=False),
        sa.Column("identity_version", sa.Integer(), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("previous_id", sa.Uuid(), nullable=True),
        sa.Column("source_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_call_id", sa.Uuid(), nullable=True),
        sa.Column("source_ticket_id", sa.Uuid(), nullable=True),
        sa.Column("source_scope_version", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["host_key"], ["huntweave.hosts.key"]),
        sa.ForeignKeyConstraint(["service_key"], ["huntweave.services.key"]),
        sa.ForeignKeyConstraint(["entry_key"], ["huntweave.web_endpoints.key"]),
        sa.ForeignKeyConstraint(["previous_id"], ["huntweave.observations.id"]),
        sa.ForeignKeyConstraint(["source_run_id"], ["huntweave.runs.id"]),
        sa.UniqueConstraint(
            "source_run_id", "source_call_id", "facts_hash", name="uq_observation_call"
        ),
        schema="huntweave",
    )
    for column in (
        "project_id",
        "host_key",
        "service_key",
        "entry_key",
        "previous_id",
        "source_run_id",
        "source_call_id",
    ):
        op.create_index(f"ix_observations_{column}", "observations", [column], schema="huntweave")

    op.create_table(
        "service_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("service_key", sa.String(length=200), nullable=False),
        sa.Column("entry_key", sa.String(length=400), nullable=True),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("basis", sa.String(length=20), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["huntweave.runs.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["huntweave.tool_calls.id"]),
        sa.ForeignKeyConstraint(["service_key"], ["huntweave.services.key"]),
        sa.ForeignKeyConstraint(["entry_key"], ["huntweave.web_endpoints.key"]),
        sa.ForeignKeyConstraint(["observation_id"], ["huntweave.observations.id"]),
        sa.UniqueConstraint("call_id", "service_key", name="uq_binding_call_service"),
        schema="huntweave",
    )
    for column in ("project_id", "run_id", "call_id", "service_key"):
        op.create_index(f"ix_service_bindings_{column}", "service_bindings", [column], schema="huntweave")

    op.create_table(
        "service_coverage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("service_key", sa.String(length=200), nullable=False),
        sa.Column("entry_key", sa.String(length=400), nullable=True),
        sa.Column("call_id", sa.Uuid(), nullable=True),
        sa.Column("directly_verified", sa.Boolean(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("input_versions", JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["huntweave.runs.id"]),
        sa.ForeignKeyConstraint(["service_key"], ["huntweave.services.key"]),
        sa.ForeignKeyConstraint(["entry_key"], ["huntweave.web_endpoints.key"]),
        sa.ForeignKeyConstraint(["call_id"], ["huntweave.tool_calls.id"]),
        sa.UniqueConstraint("run_id", "service_key", "entry_key", name="uq_coverage_run_service"),
        schema="huntweave",
    )
    for column in ("project_id", "run_id", "service_key"):
        op.create_index(f"ix_service_coverage_{column}", "service_coverage", [column], schema="huntweave")

    op.create_table(
        "service_navigation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("entry_key", sa.String(length=400), nullable=False),
        sa.Column("service_key", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_session_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_version_at_choice", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["huntweave.runs.id"]),
        sa.ForeignKeyConstraint(["entry_key"], ["huntweave.web_endpoints.key"]),
        sa.ForeignKeyConstraint(["service_key"], ["huntweave.services.key"]),
        sa.UniqueConstraint("run_id", "version", name="uq_navigation_run_version"),
        schema="huntweave",
    )
    for column in ("project_id", "run_id", "entry_key"):
        op.create_index(
            f"ix_service_navigation_{column}", "service_navigation", [column], schema="huntweave"
        )

    op.create_table(
        "service_settlements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("settlement_reference", sa.String(length=128), nullable=False),
        sa.Column("cost_units", sa.Integer(), nullable=False),
        sa.Column("output_bytes", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["huntweave.tool_calls.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["huntweave.runs.id"]),
        sa.UniqueConstraint("call_id"),
        schema="huntweave",
    )
    op.create_index(
        "ix_service_settlements_project_id", "service_settlements", ["project_id"], schema="huntweave"
    )
    op.create_index("ix_service_settlements_run_id", "service_settlements", ["run_id"], schema="huntweave")

    op.create_table(
        "service_cost_shares",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("service_key", sa.String(length=200), nullable=False),
        sa.Column("attribution", sa.String(length=20), nullable=False),
        sa.Column("share_units", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["huntweave.tool_calls.id"]),
        sa.ForeignKeyConstraint(["service_key"], ["huntweave.services.key"]),
        sa.UniqueConstraint("call_id", "service_key", name="uq_cost_share_call_service"),
        schema="huntweave",
    )
    for column in ("project_id", "call_id", "service_key"):
        op.create_index(
            f"ix_service_cost_shares_{column}", "service_cost_shares", [column], schema="huntweave"
        )

    op.create_table(
        "research_lineage_references",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_object_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("identity_version", sa.Integer(), nullable=False),
        sa.Column("retained_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_session_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["target_run_id"], ["huntweave.runs.id"]),
        sa.ForeignKeyConstraint(["source_run_id"], ["huntweave.runs.id"]),
        schema="huntweave",
    )
    for column in ("project_id", "target_run_id", "source_run_id"):
        op.create_index(
            f"ix_research_lineage_references_{column}",
            "research_lineage_references",
            [column],
            schema="huntweave",
        )

    op.create_table(
        "address_clues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("transport", sa.String(length=8), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("clue_key", sa.String(length=300), nullable=False),
        sa.Column("discovered_via", sa.String(length=20), nullable=False),
        sa.Column("outside_authorization", sa.Boolean(), nullable=False),
        sa.Column("outside_reason", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["huntweave.projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["huntweave.runs.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["huntweave.tool_calls.id"]),
        sa.UniqueConstraint("run_id", "clue_key", name="uq_clue_run_key"),
        schema="huntweave",
    )
    for column in ("project_id", "run_id"):
        op.create_index(f"ix_address_clues_{column}", "address_clues", [column], schema="huntweave")

    op.execute(IMMUTABLE_OBSERVATION)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER observations_append_only ON huntweave.observations; "
        "DROP FUNCTION huntweave.observations_are_append_only();"
    )
    op.drop_table("address_clues", schema="huntweave")
    op.drop_table("research_lineage_references", schema="huntweave")
    op.drop_table("service_cost_shares", schema="huntweave")
    op.drop_table("service_settlements", schema="huntweave")
    op.drop_table("service_navigation", schema="huntweave")
    op.drop_table("service_coverage", schema="huntweave")
    op.drop_table("service_bindings", schema="huntweave")
    op.drop_table("observations", schema="huntweave")
    op.drop_table("web_endpoints", schema="huntweave")
    op.drop_table("services", schema="huntweave")
    op.drop_table("hosts", schema="huntweave")
