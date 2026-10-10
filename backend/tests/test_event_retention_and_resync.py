"""Event retention, expired cursors and resync: the distinctions a client can branch on.

`PROJECT.md` section 12.1 asks for SSE with cursor catch-up, an explicit gap when the cursor is
past its retention period, and a state snapshot to continue from; `0006` section 9 asks for a
snapshot plus a *continuous* increment with an explicit reload requirement when continuity cannot be
offered. Those are statements about what a client can tell apart, so this file checks the
distinctions that make them true:

* ``committed`` (a writer claimed a cursor) is not ``published`` (a fresh read can continue through
  it) — a reader must not walk over an event whose transaction is still open, nor over a cursor a
  rolled-back transaction left empty;
* ``retained_from`` (events were pruned) is neither of those, and a cursor below it is refused with
  its own reason code instead of being served the oldest survivor as if it were the beginning;
* the SSE stream keeps the P1 frame shape and heartbeat, opens by stating the coordinate its
  increments belong to, and says *why* it is ending when it ends;
* the timeline prune removes events and nothing else: the business rows and the evidence index still
  answer "what did this Run do" afterwards, which is checked against real rows, not a comment.

The PostgreSQL half — real concurrent transactions, real row locks, real `DELETE ... RETURNING` — is
in `tests/test_event_retention_integration.py`, skipped unless a disposable database is declared.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import JSON, Engine, MetaData, create_engine, event, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from huntweave.access import service as access_module
from huntweave.access.service import AccessService
from huntweave.api.app import create_app
from huntweave.api.events import (
    STREAM_AUTH_INTERVAL_SECONDS,
    STREAM_HEARTBEAT_SECONDS,
    STREAM_PAGE_SIZE,
    STREAM_REVOCATION_LIMIT_SECONDS,
    create_events_router,
    stream_probe,
)
from huntweave.config import AppSettings
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.event_stream import (
    EVENT_STREAM_CONTRACT_VERSION,
    EventHistoryView,
    EventRetentionView,
    EventStreamFailureView,
    EventStreamView,
    ReloadReason,
)
from huntweave.runs import event_retention as retention_module
from huntweave.runs import events as events_module
from huntweave.runs.event_retention import (
    AUTHORITATIVE_SOURCES,
    EventRetentionService,
    as_view,
)
from huntweave.runs.events import (
    committed_cursor,
    page_events,
    prune_events,
    retained_from_cursor,
)
from huntweave.storage.models import (
    AuditEvent,
    AuthorizationScope,
    Base,
    EventCursor,
    Project,
    Run,
    WebSession,
)

REPOSITORY = Path(__file__).resolve().parents[2]
WORKSPACE = REPOSITORY / "frontend" / "src" / "workspace.ts"
SAMPLES = Path(__file__).resolve().parent / "data" / "events"
#: The access key every shell-app client signs in with; long enough for `AppSettings` to accept it.
KEY = "event-stream-test-key-" + "a" * 48
COOKIE = AppSettings(access_key=KEY).cookie_name

class ErrorEnvelope(BaseModel):
    """The one error body the platform serves (`api/app.py:84-90`), as `phase0` samples read it."""

    model_config = {"extra": "forbid"}

    reason_code: str


#: Which contract each sample has to load as. A sample names its model here, so it cannot choose a
#: laxer parser than the production one (the same rule `test_phase0_contracts.py` applies).
MODEL_FOR: dict[str, type[Any]] = {
    "event_history": EventHistoryView,
    "event_stream": EventStreamView,
    "event_stream_failure": EventStreamFailureView,
    "event_retention": EventRetentionView,
    "error_envelope": ErrorEnvelope,
}

# : The reload reasons a client can act on, taken from the contract's own literal rather than
# restated.
RELOAD_REASONS: tuple[str, ...] = ReloadReason.__args__  # type: ignore[attr-defined]

#: Fixture files this slice is responsible for, so nothing can be dropped from the set unnoticed.
FIXTURE_FILES = {
    "event_cursor_ahead.json",
    "event_cursor_expired.json",
    "event_history_page.json",
    "event_history_missing_position.json",
    "event_retention_view.json",
    "event_stream_retention.json",
    "heartbeat.json",
    "session_expired.json",
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A throwaway database small enough for cursor arithmetic: real rows, real constraints.

    The cursor semantics are arithmetic over rows plus one row lock, and the row lock is exercised
    in the PostgreSQL half. A temporary SQLite file is enough here to check that the platform
    reports three positions instead of one, without touching any deployment's database.

    The schema is provided by attaching the same file under the name `huntweave`, so the ORM's
    schema-qualified table names resolve; the tables, keys and constraints the checks rely on are
    then the same ones the migrations build in PostgreSQL.
    """
    path = tmp_path / "events.sqlite3"
    result = create_engine(f"sqlite+pysqlite:///{path}")

    @event.listens_for(result, "connect")
    def _prepare(connection: Any, record: Any) -> None:
        connection.execute(f"ATTACH DATABASE '{path.as_posix()}' AS huntweave")
        # The access service serializes key initialization with a PostgreSQL advisory lock; SQLite
        # has no such lock and single-writer semantics already give the serialization this needs.
        connection.create_function("pg_advisory_xact_lock", 1, lambda key: None)

    with result.begin() as connection:
        copy = MetaData()
        for table in Base.metadata.sorted_tables:
            clone = table.to_metadata(copy)
            for column in clone.columns:
                # The business schema uses JSONB; SQLite needs the generic JSON type to render it.
                if isinstance(column.type, JSONB):
                    column.type = JSON()
        copy.create_all(connection)
    yield result
    result.dispose()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """`append_event` stamps `created_at` from the database clock, as it does in PostgreSQL.

    SQLite has no such function, so the stamps are fixed in Python instead: the value is irrelevant
    to cursor arithmetic, and a fixed one keeps every page's `created_at` reproducible. The access
    service reads the same clock for session lifetimes, so it is patched there too — naive, because
    SQLite hands datetimes back without a zone and the comparisons have to stay comparable.
    """
    stamp = datetime(2026, 10, 11, 9, 0)
    monkeypatch.setattr(events_module, "database_now", lambda session: stamp)
    monkeypatch.setattr(access_module, "database_now", lambda session: stamp)


