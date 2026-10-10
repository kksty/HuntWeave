"""Appending one event to a Run's own committed order, and reading that order back honestly.

Two services write Run events — the orchestration service (research steps, dispatch, controls) and
the retention service (the operator's decisions about a Run's reproduction material). They must
agree on the protocol, because a stream a reader replays is only meaningful if every writer advanced
the same cursor under the same lock:

* the cursor row is locked and advanced inside the caller's transaction, so the order a reader sees
  is the commit order;
* an optional source identifier makes a repeat a no-op, which is how a retried request or a
  redelivered intent stays one event rather than two.

The lock is taken *before* the source is looked up, and the cursor row is created with
``ON CONFLICT DO NOTHING``. Both matter for one reason: two wakes for the same work can arrive at
the same moment (a retried request, a redelivered intent, a replayed graph node), and a dedupe that
reads before it locks is a race in which both writers find nothing and one of them then fails on the
unique constraint. Serialising first turns that into what it should be — the second writer sees the
first writer's committed event and does nothing.

Three positions have to stay apart, and this module is where they are defined for the whole platform
(PROJECT.md section 12.1, 0006 section 9, ADR-0015):

``committed``
    The highest cursor any transaction has *claimed*. A transaction that has taken the Run's cursor
    lock and written an event has already advanced this counter, and the row may still be
    uncommitted. It is a claim about ordering, not a statement that a reader can fetch the event.
``published``
    The highest cursor a *fresh* reader can actually continue through. Because the cursor row is
    locked for the writing transaction's whole life, nobody takes the next cursor until that
    transaction commits, so what a reader may continue through is not simply ``committed``.
``retained_from``
    The highest cursor whose events retention has removed. Everything at or below it is *gone*, and
    the platform says so instead of answering "the beginning of time".

A writer cannot commit out of order: to take cursor ``n+1`` it must first take the cursor row's
lock, which the writer of ``n`` holds until it commits or rolls back. What that does **not**
guarantee is that "everything below ``committed`` is readable *now*": the writer of ``n`` may still
be mid-claim, so ``committed`` can legitimately be ahead of ``published``, and a reader must wait
rather than be handed the events beyond the open claim. `page_events` therefore returns the
*contiguous* run of cursors that actually exist — walking them one by one and stopping at the first
hole — and names that run's end in ``published``. Should a position ever be missing from under a
committed timeline (a cursor reissued by hand, a partial restore of the event table), a reader is
refused with `event_cursor_ahead` for that position rather than being walked over it; a check that
only compared "highest committed cursor" numbers could not see the difference.

Retention is a policy about the timeline, never about the business record: pruning removes rows from
`audit_events` and moves the watermark, and touches nothing else. Whether a Run did something is
answered by its versioned business rows and its archived evidence — which is why the prune here
cannot be the thing that destroys the record of an action, only the thing that ages out the
incremental projection of it.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.storage.database import database_now
from huntweave.storage.models import AuditEvent, EventCursor

__all__ = [
    "EventWindow",
    "append_event",
    "committed_cursor",
    "page_events",
    "prune_events",
    "retained_from_cursor",
]


def append_event(
    session: Session,
    run_id: UUID,
    kind: str,
    payload: dict[str, Any],
    source: str | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Append one event to a Run's timeline, or do nothing if that source already landed.

    Precondition: the caller's transaction must already hold the Run — it created it, or locked it.
    `event_cursors.run_id` has a foreign key to `runs.id`, so a Run that does not exist in this
    transaction fails the insert below with a foreign-key violation instead of quietly opening a
    cursor for a Run nobody has. That is deliberate: an event belongs to a Run, and there is no such
    thing as an event for a Run the database does not have.

    This is the only write path to the Run event cursor (registered in
    `docs/specs/0010-phase0-source-inventory.md` section 3.5): every writer goes through it so that
    one lock owns the order.

    **The lock comes before the lookup.** The cursor row is created with
    `ON CONFLICT (run_id) DO NOTHING` and locked *before* the source is looked up, because two wakes
    for one piece of work can arrive together and a dedupe that reads before it locks is a race.

    ``occurred_at`` is *when the execution side observed the thing*, for an event that reaches the
    platform late. It is stored as the event's own time, and it is deliberately **not** the ordering
    key: the new event still takes the next publish cursor, so a client that has already passed that
    position is never handed an event inserted behind it. PROJECT.md section 12.1 states this as
    "a late event gets a new publish sequence and its original occurrence time", and ADR-0015 keeps
    the two apart so a cross-instance clock difference cannot decide the timeline's order.
    """
    session.execute(
        text(
            "INSERT INTO huntweave.event_cursors (run_id, cursor) VALUES (:run_id, 0) "
            "ON CONFLICT (run_id) DO NOTHING"
        ),
        {"run_id": run_id},
    )
    cursor = session.get(EventCursor, run_id, with_for_update=True, populate_existing=True)
    if cursor is None:  # pragma: no cover - the insert above makes the row observable
        raise RuntimeError("Event cursor row is unavailable for this Run")
    if source and session.scalar(
        select(AuditEvent.cursor).where(
            AuditEvent.run_id == run_id, AuditEvent.source_event_id == source
        )
    ) is not None:
        return
    cursor.cursor += 1
    stored = payload
    if occurred_at is not None:
        # The event's own statement of when it happened travels *inside the payload*. The row keeps
        # `created_at` = the moment the platform wrote it, which is what that column has always
        # meant; overwriting it with the occurrence time would delete the only record of how late
        # the event arrived, and would make a late event indistinguishable from a punctual one. The
        # publish cursor above stays the only ordering fact either way.
        stored = {**payload, "occurred_at": occurred_at.isoformat()}
    session.add(
        AuditEvent(
            run_id=run_id,
            cursor=cursor.cursor,
            type=kind,
            payload=stored,
            source_event_id=source,
            created_at=database_now(session),
        )
    )
    session.flush()


