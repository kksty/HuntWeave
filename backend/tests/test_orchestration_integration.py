import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from huntweave.contracts.execution import ExecutionRecord, ExecutionRequest, ExecutionResult
from huntweave.contracts.runs import Budget, PortInput, ProjectCreate, RunCreate, ScopeCreate
from huntweave.runs.orchestration import OrchestrationService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine


@pytest.fixture
def business():
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database")
    engine = connect_engine()
    runs = RunService(lambda: engine)
    project = runs.create_project(ProjectCreate(name="p0 contract verification"))
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
    run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), str(uuid4()))
    runs.start(run.id, run.version)
    yield runs, OrchestrationService(lambda: engine), run.id
    engine.dispose()


def test_replayed_decision_reserves_one_call_and_commits_one_event(business):
    runs, service, run_id = business
    claim = service.claim(run_id)
    assert claim is not None and claim["run_id"] == str(run_id)
    first = service.plan(run_id, claim["task_id"], claim["lease_generation"])
    replay = service.plan(run_id, claim["task_id"], claim["lease_generation"])
    assert replay == first
    snapshot = service.snapshot(run_id)
    assert len(snapshot["calls"]) == 1
    assert snapshot["budget"]["reserved_tool_calls"] == 1
    assert len([e for e in service.history(run_id)["events"] if e["type"] == "tool_planned"]) == 1


def test_run_awaiting_reconciliation_never_starves_the_single_scheduler(business):
    runs, service, stuck_id = business
    claim = service.claim(stuck_id)
    assert claim is not None
    service.plan(stuck_id, claim["task_id"], claim["lease_generation"])
    service.unknown(stuck_id, UUID(service.snapshot(stuck_id)["calls"][0]["id"]))
    # The unconfirmed call needs a human decision, so this Run is skipped while the
    # remaining Runs keep being served by the one active scheduler;
    assert service.claim(stuck_id) is None
    # ...and its ledger is still re-read by the dispatcher sweep, so a Runner that
    # answers later can release the Run instead of leaving it wedged.
    assert stuck_id in service.reconcilable_runs()
    view = runs.run(stuck_id)
    second = runs.create_run(
        RunCreate(scope_id=view.scope_id, scope_version=view.scope_version), str(uuid4())
    )
    runs.start(second.id, second.version)
    accepted = service.claim(second.id)
    assert accepted is not None and accepted["run_id"] == str(second.id)


def ticket_of(engine: Engine, call_id: UUID) -> ExecutionRequest:
    with engine.begin() as connection:
        raw = connection.scalar(
            text("SELECT ticket FROM huntweave.tool_calls WHERE id = :id"), {"id": call_id}
        )
    return ExecutionRequest.model_validate(raw)


def next_ticket(engine: Engine, service: OrchestrationService, run_id: UUID) -> ExecutionRequest:
    """Dispatch the Run's next action and return the ticket that action was bound to.

    A role whose bounded step is already done only finishes, so the loop keeps claiming
    until the Run plans a call it did not have before.
    """
    for _ in range(8):
        seen = {call["id"] for call in service.snapshot(run_id)["calls"]}
        claim = service.claim(run_id)
        assert claim is not None
        service.plan(run_id, claim["task_id"], claim["lease_generation"])
        fresh = [call["id"] for call in service.snapshot(run_id)["calls"] if call["id"] not in seen]
        if fresh:
            return ticket_of(engine, UUID(fresh[-1]))
    raise AssertionError("the Run planned no call")


def settle(service: OrchestrationService, ticket: ExecutionRequest) -> None:
    service.accept(
        ExecutionRecord(
            request=ticket,
            status="completed",
            result=ExecutionResult(output="FAKE fixed fixture output\n", exit_code=0, evidence=[]),
        )
    )


def test_a_multi_target_run_binds_each_call_to_its_own_authorized_endpoint(business):
    runs, service, run_id = business
    engine = connect_engine()
    try:
        scope = runs.create_scope(
            ScopeCreate(
                project_id=runs.run(run_id).project_id,
                targets_text="192.0.2.10\n192.0.2.11",
                ports=PortInput(profile="custom-tcp-v1", custom="80,443"),
                starts_at=datetime.now(UTC) - timedelta(minutes=1),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                authorization="Fixed fake actions only",
                budget=Budget(max_tool_calls=5),
            )
        )
        run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), str(uuid4()))
        runs.start(run.id, run.version)

        first = next_ticket(engine, service, run.id)
        settle(service, first)
        second = next_ticket(engine, service, run.id)
        settle(service, second)
        third = next_ticket(engine, service, run.id)

        snapshot = scope.snapshot
        authorized = {(ip, port) for ip in snapshot.targets for port in snapshot.ports}
        bound = [
            (str(ticket.target_ip), ticket.target_port) for ticket in (first, second, third)
        ]
        # One target binding per call, each inside the authorization, and not the same
        # first-entry binding recorded three times.
        assert len(set(bound)) == 3
        assert set(bound) <= authorized
        assert {ticket.policy_version for ticket in (first, second, third)} == {
            snapshot.policy_version
        }
    finally:
        engine.dispose()


def test_a_plan_aimed_outside_the_authorization_is_refused_before_it_reserves_anything(business):
    runs, service, run_id = business

    class StrayModel:
        def decide(self, role: str, step: int, context: dict) -> dict:
            return {
                "action": "fake.collect",
                "target_ip": "192.0.2.200",
                "target_port": 80,
                "summary": "A plan that reaches past the snapshot",
            }

    service.model = StrayModel()
    claim = service.claim(run_id)
    assert claim is not None
    assert service.plan(run_id, claim["task_id"], claim["lease_generation"]) is None

    snapshot = service.snapshot(run_id)
    assert snapshot["calls"] == []
    assert snapshot["budget"]["reserved_tool_calls"] == 0
    assert snapshot["run"]["status"] == "waiting"
    assert snapshot["run"]["reason_code"] == "scope_denied"
    denied = [e for e in service.history(run_id)["events"] if e["type"] == "scope_denied"]
    assert len(denied) == 1
    assert denied[0]["payload"]["planned_target_ip"] == "192.0.2.200"
