"""The same distinctions under real PostgreSQL: concurrent commits, row locks, and a real prune.

The unit checks in `tests/test_event_retention_and_resync.py` run on a throwaway SQLite file, which
is enough for cursor arithmetic and contract shapes but cannot answer the questions this slice is
actually about:

* two transactions appending to one Run **at the same time** — the row lock is what makes the
  publish order the commit order, and no in-process fake has that lock;
* an event that is written but **not committed** is invisible to a reader, so the cursor never
  passes an unpublished event (PROJECT.md section 12.1, and the "concurrent transactions finish out
  of order" row of PROJECT.md section 14.1);
* the prune is a real `DELETE ... RETURNING` under a real row lock, and the business rows and the
  evidence index survive it row for row.

They are skipped unless `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` says the database behind
`HUNTWEAVE_DATABASE_URL` may be written to, which is the same gate the other integration checks use.
Run them against a disposable project only, never against the running deployment.
"""

import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from _planning import plan_step
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from huntweave.api.events import create_events_router
from huntweave.config import AppSettings
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.event_stream import EventHistoryView, EventRetentionView
from huntweave.contracts.runs import Budget, ProjectCreate, RunCreate, ScopeCreate
from huntweave.runs.event_retention import EventRetentionService
from huntweave.runs.events import (
    append_event,
    committed_cursor,
    page_events,
    prune_events,
    retained_from_cursor,
)
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine
from huntweave.storage.models import (
    AccessKeyState,
    AgentSession,
    AuditEvent,
    AuthorizationScope,
    BudgetReservation,
    Decision,
    EventCursor,
    Evidence,
    InterruptionRecord,
    Outbox,
    Project,
    ReconciliationDecision,
    ResearchTask,
    Run,
    ToolCall,
    ToolResult,
    WebSession,
)

pytestmark = pytest.mark.integration

KEY = "event-retention-integration-key-" + "c" * 48
ORIGIN = "http://127.0.0.1:8000"
CLEARED = (
    AuditEvent,
    EventCursor,
    ToolResult,
    Outbox,
    BudgetReservation,
    Evidence,
    InterruptionRecord,
    ReconciliationDecision,
    ToolCall,
    Decision,
    AgentSession,
    ResearchTask,
    Run,
    AuthorizationScope,
    Project,
    WebSession,
    AccessKeyState,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Event retention integration checks require an explicitly disposable database")
    result = connect_engine()
    with Session(result) as session, session.begin():
        for model in CLEARED:
            session.execute(delete(model))
    yield result
    result.dispose()


def a_run(engine: Engine, *, start: bool = False) -> UUID:
    """A Run with one authorized target, created through the real service.

    It stays in ``draft`` unless ``start`` is asked for. Starting a Run appends its ``run_queued``
    event, and the event-ordering checks want the events they write to begin at cursor 1; a check
    that needs a *claimable* Run asks for ``start``, because `OrchestrationService.claim` serves
    only the Runs in `ACTIVE_RUNS` and a draft is not one of them.
    """
    runs = RunService(lambda: engine)
    project = runs.create_project(ProjectCreate(name="event retention"))
    now = datetime.now(UTC)
    scope = runs.create_scope(
        ScopeCreate(
            project_id=project.id,
            targets_text="192.0.2.1",
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            authorization="Fixed fake actions only",
            budget=Budget(max_tool_calls=4),
        )
    )
    run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), secrets.token_hex(8))
    if start:
        runs.start(run.id, run.version)
    return run.id


def write_events(engine: Engine, run_id: UUID, count: int, kind: str = "decision") -> None:
    with Session(engine) as session, session.begin():
        for index in range(count):
            append_event(session, run_id, kind, {"index": index})


