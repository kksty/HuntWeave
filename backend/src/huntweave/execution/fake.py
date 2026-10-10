"""Durable fixed-fixture executor. Never interprets commands or connects to targets.

It runs on the shared call ledger, so the demonstration side is bound by exactly the same
acceptance, fencing, lease, cancellation, stop-confirmation and evidence rules as the real one —
only what "running a call" means differs, and here it means writing labelled fixed output.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from huntweave.contracts.execution import ExecutionRecord, ExecutionRequest, FakeParameters
from huntweave.execution.ledger import CallLedger, RunnerRejected

__all__ = ["FakeRunner", "RunnerRejected"]


def _fake_parameters(request: ExecutionRequest) -> FakeParameters:
    """This executor only serves the demonstration actions, and their schema is theirs."""
    parameters = request.typed_parameters
    assert isinstance(parameters, FakeParameters)
    return parameters


class FakeRunner(CallLedger):
    """Writes fixed fixture output for the demonstration actions, on the shared ledger."""

    def _stop_fact(self, record: ExecutionRecord) -> bool:
        """The fixtures this executor runs live in its own process, so ending a call ends them.

        Nothing outside this process can be left behind, which is why this side — and only this
        side — can answer "the stop is confirmed" without asking a container runtime.
        """
        return True

    def _emit(
        self, record: ExecutionRecord, event_type: str, payload: dict[str, Any]
    ) -> ExecutionRecord:
        # Every event this executor writes says what produced it, so a timeline read later can
        # tell a fixed fixture result from a real observation.
        return super()._emit(record, event_type, {"fake": True, **payload})

    def _run_call(self, call_id: UUID) -> None:
        with self.lock:
            record = self._record(call_id)
            if record is None or record.status != "running":
                return
            parameters = _fake_parameters(record.request)
            try:
                record = self._output(record, f"FAKE {record.request.action_id}: started\n")
            except OSError:
                self._failure(record, "evidence_storage_failed")
                return
        started = datetime.now(UTC).timestamp()
        finish = started + parameters.duration_ms / 1000
        progress_sent = False
        while not self.closed.wait(0.01):
            current = self._checkpoint(call_id, heartbeat_after=5)
            if current is None:
                return
            now = datetime.now(UTC).timestamp()
            if now < finish:
                if not progress_sent and now >= (started + finish) / 2:
                    try:
                        current = self._output(current, "FAKE fixed fixture progress\n")
                    except OSError:
                        self._failure(current, "evidence_storage_failed")
                        return
                    progress_sent = True
                continue
            scenario = parameters.scenario
            output = f"FAKE {current.request.action_id}: {scenario}\n"
            try:
                current = self._output(current, output)
                result = self._result(current, 1 if scenario == "failure" else 0)
                self._change(
                    current,
                    "failed" if scenario == "failure" else "completed",
                    "dependency_failed" if scenario == "failure" else None,
                    result,
                )
            except OSError:
                self._failure(current, "evidence_storage_failed")
            return
