"""Concurrency behaviour of the scheduler and the physical execution quota, on real PostgreSQL.

What is checked here is the control plane's own accounting: who takes the next scheduling turn, how
many physical executions the deployment admits at once, which address may run one, and which control
paths must stay usable while none is free. The execution side is the durable fake one — the same
submit/query/cancel/renew path a real one uses — so the mechanism is checked end to end without
claiming anything about real execution, which the readiness gates still refuse (ADR-0010).

Two rules from spec 0006 section 7 get most of the attention, because the acceptance table names
them as things that must be *prevented* rather than things that must happen:

* a physical slot comes back only on a statement from the execution side that nothing the call
  started is still running — a settled result, a timeout and an expired lease are not that;
* a Run is not promised a slot of its own, and a full pool is backpressure on new target execution
  rather than a reason for cancelling, reconciling or reclaiming to stop working.
"""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import (
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
)
from huntweave.contracts.resources import ResourcePolicy
from huntweave.contracts.runs import Budget, PortInput, ProjectCreate, RunCreate, ScopeCreate
from huntweave.runs.dispatch import ExecutionDispatcher
from huntweave.runs.events import append_event
from huntweave.runs.orchestration import OrchestrationService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine

pytestmark = pytest.mark.integration

POLICY = ResourcePolicy(
    control_dispatch_slots=4, global_execution_slots=4, per_ip_execution_slots=1
)


def stopped(started: bool = True) -> ExecutionObservation:
    """What the execution side says about a call nothing of which is still running."""
    return ExecutionObservation(
        started=started,
        process_active=False,
        connection_open=False,
        lease_active=False,
        observed_at=datetime.now(UTC),
    )


class Deployment:
    """A control plane, its engine, and just enough fixture helpers to drive several Runs."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.runs = RunService(lambda: engine)
        self.service = OrchestrationService(lambda: engine, policy=POLICY)

    def run_on(self, targets: str, *, max_tool_calls: int = 8) -> UUID:
        project = self.runs.create_project(ProjectCreate(name=f"concurrency {uuid4()}"))
        now = datetime.now(UTC)
        scope = self.runs.create_scope(
            ScopeCreate(
                project_id=project.id,
                targets_text=targets,
                ports=PortInput(profile="custom-tcp-v1", custom="80"),
                starts_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=1),
                authorization="Fixed fake actions only",
                budget=Budget(max_tool_calls=max_tool_calls),
            )
        )
        run = self.runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), str(uuid4()))
        self.runs.start(run.id, run.version)
        return run.id

    def calls(self, run_id: UUID) -> list[str]:
        return [item["id"] for item in self.service.snapshot(run_id)["calls"]]

    def plan_next(self, run_id: UUID) -> UUID | None:
        """Take this Run's next turn and return the call it planned, if it planned one."""
        claim = self.service.claim(run_id)
        if claim is None:
            return None
        before = set(self.calls(run_id))
        self.service.plan(run_id, claim["task_id"], claim["lease_generation"])
        fresh = [call_id for call_id in self.calls(run_id) if call_id not in before]
        return UUID(fresh[-1]) if fresh else None

    def ticket(self, call_id: UUID) -> ExecutionRequest:
        with self.engine.begin() as connection:
            raw = connection.scalar(
                text("SELECT ticket FROM huntweave.tool_calls WHERE id = :id"),
                {"id": call_id},
            )
        return ExecutionRequest.model_validate(raw)

    def settle(self, call_id: UUID, observation: ExecutionObservation | None) -> None:
        self.service.accept(
            ExecutionRecord(
                request=self.ticket(call_id),
                status="completed",
                result=ExecutionResult(
                    output="FAKE fixed fixture output\n", exit_code=0, evidence=[]
                ),
                observation=observation,
            )
        )

    def count(self, statement: str, **parameters: object) -> int:
        with self.engine.begin() as connection:
            return int(connection.scalar(text(statement), parameters))

    def scalar(self, statement: str, **parameters: object) -> object:
        with self.engine.begin() as connection:
            return connection.scalar(text(statement), parameters)

    def held(self, run_id: UUID) -> int:
        """Calls of this Run that still hold a physical slot, by the same rule the query uses."""
        return self.count(
            "SELECT count(*) FROM huntweave.tool_calls WHERE run_id = :run_id AND ("
            " observation IS NULL"
            " OR observation->>'process_active' IS DISTINCT FROM 'false'"
            " OR observation->>'connection_open' IS DISTINCT FROM 'false'"
            " OR observation->>'lease_active' IS DISTINCT FROM 'false')",
            run_id=run_id,
        )

    def reservations(self, run_id: UUID) -> int:
        return self.count(
            "SELECT count(*) FROM huntweave.budget_reservations WHERE run_id = :run_id",
            run_id=run_id,
        )

    def outbox(self, run_id: UUID) -> int:
        return self.count(
            "SELECT count(*) FROM huntweave.execution_outbox o "
            "JOIN huntweave.tool_calls t ON t.id = o.call_id WHERE t.run_id = :run_id",
            run_id=run_id,
        )

    def events(self, run_id: UUID, kind: str) -> list[dict[str, object]]:
        return [
            event for event in self.service.history(run_id)["events"] if event["type"] == kind
        ]