def committed_cursor(session: Session, run_id: UUID) -> int:
    """The highest cursor claimed for this Run, committed or not.

    Read straight off the cursor row, which is what makes it a claim about ordering: a writer that
    holds the row lock has already moved it.
    """
    cursor = session.get(EventCursor, run_id)
    return cursor.cursor if cursor else 0


def retained_from_cursor(session: Session, run_id: UUID) -> int:
    """The highest cursor whose events retention has removed; 0 means "nothing was pruned"."""
    cursor = session.get(EventCursor, run_id)
    return cursor.retained_from_cursor if cursor else 0


def _contiguous_published(
    session: Session, run_id: UUID, after: int, limit: int, floor: int
) -> tuple[int, list[int], bool]:
    """The contiguous run of cursors from ``max(after, floor)``, how far it reaches, and whether the
    cursor the caller asked from is itself a position the timeline has.

    The timeline is only readable up to the first missing cursor, so the walk stops there. Walking
    one row at a time is affordable — the walk is bounded by the page the caller asked for — and it
    is the only way to answer the question the contract actually asks: "can a client continue from
    here without missing anything?", rather than the weaker "is anything ahead?".

    The walk starts at ``max(after, floor)`` because a pruned region genuinely does not contain the
    predecessors of its first survivor; inside the retained region, a missing cursor is a hole.
    """
    start = max(after, floor)
    published = start
    cursors: list[int] = []
    rows = session.scalars(
        select(AuditEvent.cursor)
        .where(AuditEvent.run_id == run_id, AuditEvent.cursor > start)
        .order_by(AuditEvent.cursor)
        # The walk reads at most one cursor past the page it may return. The bound is applied in
        # Python rather than as a parameterized `LIMIT`, so it holds whatever the driver does with
        # that clause: `limit` events may be returned, the (limit + 1)-th is read only to decide
        # whether the run continues.
        .limit(limit + 1)
    )
    for cursor in rows:
        if cursor != published + 1 or len(cursors) == limit:
            break
        published = cursor
        cursors.append(cursor)
    # The cursor the client asked from is a position the timeline has. At or below the retained
    # floor that is true by construction — those cursors were pruned, which `page_events` already
    # answered with the gap code — and inside the retained region it is true exactly when the
    # contiguous run from `after` reaches `after + 1`. One statement, so the two readings cannot
    # drift apart.
    exists = after <= floor or (cursors and cursors[0] == start + 1)
    return published, cursors, bool(exists)


