"""A blocked model must not hold a Run's row lock, and a late answer must stay late.

These checks need real transactions: the property under test is which locks a transaction holds
while an adapter is thinking, and a fake in-memory database has no locks to hold. They are skipped
unless `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1`, like the other checks that use a real PostgreSQL.

The probe is `SELECT ... FOR UPDATE NOWAIT` from a second connection. It is validated by its own
negative control below: a probe that cannot report a held lock would make every assertion here
vacuous.
"""

import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from _planning import plan_step
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import ExecutionRecord, ExecutionRequest, ExecutionResult
from huntweave.contracts.orchestration import RunSnapshot
from huntweave.contracts.runs import Budget, ProjectCreate, RunCreate, ScopeCreate
from huntweave.harness.model import PROMPT_VERSION, ModelRequest, ModelSuggestion, ModelUsage
from huntweave.runs.orchestration import OrchestrationService, attempt_identity
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine

# A demonstration plan the fake profile can really dispatch: the parameters come from the Run's own
# fixed fake profile, not from the model.
DEMONSTRATION_PLAN: dict[str, Any] = {
    "action": "fake.collect",
    "summary": "Collect fixed service observations.",
    "expected": "A labelled fixed fake result",
    "stop_condition": "One bounded action or execution failure",
}


class BlockingAdapter:
    """An adapter that does not answer until the check lets it, and reports no receipt.

    `ModelUsage()` with no receipt is not "free": it is the case the Run has to keep pending, which
    is what the usage assertions below read.
    """

    def __init__(self, content: dict[str, Any] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[ModelRequest] = []
        self.content = dict(content or DEMONSTRATION_PLAN)

    def decide(self, request: ModelRequest) -> ModelSuggestion:
        self.calls.append(request)
        self.entered.set()
        if not self.release.wait(timeout=30):
            raise AssertionError("the check never released the blocked adapter")
        return ModelSuggestion(
            content=dict(self.content),
            usage=ModelUsage(receipt=None),
            provider="blocking-check",
            model="no-receipt-v1",
            prompt_version=PROMPT_VERSION,
        )


class CountingAdapter:
    """A deterministic answer that counts how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, request: ModelRequest) -> ModelSuggestion:
        self.calls += 1
        return ModelSuggestion(
            content=dict(DEMONSTRATION_PLAN),
            usage=ModelUsage(receipt={"accounting": "counted", "billable": False}),
            provider="counting-check",
            model="counting-v1",
        )


class Outcome:
    """What a worker thread returned, or the exception it raised instead."""

    value: Any = None
    error: BaseException | None = None


def in_thread(target: Callable[[], Any]) -> tuple[threading.Thread, Outcome]:
    outcome = Outcome()

    def run() -> None:
        try:
            outcome.value = target()
        except BaseException as error:  # the check has to see whatever the thread saw
            outcome.error = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def join(thread: threading.Thread, outcome: Outcome) -> None:
    thread.join(timeout=30)
    assert not thread.is_alive(), "the planning pass never finished"
    assert outcome.error is None, f"the planning pass failed: {outcome.error!r}"


def probe_run_lock(engine: Engine, run_id: UUID) -> None:
    """Take the Run's row lock from a second connection, or fail if someone is holding it."""
    with engine.connect() as prober, prober.begin():
        prober.execute(
            text("SELECT id FROM huntweave.runs WHERE id = :id FOR UPDATE NOWAIT"),
            {"id": run_id},
        )


@pytest.fixture
def business() -> Iterator[tuple[RunService, OrchestrationService, Engine, UUID]]:
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database")
    engine = connect_engine()
    runs = RunService(lambda: engine)
    project = runs.create_project(ProjectCreate(name="model outside transactions verification"))
    yield runs, OrchestrationService(lambda: engine), engine, project.id
    engine.dispose()


def new_run(
    runs: RunService,
    project_id: UUID,
    *,
    budget_calls: int = 9,
    window: timedelta = timedelta(hours=1),
) -> Any:
    now = datetime.now(UTC)
    scope = runs.create_scope(
        ScopeCreate(
            project_id=project_id,
            targets_text="192.0.2.1",
            starts_at=now - timedelta(minutes=1),
            expires_at=now + window,
            authorization="Fixed fake actions only",
            budget=Budget(max_tool_calls=budget_calls),
        )
    )
    run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), str(uuid4()))
    runs.start(run.id, run.version)
    return run


