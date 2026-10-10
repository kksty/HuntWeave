"""Appending one event to a Run's own committed order.

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
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from huntweave.storage.database import database_now
from huntweave.storage.models import AuditEvent, EventCursor

__all__ = ["append_event"]


def append_event(
    session: Session,
    run_id: UUID,
    kind: str,
    payload: dict[str, Any],
    source: str | None = None,
) -> None:
    """Append one event to a Run's timeline, or do nothing if that source already landed.

    Precondition: the caller's transaction must already hold the Run — it created it, or locked it.
    `event_cursors.run_id` has a foreign key to `runs.id`, so a Run that does not exist in this
    transaction fails the insert below with a foreign-key violation instead of quietly opening a
    cursor for a Run nobody has. That is deliberate: an event belongs to a Run, and there is no such
    thing as an event for a Run the database does not have.
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
    session.add(
        AuditEvent(
            run_id=run_id,
            cursor=cursor.cursor,
            type=kind,
            payload=payload,
            source_event_id=source,
            created_at=database_now(session),
        )
    )
    session.flush()
