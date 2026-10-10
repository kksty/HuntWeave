import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from _planning import plan_step
from sqlalchemy import Engine, text

from huntweave.contracts.execution import (
    CallProgress,
    CallRuntime,
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
)
from huntweave.contracts.runs import Budget, PortInput, ProjectCreate, RunCreate, ScopeCreate
from huntweave.harness.model import ModelRequest, ModelSuggestion
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
    first = plan_step(service, run_id, claim["task_id"], claim["lease_generation"])
    replay = plan_step(service, run_id, claim["task_id"], claim["lease_generation"])
    assert replay == first
    snapshot = service.snapshot(run_id)
    assert len(snapshot["calls"]) == 1
    assert snapshot["budget"]["reserved_tool_calls"] == 1
    assert len([e for e in service.history(run_id)["events"] if e["type"] == "tool_planned"]) == 1


def test_run_awaiting_reconciliation_never_starves_the_single_scheduler(business):
    runs, service, stuck_id = business
    claim = service.claim(stuck_id)
    assert claim is not None
    plan_step(service, stuck_id, claim["task_id"], claim["lease_generation"])
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
        plan_step(service, run_id, claim["task_id"], claim["lease_generation"])
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
            # What a real ledger says about a call that ended: the process and the connection are
            # accounted for and no control lease is live. Physical capacity is only returned on that
            # statement (spec 0006 section 7), so a fixture that settled a call without one would be
            # describing a call that still holds its target.
            observation=ExecutionObservation(
                started=True,
                process_active=False,
                connection_open=False,
                lease_active=False,
                observed_at=datetime.now(UTC),
            ),
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
        """An adapter that names a target the Run was never authorized to act on."""

        def decide(self, request: ModelRequest) -> ModelSuggestion:
            return ModelSuggestion(
                content={
                    "action": "fake.collect",
                    "target_ip": "192.0.2.200",
                    "target_port": 80,
                    "summary": "A plan that reaches past the snapshot",
                }
            )

    claim = service.claim(run_id)
    assert claim is not None
    planned = plan_step(
        service, run_id, claim["task_id"], claim["lease_generation"], StrayModel()
    )
    assert planned is None

    snapshot = service.snapshot(run_id)
    assert snapshot["calls"] == []
    assert snapshot["budget"]["reserved_tool_calls"] == 0
    assert snapshot["run"]["status"] == "waiting"
    assert snapshot["run"]["reason_code"] == "scope_denied"
    denied = [e for e in service.history(run_id)["events"] if e["type"] == "scope_denied"]
    assert len(denied) == 1
    assert denied[0]["payload"]["planned_target_ip"] == "192.0.2.200"


def test_what_the_execution_side_said_about_a_call_reaches_the_console(business):
    """The console reads the execution side's own account of where a call acted.

    The facts are stored on the call rather than derived at read time, so a profile that changed
    since then cannot rewrite what an old call ran — and the parameters hash travels beside the
    command, because "same parameters as the record" is a claim a reader has to be able to check.
    """
    runs, service, run_id = business
    engine = connect_engine()
    try:
        ticket = next_ticket(engine, service, run_id)
        service.accept(
            ExecutionRecord(
                request=ticket,
                status="accepted",
                runtime=CallRuntime(
                    action_id=ticket.action_id,
                    execution_profile=ticket.execution_profile,
                    target_ip=str(ticket.target_ip),
                    target_port=ticket.target_port,
                    parameters_hash=ticket.parameters_hash,
                    argv=["sh", "-c", "id"],
                    cwd="/workspace",
                    user="10001:10001",
                    instance_id=uuid4(),
                    network_mode="internal",
                    gateway_ids=["gateway-1"],
                    authorized=[f"{ticket.target_ip}:{ticket.target_port}"],
                    image_digests={"tool": "sha256:" + "a" * 64},
                    tool_inventory=["curl", "redis-cli"],
                    engine="29.7.2",
                    architecture="x86_64",
                    profile_version=1,
                    observed_at=datetime.now(UTC),
                ),
                progress=CallProgress(
                    status="accepted",
                    started_at=datetime.now(UTC),
                    timeout_seconds=30,
                ),
            )
        )
        call = next(
            item
            for item in service.snapshot(run_id)["calls"]
            if item["id"] == str(ticket.call_id)
        )
        assert call["parameters_hash"] == ticket.parameters_hash
        assert call["runtime"]["argv"] == ["sh", "-c", "id"]
        assert call["runtime"]["cwd"] == "/workspace"
        assert call["runtime"]["user"] == "10001:10001"
        assert call["runtime"]["network_mode"] == "internal"
        assert call["runtime"]["tool_inventory"] == ["curl", "redis-cli"]
        assert call["progress"]["timeout_seconds"] == 30
    finally:
        engine.dispose()


