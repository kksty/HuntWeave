"""Event retention: aging out the *timeline* without touching what the Run did.

`PROJECT.md` section 12.1 requires that a cursor past its retention period returns an explicit gap
and that the client takes a state snapshot, rather than the platform pretending the catch-up was
complete. Two things have to be true for that to be honest:

1. **The authority is elsewhere.** What a Run ran, on which target, with which result and which
   archived evidence is held by the versioned business rows (`runs`, `research_tasks`,
   `agent_sessions`, `decisions`, `tool_calls`, `tool_results`, `evidence`) and by the execution
   side's archive. `audit_events` is the incremental projection and the catch-up cursor (`0006`
   section 9), so pruning it cannot be the act that destroys the record of an action. The prune
   deletes from exactly one table and moves one watermark, and the checks assert that property
   rather than trusting this comment.
2. **The gap is stated.** The watermark a prune writes is what lets a reader with an old
   `Last-Event-ID` be told "those events are gone, reload the snapshot" instead of "here is the
   oldest event I still have" — which reads as the beginning of the timeline.

Retention of the timeline is separate from retention of the execution side's artifacts: a pinned
tool version, a retained private workspace and a piece of evidence each have their own policy, their
own pins and their own reasons (`0002` section 3.8, `execution/retention.py`). Nothing here unpins,
deletes or reclaims any of them, and nothing here frees the bytes they occupy.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.runs.events import committed_cursor, prune_events, retained_from_cursor
from huntweave.storage.models import AuditEvent, Run

__all__ = ["AUTHORITATIVE_SOURCES", "EventRetentionOutcome", "EventRetentionService"]

#: What answers "what did this Run do" when the event log has been pruned. Named in the response so
#: an operator reading a `retained_from` watermark can see which records to look at instead.
AUTHORITATIVE_SOURCES = [
    "huntweave.runs",
    "huntweave.research_tasks",
    "huntweave.decisions",
    "huntweave.tool_calls",
    "huntweave.tool_results",
    "huntweave.evidence",
    "execution_archive",
]


@dataclass(frozen=True)
class EventRetentionOutcome:
    """A Run's timeline retention state after a pass: cursors, row counts, and the policy applied.

    `pruned_events` counts what this pass actually removed rather than what the policy would allow,
    so "nothing was old enough" and "the policy is off" stay apart: the first is a timeline that has
    no gap, the second is a deployment that never prunes.
    """

    run_id: UUID
    committed: int
    retained_from: int
    retained_events: int
    pruned_events: int
    keep_events: int
    pruned: bool


class EventRetentionService:
    """Applies the deployment's timeline retention to one Run, and reports what it did.

    The service is deliberately small: it decides *how far* a prune may go, and the mechanics live
    in `runs/events.py`, which is the only writer of the Run event cursor. A second implementation
    of "remove old events" with its own lock or its own watermark would be a second answer to "where
    does this Run's timeline start", and a reader could not tell which one it was given.
    """

    def __init__(self, engine: Callable[[], Engine]) -> None:
        self.engine = engine

    def state(self, run_id: UUID, keep_events: int) -> EventRetentionOutcome:
        """The retention facts for one Run, without changing anything."""
        with Session(self.engine()) as session:
            self._require_run(session, run_id)
            return self._outcome(session, run_id, keep_events, pruned=False, pruned_events=0)

    def prune(self, run_id: UUID, keep_events: int) -> EventRetentionOutcome:
        """Remove this Run's events older than its newest ``keep_events``, if the policy says to.

        ``keep_events == 0`` is "do not prune", and it is the deployment default: a platform that
        has not chosen a timeline lifetime keeps the whole timeline, and no gap can be produced by
        inaction. A negative value is refused rather than read as zero, because "keep a negative
        number" is a configuration mistake, and treating it as "prune everything" would delete the
        timeline of every Run.
        """
        if keep_events < 0:
            raise ServiceError("invalid_event_cursor", 422)
        with Session(self.engine()) as session, session.begin():
            self._require_run(session, run_id)
            committed = committed_cursor(session, run_id)
            before = retained_from_cursor(session, run_id)
            if keep_events == 0 or committed - before <= keep_events:
                # Nothing is old enough: the watermark is untouched, so an old cursor is still
                # servable and no reader is told to resync over a timeline that lost nothing.
                return self._outcome(session, run_id, keep_events, pruned=False, pruned_events=0)
            watermark = prune_events(session, run_id, committed - keep_events + 1)
            removed = watermark - before
            return self._outcome(
                session, run_id, keep_events, pruned=removed > 0, pruned_events=removed
            )

    @staticmethod
    def _require_run(session: Session, run_id: UUID) -> None:
        if session.get(Run, run_id) is None:
            raise ServiceError("run_not_found", 404)

    @staticmethod
    def _outcome(
        session: Session,
        run_id: UUID,
        keep_events: int,
        *,
        pruned: bool,
        pruned_events: int,
    ) -> EventRetentionOutcome:
        floor = retained_from_cursor(session, run_id)
        retained = (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.run_id == run_id, AuditEvent.cursor > floor)
            )
            or 0
        )
        return EventRetentionOutcome(
            run_id=run_id,
            committed=committed_cursor(session, run_id),
            retained_from=floor,
            retained_events=int(retained),
            pruned_events=pruned_events,
            keep_events=keep_events,
            pruned=pruned,
        )


def as_view(outcome: EventRetentionOutcome) -> dict[str, Any]:
    """The retention state as the API contract states it, with both authorities named.

    A disabled policy still reports a positive `keep_events`, because the field means "the newest N
    events are never pruned" and that is true of every timeline that is not pruned at all; whether
    the policy is *on* travels separately in `policy_enabled`, so "nothing was pruned because
    nothing was old" and "nothing was pruned because pruning is off" cannot be confused.
    """
    return {
        "run_id": outcome.run_id,
        "committed": outcome.committed,
        "retained_from": outcome.retained_from,
        "retained_events": outcome.retained_events,
        "pruned_events": outcome.pruned_events,
        "policy_enabled": outcome.keep_events > 0,
        "keep_events": max(1, outcome.keep_events),
        "events_are_authority": False,
        "authoritative_sources": AUTHORITATIVE_SOURCES,
    }
