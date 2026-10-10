"""The control plane's retention trace: what an operator asked, and where it is written down.

Retention itself is the execution side's job and is checked there; these checks are about the part
the control plane owns — the business-side decision record, the event it puts on each affected Run's
timeline, that a retry cannot become a second decision, and that a refusal from the execution side
keeps its own reason code instead of being rewritten into a control-plane one.
"""

import os
import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from huntweave.api.app import create_app
from huntweave.config import AppSettings
from huntweave.contracts.capabilities import Capabilities
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.retention import (
    RetentionDecisionView,
    RetentionReport,
    RetentionRequest,
    RetentionView,
)
from huntweave.contracts.runs import Budget, ProjectCreate, RunCreate, ScopeCreate
from huntweave.execution.capabilities import evaluate
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
    RetentionDecisionRecord,
    Run,
    ToolCall,
    ToolResult,
    WebSession,
)

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"
KEY = "retention-test-key-" + "b" * 64
CLEARED = (
    AuditEvent,
    EventCursor,
    ToolResult,
    Outbox,
    BudgetReservation,
    Evidence,
    InterruptionRecord,
    ReconciliationDecision,
    RetentionDecisionRecord,
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
        pytest.skip("Retention trace checks require an explicitly disposable Compose database")
    result = connect_engine()
    with Session(result) as session, session.begin():
        for model in CLEARED:
            session.execute(delete(model))
    yield result
    result.dispose()


def ready_capabilities() -> Capabilities:
    return evaluate(fake_execution_ready=True, now=datetime.now(UTC))


@pytest.fixture
def make_client(engine: Engine) -> Callable[..., TestClient]:
    """Build the app with the execution side's retention read and write replaced by a stand-in."""

    def build(
        reader: Callable[[], RetentionView],
        writer: Callable[[str, str, str, RetentionRequest], RetentionView | RetentionReport],
    ) -> TestClient:
        return TestClient(
            create_app(
                AppSettings(access_key=KEY),
                engine,
                capability_reader=ready_capabilities,
                retention_reader=reader,
                retention_writer=writer,
            ),
            base_url=ORIGIN,
        )

    return build


def login(client: TestClient) -> dict[str, str]:
    response = client.post("/auth/login", json={"access_key": KEY}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def run_of(engine: Engine) -> UUID:
    """One live Run whose timeline a retention decision can reach."""
    runs = RunService(lambda: engine)
    project = runs.create_project(ProjectCreate(name="retention trace"))
    now = datetime.now(UTC)
    scope = runs.create_scope(
        ScopeCreate(
            project_id=project.id,
            targets_text="192.0.2.1",
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            authorization="Fixed fake actions only",
            budget=Budget(max_tool_calls=2),
        )
    )
    run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), secrets.token_hex(8))
    return run.id


def decision_of(run_id: UUID, **changes: object) -> RetentionDecisionView:
    base: dict[str, object] = {
        "decision_id": uuid4(),
        "action": "delete",
        "target_kind": "artifact",
        "target": str(uuid4()),
        "at": datetime.now(UTC),
        "actor": None,
        "note": None,
        "reason_code": None,
        "affected_runs": [run_id],
        "freed_bytes": 4096,
        "deleted_artifacts": [uuid4()],
        "failed": [],
    }
    base.update(changes)
    return RetentionDecisionView.model_validate(base)


def view_with(decision: RetentionDecisionView) -> RetentionView:
    view = RetentionView.unavailable(None, management="ready")
    return view.model_copy(update={"available": True, "decisions": [decision]})


def test_a_decision_is_kept_once_and_reaches_the_timeline_of_every_run_it_concerns(
    engine: Engine, make_client: Callable[..., TestClient]
) -> None:
    run_id = run_of(engine)
    other = run_of(engine)
    decision = decision_of(run_id, affected_runs=[run_id, other])
    seen: list[RetentionRequest] = []

    def writer(
        kind: str, target: str, action: str, request: RetentionRequest
    ) -> RetentionView | RetentionReport:
        seen.append(request)
        return view_with(decision)

    client = make_client(lambda: view_with(decision), writer)
    headers = login(client)
    first = client.post(
        f"/api/v1/retention/artifacts/{decision.target}/delete",
        json={"note": "no longer needed"},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["decisions"][0]["decision_id"] == str(decision.decision_id)

    # The execution side is told who acted, from the session, never from the request body.
    assert seen and seen[0].actor is not None
    with Session(engine) as session:
        row = session.get(RetentionDecisionRecord, decision.decision_id)
        assert row is not None
        assert row.action == "delete" and row.note == "no longer needed"
        assert row.operator_session_id == UUID(seen[0].actor or "")
        assert set(row.affected_runs) == {str(run_id), str(other)}
        assert row.freed_bytes == 4096
        for affected in (run_id, other):
            events = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.run_id == affected, AuditEvent.type == "retention_decision"
                    )
                )
            )
            assert len(events) == 1
            assert events[0].payload["decision_id"] == str(decision.decision_id)
            assert events[0].payload["action"] == "delete"
            assert events[0].source_event_id == f"{decision.decision_id}:retention"

    # Repeating the same decision (a retried request) is not a second entry, and not a second event.
    again = client.post(
        f"/api/v1/retention/artifacts/{decision.target}/delete",
        json={"note": "no longer needed"},
        headers=headers,
    )
    assert again.status_code == 200
    with Session(engine) as session:
        rows = list(session.scalars(select(RetentionDecisionRecord)))
        assert len(rows) == 1
        events = list(
            session.scalars(select(AuditEvent).where(AuditEvent.type == "retention_decision"))
        )
        assert len(events) == 2  # one per affected Run, still one each


def test_a_refusal_keeps_the_reason_the_execution_side_gave(
    engine: Engine, make_client: Callable[..., TestClient]
) -> None:
    run_id = run_of(engine)

    def writer(
        kind: str, target: str, action: str, request: RetentionRequest
    ) -> RetentionView | RetentionReport:
        raise ServiceError("retention_artifact_in_use", 409)

    client = make_client(lambda: view_with(decision_of(run_id)), writer)
    headers = login(client)
    response = client.post(
        f"/api/v1/retention/artifacts/{uuid4()}/delete", json={"note": ""}, headers=headers
    )
    assert response.status_code == 409
    assert response.json() == {"reason_code": "retention_artifact_in_use"}
    # A refused action leaves no business-side decision behind: nothing happened to record.
    with Session(engine) as session:
        assert list(session.scalars(select(RetentionDecisionRecord))) == []


def test_the_read_reports_the_gap_the_execution_side_has(
    engine: Engine, make_client: Callable[..., TestClient]
) -> None:
    def writer(
        kind: str, target: str, action: str, request: RetentionRequest
    ) -> RetentionView | RetentionReport:
        raise AssertionError("no action is expected in this check")

    client = make_client(lambda: RetentionView.unavailable("runner_unavailable"), writer)
    login(client)
    body = client.get("/api/v1/retention").json()
    view = RetentionView.model_validate(body)
    assert view.available is False
    assert view.reason_code == "runner_unavailable"
    assert view.artifacts == [] and view.versions == []
