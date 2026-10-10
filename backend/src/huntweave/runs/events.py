"""Appending one event to a Run's own committed order.

Two services write Run events — the orchestration service (research steps, dispatch, controls) and
the retention service (the operator's decisions about a Run's reproduction material). They must
agree on the protocol, because a stream a reader replays is only meaningful if every writer advanced
the same cursor under the same lock:

* the cursor row is locked and advanced inside the caller's transaction, so the order a reader sees
  is the commit order;
* an optional source identifier makes a repeat a no-op, which is how a retried request or a
  redelivered intent stays one event rather than two.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
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
    """Append one event to a Run's timeline, or do nothing if that source already landed."""
    if source and session.scalar(
        select(AuditEvent.cursor).where(
            AuditEvent.run_id == run_id, AuditEvent.source_event_id == source
        )
    ) is not None:
        return
    cursor = session.get(EventCursor, run_id, with_for_update=True)
    if cursor is None:
        cursor = EventCursor(run_id=run_id, cursor=0)
        session.add(cursor)
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