def next_ticket(service: OrchestrationService, run_id: UUID) -> ExecutionRequest:
    pending = service.pending(run_id)
    assert pending, "the Run has no outstanding call"
    return ExecutionRequest.model_validate(pending[-1]["ticket"])


def settle(service: OrchestrationService, ticket: ExecutionRequest, output: str) -> None:
    """Settle one call the way the dispatcher would after the execution side reported it."""
    service.accept(
        ExecutionRecord(
            request=ticket,
            status="completed",
            result=ExecutionResult(output=output, exit_code=0, evidence=[]),
        )
    )


def test_the_lock_probe_reports_a_lock_that_is_really_held(business) -> None:
    """A probe that always succeeds proves nothing, so its negative case is checked too."""
    runs, service, engine, project_id = business
    run = new_run(runs, project_id)
    # The negative control: someone holds the row, and the probe refuses to pretend otherwise.
    with engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT id FROM huntweave.runs WHERE id = :id FOR UPDATE"), {"id": run.id}
        )
        with pytest.raises(OperationalError):
            probe_run_lock(engine, run.id)
    # And with the holder gone, the same probe succeeds.
    probe_run_lock(engine, run.id)


def test_a_blocked_model_holds_no_run_lock_and_the_controls_still_run(business) -> None:
    """Acceptance 1 and 4 together: the model is outside the transaction, so the stop wins."""
    runs, service, engine, project_id = business
    run = new_run(runs, project_id)
    # One settled call first, so this Run still has a reconciliation left to perform while it plans.
    claim = service.claim(run.id)
    assert claim is not None
    plan_step(service, run.id, claim["task_id"], claim["lease_generation"])
    settled_ticket = next_ticket(service, run.id)
    settle(service, settled_ticket, "first report\n")

    version = service.snapshot(run.id)["run"]["version"]
    second = service.claim(run.id)
    assert second is not None
    adapter = BlockingAdapter()
    thread, outcome = in_thread(
        lambda: plan_step(service, run.id, second["task_id"], second["lease_generation"], adapter)
    )
    try:
        assert adapter.entered.wait(20), "the adapter was never asked"

        # The intent is durable and readable while the model is still thinking: segment one
        # committed it, which is only possible because it was not waiting for the answer.
        before = service.snapshot(run.id)
        in_flight = before["planning"][-1]
        assert in_flight["status"] == "requested"
        assert in_flight["input_hash"] == adapter.calls[0].input_hash
        assert in_flight["lease_generation"] == second["lease_generation"]
        assert in_flight["task_id"] == second["task_id"]
        assert before["model_usage"]["unknown"] == 1

        # 1. No transaction is open, so the Run's row is free.
        probe_run_lock(engine, run.id)

        # 2. The lease of the very task whose answer is outstanding is renewed.
        renewed = service.claim(run.id)
        assert renewed is not None and renewed["task_id"] == second["task_id"]

        # 3. Reconciliation of this Run still commits: it needs the same row lock.
        settle(service, settled_ticket, "reconciled report\n")

        # 4. And the operator's pause lands while the model is blocked.
        paused = service.control(run.id, "pause", version)
        assert paused["status"] in {"pausing", "paused"}
        # The row is still free after all of that.
        probe_run_lock(engine, run.id)
    finally:
        adapter.release.set()
    join(thread, outcome)

    # The answer arrived too late, and the Run says so instead of applying it.
    after = service.snapshot(run.id)
    refused = after["planning"][-1]
    assert refused["status"] == "refused"
    assert refused["reason_code"] == "invalid_run_state"
    assert refused["suggestion"] == DEMONSTRATION_PLAN
    assert refused["provider"] == "blocking-check" and refused["model"] == "no-receipt-v1"
    assert refused["usage"]["known"] is False and refused["usage"]["receipt"] is None
    # No decision and no call came out of it, and the Run was not restarted.
    assert [call["id"] for call in after["calls"]] == [str(settled_ticket.call_id)]
    assert len(after["decisions"]) == 1
    assert after["run"]["status"] in {"pausing", "paused"}
    # The unaccounted request is named and pending, not read as zero.
    assert after["model_usage"] == {
        "requests": 2,
        "known": 1,
        "unknown": 1,
        "pending_attempt_ids": [UUID(refused["id"])],
    }