@pytest.fixture
def deployment() -> Iterator[Deployment]:
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database")
    engine = connect_engine()
    try:
        yield Deployment(engine)
    finally:
        engine.dispose()


def test_a_run_holding_a_call_yields_the_turn_to_the_other_runs(deployment: Deployment) -> None:
    """One claim per turn only makes the scheduler fair if the Run it returns can move.

    A Run with a call in flight cannot plan anything, so claiming it spends the turn on nothing —
    which is how the oldest Run with a long call used to take every turn and starve the Runs behind
    it. Three Runs plan one action each, and no Run is served twice before the others are served.
    """
    runs = [deployment.run_on(f"192.0.2.{11 + index}") for index in range(3)]
    served: list[UUID] = []
    for _ in runs:
        claim = deployment.service.claim()
        assert claim is not None, "a ready Run must be claimable while another holds a call"
        served.append(UUID(claim["run_id"]))
        deployment.service.plan(
            UUID(claim["run_id"]), claim["task_id"], claim["lease_generation"]
        )
    assert served == runs
    assert all(deployment.held(run_id) == 1 for run_id in runs)
    assert deployment.service.claim() is None, "every Run is waiting on its own call"


def test_a_run_that_cannot_be_reconciled_does_not_starve_the_others(deployment: Deployment) -> None:
    """An unconfirmed outcome blocks its own Run and nothing else."""
    stuck = deployment.run_on("192.0.2.12")
    call_id = deployment.plan_next(stuck)
    assert call_id is not None
    deployment.service.unknown(stuck, call_id)

    healthy = [deployment.run_on(f"192.0.2.{13 + index}") for index in range(3)]
    for _ in healthy:
        claim = deployment.service.claim()
        assert claim is not None
        deployment.service.plan(
            UUID(claim["run_id"]), claim["task_id"], claim["lease_generation"]
        )
    assert all(deployment.held(run_id) == 1 for run_id in healthy)
    # The stuck Run is still not claimable, and its ledger is still re-read by the sweep.
    assert deployment.service.claim(stuck) is None
    assert stuck in deployment.service.reconcilable_runs()


