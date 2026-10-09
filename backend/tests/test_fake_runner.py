from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from huntweave.contracts.execution import (
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
    parameters_hash,
)
from huntweave.execution.fake import FakeRunner, RunnerRejected
from huntweave.execution.server import create_runner


def ticket(**changes: object) -> ExecutionRequest:
    now = datetime.now(UTC)
    params = FakeParameters(duration_ms=20)
    data = dict(
        call_id=uuid4(),
        run_id=uuid4(),
        session_id=uuid4(),
        decision_id=uuid4(),
        scope_id=uuid4(),
        budget_reservation_id=uuid4(),
        action_id="fake.collect",
        parameters=params,
        parameters_hash=parameters_hash(params),
        scope_version=1,
        policy_version=1,
        lease_generation=1,
        lease_expires_at=now + timedelta(seconds=5),
        deadline_at=now + timedelta(seconds=10),
        authorized_until=now + timedelta(seconds=20),
        target_ip="192.0.2.1",
        target_port=80,
    )
    data.update(changes)
    return ExecutionRequest.model_validate(data)


def settled(
    runner: FakeRunner, call_id: UUID, *statuses: str, timeout: float = 15
) -> ExecutionRecord:
    """Poll the durable ledger instead of racing a fixed sleep on a loaded host."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        record = runner.query(call_id)
        if record is not None and record.status in statuses:
            return record
        sleep(0.01)
    raise AssertionError(f"call never reached {statuses}")


def test_acknowledgement_loss_reuses_call_and_archived_evidence(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    request = ticket()
    runner.submit(request)
    runner.submit(request)
    record = settled(runner, request.call_id, "completed")
    assert record.result is not None and record.result.output.startswith("FAKE")
    assert sum(event.type == "execution_started" for event in record.events) == 1
    evidence = record.result.evidence[0]
    assert evidence.available and (tmp_path / "evidence" / evidence.relative_path).is_file()
    runner.close()
    reopened = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    assert reopened.submit(request) == record
    reopened.close()


def test_restart_around_execution_is_unknown_and_never_replayed(tmp_path: Path) -> None:
    request = ticket(
        parameters=FakeParameters(duration_ms=1000),
        parameters_hash=parameters_hash(FakeParameters(duration_ms=1000)),
    )
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    runner.submit(request)
    sleep(0.02)
    runner.close()
    reopened = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    record = reopened.submit(request)
    assert record.status == "unknown" and record.reason_code == "execution_unknown"
    assert record.result is None
    sleep(0.05)
    assert reopened.query(request.call_id) == record
    reopened.close()


def test_unknown_after_restart_does_not_claim_a_stop_it_cannot_prove(tmp_path: Path) -> None:
    params = FakeParameters(duration_ms=1000)
    request = ticket(parameters=params, parameters_hash=parameters_hash(params))
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    runner.submit(request)
    settled(runner, request.call_id, "running")
    runner.close()
    record = FakeRunner(tmp_path / "ledger", tmp_path / "evidence").query(request.call_id)
    assert record is not None and record.status == "unknown"
    observation = record.observation
    assert observation is not None
    # The execution started, and a restart proves nothing about the process or the
    # connection it left behind: the ledger reports the gap instead of a stop.
    assert observation.started is True
    assert observation.process_active is None and observation.connection_open is None


def test_cancelling_an_unknown_call_records_the_stop_and_keeps_the_outcome(tmp_path: Path) -> None:
    params = FakeParameters(duration_ms=1000)
    request = ticket(parameters=params, parameters_hash=parameters_hash(params))
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    runner.submit(request)
    settled(runner, request.call_id, "running")
    runner.close()
    reopened = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    stopped = reopened.cancel(request.call_id, 1)
    assert stopped.status == "unknown" and stopped.reason_code == "execution_unknown"
    observation = stopped.observation
    assert observation is not None
    # Stopping what the call left behind is its own fact: the outcome stays unknown.
    assert observation.started is True
    assert observation.process_active is False and observation.connection_open is False
    assert sum(event.type == "execution_stopped" for event in stopped.events) == 1
    assert reopened.cancel(request.call_id, 1).observation == stopped.observation
    reopened.close()


def test_restart_before_start_proves_the_action_never_ran(tmp_path: Path) -> None:
    request = ticket()
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    # The runner stops before it starts accepted work: the acceptance is durable, the start
    # is not, so a later reading proves the action never ran and nothing was left to stop.
    runner.closed.set()
    runner.submit(request)
    runner.close()
    reopened = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    record = reopened.query(request.call_id)
    assert record is not None and record.status == "unknown"
    observation = record.observation
    assert observation is not None
    assert observation.started is False
    assert observation.process_active is False and observation.connection_open is False
    reopened.close()


def test_stale_generations_and_changed_tickets_are_rejected(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    request = ticket(lease_generation=2)
    runner.submit(request)
    with pytest.raises(RunnerRejected, match="call_id_conflict"):
        runner.submit(request.model_copy(update={"target_port": 81}))
    with pytest.raises(RunnerRejected, match="stale_lease_generation"):
        runner.cancel(request.call_id, 1)
    with pytest.raises(RunnerRejected, match="stale_lease_generation"):
        runner.renew(request.call_id, 1, datetime.now(UTC) + timedelta(seconds=5))
    with pytest.raises(RunnerRejected, match="stale_lease_generation"):
        runner.submit(request.model_copy(update={"call_id": uuid4(), "lease_generation": 1}))
    runner.close()


def test_lease_expiry_stops_and_cancel_is_durable(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    params = FakeParameters(duration_ms=1000)
    request = ticket(
        parameters=params,
        parameters_hash=parameters_hash(params),
        # Wide enough that a loaded host still accepts the ticket, short enough that the
        # running action outlives its control lease.
        lease_expires_at=datetime.now(UTC) + timedelta(milliseconds=250),
    )
    runner.submit(request)
    record = settled(runner, request.call_id, "cancelled")
    assert record.reason_code == "control_lease_expired"
    assert record.result is not None and record.result.exit_code is None
    second = ticket(parameters=params, parameters_hash=parameters_hash(params))
    runner.submit(second)
    cancelled = runner.cancel(second.call_id, 1)
    assert cancelled.status == "cancelled"
    runner.close()
    reopened = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    assert reopened.query(second.call_id) == cancelled
    reopened.close()


def test_missing_archive_is_explicitly_unavailable(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    request = ticket()
    runner.submit(request)
    record = settled(runner, request.call_id, "completed")
    assert record.result is not None
    (tmp_path / "evidence" / record.result.evidence[0].relative_path).unlink()
    missing = runner.query(request.call_id)
    assert missing is not None and missing.result is not None
    assert missing.result.evidence[0].available is False
    assert missing.result.evidence[0].missing_reason == "evidence_missing"
    runner.close()


def test_expired_ticket_hash_mismatch_and_scope_change_never_start(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    request = ticket()
    runner.submit(request)
    for update, reason in [
        ({"call_id": uuid4(), "parameters_hash": "0" * 64}, "parameters_hash_mismatch"),
        ({"call_id": uuid4(), "lease_expires_at": datetime.now(UTC)}, "execution_ticket_expired"),
        ({"call_id": uuid4(), "scope_id": uuid4()}, "scope_policy_mismatch"),
        ({"call_id": uuid4(), "policy_version": 2}, "scope_policy_mismatch"),
        (
            {"call_id": uuid4(), "lease_expires_at": datetime.now(UTC) + timedelta(seconds=16)},
            "control_lease_too_long",
        ),
    ]:
        rejected = request.model_copy(update=update)
        with pytest.raises(RunnerRejected, match=reason):
            runner.submit(rejected)
        assert runner.query(rejected.call_id) is None
    runner.close()


def test_renewal_extends_live_control_and_old_generation_cannot_renew(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    # The action outlasts its first control lease, so only a renewal keeps it running;
    # both windows stay wide enough for a loaded CI host.
    params = FakeParameters(duration_ms=2000)
    request = ticket(
        parameters=params,
        parameters_hash=parameters_hash(params),
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=1.5),
    )
    runner.submit(request)
    renewed = runner.renew(request.call_id, 1, datetime.now(UTC) + timedelta(seconds=3))
    assert renewed.request.lease_expires_at > request.lease_expires_at
    for generation in (0, 2):
        with pytest.raises(RunnerRejected, match="stale_lease_generation"):
            runner.renew(request.call_id, generation, datetime.now(UTC) + timedelta(seconds=3))
    # Only the extended control lease lets this 2 s action outlive its first 1.5 s window.
    record = settled(runner, request.call_id, "completed")
    assert record.reason_code is None
    runner.close()


def test_sequential_role_sessions_have_independent_lease_generations(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    collector = ticket(lease_generation=2)
    runner.submit(collector)
    worker = collector.model_copy(
        update={
            "call_id": uuid4(),
            "session_id": uuid4(),
            "action_id": "fake.verify",
            "lease_generation": 1,
        }
    )
    assert runner.submit(worker).status == "accepted"
    runner.close()


def test_long_fixture_has_durable_heartbeat_without_releasing_control(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    params = FakeParameters(duration_ms=6000)
    request = ticket(
        parameters=params,
        parameters_hash=parameters_hash(params),
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=8),
    )
    runner.submit(request)
    deadline = monotonic() + 15
    heartbeats: list = []
    while monotonic() < deadline and not heartbeats:
        current = runner.query(request.call_id)
        assert current is not None
        heartbeats = [event for event in current.events if event.type == "execution_heartbeat"]
        sleep(0.05)
    record = runner.query(request.call_id)
    assert record is not None and record.status == "running"
    assert len(heartbeats) == 1 and heartbeats[0].payload["elapsed_ms"] >= 5000
    runner.cancel(request.call_id, 1)
    runner.close()


def test_fixed_progress_chunks_are_archived_and_stop_after_cancel(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "ledger", tmp_path / "evidence")
    params = FakeParameters(duration_ms=1000)
    request = ticket(parameters=params, parameters_hash=parameters_hash(params))
    runner.submit(request)
    live = settled(runner, request.call_id, "running")
    chunks = [event for event in live.events if event.type == "execution_output"]
    assert chunks and chunks[0].payload["offset"] == 0
    cancelled = runner.cancel(request.call_id, 1)
    # Long enough for the original fixed duration to pass: a cancelled call never finishes.
    sleep(1.2)
    assert runner.query(request.call_id) == cancelled
    assert cancelled.result is not None and cancelled.result.exit_code is None
    archived = tmp_path / "evidence" / cancelled.result.evidence[0].relative_path
    assert archived.read_text() == cancelled.result.output
    assert cancelled.events[-1].payload["elapsed_ms"] >= 0
    runner.close()


def test_authenticated_http_boundary_rejects_shell_and_replays_results(tmp_path: Path) -> None:
    app = create_runner(
        token="x" * 64, state_dir=tmp_path / "state", evidence_dir=tmp_path / "evidence"
    )
    headers = {"Authorization": "Bearer " + "x" * 64}
    with TestClient(app) as client:
        request = ticket()
        data = request.model_dump(mode="json")
        assert client.post("/v1/calls", json=data).status_code == 401
        response = client.post("/v1/calls", json=data, headers=headers)
        assert response.status_code == 200
        assert (
            client.post(
                "/v1/calls", json={**data, "action_id": "shell_exec"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/calls", json={**data, "parameters": {"command": "id"}}, headers=headers
            ).status_code
            == 422
        )
        deadline = monotonic() + 15
        result = client.get(f"/v1/calls/{request.call_id}", headers=headers)
        while monotonic() < deadline and result.json()["status"] != "completed":
            sleep(0.01)
            result = client.get(f"/v1/calls/{request.call_id}", headers=headers)
        assert result.json()["status"] == "completed"
        assert client.post("/v1/calls", json=data, headers=headers).json() == result.json()
        assert (
            client.post(
                f"/v1/calls/{request.call_id}/renew",
                headers=headers,
                json={
                    "lease_generation": 0,
                    "lease_expires_at": request.lease_expires_at.isoformat(),
                },
            ).status_code
            == 422
        )
