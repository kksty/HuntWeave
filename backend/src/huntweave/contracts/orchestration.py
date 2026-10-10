"""Versioned views of durable research, execution timeline and evidence resources."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from huntweave.contracts.execution import RunRuntimeView
from huntweave.contracts.runs import Contract, RunView

ReconciliationOutcome = Literal["not_executed", "executed", "undetermined"]


class TaskView(Contract):
    id: UUID
    role: str
    status: str
    step: int
    version: int
    # Which task of this role in this Run this is. A role can own more than one: two Workers, or a
    # second round of evidence, are separate tasks with separate sessions and decisions.
    ordinal: int
    lease_generation: int


class AgentSessionView(Contract):
    id: UUID
    role: str
    status: str
    context: dict[str, Any]


class DecisionView(Contract):
    id: UUID
    session_id: UUID
    # The independent task this decision was made for. A role can own more than one task in a Run,
    # so the role alone does not say which piece of work an answer belongs to.
    task_id: UUID | None = None
    step: int
    action: str
    summary: str
    expected: str
    stop_condition: str
    evidence_ids: list[UUID]


class ModelUsageView(Contract):
    """What the provider said this request cost, or that it never said.

    ``known=False`` is a fact about the record, not a zero: the platform has no receipt for this
    request, and the request stays in the pending list until one is recorded. Nothing here derives
    a number the provider did not report.
    """

    known: bool
    receipt: dict[str, Any] | None = None


class PlanningAttemptView(Contract):
    """One model request: its intent, the frozen input, and what came back.

    The attempt is written before the model is called, so a slow or lost request is visible while
    it is still in flight. ``status`` is ``requested`` while the answer is outstanding, ``applied``
    once the Run committed it, ``refused`` when the answer arrived after the Run stopped or after
    the versions it was prepared against moved on, and ``abandoned`` when a later lease generation
    took the step over.

    A refused attempt keeps its ``suggestion``: the point of recording it is that the platform knows
    what the model proposed and still did not act on it.
    """

    id: UUID
    run_id: UUID
    task_id: UUID
    session_id: UUID
    role: str
    step: int
    # Which attempt at this step the id was derived from: `id` is a function of the task, the step
    # and this ordinal, so a replay and a second question about the same step stay distinguishable.
    attempt_ordinal: int = 0
    status: str
    input_hash: str | None = None
    input_watermark: int | None = None
    budget_snapshot: dict[str, Any] | None = None
    run_version: int | None = None
    task_version: int | None = None
    scope_version: int | None = None
    lease_generation: int | None = None
    prompt_version: str | None = None
    provider: str | None = None
    model: str | None = None
    request_count: int = 0
    usage: ModelUsageView
    reason_code: str | None = None
    supersedes_id: UUID | None = None
    suggestion: dict[str, Any] | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


class ModelUsageSummary(Contract):
    """The Run's model accounting, with the unknown part kept separate from the known part."""

    requests: int
    known: int
    unknown: int
    pending_attempt_ids: list[UUID]


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
    lease_active: bool | None = None
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
    # The normalized hash the ticket and the execution ledger both compare against. Shown beside
    # the command because "same parameters as the record" is a checkable claim, not a note.
    parameters_hash: str | None = None
    # The execution side's own statement of where this call acted: argv, cwd, user, instance,
    # image digests and gateways. `None` means no such statement exists for this call — an old
    # record — and the console says so rather than filling the gap from today's profile.
    runtime: dict[str, Any] | None = None
    # When it started, when it last produced output, how long it has been going, and the bound it
    # runs under. This is what replaces a progress bar.
    progress: dict[str, Any] | None = None
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
    # Every model request this Run made, including the ones that were refused or abandoned. This is
    # where "the model was asked, and this is what it answered" survives independently of whether
    # the answer was applied.
    planning: list[PlanningAttemptView]
    model_usage: ModelUsageSummary
    calls: list[ToolCallView]
    budget: BudgetView
    interruptions: list[InterruptionView]
    heartbeat_at: datetime | None
    cursor: int
    # The execution side's own account of this Run's containers and gateways, read through the app
    # and never merged into a fact the platform decided for itself. Absent when the execution side
    # cannot be asked, which the console shows as an observation gap.
    runtime: RunRuntimeView | None = None


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
    # Which side produced this evidence. A report never presents a fixed fixture as a real
    # observation, or the reverse, so the answer travels with the evidence rather than being
    # assumed from the Run the reader happens to be looking at.
    execution_profile: str = "fake-p0-v1"
    demonstration: bool = True
    offset: int
    content: str
    next_offset: int | None = None
    eof: bool | None = None