def a_run(engine: Engine) -> UUID:
    """One Run row, since an event belongs to a real Run by foreign key."""
    run_id, scope_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        project = Project(
            id=uuid4(),
            name="events",
            description="",
            created_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
        )
        session.add(project)
        session.add(
            AuthorizationScope(
                id=scope_id,
                project_id=project.id,
                version=1,
                snapshot={},
                created_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
            )
        )
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                scope_id=scope_id,
                scope_version=1,
                scope_snapshot={"budget": {"max_tool_calls": 4}},
                idempotency_key=f"key-{run_id}",
                request_hash="0" * 64,
                status="running",
                phase="collecting",
                version=1,
                execution_profile="fake-p0-v1",
                demonstration_scenario="positive",
                demonstration_duration_ms=1000,
                created_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
            )
        )
    return run_id


def write_events(engine: Engine, run_id: UUID, count: int, kind: str = "decision") -> None:
    """Seed a Run's timeline the way `append_event` does, minus its PostgreSQL-only prologue.

    `append_event` opens with `INSERT ... ON CONFLICT (run_id) DO NOTHING`, which is the right way
    to open the cursor under two concurrent writers and is PostgreSQL syntax; its behaviour under
    real concurrency is checked in `test_event_retention_integration.py`. What is exercised here is
    the *read* side — contiguous runs, holes, watermarks — so the rows are written the way that
    function writes them once the cursor row exists: lock it, advance it, insert at the new cursor.
    """
    with Session(engine) as session, session.begin():
        cursor = session.get(EventCursor, run_id, with_for_update=True)
        if cursor is None:
            cursor = EventCursor(run_id=run_id, cursor=0)
            session.add(cursor)
            session.flush()
        for index in range(count):
            cursor.cursor += 1
            session.add(
                AuditEvent(
                    run_id=run_id,
                    cursor=cursor.cursor,
                    type=kind,
                    payload={"index": index},
                    source_event_id=None,
                    created_at=datetime(2026, 10, 11, 9, 0),
                )
            )
        session.flush()


def fixtures() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(SAMPLES.glob("*.json"))
    ]


def frontend_messages() -> set[str]:
    import re

    text = WORKSPACE.read_text(encoding="utf-8")
    start = text.index("export const messages")
    end = text.index("\n};", start)
    return set(re.findall(r"[\n,]\s*([a-z][a-z0-9_]*):", text[start:end]))