@dataclass(frozen=True)
class EventWindow:
    """One page of a Run's timeline, with the three positions a reader needs to place it.

    ``committed``, ``published`` and ``retained_from`` are three different facts about the same
    cursor, and a client that receives only one of them cannot tell "caught up" from "truncated"
    from "you are asking ahead of the writer". They travel together for that reason.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    committed: int = 0
    published: int = 0
    retained_from: int = 0
    gap: bool = False
    resync_required: bool = False
    reload_reason: str | None = None

    @property
    def next_cursor(self) -> int:
        """Where a client continues from: the last event it may trust, or how far it may trust."""
        return self.events[-1]["cursor"] if self.events else self.published


def page_events(
    session: Session,
    run_id: UUID,
    after: int = 0,
    limit: int = 100,
    retained_from: int | None = None,
) -> EventWindow:
    """Read the committable events after ``after``, or state why that cursor cannot be served.

    ``after < retained_from`` is a *gap*, not a zero-length page: the events exist in no table any
    more, so the only honest answers are "reload the state snapshot and continue from its cursor"
    and a reason code a client can branch on. Returning the oldest surviving event instead would
    silently present a truncated timeline as a complete one, which is the exact failure PROJECT.md
    section 12.1 forbids.

    ``after > committed`` and ``after`` inside a rolled-back hole are both ahead of anything that
    exists, so both are `event_cursor_ahead`; ``after < retained_from`` is the one case where the
    events *did* exist and were pruned, so it gets its own code.
    """
    if after < 0 or not 1 <= limit <= 500:
        raise ServiceError("invalid_event_cursor", 422)
    floor = retained_from_cursor(session, run_id) if retained_from is None else retained_from
    committed = committed_cursor(session, run_id)
    if after < floor:
        raise ServiceError("event_cursor_expired", 409)
    if after > committed:
        raise ServiceError("event_cursor_ahead", 409)
    published, cursors, exists = _contiguous_published(session, run_id, after, limit, floor)
    if after > floor and not exists:
        # Nothing exists at the cursor the client asked from, yet the cursor is below `committed`:
        # a writer claimed it and its event never landed (a reissued cursor, a partial restore of
        # the event table). Answering with the events *beyond* the hole would hand the client a
        # timeline with a silent break in it, so the answer is the same as for a cursor ahead of
        # the timeline: this position is not one this timeline can continue from.
        raise ServiceError("event_cursor_ahead", 409)
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.run_id == run_id, AuditEvent.cursor.in_(cursors[:limit]))
        .order_by(AuditEvent.cursor)
    )
    events = [
        {
            "cursor": x.cursor,
            "type": x.type,
            "payload": x.payload,
            "created_at": x.created_at.isoformat(),
        }
        for x in rows
    ]
    # `published` is read from the rows themselves, so a writer that committed between the counter
    # read above and the walk can leave it *above* that earlier reading. Reporting
    # `published > committed` would state a contradiction inside one response ("you can continue
    # further than anything is claimed"), so the counter is raised to what the walk really saw: the
    # counter is at or above every committed row, and never below them.
    committed = max(committed, published)
    return EventWindow(
        events=events,
        committed=committed,
        # What a client may continue from is what it can actually fetch, not the highest claimed
        # cursor: the two are equal except while a writer holds the cursor lock or after a rollback.
        published=published,
        retained_from=floor,
    )


def prune_events(session: Session, run_id: UUID, keep_from: int) -> int:
    """Remove this Run's events below ``keep_from`` and record how far the timeline was pruned.

    Returns the new watermark. Nothing outside `audit_events` and the Run's cursor row is touched:
    the Run, its tasks, decisions, calls, results and evidence index are the authoritative record of
    what happened, and retention of the *timeline* has no mandate over them. The evidence bytes are
    the execution side's archive and are equally untouched (they are reclaimed, if at all, by the
    execution side's own retention policy with its own pins and reasons).

    The prune leaves a mark in the timeline itself: an `event_retention_applied` event names the
    cursors that are now gone. That mark is an ordinary event of this timeline and ages out with it
    — the durable record of how far pruning went is the Run's ``retained_from_cursor`` column, and
    the mark is what a reader of the timeline sees without having to read that column.
    """
    cursor = session.get(EventCursor, run_id, with_for_update=True)
    if cursor is None:
        return 0
    if keep_from <= 0 or keep_from > cursor.cursor:
        raise ServiceError("invalid_event_cursor", 422)
    pruned = list(
        session.scalars(
            delete(AuditEvent)
            .where(AuditEvent.run_id == run_id, AuditEvent.cursor < keep_from)
            .returning(AuditEvent.cursor)
        )
    )
    if not pruned:
        # Nothing was old enough: the watermark stays where it was. Advancing it here would claim a
        # gap in a timeline that has none, and every reader below it would be told to resync.
        return cursor.retained_from_cursor
    previous = cursor.retained_from_cursor
    cursor.retained_from_cursor = max(previous, max(pruned))
    append_event(
        session,
        run_id,
        "event_retention_applied",
        {
            "pruned_before_cursor": keep_from,
            "retained_from_cursor": cursor.retained_from_cursor,
            "previously_retained_from_cursor": previous,
            "pruned_events": len(pruned),
            "authoritative_source": "versioned_record",
        },
    )
    return cursor.retained_from_cursor
