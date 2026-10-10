"""The real executor: one call, one instance, one archived command.

These checks drive the real side over an in-memory runtime, so what is asserted is the decisions —
what the call is authorized to reach, what gets archived, what the record says afterwards, and what
happens to the instance on every path — rather than a container's behaviour. The lab probe covers
the same path against real containers.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_sandbox_lifecycle import PROFILES, FakeRuntime  # noqa: F401

from huntweave.config import SandboxSettings
from huntweave.contracts.execution import (
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
    parameters_hash,
)
from huntweave.execution.ledger import RunnerRejected, RunnerUnavailable
from huntweave.execution.real import RealRunner, action_command
from huntweave.execution.sandbox import SandboxManager, SandboxProfile
from huntweave.execution.server import create_runner

EGRESS_PROFILE = "sandbox-egress-v1"


def real_ticket(action_id: str, parameters: dict[str, Any], **changes: Any) -> ExecutionRequest:
    now = datetime.now(UTC)
    data: dict[str, Any] = {
        "call_id": uuid4(),
        "run_id": uuid4(),
        "session_id": uuid4(),
        "decision_id": uuid4(),
        "scope_id": uuid4(),
        "budget_reservation_id": uuid4(),
        "action_id": action_id,
        "parameters": parameters,
        "parameters_hash": parameters_hash(parameters),
        "scope_version": 1,
        "policy_version": 1,
        "lease_generation": 1,
        "lease_expires_at": now + timedelta(seconds=15),
        "deadline_at": now + timedelta(seconds=60),
        "authorized_until": now + timedelta(seconds=120),
        "execution_profile": "real-lab-v1",
        "target_ip": "10.20.0.5",
        "target_port": 7000,
    }
    data.update(changes)
    return ExecutionRequest.model_validate(data)


def build(
    tmp_path: Path, runtime: FakeRuntime, **runtime_state: Any
) -> tuple[RealRunner, SandboxManager, FakeRuntime]:
    for key, value in runtime_state.items():
        setattr(runtime, key, value)
    manager = SandboxManager(
        runtime=runtime,
        profile=SandboxProfile.load(EGRESS_PROFILE, PROFILES),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    runner = RealRunner(
        tmp_path / "state", tmp_path / "evidence", manager=manager, profile=manager.profile
    )
    return runner, manager, runtime


def settled(
    runner: RealRunner, call_id: UUID, *statuses: str, timeout: float = 10
) -> ExecutionRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = runner.query(call_id)
        if record is not None and record.status in statuses:
            return record
        time.sleep(0.01)
    raise AssertionError(f"call never reached {statuses}")


# --------------------------------------------------------------------------------------------
# One call, one instance, one archived command
# --------------------------------------------------------------------------------------------


def test_a_real_call_runs_inside_an_instance_authorized_for_its_own_target(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    runner, manager, _ = build(tmp_path, runtime, command_stdout="uid=10001\n")
    request = real_ticket("shell.exec", {"command": "id", "timeout_seconds": 30})
    runner.submit(request)
    record = settled(runner, request.call_id, "completed", "failed")

    assert record.status == "completed"
    assert record.reason_code is None
    assert record.result is not None
    assert record.result.exit_code == 0
    assert record.result.summary == {"exit_code": 0}
    # The instance was authorized for the ticket's own endpoint, and the call ended by revoking
    # it: the last policy the gateway received permits nothing.
    applied = [json.loads(payload)["permissions"] for payload in runtime.policies]
    assert [{"address": "10.20.0.5", "port": 7000}] in applied
    assert applied[-1] == []
    # The command ran in the tool container, as the ticket asked for it.
    assert runtime.commands[-1] == ["sh", "-c", "id"]
    # Everything the call produced is archived: transcript, stdout, stderr.
    names = sorted(entry.relative_path.rsplit("/", 1)[-1] for entry in record.result.evidence)
    assert names == [f"{request.call_id}.txt", "stderr.txt", "stdout.txt"]
    stdout = next(e for e in record.result.evidence if e.relative_path.endswith("stdout.txt"))
    archived = (tmp_path / "evidence" / stdout.relative_path).read_text(encoding="utf-8")
    assert archived == "uid=10001\n"
    assert stdout.sha256 and stdout.available


def test_a_call_leaves_no_instance_and_no_permit_behind(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runner, manager, _ = build(tmp_path, runtime, command_stdout="ok\n")
    request = real_ticket("shell_exec_placeholder", {}) if False else real_ticket(
        "shell.exec", {"command": "true"}
    )
    runner.submit(request)
    settled(runner, request.call_id, "completed", "failed")

    instance_id = next(iter(manager.instances))
    record = manager.instances[instance_id]
    assert record.state == "reclaimed"
    assert record.egress.authorized == []
    assert record.egress.revoked_at is not None
    assert manager.resources() == []


def test_the_summary_keeps_only_the_fields_the_action_declared(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    discovered = json.dumps(
        {"open_ports": [7000], "closed_ports": [7001], "smuggled": "value"}
    )
    runner, _, _ = build(tmp_path, runtime, command_stdout=discovered + "\n")
    request = real_ticket("discover_tcp_services", {"ports": [7000, 7001]})
    runner.submit(request)
    record = settled(runner, request.call_id, "completed", "failed")
    assert record.result is not None
    assert record.result.summary == {"open_ports": [7000], "closed_ports": [7001]}
    assert "smuggle" not in json.dumps(record.result.summary)


def test_an_http_probe_reports_what_the_target_answered(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    answered = json.dumps(
        {"status_code": 200, "server": "SimpleHTTP/0.6", "content_type": "text/html"}
    )
    runner, _, _ = build(tmp_path, runtime, command_stdout=answered + "\n")
    request = real_ticket("probe_http", {"path": "/", "method": "GET"})
    runner.submit(request)
    record = settled(runner, request.call_id, "completed", "failed")
    assert record.result is not None
    assert record.result.summary["status_code"] == 200
    # The URL the probe asked for is the ticket's own binding, not something a caller passed in.
    assert action_command(request, 20)[-1] == "http://10.20.0.5:7000/"


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "reason"),
    [(1, False, "action_failed"), (124, True, "execution_timeout")],
)
def test_a_failing_or_timed_out_action_is_reported_as_such(
    tmp_path: Path, exit_code: int, timed_out: bool, reason: str
) -> None:
    runtime = FakeRuntime()
    runner, _, _ = build(
        tmp_path,
        runtime,
        command_stdout="",
        command_stderr="boom\n",
        command_exit_code=exit_code,
        command_timed_out=timed_out,
    )
    request = real_ticket("shell.exec", {"command": "false"})
    runner.submit(request)
    record = settled(runner, request.call_id, "completed", "failed")
    assert record.status == "failed"
    assert record.reason_code == reason
    assert record.result is not None and record.result.exit_code == exit_code
    # The stderr that explains the failure is archived, not summarised away.
    stderr = next(e for e in record.result.evidence if e.relative_path.endswith("stderr.txt"))
    assert (tmp_path / "evidence" / stderr.relative_path).read_text(encoding="utf-8") == "boom\n"


def test_cancelling_ends_the_running_command_and_keeps_what_was_collected(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    gate = threading.Event()
    runner, manager, _ = build(tmp_path, runtime, command_stdout="partial\n", command_gate=gate)
    request = real_ticket("shell.exec", {"command": "sleep 60"})
    runner.submit(request)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not manager.instances:
        time.sleep(0.01)
    assert manager.instances, "the call never prepared an instance"

    cancelled = runner.cancel(request.call_id, request.lease_generation)
    assert cancelled.status == "cancelled"
    assert cancelled.reason_code == "operator_cancelled"
    # The instance this call created is released even though the cancel arrived while it was
    # still being prepared, and the command it would have run never started.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if all(record.state == "reclaimed" for record in manager.instances.values()):
            break
        time.sleep(0.05)
    assert [record.state for record in manager.instances.values()] == ["reclaimed"]
    assert runtime.commands == []
    gate.set()


def test_cancelling_a_running_command_stops_the_instance(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    gate = threading.Event()
    runner, manager, _ = build(tmp_path, runtime, command_stdout="partial\n", command_gate=gate)
    request = real_ticket("shell.exec", {"command": "sleep 60"})
    runner.submit(request)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not runtime.commands:
        time.sleep(0.01)
    assert runtime.commands, "the call never ran its command"

    cancelled = runner.cancel(request.call_id, request.lease_generation)
    assert cancelled.status == "cancelled"
    instance = next(iter(manager.instances.values()))
    assert instance.state in {"stopped", "reclaimed"}, instance.state
    assert instance.egress.authorized == []
    # The command is the one that was killed; whatever it had produced is still archived as the
    # cancelled call's evidence rather than being dropped with it.
    gate.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        settled = runner.query(request.call_id)
        if settled is not None and settled.result is not None:
            break
        time.sleep(0.05)
    assert settled is not None and settled.status == "cancelled"
    assert settled.result is not None and "partial" in settled.result.output


def test_a_call_that_cannot_prepare_is_reported_without_inventing_an_outcome(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    runner, manager, _ = build(tmp_path, runtime)
    runtime.platform = []  # the platform's own networks cannot be identified
    request = real_ticket("shell.exec", {"command": "id"})
    runner.submit(request)
    record = settled(runner, request.call_id, "failed", "cancelled")
    assert record.status == "failed"
    assert record.reason_code == "sandbox_platform_networks_unknown"
    assert record.result is None
    assert manager.resources() == []


def test_a_temporarily_unusable_runtime_is_not_a_refusal(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runner, _, _ = build(tmp_path, runtime)
    runtime.reachable = False
    request = real_ticket("shell.exec", {"command": "id"})
    # Leaving the intent where it is beats settling a call as denied because the runtime blipped.
    with pytest.raises(RunnerUnavailable) as raised:
        runner.submit(request)
    assert raised.value.reason_code == "sandbox_runtime_unreachable"
    assert runner.query(request.call_id) is None


# --------------------------------------------------------------------------------------------
# One surface, two executors, no crossing
# --------------------------------------------------------------------------------------------


def test_a_real_ticket_without_management_is_refused_rather_than_faked(tmp_path: Path) -> None:
    client = TestClient(
        create_runner(
            token="b" * 64,
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
            sandbox_settings=SandboxSettings(
                enabled=False, state_dir=tmp_path / "state", evidence_dir=tmp_path / "evidence"
            ),
        )
    )
    headers = {"Authorization": "Bearer " + "b" * 64}
    ticket = real_ticket("shell.exec", {"command": "id"}).model_dump(mode="json")
    response = client.post("/v1/calls", headers=headers, json=ticket)
    assert response.status_code == 409
    assert response.json()["reason_code"] == "real_execution_disabled"
    # Nothing was accepted, and the demonstration side never saw it.
    assert client.get(f"/v1/calls/{ticket['call_id']}", headers=headers).status_code == 404


def test_both_sides_are_served_by_one_surface_and_never_cross(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = FakeRuntime()
    manager = SandboxManager(
        runtime=runtime,
        profile=SandboxProfile.load(EGRESS_PROFILE, PROFILES),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    runtime.command_stdout = "real\n"
    settings = SandboxSettings(
        enabled=True,
        profile_id=EGRESS_PROFILE,
        profile_dir=PROFILES,
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    client = TestClient(
        create_runner(
            token="b" * 64,
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
            sandbox_settings=settings,
            sandbox_factory=lambda _: manager,
        )
    )
    headers = {"Authorization": "Bearer " + "b" * 64}

    fake = fake_ticket()
    fake_post = client.post("/v1/calls", headers=headers, json=fake.model_dump(mode="json"))
    assert fake_post.status_code == 200
    real = real_ticket("shell.exec", {"command": "id"})
    real_post = client.post("/v1/calls", headers=headers, json=real.model_dump(mode="json"))
    assert real_post.status_code == 200

    for call_id, expected_profile in ((fake.call_id, "fake-p0-v1"), (real.call_id, "real-lab-v1")):
        deadline = time.monotonic() + 10
        body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            body = client.get(f"/v1/calls/{call_id}", headers=headers).json()
            if body["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert body["status"] == "completed"
        assert body["request"]["execution_profile"] == expected_profile
    # The demonstration ledger and the real one are separate files, so neither side can answer
    # for the other's calls.
    state = sorted(path.name for path in (tmp_path / "state").glob("*.json"))
    assert "ledger.json" in state and "real.json" in state


def fake_ticket() -> ExecutionRequest:
    from test_fake_runner import ticket as fake_ticket_builder

    # The builder's own parameters, so the ticket's hash matches what the ticket carries.
    return fake_ticket_builder()


def test_a_real_action_command_never_comes_from_the_caller(tmp_path: Path) -> None:
    request = real_ticket("discover_tcp_services", {"ports": [80, 443]})
    command = action_command(request, 30)
    assert command[0:2] == ["python", "-c"]
    assert command[3] == "10.20.0.5" and command[4] == "80,443"


def test_the_real_executor_refuses_a_demonstration_action(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runner, _, _ = build(tmp_path, runtime)
    with pytest.raises(RunnerRejected) as raised:
        runner.submit(
            ExecutionRequest.model_validate(
                {
                    **real_ticket("shell.exec", {"command": "id"}).model_dump(mode="json"),
                    "execution_profile": "fake-p0-v1",
                    "action_id": "fake.collect",
                    "parameters": FakeParameters().model_dump(),
                    "parameters_hash": parameters_hash(FakeParameters()),
                }
            )
        )
    assert raised.value.reason_code == "action_not_real"
