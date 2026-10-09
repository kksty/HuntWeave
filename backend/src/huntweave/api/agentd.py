import json
import signal
import threading
from types import FrameType
from uuid import UUID, uuid4

from sqlalchemy import text

from huntweave.contracts.errors import ServiceError
from huntweave.execution.client import RunnerClient
from huntweave.harness.checkpoints import verify_checkpoint_schema
from huntweave.harness.graph import ResearchHarness
from huntweave.runs.dispatch import ExecutionDispatcher
from huntweave.runs.orchestration import OrchestrationService
from huntweave.storage.database import connect_engine, verify_business_schema

# Runs swept per pass; the window rotates so no stuck Run hides another from reconciliation.
SWEEP_LIMIT = 20


def main() -> int:
    stopping = threading.Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine = connect_engine()
    instance_id = uuid4()
    business = OrchestrationService(lambda: engine)
    harness = ResearchHarness(business)
    dispatcher = ExecutionDispatcher(business, RunnerClient())
    sweep_offset = 0
    try:
        verify_business_schema(engine)
        verify_checkpoint_schema()
        # One active daemon. This session-scoped advisory lock does not hold a
        # transaction across graph/Runner calls; task leases still protect restarts.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as leader:
            if leader.scalar(text("SELECT pg_try_advisory_lock(486735944)")) is not True:
                raise RuntimeError("Another agentd holds the scheduler lease")
            while not stopping.is_set():
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO huntweave.runtime_processes "
                            "(name, instance_id, heartbeat_at) "
                            "VALUES ('agentd', :instance_id, clock_timestamp()) "
                            "ON CONFLICT (name) DO UPDATE SET instance_id=EXCLUDED.instance_id, "
                            "heartbeat_at=EXCLUDED.heartbeat_at"
                        ),
                        {"instance_id": instance_id},
                    )
                # Verify leadership before claiming; a lost DB connection stops all dispatch.
                leader.execute(text("SELECT 1"))
                claim = business.claim()
                if claim:
                    run_id = UUID(claim["run_id"])
                    try:
                        dispatcher.reconcile(run_id)
                        harness.advance(claim)
                        # Submit what this pass just planned without waiting for the sweep.
                        dispatcher.reconcile(run_id)
                    except ServiceError as error:
                        # A Run can lose its lease or be reconciled away between the claim
                        # and this pass. Record the contract reason and keep scheduling;
                        # storage failures still stop the daemon so the supervisor reacts.
                        print(
                            json.dumps(
                                {
                                    "reason_code": error.reason_code,
                                    "run_id": str(run_id),
                                    "stage": "claim_pass",
                                }
                            ),
                            flush=True,
                        )
                # The sweep re-reads every outstanding call, including Runs the scheduler
                # cannot claim: an unconfirmed outcome is reconciled, never retried. A Run
                # that is reconciled away or cleared between the window query and this pass
                # is a contract outcome for that Run, not a reason to stop scheduling.
                try:
                    swept = dispatcher.sweep(SWEEP_LIMIT, sweep_offset)
                except ServiceError as error:
                    print(
                        json.dumps(
                            {
                                "reason_code": error.reason_code,
                                "stage": "sweep_pass",
                            }
                        ),
                        flush=True,
                    )
                    swept = 0
                sweep_offset = sweep_offset + SWEEP_LIMIT if swept == SWEEP_LIMIT else 0
                stopping.wait(0.5)
    except Exception as error:
        print(f'{{"reason_code":"scheduler_unavailable","error_type":"{type(error).__name__}"}}')
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
