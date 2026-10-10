"""The concurrency fixture: measured capacity and latency for issue #21's calibration.

Two dimensions are scaled together, because the scheduler's cost is not a function of either one
alone:

* **active calls** — how many Runs are holding a physical slot while a pass runs (1, 2, 4, 5, where
  5 is the first count that cannot be admitted at all with the default quota);
* **historical ledger volume** — how many settled calls the database already holds when that pass
  runs (0, 2 000, 20 000).

The operations measured are the ones #21 names: a scheduling turn, a settlement with evidence, a
cancellation, a lease renewal, a reconciliation sweep, an archive write, and the wait for the
advisory lock the reservation takes. The result is a table of wall-clock numbers, not a verdict:
whether the shape needs changing is #32's question (three hot spots inside one process), and this
records what the concurrent-scheduler cost actually is so that question can be answered from data.

The thresholds the run is judged against come from `huntweave.contracts.resources` and are printed
with the numbers, so a result cannot be met by editing the standard afterwards. Re-run it with:

    cd backend
    $env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
    <venv>/python -m pytest tests/test_concurrency_stress.py -q -s

`HUNTWEAVE_STRESS_LEDGER_SIZES`, `HUNTWEAVE_STRESS_ACTIVE_COUNTS` and
`HUNTWEAVE_STRESS_ITERATIONS` narrow the run without editing this file.
"""

import json
import os
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from huntweave.contracts.execution import (
    ExecutionEvidence,
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
)
from huntweave.contracts.resources import (
    LIVE_SLOT_SQL,
    ConcurrencyAcceptanceThresholds,
    ResourcePolicy,
)
from huntweave.contracts.runs import Budget, PortInput, ProjectCreate, RunCreate, ScopeCreate
from huntweave.execution.archive import EvidenceArchive
from huntweave.runs.dispatch import ExecutionDispatcher
from huntweave.runs.orchestration import (
    EXECUTION_QUOTA_LOCK,
    GLOBAL_SLOT_LOCK,
    OrchestrationService,
)
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine

pytestmark = pytest.mark.integration

POLICY = ResourcePolicy(
    control_dispatch_slots=4, global_execution_slots=4, per_ip_execution_slots=1
)
THRESHOLDS = ConcurrencyAcceptanceThresholds()

DEFAULT_LEDGER_SIZES = (0, 2_000, 20_000)
DEFAULT_ACTIVE_COUNTS = (1, 2, 4, 5)
DEFAULT_ITERATIONS = 15
LOCK_HOLD_SECONDS = 0.2
CALLS_PER_SEEDED_RUN = 20
# Addresses the fixture drives. Every one of them is inside a block reserved for documentation, so
# nothing here can name a host that exists.
HELD_ADDRESS = "198.51.100.{}"
REFUSED_ADDRESS = "198.51.100.{}"
SCRATCH_ADDRESS = "203.0.113.{}"


def from_environment(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.environ.get(name)
    return tuple(int(item) for item in raw.split(",") if item.strip()) if raw else default


def summarise(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": float(len(ordered)),
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] * 1000, 3),
        "max_ms": round(ordered[-1] * 1000, 3),
    }


def timed(action: Callable[[], object], repeats: int) -> list[float]:
    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        action()
        samples.append(time.perf_counter() - start)
    return samples


def stopped_observation() -> ExecutionObservation:
    return ExecutionObservation(
        started=True,
        process_active=False,
        connection_open=False,
        lease_active=False,
        observed_at=datetime.now(UTC),
    )


class Runner:
    """A ledger stand-in that keeps a call running, so a renewal measures the real path."""

    def __init__(self) -> None:
        self.records: dict[UUID, ExecutionRecord] = {}

    def accept(self, request: ExecutionRequest) -> ExecutionRecord:
        record = ExecutionRecord(
            request=request,
            status="running",
            observation=ExecutionObservation(
                started=True,
                process_active=True,
                connection_open=True,
                lease_active=True,
                observed_at=datetime.now(UTC),
            ),
        )
        self.records[request.call_id] = record
        return record

    def query(self, call_id: UUID) -> ExecutionRecord | None:
        return self.records.get(call_id)

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        return self.accept(request)

    def renew(self, call_id: UUID, generation: int, expires_at: datetime) -> ExecutionRecord:
        return self.records[call_id]

    def cancel(self, call_id: UUID, generation: int) -> ExecutionRecord:
        return self.records[call_id]


