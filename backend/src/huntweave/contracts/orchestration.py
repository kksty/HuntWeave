"""Versioned views of durable research, execution timeline and evidence resources."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from huntweave.contracts.runs import Contract, RunView

ReconciliationOutcome = Literal["not_executed", "executed", "undetermined"]


class TaskView(Contract):
    id: UUID
    role: str
    status: str
    step: int
    version: int
    lease_generation: int


class AgentSessionView(Contract):
    id: UUID
    role: str
    status: str
    context: dict[str, Any]


class DecisionView(Contract):
    id: UUID
    session_id: UUID
    step: int
    action: str
    summary: str
    expected: str
    stop_condition: str
    evidence_ids: list[UUID]


class ReconciliationVerdict(Contract):
    """An operator's evidence-bound decision on one call with an unconfirmed outcome."""

    outcome: ReconciliationOutcome
    version: int = Field(ge=1, strict=True)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)


class CallObservationView(Contract):
    """What the execution side could prove about one call, kept apart from any verdict."""

    started: bool
    process_active: bool | None
    connection_open: bool | None
    observed_at: datetime
    stop_confirmed: bool


class ReconciliationView(Contract):
    call_id: UUID
    outcome: ReconciliationOutcome
    operator_session_id: UUID
    scope_version: int
    evidence_ids: list[UUID]
    note: str
    observation: CallObservationView | None
    redispatch_authorized: bool
    recorded_at: datetime


class ToolCallView(Contract):
    id: UUID
    session_id: UUID
    decision_id: UUID
    status: str
    action: str
    parameters: dict[str, Any]
    result: dict[str, Any] | None
    evidence_ids: list[UUID]
    created_at: datetime
    replaces_call_id: UUID | None = None
    observation: CallObservationView | None = None
    reconciliation: ReconciliationView | None = None
    # What this call still asks of its Run: outcome_unsettled / stop_unconfirmed / call_pending.
    conditions: list[str] = Field(default_factory=list)


class BudgetView(Contract):
    reserved_tool_calls: int
    settled_tool_calls: int
    max_tool_calls: int
    output_bytes: int


class InterruptionView(Contract):
    reason_code: str
    recovery_condition: str
    created_at: datetime


class RunSnapshot(Contract):
    run: RunView
    tasks: list[TaskView]
    sessions: list[AgentSessionView]
    decisions: list[DecisionView]
    calls: list[ToolCallView]
    budget: BudgetView
    interruptions: list[InterruptionView]
    heartbeat_at: datetime | None
    cursor: int


class AuditEventView(Contract):
    cursor: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


class EventPage(Contract):
    events: list[AuditEventView]
    # P0 keeps every committed event, so a cursor can only be ahead of the Run, never lost.
    next_cursor: int
    gap: bool


class ReconciliationResult(Contract):
    run: RunView
    call: ToolCallView


class PendingCallView(Contract):
    id: UUID
    status: str
    # Why this call still blocks the Run: outcome_unsettled / stop_unconfirmed / call_pending.
    conditions: list[str]


class ResumePreview(Contract):
    version: int
    last_completed_step: int
    pending_calls: list[PendingCallView]
    remaining_tool_calls: int
    authorization_valid: bool
    can_resume: bool
    expected_actions: list[str]
    reason_code: str | None


class EvidenceView(Contract):
    id: UUID
    run_id: UUID
    call_id: UUID
    relative_path: str
    sha256: str
    size_bytes: int
    available: bool
    truncated: bool
    redacted: bool
    missing_reason: str | None
    demonstration: bool
    offset: int
    content: str
    next_offset: int | None = None
    eof: bool | None = None
