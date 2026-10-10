"""The control plane's side of selective retention: the trace a decision leaves behind.

Retention itself is the execution side's: it owns the artifacts, the capacity and the removals, and
it is the only side that may say what was really deleted. What the control plane adds here is the
part the execution side cannot know — which operator asked, and how that decision reaches the Runs
it concerns. Each recorded decision also lands on the timeline of every affected Run, so a later
review of that Run sees that its reproduction material was pinned or removed, by whom, and why.
"""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from huntweave.contracts.retention import RetentionDecisionView
from huntweave.runs.events import append_event
from huntweave.storage.database import database_now
from huntweave.storage.models import RetentionDecisionRecord, Run

__all__ = ["RetentionService"]


class RetentionService:
    """Records the operator's retention decisions; it never performs one."""

    def __init__(self, engine: Callable[[], Engine]):
        self.engine = engine

    def record(
        self, decision: RetentionDecisionView, operator_session_id: UUID, note: str = ""
    ) -> None:
        """Keep one decision, and put it on the timeline of every Run it concerns.

        The identifier is the execution side's, so recording the same decision twice (a retried
        request, a lost response) is a no-op rather than a second entry. The note is the operator's
        own words as this process received them — that is a business-side fact, so it is not read
        back from the execution side's echo. A Run named by the decision but unknown to this
        database is skipped: an event is not worth inventing a Run for.
        """
        with Session(self.engine()) as session, session.begin():
            if session.get(RetentionDecisionRecord, decision.decision_id) is not None:
                return
            session.add(
                RetentionDecisionRecord(
                    decision_id=decision.decision_id,
                    action=decision.action,
                    target_kind=decision.target_kind,
                    target=decision.target,
                    operator_session_id=operator_session_id,
                    note=note or decision.note or "",
                    reason_code=decision.reason_code,
                    affected_runs=[str(value) for value in decision.affected_runs],
                    deleted_artifacts=[str(value) for value in decision.deleted_artifacts],
                    failed=list(decision.failed),
                    freed_bytes=decision.freed_bytes,
                    created_at=database_now(session),
                )
            )
            for run_id in decision.affected_runs:
                run = session.get(Run, run_id)
                if run is None:
                    continue
                append_event(
                    session,
                    run.id,
                    "retention_decision",
                    {
                        "decision_id": str(decision.decision_id),
                        "action": decision.action,
                        "target_kind": decision.target_kind,
                        "target": decision.target,
                        "note": note or decision.note or "",
                        "reason_code": decision.reason_code,
                        "operator_session_id": str(operator_session_id),
                        "deleted_artifacts": [
                            str(value) for value in decision.deleted_artifacts
                        ],
                        "freed_bytes": decision.freed_bytes,
                    },
                    source=f"{decision.decision_id}:retention",
                )