def test_one_address_runs_one_active_action_across_runs(deployment: Deployment) -> None:
    """The per-address limit is cross-Run, and it holds a target rather than the Run.

    The first Run's call is in flight and unconfirmed, so the address is occupied. The second Run is
    told to wait for that address — it is not suspended, not failed, and it keeps its budget and its
    place — and the moment the first call's stop is confirmed, the same address admits it.
    """
    first = deployment.run_on("192.0.2.21")
    second = deployment.run_on("192.0.2.21")
    held = deployment.plan_next(first)
    assert held is not None
    assert deployment.service.execution_quota().per_ip_used["192.0.2.21"] == 1

    assert deployment.plan_next(second) is None
    assert deployment.held(second) == 0
    assert deployment.reservations(second) == 0
    assert deployment.outbox(second) == 0
    view = deployment.service.snapshot(second)["run"]
    # Backpressure is a wait, not a refusal: the Run is still running with nothing to recover from.
    assert view["status"] == "running" and view["reason_code"] is None
    events = deployment.events(second, "execution_backpressure")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["waiting_category"] == "target_execution_slot"
    assert payload["resource"] == "192.0.2.21"
    assert (payload["holders"], payload["limit"]) == (1, 1)
    assert payload["policy_version"] == POLICY.policy_version

    # ...and repeated passes say so once, instead of appending the same sentence twice a second.
    for _ in range(3):
        assert deployment.plan_next(second) is None
    assert len(deployment.events(second, "execution_backpressure")) == 1

    deployment.settle(held, stopped())
    assert "192.0.2.21" not in deployment.service.execution_quota().per_ip_used
    assert deployment.plan_next(second) is not None


def test_a_full_global_pool_backpressures_new_target_execution(deployment: Deployment) -> None:
    """Four occupied slots refuse a fifth action on a fifth address, and claim nothing for it."""
    addresses = [f"192.0.2.{30 + index}" for index in range(4)]
    for address in addresses:
        assert deployment.plan_next(deployment.run_on(address)) is not None
    quota = deployment.service.execution_quota()
    assert (quota.global_used, quota.global_limit) == (4, 4)
    assert sorted(quota.per_ip_used) == addresses

    extra = deployment.run_on("192.0.2.44")
    assert deployment.plan_next(extra) is None
    # Nothing was claimed: no call, no budget reservation, no outbox entry left behind.
    assert deployment.held(extra) == 0
    assert deployment.reservations(extra) == 0
    assert deployment.outbox(extra) == 0
    assert deployment.service.execution_quota().global_used == 4
    payload = deployment.events(extra, "execution_backpressure")[0]["payload"]
    assert payload["waiting_category"] == "global_execution_slot"
    assert payload["resource"] is None


def test_cancelling_reconciling_and_settling_stay_usable_with_every_slot_held(
    deployment: Deployment,
) -> None:
    """A full physical pool is backpressure on new actions, not a wedged platform.

    Cancelling, reading the quota, listing what still needs reconciling and settling a result all
    have their own processing capacity, and none of them consults the physical quota.
    """
    holders = [deployment.run_on(f"192.0.2.{50 + index}") for index in range(4)]
    calls: list[UUID] = []
    for run_id in holders:
        call_id = deployment.plan_next(run_id)
        assert call_id is not None
        calls.append(call_id)
    assert deployment.service.execution_quota().global_used == 4

    version = deployment.service.snapshot(holders[0])["run"]["version"]
    cancelled = deployment.service.control(holders[0], "cancel", version)
    # The call is in flight and unconfirmed, so the Run is ending rather than ended — and the
    # operator's request was accepted while every slot was held.
    assert cancelled["status"] == "cancelling"
    # Cancelling is a control action, not a stop: the slot is still occupied.
    assert deployment.service.execution_quota().global_used == 4

    deployment.settle(calls[1], stopped())
    assert deployment.scalar(
        "SELECT status FROM huntweave.tool_calls WHERE id = :id", id=calls[1]
    ) == "succeeded"
    assert deployment.service.execution_quota().global_used == 3

    # What still has to be reconciled is still listed, so the sweep can read those ledgers.
    assert deployment.service.reconcilable_runs()
    deployment.service.unknown(holders[2], calls[2])
    assert deployment.service.claim(holders[2]) is None


