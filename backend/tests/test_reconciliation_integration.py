"""Operator reconciliation of calls whose outcome the execution ledger never confirmed.

The seams here are the business Interface and the durable records it writes: a verdict is a
business-side decision bound to an operator session, the evidence it cites and the scope
version, and it never replaces an execution-side fact.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from huntweave.api.app import create_app
from huntweave.config import AppSettings
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import (
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
)
from huntweave.contracts.orchestration import ReconciliationVerdict
from huntweave.contracts.runs import Budget, ProjectCreate, RunCreate, ScopeCreate
from huntweave.runs.orchestration import OrchestrationService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine

pytestmark = pytest.mark.integration

CONSOLE_KEY = "reconciliation-test-key-" + "a" * 40
CONSOLE_ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def business() -> Iterator[tuple[RunService, OrchestrationService, UUID, Engine]]:
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database")
    engine = connect_engine()
    runs = RunService(lambda: engine)
    project = runs.create_project(ProjectCreate(name="reconciliation contract verification"))
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
    run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), str(uuid4()))
    runs.start(run.id, run.version)
    yield runs, OrchestrationService(lambda: engine), run.id, engine
    engine.dispose()


def opened_call(service: OrchestrationService, run_id: UUID) -> UUID:
    """Claim and plan exactly one call, the way the scheduler does."""
    claim = service.claim(run_id)
    assert claim is not None and claim["run_id"] == str(run_id)
    service.plan(run_id, claim["task_id"], claim["lease_generation"])
    calls = service.snapshot(run_id)["calls"]
    return UUID(calls[-1]["id"])


def operator(engine: Engine) -> UUID:
    """A browser session to bind the verdict to; the record's foreign key requires it."""
    session_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO huntweave.web_sessions (id, token_hash, csrf_token, key_version,"
                " created_at, last_seen_at, idle_expires_at, absolute_expires_at, revoked)"
                " VALUES (:id, :token, :csrf, 1, now(), now(), now() + interval '1 hour',"
                " now() + interval '2 hours', false)"
            ),
            {
                "id": session_id,
                "token": uuid4().hex + uuid4().hex,
                "csrf": uuid4().hex + uuid4().hex,
            },
        )
    return session_id


def expire_control_lease(engine: Engine, call_id: UUID) -> None:
    """Age the ticket's 15 s control lease, as real operator time does."""
    lease = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE huntweave.tool_calls SET ticket = jsonb_set("
                "ticket, '{lease_expires_at}', to_jsonb(CAST(:lease AS text))) WHERE id = :id"
            ),
            {"lease": lease, "id": call_id},
        )


def observed(
    *, started: bool, process_active: bool | None, connection_open: bool | None
) -> ExecutionObservation:
    return ExecutionObservation(
        started=started,
        process_active=process_active,
        connection_open=connection_open,
        observed_at=datetime.now(UTC),
    )


def verdict(
    service: OrchestrationService, run_id: UUID, outcome: str, **changes: object
) -> ReconciliationVerdict:
    data: dict[str, object] = {
        "outcome": outcome,
        "version": service.snapshot(run_id)["run"]["version"],
        "evidence_ids": [],
        "note": "",
    }
    data.update(changes)
    return ReconciliationVerdict.model_validate(data)


def refused(
    service: OrchestrationService,
    run_id: UUID,
    call_id: UUID,
    request: ReconciliationVerdict,
    operator_id: UUID,
    observation: ExecutionObservation | None,
) -> str:
    with pytest.raises(ServiceError) as error:
        service.reconcile(run_id, call_id, request, operator_id, observation)
    return error.value.reason_code


def events(service: OrchestrationService, run_id: UUID, kind: str) -> list[dict]:
    return [event for event in service.history(run_id)["events"] if event["type"] == kind]


def refused_control(service: OrchestrationService, run_id: UUID, action: str) -> str:
    version = service.snapshot(run_id)["run"]["version"]
    with pytest.raises(ServiceError) as error:
        service.control(run_id, action, version)
    return error.value.reason_code