def a_client(engine: Engine) -> TestClient:
    app = FastAPI()
    app.include_router(create_events_router(AppSettings(access_key=KEY), lambda: engine))

    @app.exception_handler(ServiceError)
    async def refusal(request: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse({"reason_code": error.reason_code}, status_code=error.status_code)

    return TestClient(app, base_url=ORIGIN)


def test_two_writers_cannot_invert_the_commit_order_of_one_run(engine: Engine) -> None:
    """The row lock is what makes the publish order the commit order.

    One transaction holds the Run's cursor row; the other tries to append and is *refused by the
    database* rather than allowed to take the next cursor — `lock_timeout` turns the indefinite wait
    into a bounded refusal, which is the same fact without hanging the check. Once the holder
    commits, the waiter takes the next cursor, so a reader sees one linear timeline and never cursor
    2 without cursor 1 (PROJECT.md section 12.1).
    """
    run_id = a_run(engine)
    holder = Session(engine)
    waiter = Session(engine)
    try:
        append_event(holder, run_id, "run_started", {"writer": "holder"})
        assert committed_cursor(holder, run_id) == 1
        waiter.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(OperationalError):
            # The holder still owns the cursor row, so this waits for it and is refused.
            append_event(waiter, run_id, "task_created", {"writer": "waiter"})
        waiter.rollback()
        holder.commit()
        append_event(waiter, run_id, "task_created", {"writer": "waiter"})
        waiter.commit()
        with Session(engine) as reader:
            window = page_events(reader, run_id, 0, 100)
            assert [event["cursor"] for event in window.events] == [1, 2]
            assert window.published == 2 and window.committed == 2
    finally:
        holder.rollback()
        waiter.rollback()
        holder.close()
        waiter.close()


def test_an_uncommitted_event_is_never_published_to_a_reader(engine: Engine) -> None:
    """PROJECT.md section 12.1: the cursor must not pass an event that is not yet published.

    A writer appends inside an open transaction. A separate connection reads the page and must not
    see the event, must not be told to continue past it, and must see the cursor at the last
    committed position only.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 2)
    writer = Session(engine)
    try:
        append_event(writer, run_id, "tool_planned", {"call_id": str(uuid4())})
        with Session(engine) as reader:
            window = page_events(reader, run_id, 0, 100)
            assert [event["cursor"] for event in window.events] == [1, 2]
            assert window.published == 2
            assert window.committed == 2
            assert not any("tool_planned" == event["type"] for event in window.events)
        writer.commit()
        with Session(engine) as reader:
            window = page_events(reader, run_id, 0, 100)
            assert [event["cursor"] for event in window.events] == [1, 2, 3]
            assert window.published == 3 and window.committed == 3
    finally:
        writer.rollback()
        writer.close()


def test_a_redelivered_runner_event_stays_one_event_and_keeps_the_run_order(engine: Engine) -> None:
    """A Runner that sends the same source event twice must not create a second timeline entry.

    `source_event_id` is the de-duplication key. The repeat is a no-op, so the cursor does not move
    and the event does not appear twice, while two genuinely different events from the same call
    still get their own cursors in commit order.
    """
    run_id = a_run(engine)
    call_id = uuid4()
    with Session(engine) as session, session.begin():
        append_event(
            session, run_id, "execution_started", {"call_id": str(call_id)}, "started:1"
        )
    with Session(engine) as session, session.begin():
        append_event(
            session, run_id, "execution_started", {"call_id": str(call_id)}, "started:1"
        )
    with Session(engine) as session, session.begin():
        append_event(
            session, run_id, "execution_output", {"call_id": str(call_id), "chunk": 1}, "out:1"
        )
    with Session(engine) as session:
        window = page_events(session, run_id, 0, 100)
        expected = ["execution_started", "execution_output"]
        assert [event["type"] for event in window.events] == expected
        assert [event["cursor"] for event in window.events] == [1, 2]
        assert window.committed == 2


def test_a_late_event_keeps_its_own_time_and_takes_a_new_publish_cursor(engine: Engine) -> None:
    """PROJECT.md section 12.1: a late event takes a new cursor and keeps its occurrence time.

    The event is stamped with the moment the execution side observed it, some minutes before it was
    appended. The publish cursor still moves forward, so a client that already passed that position
    is never handed an event inserted behind it; the observation time survives in the record and in
    the payload, so "when did this happen" is not rewritten as "when did we hear about it".
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 2)
    observed = datetime.now(UTC) - timedelta(minutes=7)
    with Session(engine) as session, session.begin():
        append_event(
            session,
            run_id,
            "tool_completed",
            {"call_id": str(uuid4())},
            "late:1",
            occurred_at=observed,
        )
    with Session(engine) as session:
        window = page_events(session, run_id, 0, 100)
        assert [event["cursor"] for event in window.events] == [1, 2, 3]
        late = window.events[-1]
        assert late["payload"]["occurred_at"].startswith(observed.strftime("%Y-%m-%dT%H:%M"))
        assert late["created_at"] != late["payload"]["occurred_at"]
        # A client that had already reached cursor 2 receives it; one past it would not exist.
        assert page_events(session, run_id, 2, 100).events[0]["cursor"] == 3