def shell_app(engine: Engine) -> FastAPI:
    """The router mounted the way `create_app` will mount it, plus the app's error renderer."""
    app = FastAPI()
    app.include_router(
        create_events_router(
            AppSettings(access_key=KEY), lambda: engine, mode_reader=lambda: None
        )
    )

    @app.exception_handler(ServiceError)
    async def refusal(request: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse({"reason_code": error.reason_code}, status_code=error.status_code)

    return app


def signed_in(engine: Engine) -> TestClient:
    """A client holding a real browser session, so the stream's session probe passes.

    The stream re-reads its session on every poll, which is exactly what the revocation bound is
    about, so the checks for it have to run against a session row rather than no credential at all.
    """
    client = TestClient(shell_app(engine))
    token, _ = AccessService(lambda: engine, AppSettings(access_key=KEY)).login(KEY, None)
    client.cookies.set(COOKIE, token)
    return client


# --------------------------------------------------------------------------------------------------
# Fixed response fixtures: the shape the console and the graph are meant to be built against.
# --------------------------------------------------------------------------------------------------


def test_the_fixture_set_is_complete_and_each_sample_loads_into_the_contract_it_names() -> None:
    """A sample that does not load proves nothing about the contract it claims to describe."""
    assert {path.name for path in SAMPLES.glob("*.json")} == FIXTURE_FILES
    for sample in fixtures():
        model = sample["materializes"]["model"]
        if model == "none":
            # The heartbeat frame is not a contract model: it is a fixed SSE event body.
            assert set(sample["payload"]) == {"mode"}
            continue
        MODEL_FOR[model].model_validate(sample["payload"])


def test_the_fixtures_cover_the_states_a_client_must_tell_apart() -> None:
    """Current, missing position, expired cursor, ahead cursor, revoked session, pruned timeline."""
    assert {sample["state"]["must_fail"] for sample in fixtures()} >= {
        None,
        "cursor_expired",
        "cursor_ahead",
        "session_revoked",
    }
    page = next(s for s in fixtures() if s["name"] == "event_history_page")
    missing = next(s for s in fixtures() if s["name"] == "event_history_missing_position")
    assert page["payload"]["published"] == page["payload"]["committed"]
    # `published < committed` states that a position below `committed` does not exist, and the page
    # that proves it cannot be empty: asking to continue from the end of the contiguous run before
    # a hole is refused with `event_cursor_ahead`, not answered with an empty 200. An uncommitted
    # write cannot produce this inequality at all, because `committed` is read in the reader's
    # snapshot.
    assert missing["payload"]["published"] < missing["payload"]["committed"]
    assert missing["payload"]["events"] != []
    assert missing["payload"]["next_cursor"] == missing["payload"]["published"]
    assert missing["payload"]["resync_required"] is False


def test_every_reason_a_fixture_names_has_a_sentence_in_the_console() -> None:
    """The console branches on these codes; an unmapped one leaves an operator reading a code."""
    mapped = frontend_messages()
    named = {sample["reason_code"] for sample in fixtures() if sample["reason_code"]}
    assert named == {"event_cursor_expired", "event_cursor_ahead", "authentication_required"}
    unmapped = sorted(named - mapped)
    assert named <= mapped, f"fixture reason codes with no console sentence: {unmapped}"


def test_a_reload_reason_in_a_fixture_is_never_invented() -> None:
    """The contract's reload vocabulary is closed, and the fixtures stay inside it."""
    used = {
        sample["payload"]["reload_reason"]
        for sample in fixtures()
        if sample["payload"].get("reload_reason") is not None
    }
    assert used <= set(RELOAD_REASONS)


def test_the_p1_heartbeat_cadence_and_page_size_are_kept() -> None:
    """Acceptance criterion 5: the P1 heartbeat and ordering semantics are kept as they were."""
    assert STREAM_HEARTBEAT_SECONDS == 5.0
    assert STREAM_PAGE_SIZE == 100


def test_a_revoked_session_is_not_served_past_the_stated_limit() -> None:
    """The stream re-reads its session often enough that revocation lands inside the bound."""
    assert 0 < STREAM_AUTH_INTERVAL_SECONDS <= STREAM_REVOCATION_LIMIT_SECONDS


def test_an_undisplayed_negative_reload_reason_is_refused_by_the_contract() -> None:
    with pytest.raises(ValidationError):
        EventHistoryView.model_validate(
            {
                "run_id": str(uuid4()),
                "events": [],
                "after": 0,
                "committed": 0,
                "published": 0,
                "retained_from": 0,
                "next_cursor": 0,
                "reload_reason": "because_i_said_so",
            }
        )


# --------------------------------------------------------------------------------------------------
# Cursor semantics: committed vs published vs retained_from.
# --------------------------------------------------------------------------------------------------


def test_a_reader_sees_the_events_it_can_continue_through(engine: Engine) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 3)
    with Session(engine) as session:
        window = page_events(session, run_id, 0, 100)
        assert [e["cursor"] for e in window.events] == [1, 2, 3]
        assert (window.committed, window.published, window.retained_from) == (3, 3, 0)
        assert window.next_cursor == 3 and not window.gap and not window.resync_required


