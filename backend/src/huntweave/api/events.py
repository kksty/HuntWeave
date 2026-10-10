"""The Run event stream: the history a client resumes from, and the SSE stream that continues it.

This module ships as a router *factory* so that route assembly stays in one pair of hands
(`docs/agents/p2-execution-batches.md` section 4): `api/app.py` gains two lines — an import and one
`include_router` call — and nothing else here depends on how that file is organized. Everything the
router needs is built from the engine accessor and the same `AppSettings` the app already has.

Three properties of the P1 stream are kept exactly as they were, because they are the contract the
console already reads: the frames are `id: <cursor>` / `event: audit` / `data: <json>`, the
heartbeat is `event: heartbeat` every five seconds carrying the platform's real capability mode, and
the stream re-reads the session periodically so a revoked session stops being served.

What is added is what a client needs to tell four situations apart that used to look identical:

* the cursor is *current* — a page, possibly empty, with `published == committed`;
* the cursor is *behind a writer* that still holds the Run's cursor lock — a page with
  `published < committed`, and the client waits rather than resyncing;
* the cursor is *inside a pruned region* — `event_cursor_expired` (409) on the history read, so
  the client takes the state snapshot again instead of being handed the oldest survivor as if it
  were the beginning of the timeline;
* the *session* is gone — a terminal `session_expired` frame whose reason code says whether the
  token was absent, expired, revoked, or replaced by an access-key rotation. A stream that merely
  went quiet would be read as a network blip, and the console would keep showing a timeline nobody
  may read.

The stream also opens with a `retention` frame carrying the Run's coordinate (`snapshot_cursor`,
`committed`, `published`, `retained_from`) *before* the first increment. That is what makes the
snapshot-plus-increment protocol legible at the connection: a client whose snapshot cursor is behind
`retained_from` learns immediately that the increments about to arrive do not continue its snapshot,
instead of merging them and rendering a timeline with a silent hole.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from huntweave.access.service import AccessService
from huntweave.config import AppSettings, event_retention_keep
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.event_stream import EventHistoryView, EventRetentionView
from huntweave.runs.event_retention import EventRetentionService, as_view
from huntweave.runs.orchestration import OrchestrationService

__all__ = ["create_events_router", "stream_probe"]

#: How often an open stream re-reads its session. PROJECT.md section 14.1 asks that session
#: revocation be reflected in the open stream within a stated limit; the P1 stream re-reads every 15
#: seconds, which is inside the 60-second bound the sandbox profile fixes for revocation
#: (`execution/sandboxprofile.py:473`), so the bound holds with room to spare rather than by luck.
STREAM_AUTH_INTERVAL_SECONDS = 15

#: The longest a revoked session may keep receiving events on an already-open stream. Stated here as
#: the number the checks assert against `STREAM_AUTH_INTERVAL_SECONDS`.
STREAM_REVOCATION_LIMIT_SECONDS = 60

#: How often a page of events is read while the stream is idle.
STREAM_PAGE_SECONDS = 0.5

#: How often the heartbeat frame is emitted (PROJECT.md section 12.1: heartbeat <= 5s).
STREAM_HEARTBEAT_SECONDS = 5.0

#: The page size one stream poll reads.
STREAM_PAGE_SIZE = 100


def stream_probe(
    database: Callable[[], Engine], settings: AppSettings, token: str | None
) -> tuple[str, dict[str, Any]]:
    """Whether an open stream's session may still be served, and why not when it may not.

    This is the seam a check can drive without a live socket: the stream is asynchronous and sleeps
    between polls, but *this* decision is a synchronous read of the session table and it is the
    whole of the stream's authorization logic. A missing token stays distinct from a dead session:
    the first means the stream never had a credential, the second means the credential stopped being
    valid, and an operator acts differently on each.
    """
    if token is None:
        # The stream never had a credential: the same refusal the app's middleware gives a request
        # with no session, so a client cannot read "no cookie" as a transient problem.
        return "authentication_required", {"status_code": 401}
    try:
        session = AccessService(database, settings).authenticate(token)
    except ServiceError as refusal:
        return refusal.reason_code, {"status_code": refusal.status_code}
    except SQLAlchemyError:
        # Storage, not identity: reporting `authentication_required` here would log an operator out
        # of a platform whose database merely blinked.
        return "storage_unavailable", {}
    return "ok", {
        "idle_expires_at": session.idle_expires_at.isoformat(),
        "absolute_expires_at": session.absolute_expires_at.isoformat(),
    }


def create_events_router(
    settings: AppSettings,
    database: Callable[[], Engine],
    mode_reader: Callable[[], Any] | None = None,
) -> APIRouter:
    """Build the Run timeline router from the app's own engine accessor and settings."""
    router = APIRouter()
    orchestration = OrchestrationService(database)
    retention = EventRetentionService(database)
    probe = mode_reader or (lambda: None)

    @router.get("/api/v1/runs/{run_id}/event-history")
    def event_history(
        run_id: Annotated[UUID, Path()],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventHistoryView:
        """The contiguous page after `after`, with the positions that say what to do with it.

        A pruned cursor is refused rather than answered: the events are in no table any more, so a
        page starting at the oldest survivor would present a truncated timeline as a complete one
        (PROJECT.md section 12.1). The refusal carries its own reason code, so a client branches on
        "reload the snapshot" instead of parsing a sentence.
        """
        return EventHistoryView.model_validate(orchestration.event_history(run_id, after, limit))

    @router.get("/api/v1/runs/{run_id}/event-retention")
    def event_retention(
        run_id: Annotated[UUID, Path()],
        keep: Annotated[int, Query(ge=0)] | None = None,
    ) -> EventRetentionView:
        """What this Run's timeline retention holds, what it removed, and what is authoritative.

        Read-only, and stated together with the policy, so the answer is the platform's real
        configuration rather than a default restated in a document.
        """
        policy = event_retention_keep() if keep is None else keep
        return EventRetentionView.model_validate(as_view(retention.state(run_id, policy)))

    @router.post("/api/v1/runs/{run_id}/event-retention/prune")
    def prune_event_retention(
        run_id: Annotated[UUID, Path()],
        keep: Annotated[int, Query(ge=0)] | None = None,
    ) -> EventRetentionView:
        """Apply the timeline retention policy to one Run, and report exactly what it removed.

        Separate from the read so that "what would be removed" and "what was removed" are different
        requests: an operator can look before acting, and the write leaves an
        `event_retention_applied` event naming the cursors that are now gone.
        """
        policy = event_retention_keep() if keep is None else keep
        return EventRetentionView.model_validate(as_view(retention.prune(run_id, policy)))

    @router.get("/api/v1/runs/{run_id}/events")
    async def events(
        run_id: Annotated[UUID, Path()],
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """The SSE stream of a Run's timeline, resumable from `after` or from `Last-Event-ID`.

        The cursor is validated *before* the response headers are sent, so a malformed or expired
        cursor is an ordinary HTTP refusal the client can read rather than a stream that opens and
        then dies. `Last-Event-ID` wins over the query parameter when both are present: that header
        is what a browser's `EventSource` re-sends on its own reconnect, and honouring the query
        parameter instead would silently replay from a cursor the client had already moved past.
        """
        if last_event_id is not None:
            try:
                after = int(last_event_id)
            except ValueError:
                raise ServiceError("invalid_event_cursor", 422) from None
        # Validate before the first byte: this refuses a cursor that cannot be served, and reads the
        # coordinate the opening frame reports.
        opening = orchestration.event_stream_opening(run_id, after)
        token = request.cookies.get(settings.cookie_name)

        async def stream() -> AsyncIterator[str]:
            cursor = after
            next_auth = 0.0
            next_heartbeat = 0.0
            yield _frame("retention", opening, event_id=opening["published"])
            while not await request.is_disconnected():
                try:
                    if time.monotonic() >= next_auth:
                        reason, detail = await run_in_threadpool(
                            stream_probe, database, settings, token
                        )
                        if reason != "ok":
                            yield _frame("session_expired", {"reason_code": reason, **detail})
                            return
                        next_auth = time.monotonic() + STREAM_AUTH_INTERVAL_SECONDS
                    page = await run_in_threadpool(
                        orchestration.event_history, run_id, cursor, STREAM_PAGE_SIZE
                    )
                    for event in page["events"]:
                        cursor = event["cursor"]
                        yield _audit_frame(cursor, event)
                    if time.monotonic() >= next_heartbeat:
                        # The heartbeat carries the mode the platform is really in, not a fixed
                        # demonstration claim; the observation is read off the event loop.
                        mode = getattr(await run_in_threadpool(probe), "mode", None)
                        yield f'event: heartbeat\ndata: {{"mode":"{mode}"}}\n\n'
                        next_heartbeat = time.monotonic() + STREAM_HEARTBEAT_SECONDS
                except ServiceError as refusal:
                    # The cursor left the retained timeline while the stream was open: the client
                    # gets a terminal frame naming the reason and the floor, so it reloads the
                    # snapshot instead of waiting for events that will never arrive.
                    yield _frame(
                        "resync_required",
                        {
                            "reason_code": refusal.reason_code,
                            "retained_from": await run_in_threadpool(
                                orchestration.retained_from, run_id
                            ),
                        },
                    )
                    return
                except SQLAlchemyError:
                    yield _frame("storage_unavailable", {"reason_code": "storage_unavailable"})
                    return
                await asyncio.sleep(STREAM_PAGE_SECONDS)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return router


def _frame(name: str, payload: dict[str, Any], event_id: int | None = None) -> str:
    """One SSE frame: the same `id:`/`event:`/`data:` shape the P1 stream already used."""
    identifier = f"id: {event_id}\n" if event_id is not None else ""
    return f"{identifier}event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _audit_frame(cursor: int, event: dict[str, Any]) -> str:
    """One event frame. Kept as its own function so the P1 frame shape is stated, not assembled."""
    body = json.dumps(event, ensure_ascii=False)
    return f"id: {cursor}\nevent: audit\ndata: {body}\n\n"