def test_an_expired_cursor_cannot_be_replayed_and_the_snapshot_cursor_can(engine: Engine) -> None:
    """The acceptance-criterion path: prune, then let a stale client try to catch up.

    The retry from the old cursor is refused with `event_cursor_expired` (409) — the platform does
    not pretend to backfill it. The state snapshot still reports the Run's current cursor, and
    continuing from *that* works, so the documented recovery path is the one that is actually open.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 10)
    with Session(engine) as session:
        snapshot_cursor = committed_cursor(session, run_id)
    assert snapshot_cursor == 10
    service = EventRetentionService(lambda: engine)
    outcome = service.prune(run_id, keep_events=4)
    assert outcome.pruned is True and outcome.pruned_events > 0
    pruned_from = outcome.retained_from

    client = a_client(engine)
    stale = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 1})
    assert stale.status_code == 409
    assert stale.json() == {"reason_code": "event_cursor_expired"}

    # The Run's own state is still fully readable: the snapshot is the authority, not the timeline.
    with Session(engine) as session:
        assert session.get(Run, run_id) is not None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) > 0

    # And the client that reloads and continues from the current position is served.
    with Session(engine) as session:
        resumed = page_events(session, run_id, pruned_from, 100)
    assert resumed.published >= pruned_from
    body = client.get(
        f"/api/v1/runs/{run_id}/event-history", params={"after": pruned_from}
    ).json()
    view = EventHistoryView.model_validate(body)
    assert view.retained_from == pruned_from
    assert view.resync_required is False
    assert all(event.cursor > pruned_from for event in view.events)

    retention = EventRetentionView.model_validate(
        client.get(f"/api/v1/runs/{run_id}/event-retention", params={"keep": 4}).json()
    )
    assert retention.events_are_authority is False
    assert retention.retained_from == pruned_from


def test_the_prune_leaves_every_business_record_and_the_evidence_index_standing(
    engine: Engine,
) -> None:
    """Acceptance criterion 2, checked row for row rather than argued in a comment.

    A Run with calls, results, decisions, tasks, sessions, a reservation and evidence is pruned.
    Every one of those rows is still there, unchanged and still reachable, while the old events are
    gone and the gap is stated by the watermark.
    """
    run_id = a_run(engine, start=True)
    from huntweave.runs.orchestration import OrchestrationService

    orchestration = OrchestrationService(lambda: engine)
    claimed = orchestration.claim(run_id)
    assert claimed is not None
    planned = plan_step(
        orchestration, run_id, claimed["task_id"], claimed["lease_generation"]
    )
    assert planned is not None
    with Session(engine) as session, session.begin():
        call = session.scalar(select(ToolCall).where(ToolCall.run_id == run_id))
        assert call is not None
        session.add(
            ToolResult(
                call_id=call.id,
                content={"output": "fixed", "evidence": []},
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            Evidence(
                id=uuid4(),
                run_id=run_id,
                call_id=call.id,
                metadata_json={"relative_path": "runs/x/stdout.bin", "sha256": "0" * 64},
            )
        )

    def counts() -> dict[str, int]:
        with Session(engine) as session:
            return {
                model.__tablename__: session.scalar(
                    select(func.count()).select_from(model)
                )
                or 0
                for model in (
                    Run,
                    ResearchTask,
                    AgentSession,
                    Decision,
                    ToolCall,
                    ToolResult,
                    Evidence,
                    BudgetReservation,
                )
            }

    before = counts()
    assert before["tool_calls"] == 1 and before["evidence"] == 1 and before["decisions"] >= 1
    with Session(engine) as session, session.begin():
        watermark = prune_events(session, run_id, committed_cursor(session, run_id))
    assert watermark > 0
    assert counts() == before
    with Session(engine) as session:
        assert retained_from_cursor(session, run_id) == watermark
        remaining = list(
            session.scalars(select(AuditEvent).where(AuditEvent.run_id == run_id))
        )
        # `prune_events` refuses `keep_from > committed`, so the event at the committed cursor
        # survives beside the prune's own record — but nothing older does, and it is the prune's
        # record that names the cursors which are gone.
        assert len(remaining) == 2, [event.type for event in remaining]
        mark = remaining[-1]
        assert mark.type == "event_retention_applied"
        assert mark.payload["pruned_events"] > 0
        assert mark.payload["authoritative_source"] == "versioned_record"


def test_pruning_the_whole_timeline_makes_the_only_readable_position_the_watermark(
    engine: Engine,
) -> None:
    """Nothing is left below the watermark, and the cursor at it is still a valid starting point.

    This is the degenerate case of the recovery path: every event is gone, so the client can only
    continue from the watermark itself or from the Run's current cursor. Neither is refused, and
    neither is presented as a replay of the pruned events.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 3)
    with Session(engine) as session, session.begin():
        watermark = prune_events(session, run_id, committed_cursor(session, run_id))
    with Session(engine) as session:
        for stale in range(watermark):
            with pytest.raises(ServiceError) as refusal:
                page_events(session, run_id, stale, 100)
            assert refusal.value.reason_code == "event_cursor_expired"
        window = page_events(session, run_id, watermark, 100)
        # `prune_events` refuses `keep_from > committed`, so the cursor at `committed` always
        # survives, and the prune's own `event_retention_applied` mark is appended above it. What is
        # served is therefore those two cursors — and both are strictly above the watermark, so
        # nothing at or below it is ever presented as a replay.
        assert [event["cursor"] for event in window.events] == [watermark + 1, watermark + 2]
        assert window.retained_from == watermark