class Fixture:
    """The measured deployment: real services over a real database, plus a synthetic history."""

    def __init__(self, engine: Engine, evidence_root: Path):
        self.engine = engine
        self.evidence_root = evidence_root
        self.runs = RunService(lambda: engine)
        self.service = OrchestrationService(lambda: engine, policy=POLICY)
        self.runner = Runner()
        self.dispatcher = ExecutionDispatcher(self.service, self.runner)  # type: ignore[arg-type]
        self.project = self.runs.create_project(ProjectCreate(name=f"stress {uuid4()}"))
        self.scopes: dict[str, UUID] = {}
        self.generation = 0
        self.scratch = 0

    # -- addresses and Runs --------------------------------------------------------------------

    def _scope_for(self, address: str) -> UUID:
        if address not in self.scopes:
            now = datetime.now(UTC)
            scope = self.runs.create_scope(
                ScopeCreate(
                    project_id=self.project.id,
                    targets_text=address,
                    ports=PortInput(profile="custom-tcp-v1", custom="80"),
                    starts_at=now - timedelta(minutes=1),
                    expires_at=now + timedelta(hours=2),
                    authorization="Fixed fake actions only",
                    budget=Budget(max_tool_calls=1000),
                )
            )
            self.scopes[address] = scope.id
        return self.scopes[address]

    def ready_run(self, address: str) -> UUID:
        run = self.runs.create_run(
            RunCreate(scope_id=self._scope_for(address), scope_version=1), str(uuid4())
        )
        self.runs.start(run.id, run.version)
        return run.id

    def scratch_run(self) -> UUID:
        """A Run on an address of its own, so no measurement is ever refused by the per-IP rule."""
        self.scratch += 1
        return self.ready_run(SCRATCH_ADDRESS.format(self.scratch))

    # -- synthetic history ---------------------------------------------------------------------

    def seed_to(self, target: int) -> int:
        """Grow the ledger to `target` calls with already-settled, provably stopped history.

        The seeded calls must not hold a physical slot: the dimension is the cost of *reading* a
        large ledger, so every seeded call carries the same statement a finished call really has.
        Each pass takes a fresh identifier range, so re-seeding adds history instead of colliding
        with what an earlier pass wrote.
        """
        current = self.count_ledger()
        if target <= current:
            return current
        self.generation += 1
        base = self.generation * 1_000_000
        runs = max(1, (target - current) // CALLS_PER_SEEDED_RUN)
        scope_id = self._scope_for(HELD_ADDRESS.format(1))
        snapshot = json.dumps(self.runs.scope(scope_id).snapshot.model_dump(mode="json"))
        stopped = json.dumps(
            {
                "started": True,
                "process_active": False,
                "connection_open": False,
                "lease_active": False,
                "observed_at": datetime.now(UTC).isoformat(),
            }
        )
        ticket = json.dumps(self._seed_ticket(scope_id))
        # Deterministic identifiers inside a per-pass range, so the foreign keys line up without a
        # round trip per row. `lpad` keeps the last UUID group at twelve digits; the leading group
        # is what tells the seeded rows of one table from another.
        runs_from = f"generate_series({base} + 1, {base} + :runs)"
        calls_from = f"{runs_from} r, generate_series(1, :per_run) c"
        seed_run = "'00000000-0000-4000-b000-' || lpad(g::text, 12, '0')"
        keyed_run = "'00000000-0000-4000-b000-' || lpad(r::text, 12, '0')"
        seed_call = "'00000000-0000-4000-c000-' || lpad((r * 1000 + c)::text, 12, '0')"
        seed_decision = "'00000000-0000-4000-e000-' || lpad((r * 1000 + c)::text, 12, '0')"
        seed_task = "'00000000-0000-4000-f000-' || lpad(r::text, 12, '0')"
        seed_session = "'00000000-0000-4000-a100-' || lpad(r::text, 12, '0')"
        # A Run's history is not only its calls: the task, the role session and the decisions the
        # calls were bound to are part of the volume the scheduler reads past, and the foreign keys
        # would not let the calls exist without them.
        statements = (
            "INSERT INTO huntweave.research_tasks (id, run_id, role, status, step, version,"
            f" lease_generation, lease_owner, lease_expires_at) SELECT ({seed_task})::uuid,"
            f" ({keyed_run})::uuid, 'collector', 'completed', :per_run, :per_run + 1, 1, NULL,"
            f" NULL FROM {runs_from} r",
            "INSERT INTO huntweave.agent_sessions (id, run_id, task_id, role, status, context)"
            f" SELECT ({seed_session})::uuid, ({keyed_run})::uuid, ({seed_task})::uuid,"
            f" 'collector', 'completed', CAST('{{}}' AS jsonb) FROM {runs_from} r",
            "INSERT INTO huntweave.decisions (id, run_id, session_id, step, content)"
            f" SELECT ({seed_decision})::uuid, ({keyed_run})::uuid, ({seed_session})::uuid, c,"
            " CAST('{\"action\": \"fake.collect\"}' AS jsonb)"
            f" FROM {calls_from}",
            "INSERT INTO huntweave.tool_calls (id, run_id, session_id, decision_id, status,"
            " ticket, created_at, observation)"
            f" SELECT ({seed_call})::uuid, ({keyed_run})::uuid, ({seed_session})::uuid,"
            f" ({seed_decision})::uuid, 'succeeded', CAST(:ticket AS jsonb),"
            " now() - interval '1 hour', CAST(:stopped AS jsonb)"
            f" FROM {calls_from}",
            "INSERT INTO huntweave.budget_reservations (id, run_id, call_id, settled,"
            " output_bytes)"
            " SELECT ('00000000-0000-4000-d000-' || lpad((r * 1000 + c)::text, 12, '0'))::uuid,"
            f" ({keyed_run})::uuid, ({seed_call})::uuid, true, 0"
            f" FROM {calls_from}",
            "INSERT INTO huntweave.audit_events (run_id, cursor, type, payload, source_event_id,"
            " created_at)"
            f" SELECT ({keyed_run})::uuid, c, 'tool_completed', CAST('{{}}' AS jsonb),"
            f" 'seed:' || r || ':' || c, now() - interval '1 hour' FROM {calls_from}",
            "INSERT INTO huntweave.event_cursors (run_id, cursor)"
            f" SELECT ({seed_run})::uuid, :per_run FROM {runs_from} g"
            " ON CONFLICT (run_id) DO NOTHING",
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO huntweave.runs (id, project_id, scope_id, scope_version,"
                    " scope_snapshot, idempotency_key, request_hash, status, phase, version,"
                    " created_at, demonstration_scenario, demonstration_duration_ms,"
                    " execution_profile, started_at, reason_code)"
                    f" SELECT ({seed_run})::uuid, :project, :scope, 1, CAST(:snapshot AS jsonb),"
                    " 'stress-' || g, repeat('0', 64), 'closed', 'awaiting_human', 5,"
                    " now() - interval '1 hour', 'positive', 1, 'fake-p0-v1',"
                    " now() - interval '1 hour', NULL"
                    f" FROM {runs_from} g"
                ),
                {
                    "project": self.project.id,
                    "scope": scope_id,
                    "snapshot": snapshot,
                    "runs": runs,
                },
            )
            for statement in statements:
                connection.execute(
                    text(statement),
                    {
                        "ticket": ticket,
                        "stopped": stopped,
                        "runs": runs,
                        "per_run": CALLS_PER_SEEDED_RUN,
                    },
                )
        return self.count_ledger()

    @staticmethod
    def _seed_ticket(scope_id: UUID) -> dict[str, object]:
        return {
            "call_id": str(UUID(int=0)),
            "run_id": str(UUID(int=0)),
            "session_id": str(UUID(int=0)),
            "decision_id": str(UUID(int=0)),
            "scope_id": str(scope_id),
            "budget_reservation_id": str(UUID(int=0)),
            "action_id": "fake.collect",
            "parameters": {"scenario": "success", "duration_ms": 0},
            "parameters_hash": "0" * 64,
            "scope_version": 1,
            "policy_version": 1,
            "lease_generation": 1,
            "lease_expires_at": "2026-10-11T00:00:00+00:00",
            "deadline_at": "2026-10-11T00:00:00+00:00",
            "authorized_until": "2026-10-11T00:00:00+00:00",
            "execution_profile": "fake-p0-v1",
            "target_ip": "198.51.100.1",
            "target_port": 80,
        }

    def count_ledger(self) -> int:
        with self.engine.begin() as connection:
            return int(connection.scalar(text("SELECT count(*) FROM huntweave.tool_calls")))

    def held_calls(self) -> int:
        with self.engine.begin() as connection:
            return int(
                connection.scalar(
                    text(f"SELECT count(*) FROM huntweave.tool_calls WHERE {LIVE_SLOT_SQL}")
                )
            )

    # -- driving the real paths ----------------------------------------------------------------

    def plan_once(self, run_id: UUID) -> UUID | None:
        """Take one scheduling turn for this Run and return the call it planned, if any."""
        before = {item["id"] for item in self.service.snapshot(run_id)["calls"]}
        claim = self.service.claim(run_id)
        if claim is None:
            return None
        self.service.plan(run_id, claim["task_id"], claim["lease_generation"])
        fresh = [
            item["id"]
            for item in self.service.snapshot(run_id)["calls"]
            if item["id"] not in before
        ]
        return UUID(fresh[-1]) if fresh else None

    def hold(self, ordinal: int) -> tuple[UUID, UUID] | None:
        """A Run on its own authorized address with one call in flight and unconfirmed."""
        run_id = self.ready_run(HELD_ADDRESS.format(ordinal + 1))
        call_id = self.plan_once(run_id)
        if call_id is None:
            return None
        # The ledger's own word that this call is running: capacity has to stay held by it.
        self.service.accept(self.runner.accept(self.ticket(call_id)))
        return run_id, call_id

    def ticket(self, call_id: UUID) -> ExecutionRequest:
        with self.engine.begin() as connection:
            raw = connection.scalar(
                text("SELECT ticket FROM huntweave.tool_calls WHERE id = :id"), {"id": call_id}
            )
        return ExecutionRequest.model_validate(raw)

    def settle(self, call_id: UUID) -> None:
        evidence = [
            ExecutionEvidence(
                id=uuid4(),
                relative_path=f"{call_id}/stdout-{index}.txt",
                sha256="0" * 64,
                size_bytes=1024,
                available=True,
            )
            for index in range(2)
        ]
        self.service.accept(
            ExecutionRecord(
                request=self.ticket(call_id),
                status="completed",
                result=ExecutionResult(output="measured\n", exit_code=0, evidence=evidence),
                observation=stopped_observation(),
            )
        )

    def cancel(self, run_id: UUID) -> None:
        """Ask this Run to end, unless it already has.

        A Run that already converged to a terminal status is not a cancelled request: asking again
        would be exercising `invalid_run_state`, not the cancellation path the fixture measures.
        """
        run = self.service.snapshot(run_id)["run"]
        if run["status"] in {"closed", "cancelled", "failed", "draft"}:
            return
        self.service.control(run_id, "cancel", run["version"])

    def calls_of(self, run_id: UUID) -> int:
        return len(self.service.snapshot(run_id)["calls"])

    def settle_step(self) -> float:
        """Settle one call that carries evidence, and time only the settlement.

        The Run, its ticket and its reservation are prepared outside the measured window: what the
        record needs is the cost of the settlement path — the result, the evidence rows and the
        convergence it triggers — not the cost of setting the fixture up.
        """
        run_id = self.scratch_run()
        call_id = self.plan_once(run_id)
        assert call_id is not None
        start = time.perf_counter()
        self.settle(call_id)
        elapsed = time.perf_counter() - start
        self.cancel(run_id)
        return elapsed

    def release(self, holders: list[tuple[UUID, UUID]]) -> None:
        for run_id, call_id in holders:
            self.settle(call_id)
            self.cancel(run_id)


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[Fixture]:
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database")
    engine = connect_engine()
    try:
        yield Fixture(engine, tmp_path / "evidence")
    finally:
        engine.dispose()


def test_measured_capacity_and_latency_under_concurrent_runs(fixture: Fixture) -> None:
    """Produce the calibration table and check it against the thresholds fixed before the run.

    Every number printed here belongs in the validation record. The assertions cover only the
    thresholds the record is judged by, so a slow machine reports slow numbers rather than failing a
    check whose standard was never about this machine.
    """
    repeats = int(os.environ.get("HUNTWEAVE_STRESS_ITERATIONS", str(DEFAULT_ITERATIONS)))
    ledger_sizes = from_environment("HUNTWEAVE_STRESS_LEDGER_SIZES", DEFAULT_LEDGER_SIZES)
    active_counts = from_environment("HUNTWEAVE_STRESS_ACTIVE_COUNTS", DEFAULT_ACTIVE_COUNTS)
    slots = POLICY.global_execution_slots
    rows: list[dict[str, object]] = []
    worst: dict[str, float] = {}
    duplicate_actions = 0
    wrong_releases = 0

    def record(operation: str, samples: list[float], **dimensions: object) -> dict[str, float]:
        summary = summarise(samples)
        rows.append({"operation": operation, **dimensions, **summary})
        worst[operation] = max(worst.get(operation, 0.0), summary["max_ms"])
        return summary

    for ledger in ledger_sizes:
        fixture.seed_to(ledger)
        # Seeded history is settled and provably stopped: it must cost reads and occupy nothing.
        assert fixture.held_calls() == 0
        for active in active_counts:
            admitted = min(active, slots)
            holders = [item for item in (fixture.hold(i) for i in range(admitted)) if item]
            assert len(holders) == admitted
            quota = fixture.service.execution_quota()
            assert quota.global_used == admitted, (quota.global_used, admitted)
            assert fixture.held_calls() == quota.global_used
            # Nothing was released without a trusted stop, and nothing already in flight was acted
            # on a second time.
            wrong_releases += max(0, admitted - quota.global_used)
            duplicate_actions += sum(
                1 for run_id, _ in holders if fixture.calls_of(run_id) != 1
            )
            # The next address cannot be admitted while the pool is full: that is the backpressure
            # #21 asks for. It is refused, not failed, and it claims nothing.
            extra = fixture.hold(admitted)
            assert (extra is None) == (admitted == slots)
            if extra is not None:
                holders.append(extra)

            record(
                "claim_turn",
                timed(fixture.service.claim, repeats),
                ledger_calls=ledger,
                active_calls=admitted,
            )
            record(
                "quota_count",
                timed(fixture.service.execution_quota, repeats),
                ledger_calls=ledger,
                active_calls=admitted,
            )
            if admitted == slots:
                observer = fixture.scratch_run()
                record(
                    "plan_refused_pool_full",
                    timed(lambda target=observer: fixture.plan_once(target), repeats),
                    ledger_calls=ledger,
                    active_calls=admitted,
                )
                assert fixture.calls_of(observer) == 0
                fixture.cancel(observer)
            fixture.release(holders)

    # The control paths and the archive, measured with the pool as full as it gets. Settling and
    # archiving act on a call of their own, so they are measured with two slots given back; neither
    # of them consults the quota.
    for ledger in ledger_sizes:
        fixture.seed_to(ledger)
        holders = [item for item in (fixture.hold(i) for i in range(slots)) if item]
        assert len(holders) == slots
        record(
            "cancel",
            timed(lambda target=holders[0][0]: fixture.cancel(target), repeats),
            ledger_calls=ledger,
            active_calls=slots,
        )
        record(
            "renew",
            timed(lambda target=holders[1][0]: fixture.dispatcher.reconcile(target), repeats),
            ledger_calls=ledger,
            active_calls=slots,
        )
        record(
            "sweep",
            timed(lambda: fixture.dispatcher.sweep(20, 0), repeats),
            ledger_calls=ledger,
            active_calls=slots,
        )
        record("quota_lock_wait", lock_wait(fixture), ledger_calls=ledger, active_calls=slots)
        fixture.release(holders[2:])
        record(
            "settle_with_evidence",
            [fixture.settle_step() for _ in range(repeats)],
            ledger_calls=ledger,
            active_calls=len(holders[:2]),
        )
        record(
            "archive_write",
            timed(lambda: archive_write(fixture), repeats),
            ledger_calls=ledger,
            active_calls=len(holders[:2]),
        )
        fixture.release(holders[:2])

    report = {
        "policy_version": POLICY.policy_version,
        "thresholds_version": THRESHOLDS.thresholds_version,
        "global_execution_slots": slots,
        "per_ip_execution_slots": POLICY.per_ip_execution_slots,
        "ledger_sizes": list(ledger_sizes),
        "active_counts": list(active_counts),
        "iterations": repeats,
        "rows": rows,
        "worst_ms": worst,
        "duplicate_actions": duplicate_actions,
        "wrong_releases": wrong_releases,
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    control_path = max(
        worst["cancel"], worst["renew"], worst["settle_with_evidence"], worst["sweep"]
    )
    assert control_path / 1000 <= THRESHOLDS.control_path_seconds
    assert duplicate_actions == THRESHOLDS.unknown_retries_max
    assert wrong_releases <= THRESHOLDS.wrong_releases_max
    growth = growth_ms_per_1000_calls(rows, "quota_count")
    if growth is not None:
        assert growth <= THRESHOLDS.pass_growth_ms_per_1000_calls


def archive_write(fixture: Fixture) -> None:
    writer = EvidenceArchive(fixture.evidence_root, artifact_quota_bytes=1 << 20)
    writer.archive(f"stress/{uuid4()}/stdout.txt", "x" * 65536)


def lock_wait(fixture: Fixture) -> list[float]:
    """How long a reservation waits for the advisory lock another transaction is holding.

    One deliberate measurement rather than a sampled one: the holder takes the global slot key and
    releases it after a fixed delay, so the answer is the wait rather than a mixture of the wait and
    the work behind it. An uncontended reservation on the same database is measured first and
    subtracted, because the plan thread starts before it reaches the lock.
    """
    baseline_run = fixture.scratch_run()
    baseline_claim = fixture.service.claim(baseline_run)
    assert baseline_claim is not None
    baseline = timed(
        lambda: fixture.service.plan(
            baseline_run, baseline_claim["task_id"], baseline_claim["lease_generation"]
        ),
        1,
    )[0]
    fixture.cancel(baseline_run)

    contender = fixture.scratch_run()
    claim = fixture.service.claim(contender)
    assert claim is not None
    holder = connect_engine()
    try:
        with holder.connect() as holding:
            holding.execute(
                text("SELECT pg_advisory_xact_lock(:class, :key)"),
                {"class": EXECUTION_QUOTA_LOCK, "key": GLOBAL_SLOT_LOCK},
            )
            outcome: dict[str, float] = {}

            def reserve() -> None:
                start = time.perf_counter()
                fixture.service.plan(contender, claim["task_id"], claim["lease_generation"])
                outcome["elapsed"] = time.perf_counter() - start

            worker = threading.Thread(target=reserve)
            worker.start()
            time.sleep(LOCK_HOLD_SECONDS)
            holding.rollback()
            worker.join(timeout=60)
            assert not worker.is_alive(), "the reservation never came back from the lock"
            measured = outcome["elapsed"] - baseline
    finally:
        holder.dispose()
    fixture.cancel(contender)
    return [max(0.0, measured)]


def growth_ms_per_1000_calls(rows: list[dict[str, object]], operation: str) -> float | None:
    """How much slower a read gets per 1000 calls already in the ledger.

    A growth figure rather than a ceiling: an absolute budget would be met by a fast machine with a
    bad shape, and the question the threshold asks is whether the cost is flat in the ledger size.
    """
    counts = sorted({int(row["ledger_calls"]) for row in rows if row["operation"] == operation})
    if len(counts) < 2 or counts[0] != 0:
        return None
    first, last = counts[0], counts[-1]

    def median(ledger: int) -> float:
        return statistics.median(
            [
                float(row["median_ms"])
                for row in rows
                if row["operation"] == operation and int(row["ledger_calls"]) == ledger
            ]
        )

    return (median(last) - median(first)) / ((last - first) / 1000)
