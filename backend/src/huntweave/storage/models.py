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


class LoginBucket(Base):
    __tablename__ = "login_buckets"
    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)


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
    __tablename__ = "research_tasks"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
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
    __tablename__ = "decisions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.runs.id"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("huntweave.agent_sessions.id"))
    step: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)


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
