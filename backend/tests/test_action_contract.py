"""The versioned action registry: one ticket shape, per-action schemas, no cross-profile calls.

These checks pin the contract that P1's real actions share with the demonstration ones. They are
about what a ticket may say, not about what an executor does with it: the executor checks live
next to the executors.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huntweave.contracts.execution import (
    ACTIONS,
    FAKE_ACTIONS,
    PROFILE_ACTIONS,
    REAL_ACTIONS,
    DiscoverTcpParameters,
    ExecutionRequest,
    FakeParameters,
    ProbeHttpParameters,
    ShellExecParameters,
    action_spec,
    parameters_hash,
)


def ticket(action_id: str, parameters: dict[str, Any], **changes: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "call_id": str(uuid4()),
        "run_id": str(uuid4()),
        "session_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "budget_reservation_id": str(uuid4()),
        "action_id": action_id,
        "parameters": parameters,
        "parameters_hash": parameters_hash(parameters),
        "scope_version": 1,
        "policy_version": 1,
        "lease_generation": 1,
        "lease_expires_at": (now + timedelta(seconds=5)).isoformat(),
        "deadline_at": (now + timedelta(seconds=10)).isoformat(),
        "authorized_until": (now + timedelta(seconds=20)).isoformat(),
        "execution_profile": "real-lab-v1" if action_id in REAL_ACTIONS else "fake-p0-v1",
        "target_ip": "192.0.2.10",
        "target_port": 8080,
        **changes,
    }


def test_every_action_is_registered_once_and_belongs_to_one_profile() -> None:
    assert set(ACTIONS) == set(FAKE_ACTIONS) | set(REAL_ACTIONS)
    assert FAKE_ACTIONS == PROFILE_ACTIONS["fake-p0-v1"]
    assert REAL_ACTIONS == PROFILE_ACTIONS["real-lab-v1"]
    for action_id, spec in ACTIONS.items():
        assert spec.action == action_id
        assert spec.parameters.__name__.endswith("Parameters")
        assert 1 <= spec.default_timeout_seconds <= spec.hard_timeout_seconds <= 300


def test_an_unregistered_action_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ValueError):
        action_spec("shell.exec_all")
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(ticket("nmap.scan", {"command": "id"}))


def test_a_real_action_keeps_its_own_schema() -> None:
    request = ExecutionRequest.model_validate(
        ticket("shell.exec", {"command": "id", "timeout_seconds": 30})
    )
    assert request.spec.profile == "real-lab-v1"
    assert isinstance(request.typed_parameters, ShellExecParameters)
    # The demonstration schema is not accepted for a real action, so one is never a wrapper for
    # the other.
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(ticket("shell.exec", {"scenario": "success"}))
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(
            ticket("shell.exec", {"command": "id", "timeout_seconds": 600})
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"ports": []},
        {"ports": [0]},
        {"ports": [70000]},
        {"ports": [80, 80]},
        {"ports": ["80"]},
    ],
)
def test_a_discovery_ticket_only_asks_for_real_unique_ports(parameters: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(ticket("discover_tcp_services", parameters))
    assert DiscoverTcpParameters(ports=[80, 443])


@pytest.mark.parametrize("path", ["", "relative", "//host/path", "http://elsewhere/"])
def test_an_http_probe_stays_on_the_authorized_target(path: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(ticket("probe_http", {"path": path}))
    probe = ProbeHttpParameters(path="/status")
    assert probe.method == "GET" and probe.scheme == "http"


def test_a_ticket_may_not_name_an_action_from_another_profile() -> None:
    data = ticket("fake.collect", FakeParameters().model_dump())
    data["execution_profile"] = "real-lab-v1"
    with pytest.raises(ValidationError, match="profile"):
        ExecutionRequest.model_validate(data)
    data = ticket("probe_http", {"path": "/"})
    data["execution_profile"] = "fake-p0-v1"
    with pytest.raises(ValidationError, match="profile"):
        ExecutionRequest.model_validate(data)


def test_the_fake_actions_still_share_one_schema_and_one_ticket_shape() -> None:
    for action_id in FAKE_ACTIONS:
        request = ExecutionRequest.model_validate(
            ticket(action_id, FakeParameters(scenario="failure").model_dump())
        )
        assert request.typed_parameters.scenario == "failure"
        assert request.execution_profile == "fake-p0-v1"


def test_the_summary_a_caller_branches_on_is_declared_per_action() -> None:
    assert ACTIONS["discover_tcp_services"].summary_fields == ("open_ports", "closed_ports")
    assert ACTIONS["probe_http"].summary_fields == ("status_code", "server", "content_type")
    # The demonstration actions read no structured summary out of fixed output.
    assert ACTIONS["fake.verify"].summary_fields == ()
