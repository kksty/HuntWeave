"""Suspending a Run, and recording why.

Six reason paths stop a Run's forward progress and state the condition under which it may move
again. Five of them also block the research task and its agent session, because the work they were
doing cannot continue; the authorization-window path does not, because there the Run is waiting for
a new authorization rather than for this task. All six record the interruption, and this module owns
both the state change and the record, so a new reason path cannot set the status without stating a
recovery condition, and cannot state one without it being committed.

The interruption record is written here, inside the caller's transaction, and the ordering is part
of the interface rather than an implementation detail: the record is skipped when the Run already
carries the same reason code, and `OrchestrationService._converge` can run immediately after a
control request — so a caller that deferred the record to a later pass would find the reason
already set and write nothing.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from huntweave.runs.events import append_event
from huntweave.storage.database import database_now
from huntweave.storage.models import AgentSession, InterruptionRecord, ResearchTask, Run

__all__ = ["Suspension", "record_interruption", "suspend"]


@dataclass(frozen=True)
class Suspension:
    """Why one Run stopped, and the condition under which it may move again.

    Both fields are required: a Run that stops without stating a way forward is a Run nobody can
    resume.
    """

    reason_code: str
    recovery_condition: str


def record_interruption(session: Session, run: Run, reason: str, condition: str) -> None:
    """State why a Run cannot go on, once.

    A Run already carrying this reason code is left alone, so a repeated pass records nothing
    rather than a second identical interruption. The caller owns the transaction: this never
    commits, and it must be called while the Run does not yet carry the reason code.
    """
    if run.reason_code == reason:
        return
    session.add(
        InterruptionRecord(
            run_id=run.id,
            reason_code=reason,
            recovery_condition=condition,
            created_at=database_now(session),
        )
    )
    append_event(
        session,
        run.id,
        "interrupted",
        {"reason_code": reason, "recovery_condition": condition},
    )
    run.reason_code = reason


def suspend(
    session: Session,
    run: Run,
    suspension: Suspension,
    *,
    task: ResearchTask | None = None,
    agent: AgentSession | None = None,
) -> None:
    """Stop a Run's forward progress, block the work it was doing, and record why.

    ``task`` and ``agent`` are optional because they are two different facts. A Run that stops
    because the work it was doing cannot continue blocks that work; a Run that stops because its
    authorization window closed is waiting for a new authorization, and blocking the task would
    claim something the authorization's absence does not. Pass them together or not at all.

    ``run.version`` moves with ``run.status``, in one place, because the two are one state change:
    the version is what a human control request is compared against, so a transition that left it
    behind would let a stale request land on a Run that already changed.
    """
    if (task is None) != (agent is None):
        raise ValueError("A suspension blocks the task and its session together, or neither")
    run.status = "waiting"
    run.version += 1
    blocked_task, blocked_agent = task, agent
    if blocked_task is not None and blocked_agent is not None:
        blocked_task.status = "blocked"
        blocked_agent.status = "blocked"
    record_interruption(session, run, suspension.reason_code, suspension.recovery_condition)
