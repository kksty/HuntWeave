"""What decides the next action: the tool's own result, and the Run's fixed execution mode.

The demonstration adapter used to branch on a label chosen before anything ran, which could not
show a feedback loop. These checks pin the real one: the exit code of a shell action and the ports
a discovery actually found select different next actions, including the branch where nothing was
found. They also pin the two control-side rules that keep a real Run honest — it cannot be opened
while the readiness gates are unmet, and a real call is never offered to a demonstration side.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import ExecutionRequest, parameters_hash
from huntweave.contracts.runs import Budget, RunCreate, ScopeSnapshot
from huntweave.harness.model import PROMPT_VERSION, DeterministicModel, ModelAdapter, ModelRequest
from huntweave.runs.dispatch import ExecutionDispatcher
from huntweave.runs.service import RunService


def context(**values: object) -> dict[str, object]:
    return {"execution_profile": "real-lab-v1", "candidate_ports": [7000, 7001], **values}


def planned(role: str, step: int, context: dict[str, Any]) -> dict[str, Any]:
    """Ask the shipped adapter the way the harness does: one request in, one suggestion out."""
    suggestion = DeterministicModel().decide(
        ModelRequest(
            attempt_id="check-attempt",
            role=role,
            step=step,
            input_hash="0" * 64,
            prompt_version=PROMPT_VERSION,
            context=context,
        )
    )
    return suggestion.content


def test_the_first_real_action_establishes_the_execution_identity() -> None:
    decision = planned("collector", 0, context())
    assert decision["action"] == "shell.exec"
    assert decision["parameters"] == {"command": "id -u", "timeout_seconds": 30}


def test_successful_discovery_leads_to_a_request_and_an_empty_one_does_not() -> None:
    found = planned(
        "reviewer",
        1,
        context(
            last_summary={"open_ports": [7000]},
            last_target={"ip": "192.0.2.10", "port": 7000},
        ),
    )
    assert found["action"] == "probe_http"
    # The endpoint the plan looks at is the ticket's binding, and the action only carries the
    # request itself.
    assert found["target_ip"] == "192.0.2.10" and found["target_port"] == 7000
    assert "port" not in found["parameters"]
    # Without a recorded target the plan cannot name an endpoint: it asks to rotate instead of
    # inventing one, and the ticket builder validates whatever it names against the authorization.
    unnamed = planned("reviewer", 1, context(last_summary={"open_ports": [7000]}))
    assert "target_port" not in unnamed
    # The counterexample: nothing listened, so there is nothing to request — a different action,
    # decided by what the target really answered rather than by a label chosen in advance.
    empty = planned("reviewer", 1, context(last_summary={"open_ports": []}))
    assert empty["action"] == "finish_research"


def test_the_worker_only_discovers_after_it_can_show_it_ran() -> None:
    first = planned("worker", 0, context())
    assert first["action"] == "shell.exec"
    second = planned("worker", 0, context(last_summary={"exit_code": 0}))
    assert second["action"] == "discover_tcp_services"
    assert second["parameters"]["ports"] == [7000, 7001]
    assert planned("worker", 1, context(last_summary={"open_ports": [7000]}))["action"] == (
        "finish_research"
    )


def test_the_demonstration_adapter_is_unchanged() -> None:
    assert planned("collector", 0, {"scenario": "positive"})["action"] == "fake.collect"
    assert planned("worker", 0, {"scenario": "negative"})["action"] == "finish_research"
    assert planned("reviewer", 0, {})["action"] == "fake.review"


def test_the_shipped_adapter_states_its_own_accounting_through_the_one_interface() -> None:
    """Both adapters answer the same Interface, and an answer that used no provider says so.

    Staying silent would mean "no receipt arrived", which is precisely the state the Run has to
    keep pending; saying `billable: false` is a statement the platform can record as known.
    """
    adapter = DeterministicModel()
    assert isinstance(adapter, ModelAdapter)
    suggestion = adapter.decide(
        ModelRequest(
            attempt_id="check-attempt",
            role="collector",
            step=0,
            input_hash="0" * 64,
            prompt_version=PROMPT_VERSION,
            context={"scenario": "positive"},
        )
    )
    assert suggestion.usage.known
    assert suggestion.usage.receipt == {"accounting": "not_applicable", "billable": False}
    assert suggestion.provider == "deterministic" and suggestion.model
    assert suggestion.prompt_version == PROMPT_VERSION


def test_a_real_run_cannot_be_opened_while_the_gates_are_unmet() -> None:
    request = RunCreate(
        scope_id=uuid4(), scope_version=1, execution_profile="real-lab-v1"
    )
    # The gate is read before anything is created; a service without a readiness reader is a
    # deployment that cannot claim real execution at all — and nothing reaches the database.
    for readiness in (None, lambda: False):
        service = RunService(engine=_no_database, readiness=readiness)
        with pytest.raises(ServiceError) as raised:
            service.create_run(request, "key-1")
        assert raised.value.reason_code == "real_execution_not_ready"
    # The demonstration path never asks the execution side for permission, so it goes on to look
    # the scope up (and fails here only because this check has no database).
    demonstration = RunCreate(scope_id=uuid4(), scope_version=1)
    with pytest.raises(ServiceError) as raised:
        RunService(engine=_no_database).create_run(demonstration, "key-2")
    assert raised.value.reason_code == "scope_not_found"


def _no_database() -> None:
    raise ServiceError("scope_not_found", 404)


def test_an_authorization_that_permits_real_execution_is_expressible() -> None:
    snapshot = ScopeSnapshot(
        targets=["192.0.2.10"],
        ports=[7000],
        port_profile="custom-tcp-v1",
        starts_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        authorization="local lab",
        budget=Budget(),
        mode="real",
        execution_profile="real-lab-v1",
    )
    assert snapshot.execution_profile == "real-lab-v1" and snapshot.mode == "real"
    # And an authorization that says nothing about the mode is a demonstration, not a real one.
    assert ScopeSnapshot(
        targets=["192.0.2.10"],
        ports=[7000],
        port_profile="custom-tcp-v1",
        starts_at=snapshot.starts_at,
        expires_at=snapshot.expires_at,
        authorization="local lab",
        budget=Budget(),
    ).execution_profile == "fake-p0-v1"


class Business:
    """Records the dispatcher's decisions without touching PostgreSQL."""

    def __init__(self) -> None:
        self.held: list[str] = []
        self.unknown: list[str] = []

    def reconcilable(self, run_id: object) -> list[dict[str, object]]:
        return [{"status": "planned", "ticket": self.ticket}]

    def snapshot(self, run_id: object) -> dict[str, dict[str, str]]:
        return {"run": {"status": "running"}}

    def hold_for_readiness(self, run_id: object, call_id: object, reason: str) -> None:
        self.held.append(reason)

    def unknown(self, run_id: object, call_id: object, reason: str = "execution_unknown") -> None:
        self.unknown.append(reason)

    def accept(self, record: object) -> None:  # pragma: no cover - nothing is accepted here
        raise AssertionError("a held call must not be accepted")