def test_a_page_never_contains_a_position_the_reader_cannot_continue_from(
    engine: Engine,
) -> None:
    """PROJECT.md section 12.1: a cursor must not pass an event that is not yet published.

    Two readings of the same row are checked here, and they are deliberately kept apart because
    conflating them is what made `published` page-relative: `next_cursor` is where *this* reader
    continues (it was handed one event), while `published` is how far a *fresh* reader can continue
    through. A page limited to one event therefore stops at cursor 1 with `next_cursor == 1`, yet
    reports `published == 3` because the whole timeline is contiguous. A client that follows
    `next_cursor` sees every event exactly once and in order, which is the property the sentence is
    about; the concurrent-writer half needs a real row lock and lives in
    `test_event_retention_integration.py`.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 3)
    with Session(engine) as session:
        first = page_events(session, run_id, 0, 1)
        assert [event["cursor"] for event in first.events] == [1]
        assert first.next_cursor == 1
        assert first.published == 3 and first.committed == 3
        second = page_events(session, run_id, first.next_cursor, 1)
        assert [event["cursor"] for event in second.events] == [2]
        assert second.next_cursor == 2
        third = page_events(session, run_id, second.next_cursor, 100)
        assert [event["cursor"] for event in third.events] == [3]
        assert third.published == 3 and third.next_cursor == 3
        # Paging on `next_cursor` is what delivers the timeline, and `published` is the reach a
        # fresh reader gets in one answer — the two must agree on where the timeline ends.
        delivered = [event["cursor"] for event in first.events + second.events + third.events]
        assert delivered == [1, 2, 3]
        assert third.published == delivered[-1]


def test_a_hole_in_the_timeline_stops_the_reader_instead_of_being_walked_over(
    engine: Engine,
) -> None:
    """A position missing from under a committed timeline is refused, not skipped.

    `append_event` never produces this — a rolled-back claim takes its cursor back — so the hole is
    injected the only way it could really appear (a cursor reissued by hand, a partial restore), and
    the reader's behaviour at it is what the check is about: stop, and say the cursor is ahead of
    anything that exists, rather than jumping to 3.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 1)
    with Session(engine) as session, session.begin():
        cursor = session.get(EventCursor, run_id, with_for_update=True)
        assert cursor is not None
        cursor.cursor += 1  # cursor 2 is now claimed and will never have an event
    write_events(engine, run_id, 1)  # cursor 3 exists and is committed
    with Session(engine) as session:
        assert committed_cursor(session, run_id) == 3
        window = page_events(session, run_id, 0, 100)
        assert [e["cursor"] for e in window.events] == [1]
        assert window.published == 1 and window.committed == 3
        # Asking from the position that is missing is refused: that is where a client would land if
        # it had asked from the hole itself rather than from the last event it really holds.
        with pytest.raises(ServiceError) as refusal:
            page_events(session, run_id, 1, 100)
        assert refusal.value.reason_code == "event_cursor_ahead"
        # A page that ends below the hole is served normally: the reader is only stopped when it
        # asks from the missing position itself.
        assert [e["cursor"] for e in page_events(session, run_id, 0, 100).events] == [1]


def seed_watermark(engine: Engine, run_id: UUID, retained_from: int) -> None:
    """Put a Run's timeline in the state a prune leaves it in, without running one.

    The prune itself is `DELETE ... RETURNING` under a row lock, which is PostgreSQL and is checked
    there; what these checks are about is how a *reader* answers a cursor below the watermark, so
    the watermark is written directly and the read path is exercised for real.
    """
    with Session(engine) as session, session.begin():
        cursor = session.get(EventCursor, run_id, with_for_update=True)
        assert cursor is not None
        cursor.retained_from_cursor = retained_from