def test_a_cancelled_run_refuses_a_late_answer_and_keeps_its_usage(business) -> None:
    runs, service, engine, project_id = business
    run = new_run(runs, project_id)
    claim = service.claim(run.id)
    assert claim is not None
    # The version a control request is compared against is read after the claim: starting a queued
    # Run is itself a version change.
    version = service.snapshot(run.id)["run"]["version"]
    adapter = BlockingAdapter()
    thread, outcome = in_thread(
        lambda: plan_step(service, run.id, claim["task_id"], claim["lease_generation"], adapter)
    )
    try:
        assert adapter.entered.wait(20)
        assert service.control(run.id, "cancel", version)["status"] in {"cancelling", "cancelled"}
    finally:
        adapter.release.set()
    join(thread, outcome)

    snapshot = service.snapshot(run.id)
    refused = snapshot["planning"][-1]
    assert refused["status"] == "refused" and refused["reason_code"] == "invalid_run_state"
    assert snapshot["run"]["status"] == "cancelled"
    assert all(task["status"] == "cancelled" for task in snapshot["tasks"])
    assert snapshot["calls"] == [] and snapshot["decisions"] == []

    # 未知用量仍待核对: a receipt recorded later is stored verbatim, and a different one is refused
    # rather than rewritten in place.
    receipt = {"input_tokens": 128, "output_tokens": 32, "cost_usd": "0.0004"}
    recorded = service.record_model_usage(refused["id"], receipt)
    assert recorded["usage"] == {"known": True, "receipt": receipt}
    assert service.snapshot(run.id)["model_usage"]["unknown"] == 0
    assert service.record_model_usage(refused["id"], receipt)["usage"]["known"] is True
    with pytest.raises(ServiceError) as raised:
        service.record_model_usage(refused["id"], {"input_tokens": 1})
    assert raised.value.reason_code == "reconciliation_conflict"


def test_an_expired_authorization_refuses_a_late_answer(business) -> None:
    """到期后的晚到建议: the window closed while the model was still thinking."""
    runs, service, engine, project_id = business
    run = new_run(runs, project_id, window=timedelta(seconds=2))
    expires_at = datetime.now(UTC) + timedelta(seconds=2)
    claim = service.claim(run.id)
    assert claim is not None
    adapter = BlockingAdapter()
    thread, outcome = in_thread(
        lambda: plan_step(service, run.id, claim["task_id"], claim["lease_generation"], adapter)
    )
    try:
        assert adapter.entered.wait(20)
        # Let the authorization window close while the request is still outstanding.
        time.sleep(max(0.0, (expires_at - datetime.now(UTC)).total_seconds()) + 0.3)
    finally:
        adapter.release.set()
    join(thread, outcome)

    snapshot = service.snapshot(run.id)
    assert snapshot["planning"][-1]["status"] == "refused"
    assert snapshot["planning"][-1]["reason_code"] == "authorization_expired"
    assert snapshot["run"]["status"] == "waiting"
    assert snapshot["run"]["reason_code"] == "authorization_expired"
    assert snapshot["calls"] == []


def test_a_replayed_step_reuses_the_committed_answer_and_dispatches_one_call(business) -> None:
    """Acceptance 3: the answer is reused, and nothing is dispatched or settled twice."""
    runs, service, engine, project_id = business
    run = new_run(runs, project_id)
    claim = service.claim(run.id)
    assert claim is not None
    adapter = CountingAdapter()
    first = plan_step(service, run.id, claim["task_id"], claim["lease_generation"], adapter)
    replay = plan_step(service, run.id, claim["task_id"], claim["lease_generation"], adapter)

    assert adapter.calls == 1, "a replay must not ask the model again"
    assert first == replay and first is not None
    snapshot = service.snapshot(run.id)
    assert len(snapshot["calls"]) == 1
    assert snapshot["budget"]["reserved_tool_calls"] == 1
    assert len([x for x in snapshot["planning"] if x["status"] == "applied"]) == 1
    assert [x["id"] for x in snapshot["decisions"]] == [first["id"]]
    events = service.history(run.id)["events"]
    assert len([e for e in events if e["type"] == "tool_planned"]) == 1
    assert len([e for e in events if e["type"] == "model_requested"]) == 1
    assert snapshot["model_usage"] == {
        "requests": 1,
        "known": 1,
        "unknown": 0,
        "pending_attempt_ids": [],
    }


