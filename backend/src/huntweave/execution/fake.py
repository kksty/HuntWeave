"""Durable fixed-fixture executor. Never interprets commands or connects to targets.

It runs on the shared call ledger, so the demonstration side is bound by exactly the same
acceptance, fencing, lease, cancellation, stop-confirmation and evidence rules as the real one —
only what "running a call" means differs, and here it means writing labelled fixed output.

It also reports the same shape of runtime and progress facts the real side does. That is not
decoration: the console has one view model, and a demonstration call that omitted them would make
the console's own rendering depend on which side produced the call — exactly the confusion the
profile separation exists to prevent.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from huntweave.contracts.execution import (
    CallRuntime,
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
)
from huntweave.execution.ledger import ArchiveRejected, CallLedger, RunnerRejected

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
            record = self._fixture_runtime(record, parameters)
            try:
                record = self._output(record, f"FAKE {record.request.action_id}: started\n")
            except ArchiveRejected as refusal:
                raise refusal
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
                    except ArchiveRejected as refusal:
                        raise refusal
                    except OSError:
                        self._failure(current, "evidence_storage_failed")
                        return
                    progress_sent = True
                continue
            scenario = parameters.scenario
            output = f"FAKE {current.request.action_id}: {scenario}\n"
            result = None
            try:
                current = self._output(current, output)
                result = self._result(current, 1 if scenario == "failure" else 0)
                assert result is not None
                result = result.model_copy(
                    update={
                        "duration_ms": int((datetime.now(UTC).timestamp() - started) * 1000),
                        "truncated": any(
                            event.type == "execution_output_truncated" for event in current.events
                        ),
                        "redacted": any(
                            event.type == "execution_output_redacted" for event in current.events
                        ),
                    }
                )
            except ArchiveRejected as refusal:
                raise refusal
            except OSError:
                self._failure(current, "evidence_storage_failed")
                return
            # The end of the command and the end of the call are written as one step: a reader that
            # sees the terminal status must see the whole call, or "the record I just read" and
            # "the record I read a moment later" would disagree for reasons nobody could act on.
            finished = self._change(
                current,
                "failed" if scenario == "failure" else "completed",
                "dependency_failed" if scenario == "failure" else None,
                result,
            )
            self._emit(
                finished,
                "execution_command_completed",
                {
                    "exit_code": result.exit_code,
                    "timed_out": False,
                    "duration_ms": result.duration_ms,
                    "ended_at": datetime.now(UTC).isoformat(),
                },
            )
            return

    def _fixture_runtime(
        self, record: ExecutionRecord, parameters: FakeParameters
    ) -> ExecutionRecord:
        """Describe the fixed fixture the way the real side describes an instance.

        The point is not that a fixture has a container — it does not — but that the console reads
        one shape of runtime facts and is told, in the same fields, that this call ran inside the
        Runner's own process as a fixed scenario (spec 0002 section 2 clause 2).
        """
        return self._runtime(
            record,
            CallRuntime(
                action_id=record.request.action_id,
                execution_profile=record.request.execution_profile,
                target_ip=str(record.request.target_ip),
                target_port=record.request.target_port,
                parameters_hash=record.request.parameters_hash,
                # The demonstration side composes no argv at all: saying so is more honest than
                # showing a command that was never run.
                argv=[],
                cwd=None,
                user=None,
                network_mode="none",
                authorized=[],
                observed_at=datetime.now(UTC),
            ),
        )
