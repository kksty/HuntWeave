"""Recovery rules of the outbox dispatcher, checked against a stubbed Runner ledger."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from huntweave.contracts.execution import (
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
    parameters_hash,
)
from huntweave.runs.dispatch import ExecutionDispatcher


def ticket(**changes: Any) -> ExecutionRequest:
    now = datetime.now(UTC)
    parameters = FakeParameters(duration_ms=20)
    data: dict[str, Any] = dict(
        call_id=uuid4(),
        run_id=uuid4(),
        session_id=uuid4(),
        decision_id=uuid4(),
        scope_id=uuid4(),
        budget_reservation_id=uuid4(),
        action_id="fake.collect",
        parameters=parameters,
        parameters_hash=parameters_hash(parameters),
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


class Business:
    """Records the dispatcher's decisions without touching PostgreSQL."""

    def __init__(self, status: str, calls: list[dict[str, Any]]):
        self.status = status
        self.calls = calls
        self.accepted: list[ExecutionRecord] = []
        self.unknowns: list[UUID] = []

    def snapshot(self, run_id: UUID) -> dict[str, Any]:
        return {"run": {"status": self.status}}

    def pending(self, run_id: UUID) -> list[dict[str, Any]]:
        return self.calls

    def accept(self, record: ExecutionRecord) -> None:
        self.accepted.append(record)

    def unknown(self, run_id: UUID, call_id: UUID, reason: str = "execution_unknown") -> None:
        self.unknowns.append(call_id)


class Runner:
    """Minimal authenticated-ledger stand-in: query, submit, renew and cancel."""

    def __init__(self, record: ExecutionRecord | None = None):
        self.record = record
        self.submitted: list[ExecutionRequest] = []
        self.renewed: list[tuple[UUID, int]] = []
        self.cancelled: list[tuple[UUID, int]] = []
        self.submit_error: httpx.HTTPStatusError | None = None
        self.query_error: httpx.HTTPError | None = None

    def query(self, call_id: UUID) -> ExecutionRecord | None:
        if self.query_error is not None:
            raise self.query_error
        return self.record

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(request)
        self.record = ExecutionRecord(request=request, status="accepted")
        return self.record

    def renew(self, call_id: UUID, generation: int, expires_at: datetime) -> ExecutionRecord:
        self.renewed.append((call_id, generation))
        assert self.record is not None
        return self.record

    def cancel(self, call_id: UUID, generation: int) -> ExecutionRecord:
        self.cancelled.append((call_id, generation))
        assert self.record is not None
        return self.record.model_copy(
            update={"status": "cancelled", "reason_code": "operator_cancelled"}
        )


def pending(request: ExecutionRequest, status: str = "planned") -> list[dict[str, Any]]:
    return [
        {"id": str(request.call_id), "status": status, "ticket": request.model_dump(mode="json")}
    ]


def rejection(status_code: int) -> httpx.HTTPStatusError:
    response = httpx.Response(status_code, request=httpx.Request("POST", "http://runner/v1/calls"))
    return httpx.HTTPStatusError("rejected", request=response.request, response=response)


def test_accepted_call_is_maintained_instead_of_fenced_by_a_scheduling_restart():
    # A restart re-leases the role task, so the local lease generation moves while the
    # Runner still runs the call it accepted. That call must be kept alive, not stopped.
    call = ticket(lease_generation=2)
    record = ExecutionRecord(request=call, status="running")
    runner = Runner(record)
    business = Business("running", pending(call, "running"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert runner.renewed == [(call.call_id, 2)]
    assert runner.submitted == [] and runner.cancelled == []
    assert [item.status for item in business.accepted] == ["running"]
    assert business.unknowns == []


def test_cancelling_run_stops_the_accepted_call_with_its_own_generation():
    call = ticket(lease_generation=2)
    record = ExecutionRecord(request=call, status="running")
    runner = Runner(record)
    business = Business("cancelling", pending(call, "running"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert runner.cancelled == [(call.call_id, 2)]
    assert runner.renewed == []
    assert [item.status for item in business.accepted] == ["cancelled"]


def test_valid_undispatched_ticket_is_submitted_once():
    call = ticket()
    runner = Runner(None)
    business = Business("running", pending(call))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert runner.submitted == [call]
    assert [item.status for item in business.accepted] == ["accepted"]


def test_undispatched_ticket_past_its_control_lease_never_starts():
    # The Runner ledger has no record, so the effect never happened: releasing the run
    # is safe, while submitting an expired intent is not.
    call = ticket(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    runner = Runner(None)
    business = Business("running", pending(call))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert runner.submitted == []
    assert [(item.status, item.reason_code) for item in business.accepted] == [
        ("cancelled", "cancelled_before_dispatch")
    ]
    assert business.unknowns == []


def test_runner_refusal_of_a_new_intent_settles_without_guessing():
    call = ticket()
    runner = Runner(None)
    runner.submit_error = rejection(409)
    business = Business("running", pending(call))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert [(item.status, item.reason_code) for item in business.accepted] == [
        ("cancelled", "cancelled_before_dispatch")
    ]
    assert business.unknowns == []


@pytest.mark.parametrize("failure", [httpx.ConnectError("unreachable"), rejection(503)])
def test_unconfirmed_runner_state_stays_unknown(failure: httpx.HTTPError):
    call = ticket()
    runner = Runner(None)
    runner.query_error = failure
    business = Business("running", pending(call))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert business.unknowns == [call.call_id]
    assert business.accepted == []


def test_ticket_locally_dispatched_but_absent_from_the_ledger_is_unknown():
    call = ticket()
    runner = Runner(None)
    business = Business("running", pending(call, "running"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert business.unknowns == [call.call_id]
    assert runner.submitted == []
