"""Business persistence; framework checkpoint tables remain in their own schema."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    __abstract__ = True
    # Declared for typing only: subclasses may replace it with a tuple of constraints.
    __table_args__: Any = {"schema": "huntweave"}


class AccessKeyState(Base):
    __tablename__ = "access_key_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    key_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthorizationScope(Base):
    __tablename__ = "authorization_scopes"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.projects.id"), index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.authorization_scopes.id"))
    scope_version: Mapped[int] = mapped_column(Integer)
    scope_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    demonstration_scenario: Mapped[str] = mapped_column(String(20), default="positive")
    demonstration_duration_ms: Mapped[int] = mapped_column(Integer, default=1500)
    # Fixed when the Run is created: a Run never switches between demonstration and real
    # execution, so its calls can never be answered by the wrong side (issue #17, criterion 6).
    execution_profile: Mapped[str] = mapped_column(String(20), default="fake-p0-v1")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ResearchTask(Base):
    """One independent piece of work a role owes this Run.

    A role is not a task. Two Workers of the same Run, or a Worker asked for a second round of
    evidence after its first task finished, are different tasks with different sessions, model
    attempts, decisions and calls. ``ordinal`` is what makes that difference part of the key, and
    the unique constraint turns a collision into a refusal instead of two workers sharing a row.
    """

    __tablename__ = "research_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "role", "ordinal", name="uq_research_tasks_run_role_ordinal"),
        {"schema": "huntweave"},
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    # Which task of this role in this Run this is: 0 for the first one, 1 for a follow-up round.
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))
    step: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.research_tasks.id"))
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Decision(Base):
    """One planning attempt: what was asked of the model, and what came back.

    The row is written before the model is called, because the request intent has to be durable
    before anything leaves the platform. ``status`` says how far it got: ``requested`` is an intent
    with a frozen input and no answer yet, ``applied`` is an answer the Run committed (and its
    ``content`` is the decision the graph and the console read), ``refused`` is an answer that
    arrived after the Run stopped or after the versions it was prepared against moved on, and
    ``abandoned`` is an intent a later lease generation took over.

    ``content`` carries whatever the model proposed, including a suggestion that was refused: the
    record keeps the source and the answer even when applying it would be wrong.

    ``usage_state`` is deliberately not a number. A request the provider never accounted for reads
    ``unknown`` and stays in the pending list; writing zero would claim a cost nobody measured.

    ``task_id`` is what binds a decision to one independent task rather than to a role, and
    ``input_snapshot`` is the frozen input the answer belongs to — the same content hashed into
    ``input_hash``, so a replay can tell "the same question" from "a question that looks similar".
    """

    __tablename__ = "decisions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.agent_sessions.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.research_tasks.id"))
    step: Mapped[int] = mapped_column(Integer)
    # Which attempt at this step this is: the identity of one question, not of the answer to it.
    attempt_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    # requested / applied / refused / abandoned.
    status: Mapped[str] = mapped_column(String(16), default="applied")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The frozen input of the request, and the watermark and versions it was prepared against.
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_watermark: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    run_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which model request this was, in the deployment's own terms. Plain strings: the harness owns
    # every framework and vendor type, so no provider object is ever stored here.
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    # The provider's own accounting, verbatim. NULL is "no receipt", never "cost nothing".
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    usage_state: Mapped[str] = mapped_column(String(16), default="unknown")
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The attempt this one retries, so an abandoned request's unknown usage is not lost when the
    # same step is asked again under a later lease generation.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("huntweave.decisions.id"), nullable=True
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.agent_sessions.id"))
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("huntweave.decisions.id"), unique=True
    )
    status: Mapped[str] = mapped_column(String(20))
    ticket: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # A re-dispatched call names the call whose verdict proved the original never ran.
    replaces_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("huntweave.tool_calls.id"), nullable=True
    )
    # The execution side's own last word on this call's process and connection.
    observation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # What the execution side decided about where the call acts (argv, cwd, user, instance,
    # image digests, gateways), written when it said so. Kept on the call rather than derived later
    # from a profile: a deployment's profile can change, and the record of what an old call actually
    # ran must not change with it.
    runtime: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # How long the call had been running and when it last produced output, in the execution side's
    # own terms. This is what the console shows instead of a progress bar (issue #19).
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ReconciliationDecision(Base):
    """An operator's evidence-bound verdict on one call with an unconfirmed outcome.

    The verdict is business-side only: it never rewrites the execution ledger, and it never
    stands in for a stop confirmation the execution side has to give separately.
    """

    __tablename__ = "reconciliation_decisions"
    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("huntweave.tool_calls.id"), primary_key=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    # The operator's session identifier is kept by value, without a foreign key: expired
    # sessions are housekeeping data the access layer deletes, while this verdict keeps
    # naming who decided as durable audit history.
    operator_session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    scope_version: Mapped[int] = mapped_column(Integer)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB)
    note: Mapped[str] = mapped_column(Text)
    observation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    redispatch_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RetentionDecisionRecord(Base):
    """The control plane's trace of one retention decision.

    The execution side keeps what it really removed; this row is the operator-facing record: who
    asked, for which version or artifact, what the execution side answered, and which Runs it
    concerns. The identifier is the execution side's own decision id, so a retried request cannot
    become a second decision.
    """

    __tablename__ = "retention_decisions"
    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action: Mapped[str] = mapped_column(String(20))
    target_kind: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(String(128))
    # Kept by value, without a foreign key: expired sessions are housekeeping data, while this
    # record is durable audit history naming who decided (same reasoning as reconciliation).
    operator_session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    note: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    affected_runs: Mapped[list[str]] = mapped_column(JSONB)
    deleted_artifacts: Mapped[list[str]] = mapped_column(JSONB)
    failed: Mapped[list[str]] = mapped_column(JSONB)
    freed_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolResult(Base):
    __tablename__ = "tool_results"
    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("huntweave.tool_calls.id"), primary_key=True
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BudgetReservation(Base):
    __tablename__ = "budget_reservations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.tool_calls.id"), unique=True)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    output_bytes: Mapped[int] = mapped_column(Integer, default=0)


class Outbox(Base):
    __tablename__ = "execution_outbox"
    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("huntweave.tool_calls.id"), primary_key=True
    )
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.tool_calls.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EventCursor(Base):
    __tablename__ = "event_cursors"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("run_id", "source_event_id"), {"schema": "huntweave"})
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_event_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InterruptionRecord(Base):
    __tablename__ = "interruption_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    recovery_condition: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