def test_a_call_without_an_execution_side_statement_says_so_rather_than_guessing(business):
    """An old record has no runtime view, and the console must not fill the gap from the profile."""
    runs, service, run_id = business
    engine = connect_engine()
    try:
        ticket = next_ticket(engine, service, run_id)
        settle(service, ticket)
        call = next(
            item
            for item in service.snapshot(run_id)["calls"]
            if item["id"] == str(ticket.call_id)
        )
        assert call["runtime"] is None
        # The hash still travels: the ticket has it whether or not the execution side spoke.
        assert call["parameters_hash"] == ticket.parameters_hash
    finally:
        engine.dispose()


def test_the_evidence_route_answers_in_the_shape_its_contract_promises(business, tmp_path: Path):
    """The read path is checked through the view the browser receives, not through the dictionary.

    `EvidenceView` forbids fields the browser has no rendering for, so a key added to the payload
    without being declared there is not a cosmetic problem: the request fails, and the operator gets
    an error instead of the evidence the whole conclusion rests on. This check reads a real archived
    file back through that view, which is the seam the earlier checks were missing.
    """
    import hashlib

    from huntweave.contracts.orchestration import EvidenceView
    from huntweave.storage.models import Evidence  # noqa: F401 - documents the table under test

    runs, service, run_id = business
    # The archive this check writes to is its own: the deployment's evidence volume is mounted
    # read-only for the control side, which is itself the boundary being relied on here.
    reading = OrchestrationService(lambda: connect_engine(), tmp_path / "evidence")
    engine = connect_engine()
    try:
        ticket = next_ticket(engine, service, run_id)
        settle(service, ticket)
        call = next(
            item
            for item in service.snapshot(run_id)["calls"]
            if item["id"] == str(ticket.call_id)
        )
        relative = f"{run_id}/{ticket.call_id}/stdout.txt"
        target = (tmp_path / "evidence" / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = b"uid=10001\n"
        target.write_bytes(payload)
        evidence_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO huntweave.evidence (id, run_id, call_id, metadata_json) "
                    "VALUES (:id, :run_id, :call_id, CAST(:metadata AS jsonb))"
                ),
                {
                    "id": evidence_id,
                    "run_id": run_id,
                    "call_id": ticket.call_id,
                    "metadata": json.dumps(
                        {
                            "id": str(evidence_id),
                            "relative_path": relative,
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "size_bytes": len(payload),
                            "available": True,
                            "truncated": False,
                            "redacted": False,
                            "missing_reason": None,
                        }
                    ),
                },
            )
        # The view the route returns, validated exactly as the route validates it.
        view = EvidenceView.model_validate(reading.evidence(evidence_id))
        assert view.content == "uid=10001\n"
        assert view.available and not view.truncated and not view.redacted
        assert view.demonstration  # this Run is a fixed-fixture Run, and the view says so
        # And the call the evidence belongs to names it, so a reader can get from the call to the
        # bytes it rests on by one hop.
        assert evidence_id in [
            UUID(item)
            for item in next(
                candidate["evidence_ids"]
                for candidate in service.snapshot(run_id)["calls"]
                if candidate["id"] == call["id"]
            )
        ]
        assert call["parameters_hash"] == ticket.parameters_hash
    finally:
        engine.dispose()