def test_an_expired_cursor_is_a_gap_with_its_own_code(engine: Engine) -> None:
    """The events did exist and were pruned: "reload the snapshot" is the only honest answer."""
    run_id = a_run(engine)
    write_events(engine, run_id, 5)
    seed_watermark(engine, run_id, 3)
    with Session(engine) as session:
        assert retained_from_cursor(session, run_id) == 3
        with pytest.raises(ServiceError) as refusal:
            page_events(session, run_id, 1, 100)
        assert refusal.value.reason_code == "event_cursor_expired"
        window = page_events(session, run_id, 3, 100)
        # 4 and 5 survive: everything below the watermark is gone and the watermark itself is the
        # cursor the reader is told to resume from.
        assert [e["cursor"] for e in window.events] == [4, 5]
        assert window.retained_from == 3 and window.committed == 5


def test_pruning_refuses_a_cursor_the_run_never_claimed(engine: Engine) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 1)
    with Session(engine) as session, session.begin():
        with pytest.raises(ServiceError) as refusal:
            prune_events(session, run_id, 99)
        assert refusal.value.reason_code == "invalid_event_cursor"


def test_the_authoritative_source_list_names_business_records_not_the_event_log() -> None:
    assert "huntweave.runs" in AUTHORITATIVE_SOURCES
    assert "huntweave.evidence" in AUTHORITATIVE_SOURCES
    assert not any("audit_events" in source for source in AUTHORITATIVE_SOURCES)


def test_the_retention_service_refuses_a_negative_policy_rather_than_reading_it_as_zero(
    engine: Engine,
) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 2)
    with pytest.raises(ServiceError) as refusal:
        EventRetentionService(lambda: engine).prune(run_id, -1)
    assert refusal.value.reason_code == "invalid_event_cursor"


def test_a_disabled_policy_reports_itself_as_disabled(engine: Engine) -> None:
    """The read path states the policy; the prune that would apply it is checked on PostgreSQL."""
    run_id = a_run(engine)
    write_events(engine, run_id, 3)
    outcome = EventRetentionService(lambda: engine).state(run_id, 0)
    view = EventRetentionView.model_validate(as_view(outcome))
    assert view.policy_enabled is False
    assert view.retained_events == 3 and view.retained_from == 0
    # The field still means "the newest N events are never pruned", which is true of a whole
    # timeline.
    assert view.keep_events == 1


def test_the_retention_outcome_of_an_unknown_run_is_refused(engine: Engine) -> None:
    with pytest.raises(ServiceError) as refusal:
        EventRetentionService(lambda: engine).state(uuid4(), 5)
    assert refusal.value.reason_code == "run_not_found"


def test_the_retention_module_touches_no_table_but_the_event_log(engine: Engine) -> None:
    """The mechanical half of the "authority is elsewhere" claim, read off the module's source."""
    source = Path(retention_module.__file__).read_text(encoding="utf-8")
    assert "delete(" not in source, "event retention must not delete rows itself"
    assert "prune_events" in source
    for forbidden in ("ToolCall", "ToolResult", "Evidence", "Decision", "ResearchTask"):
        assert forbidden not in source, f"event retention has no business touching {forbidden}"


# --------------------------------------------------------------------------------------------------
# HTTP surface: the router as the app mounts it.
# --------------------------------------------------------------------------------------------------


def test_the_history_endpoint_carries_all_three_positions(engine: Engine) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 2)
    client = TestClient(shell_app(engine))
    body = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 0}).json()
    view = EventHistoryView.model_validate(body)
    assert view.contract_version == EVENT_STREAM_CONTRACT_VERSION
    assert [event.cursor for event in view.events] == [1, 2]
    assert (view.committed, view.published, view.retained_from) == (2, 2, 0)
    assert view.after == 0 and view.next_cursor == 2
    assert view.resync_required is False and view.gap is False


