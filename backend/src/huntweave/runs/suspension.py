"""Suspending a Run: one place that decides what "this Run stops here" means.

Seven paths end a Run's forward progress — an expired authorization window, an exhausted
budget, a plan aimed outside the snapshot, a failed or cancelled call, incomplete evidence,
and an outcome nobody could confirm. Each used to write the same four lines by hand and then
record why, so the contract had no owner: a new reason path could set the status without
blocking the task, or without stating the condition under which the Run may move again.

That contract is what this module owns. Callers still decide *whether* to suspend and *why*;
this module decides what a suspension consists of, and records it in the ordering the Run's
own facts require.

The ordering is part of the interface, not an implementation detail. The interruption record
is skipped when the Run already carries the same reason code, and
`OrchestrationService._converge` can run immediately after a control request — so the record
has to be written here, while the caller is still composing the transaction, and never
deferred to a later pass that would find the reason already set and record nothing.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from huntweave.runs.events import append_event
from huntweave.storage.database import database_now
from huntweave.storage.models import AgentSession, InterruptionRecord, ResearchTask, Run

__all__ = ["Suspension", "suspend"]


@dataclass(frozen=True)
class Suspension:
    """Why one Run stopped, and the condition under which it may move again.

    Both fields are required: a Run that stops without stating a way forward is a Run nobody
    can resume, which is why the same four lines were copied to every reason path rather than
    being inlined once.
    """

    reason_code: str
    recovery_condition: str


def suspend(
    session: Session,
    run: Run,
    task: ResearchTask,
    agent: AgentSession,
    suspension: Suspension,
) -> None:
    """Stop a Run's forward progress, block its task and session, and record why.

    The sequence mirrors what every suspension path wrote by hand, in the order the Run's facts
    require: status and version move together (the version is what fences a human control
    request), the task and its agent session are both blocked, and the interruption is recorded
    while the Run does not yet carry this reason code.
    """
    run.status = "waiting"
    run.version += 1
    task.status = "blocked"
    agent.status = "blocked"
    if run.reason_code != suspension.reason_code:
        session.add(
            InterruptionRecord(
                run_id=run.id,
                reason_code=suspension.reason_code,
                recovery_condition=suspension.recovery_condition,
                created_at=database_now(session),
            )
        )
        append_event(
            session,
            run.id,
            "interrupted",
            {
                "reason_code": suspension.reason_code,
                "recovery_condition": suspension.recovery_condition,
            },
        )
    run.reason_code = suspension.reason_code
