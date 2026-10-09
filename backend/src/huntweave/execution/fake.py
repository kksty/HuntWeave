"""Durable fixed-fixture executor. Never interprets commands or connects to targets."""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from huntweave.contracts.execution import (
    ExecutionEvent,
    ExecutionEvidence,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
    parameters_hash,
)


class RunnerRejected(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class FakeRunner:
    def __init__(self, state_dir: Path, evidence_dir: Path):
        self.state_dir, self.evidence_dir = state_dir, evidence_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.threads: list[threading.Thread] = []
        self.owner = (state_dir / "owner.lock").open("a+b")
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
        self.path = state_dir / "ledger.json"
        self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}
        for item in self.data.values():
            record = ExecutionRecord.model_validate(item["record"])
            if record.status in {"accepted", "running"}:
                self._change(record, "unknown", "execution_unknown")

    def _persist(self) -> None:
        atomic_write(self.path, json.dumps(self.data, sort_keys=True).encode())

    def _change(
        self,
        record: ExecutionRecord,
        status: Literal["accepted", "running", "completed", "failed", "cancelled", "unknown"],
        reason: str | None = None,
        result: ExecutionResult | None = None,
    ) -> ExecutionRecord:
        event = ExecutionEvent(
            source_event_id=f"{record.request.call_id}:{len(record.events)}",
            type="execution_started" if status == "running" else f"execution_{status}",
            payload={"reason_code": reason},
            created_at=datetime.now(UTC),
        )
        updated = ExecutionRecord(
            request=record.request,
            status=status,
            reason_code=reason,
            result=result,
            events=[*record.events, event],
        )
        self.data[str(record.request.call_id)]["record"] = updated.model_dump(mode="json")
        self._persist()
        return updated

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
            for item in self.data.values():
                old = ExecutionRecord.model_validate(item["record"]).request
                if old.run_id == request.run_id:
                    if request.lease_generation < old.lease_generation:
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
            record = ExecutionRecord(request=request, status="accepted")
            self.data[str(request.call_id)] = {
                "fingerprint": fingerprint,
                "record": record.model_dump(mode="json"),
            }
            self._persist()
            worker = threading.Thread(target=self._execute, args=(request.call_id,), daemon=True)
            self.threads.append(worker)
            worker.start()
            return record

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

    def _execute(self, call_id: UUID) -> None:
        with self.lock:
            record = self.query(call_id)
            assert record is not None
            if record.status != "accepted":
                return
            now = datetime.now(UTC)
            if (
                min(
                    record.request.lease_expires_at,
                    record.request.deadline_at,
                    record.request.authorized_until,
                )
                <= now
            ):
                self._change(record, "cancelled", "execution_ticket_expired")
                return
            record = self._change(record, "running")
        finish = datetime.now(UTC).timestamp() + record.request.parameters.duration_ms / 1000
        while not self.closed.wait(0.01):
            with self.lock:
                record = self.query(call_id)
                assert record is not None
                if record.status != "running":
                    return
                now = datetime.now(UTC)
                if (
                    min(
                        record.request.lease_expires_at,
                        record.request.deadline_at,
                        record.request.authorized_until,
                    )
                    <= now
                ):
                    self._change(
                        record,
                        "cancelled",
                        "control_lease_expired"
                        if record.request.lease_expires_at <= now
                        else "execution_timeout",
                    )
                    return
                if now.timestamp() < finish:
                    continue
                scenario = record.request.parameters.scenario
                output = f"FAKE {record.request.action_id}: {scenario}\n"
                payload = output.encode()
                relative = f"{record.request.run_id}/{call_id}.txt"
                destination = self.evidence_dir / relative
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(destination, payload)
                    evidence = ExecutionEvidence(
                        id=uuid5(call_id, "output"),
                        relative_path=relative,
                        sha256=hashlib.sha256(payload).hexdigest(),
                        size_bytes=len(payload),
                        available=True,
                    )
                    result = ExecutionResult(
                        output=output,
                        exit_code=1 if scenario == "failure" else 0,
                        evidence=[evidence],
                    )
                    self._change(
                        record,
                        "failed" if scenario == "failure" else "completed",
                        "dependency_failed" if scenario == "failure" else None,
                        result,
                    )
                except OSError:
                    self._change(record, "failed", "evidence_storage_failed")
                return

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
            renewed = record.model_copy(update={"request": request})
            self.data[str(call_id)]["record"] = renewed.model_dump(mode="json")
            self._persist()
            return renewed

    def cancel(self, call_id: UUID, generation: int) -> ExecutionRecord:
        with self.lock:
            record = self.query(call_id)
            if record is None:
                raise RunnerRejected("call_not_found")
            if generation != record.request.lease_generation:
                raise RunnerRejected("stale_lease_generation")
            if record.status in {"accepted", "running"}:
                return self._change(record, "cancelled", "operator_cancelled")
            return record

    def close(self) -> None:
        self.closed.set()
        for worker in self.threads:
            worker.join(timeout=1)
        self.owner.close()