def test_a_settled_result_does_not_return_capacity_but_a_confirmed_stop_does(
    deployment: Deployment,
) -> None:
    """The outcome and the stop are separate facts, and only the second one frees the slot.

    The second record is the shape a ledger takes when the stop is confirmed after the outcome
    already arrived: the result is kept, the call does not change status, and capacity comes back
    because the execution side said the process and the connection are accounted for.
    """
    run_id = deployment.run_on("192.0.2.61")
    call_id = deployment.plan_next(run_id)
    assert call_id is not None
    assert deployment.service.execution_quota().global_used == 1

    deployment.settle(call_id, None)
    assert deployment.scalar(
        "SELECT status FROM huntweave.tool_calls WHERE id = :id", id=call_id
    ) == "succeeded"
    assert deployment.service.execution_quota().global_used == 1, "a settled result is not a stop"

    deployment.settle(call_id, stopped())
    assert deployment.service.execution_quota().global_used == 0


def test_an_unknown_outcome_with_a_confirmed_stop_returns_capacity_and_keeps_reconciling(
    deployment: Deployment,
) -> None:
    """`unknown` plus a confirmed stop releases the slot; the result still has to be reconciled."""
    run_id = deployment.run_on("192.0.2.62")
    call_id = deployment.plan_next(run_id)
    assert call_id is not None
    deployment.service.unknown(run_id, call_id)
    assert deployment.service.execution_quota().global_used == 1

    deployment.service.accept(
        ExecutionRecord(
            request=deployment.ticket(call_id),
            status="unknown",
            reason_code="execution_unknown",
            observation=stopped(),
        )
    )
    assert deployment.service.execution_quota().global_used == 0
    assert deployment.scalar(
        "SELECT status FROM huntweave.tool_calls WHERE id = :id", id=call_id
    ) == "unknown"
    assert deployment.service.snapshot(run_id)["run"]["reason_code"] == "execution_unknown"
    assert run_id in deployment.service.reconcilable_runs()
    assert deployment.service.preview(run_id)["reason_code"] == "execution_reconciliation_required"


class RefusingRunner:
    """A Runner whose ledger refuses a new intent outright. Nothing was accepted."""

    def query(self, call_id: UUID) -> ExecutionRecord | None:
        return None

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        raise httpx.HTTPStatusError(
            "refused",
            request=httpx.Request("POST", "http://runner/v1/calls"),
            response=httpx.Response(409, request=httpx.Request("POST", "http://runner/v1/calls")),
        )

    def renew(self, call_id: UUID, generation: int, expires_at: datetime) -> ExecutionRecord:
        raise AssertionError("a refused intent is never renewed")

    def cancel(self, call_id: UUID, generation: int) -> ExecutionRecord:
        raise AssertionError("a refused intent is never cancelled")


def test_a_refused_intent_releases_the_slot_it_reserved(deployment: Deployment) -> None:
    """The ledger has no record of the call, so the reservation it held is provably unused.

    Spec 0006 section 7 lets exactly this release its slot: the call was never dispatched and the
    execution side's own ledger proves it never started. Leaving the lease unstated here would keep
    a physical slot held for an action that never happened.
    """
    run_id = deployment.run_on("192.0.2.63")
    call_id = deployment.plan_next(run_id)
    assert call_id is not None
    assert deployment.service.execution_quota().global_used == 1

    dispatcher = ExecutionDispatcher(deployment.service, RefusingRunner())  # type: ignore[arg-type]
    dispatcher.reconcile(run_id)

    assert deployment.scalar(
        "SELECT status FROM huntweave.tool_calls WHERE id = :id", id=call_id
    ) == "cancelled"
    observation = deployment.scalar(
        "SELECT observation FROM huntweave.tool_calls WHERE id = :id", id=call_id
    )
    assert isinstance(observation, dict)
    assert observation["started"] is False and observation["lease_active"] is False
    assert deployment.service.execution_quota().global_used == 0