def test_the_history_endpoint_refuses_an_expired_cursor_with_its_own_code(engine: Engine) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 5)
    seed_watermark(engine, run_id, 3)
    client = TestClient(shell_app(engine))
    response = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 1})
    assert response.status_code == 409
    assert response.json() == {"reason_code": "event_cursor_expired"}
    # The same cursor on the stream is refused before a frame is sent, for the same reason.
    streamed = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"})
    assert streamed.status_code == 409
    assert streamed.json() == {"reason_code": "event_cursor_expired"}
    # Continuing from the watermark is served: the documented recovery path is open.
    resumed = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 3})
    assert resumed.status_code == 200
    assert [event["cursor"] for event in resumed.json()["events"]] == [4, 5]


def test_the_history_endpoint_still_refuses_a_cursor_ahead(engine: Engine) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 1)
    client = TestClient(shell_app(engine))
    response = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 50})
    assert response.status_code == 409
    assert response.json() == {"reason_code": "event_cursor_ahead"}


def test_the_history_endpoint_refuses_an_unknown_run(engine: Engine) -> None:
    client = TestClient(shell_app(engine))
    response = client.get(f"/api/v1/runs/{uuid4()}/event-history")
    assert response.status_code == 404
    assert response.json() == {"reason_code": "run_not_found"}


def test_a_bad_last_event_id_is_refused_before_a_stream_opens(engine: Engine) -> None:
    run_id = a_run(engine)
    client = TestClient(shell_app(engine))
    response = client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "not-a-cursor"}
    )
    assert response.status_code == 422
    assert response.json() == {"reason_code": "invalid_event_cursor"}


def test_the_stream_opens_with_its_coordinate_then_the_events_in_order(engine: Engine) -> None:
    """The P1 frame shape and ordering are kept; the coordinate frame is what this slice adds."""
    run_id = a_run(engine)
    write_events(engine, run_id, 2)
    client = signed_in(engine)
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", params={"after": 0}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        for chunk in response.iter_text():
            body += chunk
            if "id: 2\nevent: audit" in body:
                break
    assert "event: retention" in body
    assert body.index("event: retention") < body.index("event: audit")
    assert "id: 1\nevent: audit" in body and "id: 2\nevent: audit" in body
    assert '"cursor": 1' in body and '"cursor": 2' in body
    opening = json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])
    assert opening["committed"] == 2 and opening["published"] == 2
    assert opening["retained_from"] == 0 and opening["resync_required"] is False


def test_the_stream_ends_with_a_reason_when_it_has_no_session(engine: Engine) -> None:
    """No cookie means the stream never had a credential, and it says exactly that."""
    run_id = a_run(engine)
    client = TestClient(shell_app(engine))
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", params={"after": 0}) as response:
        body = "".join(response.iter_text())
    assert "event: session_expired" in body
    assert '"reason_code": "authentication_required"' in body
    assert '"status_code": 401' in body


def test_the_stream_ends_with_a_reason_when_its_session_was_revoked(engine: Engine) -> None:
    """A revoked session is not a network blip: the stream names the reason and stops.

    Revocation is written the way `logout` and an access-key rotation write it, and the check drives
    the very probe the stream calls after the revocation, so the answer is the stream's own.
    """
    run_id = a_run(engine)
    client = signed_in(engine)
    settings = AppSettings(access_key=KEY)
    before = stream_probe(lambda: engine, settings, client.cookies.get(COOKIE))
    assert before[0] == "ok"
    with Session(engine) as session, session.begin():
        session.execute(update(WebSession).values(revoked=True))
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", params={"after": 0}) as response:
        body = "".join(response.iter_text())
    assert "event: session_expired" in body
    assert '"reason_code": "authentication_required"' in body
    after = stream_probe(lambda: engine, settings, client.cookies.get(COOKIE))
    assert after[0] == "authentication_required"


def test_the_last_event_id_header_wins_over_the_query_parameter(engine: Engine) -> None:
    """A browser's `EventSource` re-sends the header on reconnect; the query parameter is stale.

    The client asks from the query parameter's cursor (3, the end of the timeline) but its header
    says 1, and the header is what a reconnect really carries: the stream honours it and sends 2 and
    3.
    """
    run_id = a_run(engine)
    write_events(engine, run_id, 3)
    client = signed_in(engine)
    with client.stream(
        "GET",
        f"/api/v1/runs/{run_id}/events",
        params={"after": 3},
        headers={"Last-Event-ID": "1"},
    ) as response:
        body = ""
        for chunk in response.iter_text():
            body += chunk
            if "id: 3\nevent: audit" in body:
                break
    assert "id: 2\nevent: audit" in body and "id: 3\nevent: audit" in body
    # The query parameter's cursor would have sent nothing; the header's cursor is what was used.
    assert body.count("event: audit") == 2