class Runner:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    def query(self, call_id: object) -> None:
        return None

    def submit(self, ticket: object) -> None:  # pragma: no cover - a held call is not submitted
        self.submitted.append(ticket)
        raise AssertionError("a call without readiness must not be submitted")


def real_ticket() -> ExecutionRequest:
    now = datetime.now(UTC)
    parameters = {"command": "id -u", "timeout_seconds": 30}
    return ExecutionRequest.model_validate(
        {
            "call_id": str(uuid4()),
            "run_id": str(uuid4()),
            "session_id": str(uuid4()),
            "decision_id": str(uuid4()),
            "scope_id": str(uuid4()),
            "budget_reservation_id": str(uuid4()),
            "action_id": "shell.exec",
            "parameters": parameters,
            "parameters_hash": parameters_hash(parameters),
            "scope_version": 1,
            "policy_version": 1,
            "lease_generation": 1,
            "lease_expires_at": (now + timedelta(seconds=10)).isoformat(),
            "deadline_at": (now + timedelta(seconds=30)).isoformat(),
            "authorized_until": (now + timedelta(hours=1)).isoformat(),
            "execution_profile": "real-lab-v1",
            "target_ip": "192.0.2.10",
            "target_port": 7000,
        }
    )


def test_a_real_call_is_held_rather_than_offered_without_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business, runner = Business(), Runner()
    ticket = real_ticket()
    business.ticket = ticket  # type: ignore[attr-defined]
    dispatcher = ExecutionDispatcher(business, runner, real_ready=lambda: False)  # type: ignore[arg-type]
    dispatcher.reconcile(ticket.run_id)
    assert business.held == ["real_execution_not_ready"]
    assert runner.submitted == []

    # With the gates back, the very same call is offered again: holding is a pause, not a verdict.
    ready = ExecutionDispatcher(business, runner, real_ready=lambda: True)  # type: ignore[arg-type]
    ready._submit = lambda run_id, value: None  # type: ignore[method-assign]
    ready.reconcile(ticket.run_id)
    assert business.held == ["real_execution_not_ready"]