def test_the_snapshot_contract_accepts_a_run_that_has_model_attempts(business) -> None:
    """The console reads this payload through a strict contract, so every attempt has to fit it.

    `RunSnapshot` forbids unknown fields, which is what makes this more than a shape check: a field
    added to the attempt view without being declared, or a required field left out, fails here
    instead of in the operator's browser.
    """
    runs, service, engine, project_id = business
    run = new_run(runs, project_id)
    claim = service.claim(run.id)
    assert claim is not None
    # Starting a queued Run is itself a version change, so the version a control request is
    # compared against is read after the claim.
    version = service.snapshot(run.id)["run"]["version"]
    adapter = BlockingAdapter()
    thread, outcome = in_thread(
        lambda: plan_step(service, run.id, claim["task_id"], claim["lease_generation"], adapter)
    )
    try:
        assert adapter.entered.wait(20)
        in_flight = RunSnapshot.model_validate(service.snapshot(run.id))
        assert in_flight.planning[-1].status == "requested"
        assert in_flight.planning[-1].suggestion is None
        assert in_flight.tasks[0].ordinal == 0
        assert in_flight.model_usage is not None and in_flight.model_usage.unknown == 1
        service.control(run.id, "pause", version)
    finally:
        adapter.release.set()
    join(thread, outcome)

    settled = RunSnapshot.model_validate(service.snapshot(run.id))
    refused = settled.planning[-1]
    assert refused.status == "refused" and refused.reason_code == "invalid_run_state"
    assert refused.suggestion == DEMONSTRATION_PLAN
    assert refused.usage.known is False and refused.usage.receipt is None
    assert settled.model_usage is not None
    assert settled.model_usage.pending_attempt_ids == [refused.id]
    assert [decision.id for decision in settled.decisions] == []


def test_two_workers_of_one_role_cannot_collide_on_a_task_session_or_decision(business) -> None:
    """Acceptance 2: a role is not an identity.

    Whether a second Worker is *reached* is the multi-Worker scheduling slice's question; that its
    task, session, attempt, decision and call cannot collide with the first Worker's is this one's.
    The check therefore plans whatever the scheduler offers and asserts the identity derivation on
    every row it produced, instead of assuming a particular order of roles.
    """
    runs, service, engine, project_id = business
    run = new_run(runs, project_id, budget_calls=8)
    assert service.claim(run.id) is not None
    first = service.open_follow_up_task(run.id, "worker", "first Worker of this role")
    second = service.open_follow_up_task(run.id, "worker", "reviewer asked for more evidence")
    assert (first["ordinal"], second["ordinal"]) == (0, 1)
    assert first["task_id"] != second["task_id"]
    assert first["session_id"] != second["session_id"]

    # The database refuses a second task with the same role and ordinal, so "two Workers of one
    # role" cannot silently become one row no matter what a caller does.
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO huntweave.research_tasks "
                "(id, run_id, role, ordinal, status, step, version, lease_generation) "
                "VALUES (:id, :run_id, 'worker', 0, 'queued', 0, 1, 0)"
            ),
            {"id": uuid4(), "run_id": run.id},
        )

    planned: dict[str, set[str]] = {}
    calls: set[str] = set()
    for _ in range(16):
        claim = service.claim(run.id)
        if claim is None:
            break
        before = {call["id"] for call in service.snapshot(run.id)["calls"]}
        plan_step(service, run.id, claim["task_id"], claim["lease_generation"])
        snapshot = service.snapshot(run.id)
        fresh = [call for call in snapshot["calls"] if call["id"] not in before]
        if fresh:
            attempt = snapshot["planning"][-1]
            assert attempt["task_id"] == claim["task_id"]
            planned.setdefault(attempt["task_id"], set()).add(fresh[-1]["decision_id"])
            calls.add(fresh[-1]["id"])
            settle(service, next_ticket(service, run.id), "round output\n")

    snapshot = service.snapshot(run.id)
    tasks = {task["id"]: task for task in snapshot["tasks"]}
    assert len(planned) >= 2, f"only one task ever produced a decision: {sorted(planned)}"
    # Every decision is what its own task, step and attempt ordinal derive, and every call hangs
    # off its own decision: nothing here is derived from the role.
    for attempt in snapshot["planning"]:
        expected = attempt_identity(
            UUID(attempt["task_id"]), attempt["step"], attempt["attempt_ordinal"]
        )
        assert UUID(attempt["id"]) == expected
        assert attempt["task_id"] in tasks
    produced = [item for items in planned.values() for item in items]
    assert len(produced) == len(set(produced)), "two tasks produced the same decision"
    assert len(calls) == len(produced)
    # The two same-role tasks are two rows with two ordinals, whatever they were asked.
    worker_ids = {first["task_id"], second["task_id"]}
    assert worker_ids <= set(tasks)
    assert {tasks[x]["ordinal"] for x in worker_ids} == {0, 1}
    assert len({tasks[x]["id"] for x in worker_ids}) == 2