def test_a_page_from_a_middle_cursor_still_works(engine: Engine) -> None:
    """A cursor in the middle of the retained timeline is not a hole and must be served."""
    run_id = a_run(engine)
    write_events(engine, run_id, 3)
    client = signed_in(engine)
    response = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 1})
    assert response.status_code == 200
    view = EventHistoryView.model_validate(response.json())
    assert [event.cursor for event in view.events] == [2, 3]


def test_the_retention_endpoint_reports_both_authorities_and_the_policy(engine: Engine) -> None:
    """The read states the policy and both authorities; a prune is checked on PostgreSQL."""
    run_id = a_run(engine)
    write_events(engine, run_id, 5)
    client = TestClient(shell_app(engine))
    before = EventRetentionView.model_validate(
        client.get(f"/api/v1/runs/{run_id}/event-retention", params={"keep": 4}).json()
    )
    assert before.retained_from == 0 and before.retained_events == 5
    assert before.keep_events == 4 and before.policy_enabled is True
    assert before.events_are_authority is False
    assert "huntweave.tool_calls" in before.authoritative_sources

    seed_watermark(engine, run_id, 1)
    after = EventRetentionView.model_validate(
        client.get(f"/api/v1/runs/{run_id}/event-retention", params={"keep": 4}).json()
    )
    assert after.retained_from == 1 and after.retained_events == 4
    # The watermark is visible in the state the console reads, so "pruned" is never inferred.
    snapshot = client.get(f"/api/v1/runs/{run_id}/event-history", params={"after": 1}).json()
    view = EventHistoryView.model_validate(snapshot)
    assert view.retained_from == 1 and view.resync_required is False


def test_a_disabled_policy_says_so_instead_of_pruning_nothing_silently(engine: Engine) -> None:
    run_id = a_run(engine)
    write_events(engine, run_id, 5)
    client = TestClient(shell_app(engine))
    view = EventRetentionView.model_validate(
        client.get(f"/api/v1/runs/{run_id}/event-retention", params={"keep": 0}).json()
    )
    assert view.policy_enabled is False
    assert view.retained_from == 0 and view.retained_events == 5


def test_the_history_contract_refuses_a_field_the_console_would_have_trusted() -> None:
    """Extra fields are refused, so a producer cannot quietly widen the response."""
    with pytest.raises(ValidationError):
        EventHistoryView.model_validate(
            {
                "run_id": str(uuid4()),
                "events": [],
                "after": 0,
                "committed": 0,
                "published": 0,
                "retained_from": 0,
                "next_cursor": 0,
                "surprise": True,
            }
        )


def test_the_packaged_app_serves_the_new_timeline_contract_and_not_the_old_one() -> None:
    """The router replaces two inline handlers, so the *packaged* schema is the thing to check.

    FastAPI keeps the first registration for a method+path pair, so leaving the old inline
    ``event_history``/``events`` handlers in `api/app.py` beside the new router would keep serving
    them: the router would exist, every check that mounts the router itself would still pass, and
    production would answer with the old contract. Both handlers were named `event_history`, so the
    route table alone cannot tell them apart — the response contract can: the old one answered
    `EventPage`, the new one answers `EventHistoryView`.

    The generated OpenAPI document is read rather than `app.routes`, because an included router is
    a single ``_IncludedRouter`` entry there and its routes are not flattened into the parent list.
    No request is made, so no database connection is opened.
    """
    schema = create_app(AppSettings(access_key="k" * 32)).openapi()
    paths = schema["paths"]

    # Two routes the router adds, and the two it replaces.
    for path in (
        "/api/v1/runs/{run_id}/event-history",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/runs/{run_id}/event-retention",
        "/api/v1/runs/{run_id}/event-retention/prune",
    ):
        assert path in paths, sorted(paths)

    answer = paths["/api/v1/runs/{run_id}/event-history"]["get"]["responses"]["200"]
    reference = answer["content"]["application/json"]["schema"]["$ref"]
    assert reference.endswith("/EventHistoryView"), reference
    # The route the inline pair never had: its presence is what says the router is mounted at all,
    # and the ref above is what says the old handler is not the one answering.
    assert paths["/api/v1/runs/{run_id}/event-retention"]["get"]["responses"]["200"]