def test_a_prune_is_idempotent_and_never_rewinds_the_watermark(engine: Engine) -> None:
    """Running retention twice must not delete more than the policy allows or move the mark back."""
    run_id = a_run(engine)
    write_events(engine, run_id, 8)
    service = EventRetentionService(lambda: engine)
    first = service.prune(run_id, keep_events=3)
    second = service.prune(run_id, keep_events=3)
    assert second.retained_from >= first.retained_from
    assert second.pruned_events <= 1  # only the event the first pass left as the mark can age out
    third = service.prune(run_id, keep_events=0)
    assert third.retained_from == second.retained_from
    assert third.pruned is False
    with Session(engine) as session:
        assert retained_from_cursor(session, run_id) == second.retained_from


def test_published_is_the_timelines_reach_and_not_the_pages_end(engine: Engine) -> None:
    """`published` answers "how far can a fresh reader continue", not "where did this page stop".

    A page that fills up means there is more to read. Reporting its last cursor as `published` made
    the three positions lie about a timeline that is entirely contiguous: measured on a real Run of
    600 events read 500 at a time, the opening frame said `committed=600, published=500`, which by
    this platform's own contract means "a position below `committed` is missing". Nothing was
    missing. The reach must not depend on the page size either.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 600)
    with Session(engine) as session:
        page = page_events(session, run_id, 0, 500)
        assert len(page.events) == 500
        assert page.next_cursor == 500
        assert page.committed == 600
        assert page.published == 600
        assert page.resync_required is False
    with Session(engine) as session:
        small = page_events(session, run_id, 0, 1)
        assert [event["cursor"] for event in small.events] == [1]
        assert small.next_cursor == 1
        assert small.published == 600