def governed_control(service: OrchestrationService, run_id: UUID, action: str) -> dict:
    return service.control(run_id, action, service.snapshot(run_id)["run"]["version"])


def ticket_of(engine: Engine, call_id: UUID) -> ExecutionRequest:
    with engine.begin() as connection:
        raw = connection.scalar(
            text("SELECT ticket FROM huntweave.tool_calls WHERE id = :id"), {"id": call_id}
        )
    return ExecutionRequest.model_validate(raw)


def ledger_answer(
    engine: Engine, call_id: UUID, observation: ExecutionObservation
) -> ExecutionRecord:
    """One more answer from the same durable ledger, as the dispatcher sweep reads it."""
    return ExecutionRecord(
        request=ticket_of(engine, call_id),
        status="unknown",
        reason_code="execution_unknown",
        observation=observation,
    )


def test_never_executed_settles_the_call_and_authorises_one_redispatch(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    expire_control_lease(engine, call_id)
    not_executed = observed(started=False, process_active=False, connection_open=False)

    result = service.reconcile(
        run_id,
        call_id,
        verdict(service, run_id, "not_executed", note="ledger proves the acceptance never ran"),
        operator(engine),
        not_executed,
    )

    assert result["call"]["status"] == "cancelled"
    assert result["call"]["reconciliation"]["outcome"] == "not_executed"
    assert result["call"]["reconciliation"]["redispatch_authorized"] is True
    assert result["call"]["observation"]["stop_confirmed"] is True
    # Settling the only unknown call clears the interruption the Run was waiting on.
    assert result["run"]["reason_code"] is None
    recorded = events(service, run_id, "reconciliation_recorded")
    assert len(recorded) == 1 and recorded[0]["payload"]["call_id"] == str(call_id)

    repeat = service.reconcile(
        run_id,
        call_id,
        verdict(service, run_id, "not_executed"),
        operator(engine),
        not_executed,
    )
    assert repeat["call"]["reconciliation"] == result["call"]["reconciliation"]
    assert len(events(service, run_id, "reconciliation_recorded")) == 1

    conflicting = verdict(service, run_id, "executed")
    assert (
        refused(service, run_id, call_id, conflicting, operator(engine), not_executed)
        == "reconciliation_conflict"
    )


def test_never_executed_needs_a_record_that_proves_it_and_a_released_lease(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    operator_id = operator(engine)
    request = verdict(service, run_id, "not_executed")

    # No trusted record at all: a click is not evidence.
    assert (
        refused(service, run_id, call_id, request, operator_id, None)
        == "reconciliation_evidence_missing"
    )
    # A record that the action started contradicts the verdict outright.
    started = observed(started=True, process_active=False, connection_open=False)
    assert (
        refused(service, run_id, call_id, request, operator_id, started)
        == "reconciliation_evidence_contradicted"
    )
    # The original control lease still authorises the old call, so it cannot be re-dispatched.
    not_started = observed(started=False, process_active=False, connection_open=False)
    assert (
        refused(service, run_id, call_id, request, operator_id, not_started)
        == "reconciliation_lease_active"
    )
    # The Run is untouched by every refusal above.
    assert service.snapshot(run_id)["calls"][0]["status"] == "unknown"
    assert events(service, run_id, "reconciliation_recorded") == []


def test_a_stale_verdict_version_never_rewrites_a_moved_run(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    request = verdict(service, run_id, "undetermined", version=1)
    assert (
        refused(
            service,
            run_id,
            call_id,
            request,
            operator(engine),
            observed(started=False, process_active=False, connection_open=False),
        )
        == "version_conflict"
    )


def test_confirmed_execution_ends_the_call_incomplete_without_inventing_a_result(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    started = observed(started=True, process_active=False, connection_open=False)

    result = service.reconcile(
        run_id,
        call_id,
        verdict(service, run_id, "executed", note="partial output archived before the restart"),
        operator(engine),
        started,
    )

    assert result["call"]["status"] == "incomplete"
    assert result["call"]["result"] is None
    assert result["call"]["reconciliation"]["redispatch_authorized"] is False
    # The research continues past a step nobody can report on, so the Run can be resumed.
    assert service.snapshot(run_id)["tasks"][0]["step"] == 1
    assert refused_control(service, run_id, "close") == "invalid_run_state"
    assert governed_control(service, run_id, "resume")["status"] == "running"


def test_confirmed_execution_still_waits_for_a_confirmed_stop(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    unconfirmed = observed(started=True, process_active=None, connection_open=None)

    result = service.reconcile(
        run_id, call_id, verdict(service, run_id, "executed"), operator(engine), unconfirmed
    )

    assert result["call"]["status"] == "incomplete"
    assert result["call"]["observation"]["stop_confirmed"] is False
    # An operator's verdict never releases the execution side's resources.
    assert result["run"]["reason_code"] == "execution_stop_unconfirmed"
    assert refused_control(service, run_id, "resume") == "execution_stop_unconfirmed"
    preview = service.preview(run_id)
    assert preview["can_resume"] is False
    assert preview["reason_code"] == "execution_stop_unconfirmed"
    assert preview["pending_calls"][0]["conditions"] == ["stop_unconfirmed"]

    # The ledger later confirms the stop; only then does the Run move on.
    service.accept(
        ledger_answer(
            engine, call_id, observed(started=True, process_active=False, connection_open=False)
        )
    )
    assert service.snapshot(run_id)["run"]["reason_code"] is None
    assert governed_control(service, run_id, "resume")["status"] == "running"


def test_an_undetermined_outcome_can_only_end_as_a_limited_run(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    unconfirmed = observed(started=True, process_active=None, connection_open=None)

    result = service.reconcile(
        run_id,
        call_id,
        verdict(service, run_id, "undetermined", note="evidence is not conclusive either way"),
        operator(engine),
        unconfirmed,
    )

    assert result["call"]["status"] == "unknown"
    assert result["call"]["reconciliation"]["outcome"] == "undetermined"
    assert refused_control(service, run_id, "resume") == "execution_reconciliation_required"
    preview = service.preview(run_id)
    assert preview["pending_calls"][0]["conditions"] == ["outcome_unsettled", "stop_unconfirmed"]
    assert preview["reason_code"] == "execution_reconciliation_required"

    # Cancelling stays possible, but the Run does not converge on unaccounted resources.
    assert governed_control(service, run_id, "cancel")["status"] == "cancelling"
    assert service.snapshot(run_id)["run"]["status"] == "cancelling"

    service.accept(
        ledger_answer(
            engine, call_id, observed(started=True, process_active=False, connection_open=False)
        )
    )
    snapshot = service.snapshot(run_id)
    assert snapshot["run"]["status"] == "cancelled"
    # The end says what it is: a call's result never arrived, so this is not a normal end.
    assert snapshot["run"]["reason_code"] == "result_incomplete"
    cancelled = events(service, run_id, "run_cancelled")[-1]["payload"]
    assert cancelled["limited"] is True and cancelled["incomplete_calls"] == [str(call_id)]


def test_a_proven_never_executed_call_is_dispatched_again_under_a_new_call_id(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    expire_control_lease(engine, call_id)
    service.reconcile(
        run_id,
        call_id,
        verdict(service, run_id, "not_executed"),
        operator(engine),
        observed(started=False, process_active=False, connection_open=False),
    )
    assert governed_control(service, run_id, "resume")["status"] == "running"

    claim = service.claim(run_id)
    assert claim is not None
    service.plan(run_id, claim["task_id"], claim["lease_generation"])

    first, second = service.snapshot(run_id)["calls"]
    assert first["id"] == str(call_id) and first["status"] == "cancelled"
    assert second["id"] != first["id"] and second["status"] == "planned"
    # The replacement is related to the call it replaces and repeats its action unchanged.
    assert second["replaces_call_id"] == first["id"]
    assert second["decision_id"] != first["decision_id"]
    assert second["action"] == first["action"] and second["parameters"] == first["parameters"]
    planned = events(service, run_id, "tool_planned")
    assert [event["payload"]["call_id"] for event in planned] == [first["id"], second["id"]]

    # A graph replay of the same step must not dispatch a third call.
    service.plan(run_id, claim["task_id"], claim["lease_generation"])
    assert len(service.snapshot(run_id)["calls"]) == 2


def console(engine: Engine, observation: ExecutionObservation | None) -> TestClient:
    """The operator's own entry point, with the ledger answer the request path would read."""
    return TestClient(
        create_app(AppSettings(access_key=CONSOLE_KEY), engine, lambda call_id: observation),
        base_url=CONSOLE_ORIGIN,
    )


def login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/auth/login", json={"access_key": CONSOLE_KEY}, headers={"Origin": CONSOLE_ORIGIN}
    )
    assert response.status_code == 200
    return {"Origin": CONSOLE_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def reconciliation_path(run_id: UUID, call_id: UUID) -> str:
    return f"/api/v1/runs/{run_id}/calls/{call_id}/reconciliation"


def test_the_console_records_a_verdict_as_an_operator_decision(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    expire_control_lease(engine, call_id)
    answer = observed(started=False, process_active=False, connection_open=False)
    client = console(engine, answer)

    assert client.post(
        reconciliation_path(run_id, call_id), json={"outcome": "not_executed", "version": 1}
    ).status_code == 401

    headers = login(client)
    version = service.snapshot(run_id)["run"]["version"]
    response = client.post(
        reconciliation_path(run_id, call_id),
        json={
            "outcome": "not_executed",
            "version": version,
            "evidence_ids": [],
            "note": "The ledger kept the acceptance and never started the action.",
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["call"]["status"] == "cancelled"
    assert body["call"]["reconciliation"]["outcome"] == "not_executed"
    assert body["call"]["reconciliation"]["note"].startswith("The ledger kept")
    # The verdict names its operator, and the execution facts stay the execution side's.
    assert body["call"]["reconciliation"]["operator_session_id"] != str(call_id)
    assert body["call"]["observation"]["started"] is False
    assert body["call"]["observation"]["stop_confirmed"] is True

    preview = client.get(f"/api/v1/runs/{run_id}/resume-preview", headers=headers).json()
    assert preview["can_resume"] is True and preview["reason_code"] is None


def test_the_console_reports_what_a_stuck_run_is_missing(business):
    runs, service, run_id, engine = business
    call_id = opened_call(service, run_id)
    service.unknown(run_id, call_id)
    unconfirmed = observed(started=True, process_active=None, connection_open=None)
    client = console(engine, unconfirmed)
    headers = login(client)
    version = service.snapshot(run_id)["run"]["version"]

    response = client.post(
        reconciliation_path(run_id, call_id),
        json={"outcome": "executed", "version": version, "note": "Partial output is archived."},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["call"]["status"] == "incomplete"

    preview = client.get(f"/api/v1/runs/{run_id}/resume-preview", headers=headers).json()
    assert preview["can_resume"] is False
    assert preview["reason_code"] == "execution_stop_unconfirmed"
    assert preview["pending_calls"] == [
        {"id": str(call_id), "status": "incomplete", "conditions": ["stop_unconfirmed"]}
    ]
    assert client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={"version": service.snapshot(run_id)["run"]["version"]},
        headers=headers,
    ).status_code == 409

    snapshot = client.get(f"/api/v1/runs/{run_id}/snapshot", headers=headers).json()
    assert snapshot["calls"][0]["reconciliation"]["outcome"] == "executed"
