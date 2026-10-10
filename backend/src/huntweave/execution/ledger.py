"""The durable call ledger both executors share, and the one place a call's state is decided.

Everything that must not differ between the demonstration side and the real one lives here: one
record per `call_id`, the fencing on scope, policy and lease generation, the control lease and its
expiry, cancellation, the separate stop confirmation, and the evidence read that re-checks a file's
hash instead of trusting the record. A subclass only decides what *running* one call means — and
both of them decide it through the same hooks, which is what makes replacing the execution side a
checkable fact rather than a hope (spec 0002 section 3.2, criterion 4 of issue #17).
"""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from huntweave.contracts.execution import (
    ExecutionEvent,
    ExecutionEvidence,
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
    parameters_hash,
)
from huntweave.execution.durable import atomic_write

CallStatus = Literal["accepted", "running", "completed", "failed", "cancelled", "unknown"]


class RunnerRejected(Exception):
    """The execution side refused the intent outright. Nothing was accepted."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class RunnerUnavailable(Exception):
    """The execution side cannot take work *right now*.

    Different from a refusal on purpose: a refusal settles the call, a temporary inability must
    leave the intent where it is so the control plane can offer it again (spec 0002 section 3.4).
    """

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class CallLedger:
    # Each executor keeps its own durable file and its own ownership lock, so the demonstration
    # side and the real side can run in one process without fighting over either.
    ledger_name = "ledger"

    def __init__(self, state_dir: Path, evidence_dir: Path):
        self.state_dir, self.evidence_dir = state_dir, evidence_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.threads: list[threading.Thread] = []
        self.owner = (state_dir / f"{self.ledger_name}.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.owner.seek(0)
                self.owner.write(b"0")
                self.owner.flush()
                self.owner.seek(0)
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RunnerRejected("runner_state_already_owned") from None
        self.path = state_dir / f"{self.ledger_name}.json"
        self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}
        for item in self.data.values():
            record = ExecutionRecord.model_validate(item["record"])
            if record.status in {"accepted", "running"}:
                # A ledger that restarted cannot say what the call it was running did; that is
                # exactly the state reconciliation exists for.
                self._change(record, "unknown", "execution_unknown")

    # -- durability ---------------------------------------------------------------------------

    def _persist(self) -> None:
        atomic_write(self.path, json.dumps(self.data, sort_keys=True).encode())

    def _store(self, record: ExecutionRecord) -> ExecutionRecord:
        self.data[str(record.request.call_id)]["record"] = record.model_dump(mode="json")
        self._persist()
        return record

    @staticmethod
    def _observed(
        started: bool,
        process_active: bool | None,
        connection_open: bool | None,
        lease_active: bool | None,
    ) -> ExecutionObservation:
        return ExecutionObservation(
            started=started,
            process_active=process_active,
            connection_open=connection_open,
            lease_active=lease_active,
            observed_at=datetime.now(UTC),
        )

    def _has_started(self, record: ExecutionRecord) -> bool:
        return any(event.type == "execution_started" for event in record.events)

    def _observation_after(
        self, record: ExecutionRecord, status: CallStatus
    ) -> ExecutionObservation:
        """Report what this ledger can prove about the call's process, connection and lease.

        A ledger that stopped observing an execution cannot prove the process it started is
        gone: a shell may have left descendants behind. Only a record that never started, or
        one whose executor finished or was stopped, supports claiming a stop. A lease is live
        only while the ledger would still renew it.
        """
        if status == "accepted":
            return self._observed(False, False, False, True)
        if status == "running":
            return self._observed(True, True, True, True)
        if status == "unknown":
            started = self._has_started(record)
            unaccounted = None if started else False
            return self._observed(started, unaccounted, unaccounted, False)
        return self._observed(self._has_started(record), False, False, False)

    def _change(
        self,
        record: ExecutionRecord,
        status: CallStatus,
        reason: str | None = None,
        result: ExecutionResult | None = None,
    ) -> ExecutionRecord:
        now = datetime.now(UTC)
        started = next(
            (event.created_at for event in record.events if event.type == "execution_started"), now
        )
        event = ExecutionEvent(
            source_event_id=f"{record.request.call_id}:{len(record.events)}",
            type="execution_started" if status == "running" else f"execution_{status}",
            payload={
                "reason_code": reason,
                "elapsed_ms": int((now - started).total_seconds() * 1000),
            },
            created_at=now,
        )
        updated = ExecutionRecord(
            request=record.request,
            status=status,
            reason_code=reason,
            result=result,
            events=[*record.events, event],
            observation=self._observation_after(record, status),
        )
        return self._store(updated)

    def _emit(
        self, record: ExecutionRecord, event_type: str, payload: dict[str, Any]
    ) -> ExecutionRecord:
        """Append one event to the call's own timeline, in commit order."""
        event = ExecutionEvent(
            source_event_id=f"{record.request.call_id}:{len(record.events)}",
            type=event_type,
            payload=payload,
            created_at=datetime.now(UTC),
        )
        return self._store(record.model_copy(update={"events": [*record.events, event]}))

    # -- submission and control ---------------------------------------------------------------

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.lock:
            previous = self.data.get(str(request.call_id))
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise RunnerRejected("call_id_conflict")
                return ExecutionRecord.model_validate(previous["record"])
            now = datetime.now(UTC)
            if request.parameters_hash != parameters_hash(request.parameters):
                raise RunnerRejected("parameters_hash_mismatch")
            if min(request.deadline_at, request.authorized_until, request.lease_expires_at) <= now:
                raise RunnerRejected("execution_ticket_expired")
            if (request.lease_expires_at - now).total_seconds() > 15:
                raise RunnerRejected("control_lease_too_long")
            self.check_scope(request)
            record = ExecutionRecord(
                request=request,
                status="accepted",
                observation=self._observed(False, False, False, True),
            )
            self.data[str(request.call_id)] = {
                "fingerprint": fingerprint,
                "record": record.model_dump(mode="json"),
            }
            self._persist()
            worker = threading.Thread(target=self._execute, args=(request.call_id,), daemon=True)
            self.threads.append(worker)
            worker.start()
            return record

    def check_scope(self, request: ExecutionRequest) -> None:
        """Refuse a ticket that contradicts the fences this ledger already holds.

        Runs for every submission; a subclass may re-check more before it touches a target.
        """
        for item in self.data.values():
            old = ExecutionRecord.model_validate(item["record"]).request
            if old.run_id == request.run_id:
                if (
                    old.session_id == request.session_id
                    and request.lease_generation < old.lease_generation
                ):
                    raise RunnerRejected("stale_lease_generation")
                if (
                    old.scope_id,
                    old.scope_version,
                    old.policy_version,
                    old.authorized_until,
                ) != (
                    request.scope_id,
                    request.scope_version,
                    request.policy_version,
                    request.authorized_until,
                ):
                    raise RunnerRejected("scope_policy_mismatch")

    def query(self, call_id: UUID) -> ExecutionRecord | None:
        with self.lock:
            item = self.data.get(str(call_id))
            if not item:
                return None
            record = ExecutionRecord.model_validate(item["record"])
            if record.result:
                evidence = []
                for entry in record.result.evidence:
                    reason = entry.missing_reason
                    try:
                        data = (self.evidence_dir / entry.relative_path).read_bytes()
                        if hashlib.sha256(data).hexdigest() != entry.sha256:
                            reason = "evidence_hash_mismatch"
                    except OSError:
                        reason = "evidence_missing"
                    evidence.append(
                        entry.model_copy(
                            update={"available": reason is None, "missing_reason": reason}
                        )
                    )
                if evidence != record.result.evidence:
                    result = record.result.model_copy(update={"evidence": evidence})
                    record = record.model_copy(
                        update={"result": result, "reason_code": "evidence_incomplete"}
                    )
                    item["record"] = record.model_dump(mode="json")
                    self._persist()
            return record

    def renew(self, call_id: UUID, generation: int, expires_at: datetime) -> ExecutionRecord:
        with self.lock:
            record = self.query(call_id)
            if record is None:
                raise RunnerRejected("call_not_found")
            if generation != record.request.lease_generation:
                raise RunnerRejected("stale_lease_generation")
            now = datetime.now(UTC)
            if (
                record.status not in {"accepted", "running"}
                or record.request.lease_expires_at <= now
            ):
                raise RunnerRejected("control_lease_expired")
            if (
                not now
                < expires_at
                <= min(record.request.deadline_at, record.request.authorized_until)
                or (expires_at - now).total_seconds() > 15
            ):
                raise RunnerRejected("invalid_control_lease")
            request = record.request.model_copy(update={"lease_expires_at": expires_at})
            return self._store(record.model_copy(update={"request": request}))

    def cancel(self, call_id: UUID, generation: int) -> ExecutionRecord:
        """End a call, asking the executor to stop it *outside* the ledger lock.

        The lock discipline matters: the ledger lock guards the ledger and the executor's own
        lock guards its resources, and a cancel takes them in that order while a running call
        takes them the other way round. Holding one while asking for the other is a deadlock, so
        the stop happens between two short visits to the ledger.
        """
        with self.lock:
            record = self.query(call_id)
            if record is None:
                raise RunnerRejected("call_not_found")
            if generation != record.request.lease_generation:
                raise RunnerRejected("stale_lease_generation")
            if record.status not in {"accepted", "running"}:
                if record.status == "unknown":
                    return self._stop(record)
                return record
        stopped = self._stop_call(record)
        with self.lock:
            current = self.query(call_id)
            if current is None:
                raise RunnerRejected("call_not_found")
            if current.status not in {"accepted", "running"}:
                return current
            return self._change(
                current,
                "cancelled",
                "operator_cancelled",
                stopped if stopped is not None else self._result(current, None),
            )

    def _note(self, call_id: UUID, key: str, value: str) -> None:
        """Keep one executor-private fact about a call in the same durable record.

        The control plane never sees these; they exist so a cancel that arrives in another
        request can find what the call is using — the instance it must stop, for instance.
        """
        with self.lock:
            item = self.data.get(str(call_id))
            if item is None:
                return
            item.setdefault("notes", {})[key] = value
            self._persist()

    def _noted(self, call_id: UUID, key: str) -> str | None:
        with self.lock:
            item = self.data.get(str(call_id))
            if item is None:
                return None
            notes = item.get("notes") or {}
            value = notes.get(key)
            return value if isinstance(value, str) else None

    def _stop(self, record: ExecutionRecord) -> ExecutionRecord:
        """Record a performed stop without inventing an outcome for the call.

        The ledger cannot say whether the action happened; it can say that whatever the call
        left running is stopped. That is a separate fact the control plane needs, and it is
        never derived from an operator's reconciliation verdict.
        """
        observation = record.observation
        if observation is not None and observation.process_active is False:
            return record
        stopped = self._emit(
            record,
            "execution_stopped",
            {"reason_code": "operator_stopped", "outcome": record.status},
        )
        return self._store(
            stopped.model_copy(
                update={
                    "observation": self._observed(self._has_started(record), False, False, False)
                }
            )
        )

    def close(self) -> None:
        self.closed.set()
        for worker in self.threads:
            worker.join(timeout=1)
        self.owner.close()

    # -- what a subclass implements -----------------------------------------------------------

    def _run_call(self, call_id: UUID) -> None:
        """Run one accepted call. Subclasses decide what that means and when it is over."""
        raise NotImplementedError

    def _stop_call(self, record: ExecutionRecord) -> ExecutionResult | None:
        """Stop whatever this call is doing, before its record is marked cancelled.

        The answer is the result (with whatever evidence exists) or ``None`` when nothing was
        collected, and it is this hook that makes "cancelled" mean something on the real side
        instead of only on the demonstration one.
        """
        return None

    # -- the hooks a subclass runs its call through -------------------------------------------

    def _execute(self, call_id: UUID) -> None:
        with self.lock:
            record = self.query(call_id)
            if record is None or record.status != "accepted":
                return
            if self.closed.is_set():
                # A runner that is stopping does not start accepted work: the durable
                # acceptance stays, so the next reading proves the action never ran.
                return
            reason = self._expired_reason(record.request)
            if reason is not None:
                self._change(record, "cancelled", reason)
                return
            record = self._change(record, "running")
        self._run_call(call_id)

    @staticmethod
    def _expired_reason(request: ExecutionRequest) -> str | None:
        now = datetime.now(UTC)
        if min(request.lease_expires_at, request.deadline_at, request.authorized_until) <= now:
            return "control_lease_expired"
        return None

    def _record(self, call_id: UUID) -> ExecutionRecord | None:
        return self.query(call_id)

    def _checkpoint(
        self, call_id: UUID, *, heartbeat_after: float | None = None
    ) -> ExecutionRecord | None:
        """Read the call back mid-flight: ``None`` means it is no longer running.

        A call that was cancelled or whose lease lapsed while the action ran ends here, with the
        reason the control plane will see. Heartbeats are emitted by the same read, so a long
        action shows progress without inventing a percentage.
        """
        with self.lock:
            record = self.query(call_id)
            if record is None or record.status != "running":
                return None
            reason = self._expired_reason(record.request)
            if reason is not None:
                self._change(record, "cancelled", reason, self._result(record, None))
                return None
            if heartbeat_after is not None:
                started = next(
                    (
                        event.created_at
                        for event in record.events
                        if event.type == "execution_started"
                    ),
                    datetime.now(UTC),
                )
                elapsed = (datetime.now(UTC) - started).total_seconds()
                last = max(
                    (
                        event.created_at
                        for event in record.events
                        if event.type == "execution_heartbeat"
                    ),
                    default=started,
                )
                if (datetime.now(UTC) - last).total_seconds() >= heartbeat_after:
                    record = self._emit(
                        record,
                        "execution_heartbeat",
                        {"status": "running", "elapsed_ms": int(elapsed * 1000)},
                    )
            return record

    # -- evidence -----------------------------------------------------------------------------

    def _archive(self, record: ExecutionRecord, name: str, payload: bytes) -> ExecutionEvidence:
        """Write one evidence file, then hash the file that was written.

        The order matters: the hash describes the bytes on disk, not the bytes the caller meant
        to write, and evidence that cannot be written is reported as missing rather than
        assembled from memory.
        """
        relative = f"{record.request.run_id}/{record.request.call_id}/{name}"
        destination = self.evidence_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, payload)
        return ExecutionEvidence(
            id=_evidence_id(record.request.call_id, name),
            relative_path=relative,
            sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
            size_bytes=destination.stat().st_size,
            available=True,
        )

    def _output_path(self, record: ExecutionRecord) -> str:
        return f"{record.request.run_id}/{record.request.call_id}.txt"

    def _output(self, record: ExecutionRecord, text: str) -> ExecutionRecord:
        """Append raw output to this call's transcript and to the archived stream."""
        relative = self._output_path(record)
        destination = self.evidence_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = destination.read_bytes() if destination.exists() else b""
        atomic_write(destination, previous + text.encode())
        return self._emit(
            record,
            "execution_output",
            {
                "text": text,
                "offset": len(previous),
                "next_offset": len(previous) + len(text.encode()),
            },
        )

    def _result(self, record: ExecutionRecord, exit_code: int | None) -> ExecutionResult | None:
        """The transcript as the call's output, when there is one."""
        destination = self.evidence_dir / self._output_path(record)
        if not destination.exists():
            return None
        payload = destination.read_bytes()
        evidence = ExecutionEvidence(
            id=_evidence_id(record.request.call_id, "output"),
            relative_path=self._output_path(record),
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            available=True,
        )
        return ExecutionResult(output=payload.decode(), exit_code=exit_code, evidence=[evidence])

    def _failure(self, record: ExecutionRecord, reason: str) -> ExecutionRecord:
        return self._change(record, "failed", reason, self._result(record, None))


def _evidence_id(call_id: UUID, name: str) -> UUID:
    from uuid import uuid5

    return uuid5(call_id, name)
