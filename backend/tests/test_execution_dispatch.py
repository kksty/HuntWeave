"""Recovery rules of the outbox dispatcher, checked against a stubbed Runner ledger."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from huntweave.contracts.execution import (
    ExecutionObservation,
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
        parameters=parameters.model_dump(),
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
        self.unreachable_calls: list[UUID] = []

    def snapshot(self, run_id: UUID) -> dict[str, Any]:
        return {"run": {"status": self.status}}

    def pending(self, run_id: UUID) -> list[dict[str, Any]]:
        return [
            item
            for item in self.calls
            if ExecutionRequest.model_validate(item["ticket"]).run_id == run_id
        ]

    def reconcilable(self, run_id: UUID) -> list[dict[str, Any]]:
        return self.pending(run_id)

    def reconcilable_runs(self, limit: int = 20, offset: int = 0) -> list[UUID]:
        run_ids = [
            ExecutionRequest.model_validate(item["ticket"]).run_id for item in self.calls
        ]
        return run_ids[offset : offset + limit]

    def accept(self, record: ExecutionRecord) -> None:
        self.accepted.append(record)

    def unknown(self, run_id: UUID, call_id: UUID, reason: str = "execution_unknown") -> None:
        self.unknowns.append(call_id)

    def unreachable(self, run_id: UUID, call_id: UUID) -> None:
        self.unreachable_calls.append(call_id)


class Runner:
    """Minimal authenticated-ledger stand-in: query, submit, renew and cancel."""

    def __init__(
        self, record: ExecutionRecord | None = None, stopped: ExecutionRecord | None = None
    ):
        self.record = record
        self.stopped = stopped
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
        if self.stopped is not None:
            return self.stopped
        assert self.record is not None
        return self.record.model_copy(
            update={"status": "cancelled", "reason_code": "operator_cancelled"}
        )


def pending(request: ExecutionRequest, status: str = "planned") -> list[dict[str, Any]]:
    return [
        {"id": str(request.call_id), "status": status, "ticket": request.model_dump(mode="json")}
    ]


def rejection(status_code: int, reason_code: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://runner/v1/calls")
    body = {"reason_code": reason_code} if reason_code else None
    response = httpx.Response(status_code, request=request, json=body)
    return httpx.HTTPStatusError("rejected", request=request, response=response)


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


@pytest.mark.parametrize(
    "refusal,expected",
    [("stale_lease_generation", "stale_lease_generation"), (None, "cancelled_before_dispatch")],
)
def test_runner_refusal_of_a_new_intent_settles_without_guessing(
    refusal: str | None, expected: str
):
    # The Runner's own reason is kept, so a fenced or expired intent never looks like an
    # operator cancellation; a refusal without a reason falls back to the local code.
    call = ticket()
    runner = Runner(None)
    runner.submit_error = rejection(409, refusal)
    business = Business("running", pending(call))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert [(item.status, item.reason_code) for item in business.accepted] == [
        ("cancelled", expected)
    ]
    assert business.unknowns == []


def test_sweep_reconciles_calls_the_scheduler_cannot_claim():
    # An unconfirmed call is never claimed again, but its ledger must still be read: this
    # is how a Runner that answers later releases the Run.
    call = ticket()
    record = ExecutionRecord(request=call, status="completed")
    runner = Runner(record)
    business = Business("waiting", pending(call, "unknown"))
    dispatcher = ExecutionDispatcher(business, runner)  # type: ignore[arg-type]
    assert dispatcher.sweep() == 1
    assert business.accepted == [record]


def test_sweep_window_rotates_over_every_pending_run():
    first, second = ticket(), ticket()
    runner = Runner(None)
    runner.query_error = httpx.ConnectError("unreachable")
    business = Business("waiting", [*pending(first, "unknown"), *pending(second, "unknown")])
    dispatcher = ExecutionDispatcher(business, runner)  # type: ignore[arg-type]
    assert dispatcher.sweep(limit=1, offset=0) == 1
    assert business.unreachable_calls == [first.call_id]
    assert dispatcher.sweep(limit=1, offset=1) == 1
    assert business.unreachable_calls == [first.call_id, second.call_id]


@pytest.mark.parametrize("failure", [httpx.ConnectError("unreachable"), rejection(503)])
def test_unavailable_runner_leaves_local_state_alone(failure: httpx.HTTPError):
    # An outage is not an outcome: the pending call keeps its status, no verdict is
    # invented, and the same call_id is reconciled once the Runner answers again.
    call = ticket()
    runner = Runner(None)
    runner.query_error = failure
    business = Business("running", pending(call))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert business.unreachable_calls == [call.call_id]
    assert business.unknowns == [] and business.accepted == []


def test_unavailable_runner_never_abandons_a_new_intent():
    call = ticket()
    runner = Runner(None)
    runner.query_error = httpx.ConnectError("unreachable")
    business = Business("running", pending(call, "dispatched"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert business.unreachable_calls == [call.call_id]
    assert business.unknowns == [] and business.accepted == []


def test_ticket_locally_dispatched_but_absent_from_the_ledger_is_unknown():
    call = ticket()
    runner = Runner(None)
    business = Business("running", pending(call, "running"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert business.unknowns == [call.call_id]
    assert runner.submitted == []
    assert business.unreachable_calls == []


@pytest.mark.parametrize("run_status", ["cancelling", "pausing"])
def test_an_ending_run_asks_the_ledger_to_confirm_the_stop(run_status: str):
    # The operator is ending the Run while one call's outcome is unconfirmed. Waiting for an
    # outcome that will never arrive would wedge the Run, so the dispatcher has the
    # execution side stop what the call left and records that stop as its own fact.
    call = ticket()
    unknown = ExecutionRecord(
        request=call,
        status="unknown",
        reason_code="execution_unknown",
        observation=ExecutionObservation(
            started=True,
            process_active=None,
            connection_open=None,
            lease_active=False,
            observed_at=datetime.now(UTC),
        ),
    )
    stopped = unknown.model_copy(
        update={
            "observation": ExecutionObservation(
                started=True,
                process_active=False,
                connection_open=False,
                lease_active=False,
                observed_at=datetime.now(UTC),
            )
        }
    )
    runner = Runner(unknown, stopped)
    business = Business(run_status, pending(call, "unknown"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert runner.cancelled == [(call.call_id, call.lease_generation)]
    assert business.accepted == [stopped]


def test_an_unknown_call_is_left_alone_while_the_run_is_not_stopping():
    call = ticket()
    unknown = ExecutionRecord(request=call, status="unknown", reason_code="execution_unknown")
    runner = Runner(unknown)
    business = Business("waiting", pending(call, "unknown"))
    ExecutionDispatcher(business, runner).reconcile(call.run_id)  # type: ignore[arg-type]
    assert runner.cancelled == []
    assert business.accepted == [unknown]