def test_each_run_keeps_its_own_event_cursor_and_a_reader_cannot_pass_it(
    deployment: Deployment,
) -> None:
    """Cursors are per Run, dense, and never read ahead of what was committed."""
    first = deployment.run_on("192.0.2.71")
    second = deployment.run_on("192.0.2.72")
    with Session(deployment.engine) as session, session.begin():
        for index in range(3):
            append_event(session, first, "wake", {"run": "first", "n": index})
            append_event(session, second, "wake", {"run": "second", "n": index})

    first_events = deployment.service.history(first)["events"]
    second_events = deployment.service.history(second)["events"]
    assert [event["cursor"] for event in first_events] == list(range(1, len(first_events) + 1))
    assert [event["cursor"] for event in second_events] == list(range(1, len(second_events) + 1))
    wakes = {
        "first": [event for event in first_events if event["type"] == "wake"],
        "second": [event for event in second_events if event["type"] == "wake"],
    }
    assert len(wakes["first"]) == len(wakes["second"]) == 3
    # A reader inside one Run's stream never receives the other Run's events.
    assert {event["payload"]["run"] for event in wakes["first"]} == {"first"}
    assert {event["payload"]["run"] for event in wakes["second"]} == {"second"}
    # And it cannot be positioned past what was committed.
    with pytest.raises(ServiceError) as error:
        deployment.service.history(first, after=len(first_events) + 1)
    assert error.value.reason_code == "event_cursor_ahead" and error.value.status_code == 409


def test_a_duplicate_wakeup_lands_once_even_when_both_arrive_at_the_same_time(
    deployment: Deployment,
) -> None:
    """Idempotence has to hold for the retry the platform actually sees, not only in sequence.

    A retried request or a redelivered intent can arrive while the first one is still committing.
    The cursor row is locked before the source is looked up, so the second writer sees the first
    writer's committed event instead of racing it into a unique violation.
    """
    run_id = deployment.run_on("192.0.2.73")

    def wake(_: int) -> None:
        engine = connect_engine()
        try:
            with Session(engine) as session, session.begin():
                append_event(session, run_id, "wake", {"source": "timer"}, "timer:1")
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(wake, range(8)))
    assert len(deployment.events(run_id, "wake")) == 1
    with Session(deployment.engine) as session, session.begin():
        append_event(session, run_id, "wake", {"source": "timer"}, "timer:1")
    assert len(deployment.events(run_id, "wake")) == 1


def test_a_restart_resumes_from_the_durable_records_and_reserves_no_second_call(
    deployment: Deployment,
) -> None:
    """After a restart the same Run continues: the lease is re-taken, not the action repeated.

    A second scheduler is a restart from the database's point of view — same Run, same records, a
    different lease owner. The turn it takes is planned twice, which is what a replayed graph node
    looks like, and the second planning adds no call: the decision and the call are the same
    records, so the Run resumes instead of acting twice.
    """
    run_id = deployment.run_on("192.0.2.81")
    call_id = deployment.plan_next(run_id)
    assert call_id is not None
    deployment.settle(call_id, stopped())

    restarted = OrchestrationService(lambda: deployment.engine, policy=POLICY)
    task_id = UUID(str(deployment.scalar(
        "SELECT id FROM huntweave.research_tasks WHERE run_id = :run_id", run_id=run_id
    )))
    with deployment.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE huntweave.research_tasks SET lease_expires_at = now() - interval '1s' "
                "WHERE id = :id"
            ),
            {"id": task_id},
        )

    generations: list[int] = []
    planned = 0
    for _ in range(8):
        claim = restarted.claim(run_id)
        if claim is None:
            break
        generations.append(int(claim["lease_generation"]))
        before = len(deployment.calls(run_id))
        first = restarted.plan(run_id, claim["task_id"], claim["lease_generation"])
        after = len(deployment.calls(run_id))
        replay = restarted.plan(run_id, claim["task_id"], claim["lease_generation"])
        assert replay == first, "a replayed turn must answer with the record it already has"
        assert len(deployment.calls(run_id)) == after, "a replay is not a second action"
        assert after - before <= 1
        planned += after - before
    # The restarted owner really re-took the task lease rather than inheriting the old one...
    assert generations and max(generations) >= 2
    # ...and the Run really took a further action from its durable state.
    assert planned >= 1
