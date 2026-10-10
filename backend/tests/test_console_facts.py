"""The console's own facts: the archive's refusals, and the runtime statement a call carries.

Issue #19 asks for three branches to be *really reachable* rather than documented: output that was
truncated, output that was redacted, and an archive that could not be written at all. The first two
are labeled on the record; the third blocks the affected execution instead of letting the platform
carry on with a gap nobody can see. This module also pins the execution side's own account of where
a call acted — what makes "what really ran" checkable — and the timing the console shows when an
action is silent.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest

from huntweave.contracts.execution import (
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
    ShellExecParameters,
    parameters_hash,
)
from huntweave.execution.archive import (
    ARTIFACT_QUOTA_BYTES,
    ArchiveRejected,
    EvidenceArchive,
    prepared,
)
from huntweave.execution.fake import FakeRunner
from huntweave.execution.real import action_command


def ticket(**changes: object) -> ExecutionRequest:
    """A demonstration ticket with the fields the console reads, overridable per test."""
    now = datetime.now(UTC)
    parameters = FakeParameters(duration_ms=20)
    data: dict[str, object] = dict(
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


def shell_ticket(**changes: object) -> ExecutionRequest:
    """A real-action ticket, built the way the ledger and the console would see one."""
    parameters = ShellExecParameters(command="id", timeout_seconds=5)
    return ticket(
        action_id="shell.exec",
        parameters=parameters.model_dump(),
        parameters_hash=parameters_hash(parameters),
        execution_profile="real-lab-v1",
        **changes,
    )


def settled(
    runner: FakeRunner, call_id: UUID, *statuses: str, timeout: float = 15
) -> ExecutionRecord:
    """Poll the durable ledger instead of racing a fixed sleep on a loaded host."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        record = runner.query(call_id)
        if record is not None and record.status in statuses:
            return record
        sleep(0.01)
    raise AssertionError(f"call never reached {statuses}")


def workspace(tmp_path: Path, name: str) -> tuple[Path, Path]:
    return tmp_path / f"ledger-{name}", tmp_path / f"evidence-{name}"


# -- what really ran ----------------------------------------------------------------------------


def test_a_scripted_action_states_its_interpreter_and_its_command_separately() -> None:
    """A reader must be able to tell the fixed program from the ticket's own arguments."""
    assert action_command(shell_ticket(), 5) == ["sh", "-c", "id"]


def test_the_demonstration_side_describes_itself_without_inventing_a_container(
    tmp_path: Path,
) -> None:
    """A fixed fixture has no container; it says so instead of borrowing a real instance's shape."""
    state, evidence = workspace(tmp_path, "fake-runtime")
    runner = FakeRunner(state, evidence)
    try:
        request = ticket()
        runner.submit(request)
        record = settled(runner, request.call_id, "completed")
        assert record.runtime is not None
        runtime = record.runtime
        # The ticket's binding is restated, so the console never has to trust the plan for it.
        assert runtime.target_ip == "192.0.2.1" and runtime.target_port == 80
        assert runtime.parameters_hash == request.parameters_hash
        assert runtime.execution_profile == "fake-p0-v1" and runtime.action_id == "fake.collect"
        # No container was created, so no argv, user, instance or gateway is claimed.
        assert runtime.argv == [] and runtime.cwd is None and runtime.instance_id is None
        assert runtime.network_mode == "none" and runtime.authorized == []
    finally:
        runner.close()


def test_progress_is_read_from_the_call_s_own_events(tmp_path: Path) -> None:
    """The console's "running for Ns / last output ..." comes from the record, not a stored copy."""
    state, evidence = workspace(tmp_path, "progress")
    runner = FakeRunner(state, evidence)
    try:
        request = ticket()
        runner.submit(request)
        record = settled(runner, request.call_id, "completed")
        assert record.progress is not None
        assert record.progress.status == "ended"
        assert record.progress.started_at is not None
        assert record.progress.last_output_at is not None
        assert record.progress.output_bytes > 0
        assert record.progress.timeout_seconds == 30
        assert record.progress.elapsed_ms is not None and record.progress.elapsed_ms >= 0
        view = record.progress_view
        assert view is not None and view.status == "ended"
    finally:
        runner.close()


# -- the archive's three branches ---------------------------------------------------------------


def test_redaction_replaces_a_credential_shape_and_keeps_what_named_it() -> None:
    body, truncated, redactions = prepared(
        "GET /\nAuthorization: Bearer abc123def456\npassword=hunter2\nok\n",
        max_bytes=ARTIFACT_QUOTA_BYTES,
    )
    assert not truncated
    assert redactions == {"authorization_header": 1, "password_assignment": 1}
    # The value is gone; the line still says what the tool was doing.
    assert "abc123def456" not in body and "hunter2" not in body
    assert "Authorization: [redacted:authorization_header]" in body
    assert "password= [redacted:password_assignment]" in body
    assert "ok" in body


def test_truncation_cuts_the_tail_and_says_so_rather_than_keeping_a_partial_secret() -> None:
    body, truncated, _ = prepared("x" * 100 + "password=secret", max_bytes=50)
    assert truncated
    assert len(body.encode()) <= 50
    assert "secret" not in body


def test_an_artifact_over_the_quota_is_refused_rather_than_silently_cut(tmp_path: Path) -> None:
    archive = EvidenceArchive(tmp_path, artifact_quota_bytes=16)
    with pytest.raises(ArchiveRejected) as refusal:
        archive.archive("run/call/stdout.txt", "y" * 64)
    assert refusal.value.reason_code == "evidence_artifact_too_large"
    assert not (tmp_path / "run" / "call" / "stdout.txt").exists()


def test_the_archive_refuses_before_writing_when_the_volume_cannot_hold_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage-full is a decision this side made, not an OSError whose meaning depends on timing."""
    import huntweave.execution.archive as module

    class Full:
        free = 0
        used = 0
        total = 0

    monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: Full())
    archive = EvidenceArchive(tmp_path, artifact_quota_bytes=1024)
    with pytest.raises(ArchiveRejected) as refusal:
        archive.archive("run/call/stdout.txt", "anything")
    assert refusal.value.reason_code == "evidence_storage_full"
    assert not list(tmp_path.rglob("*.txt"))


def test_a_full_transcript_stops_growing_and_records_the_cut(tmp_path: Path) -> None:
    archive = EvidenceArchive(tmp_path, artifact_quota_bytes=1024)
    written = archive.append("run/call.txt", "abcdefgh", quota_bytes=8)
    assert written is not None and written.text == "abcdefgh"
    # Nothing more is written once the quota is spent: the caller is told, and records it.
    assert archive.append("run/call.txt", "more", quota_bytes=8) is None
    assert archive.stored("run/call.txt") == b"abcdefgh"


def test_the_resolved_transcript_is_labelled_truncated_not_presented_as_whole(
    tmp_path: Path,
) -> None:
    """A cut transcript must be readable *and* marked: a reader cannot tell by size alone."""
    state, evidence = workspace(tmp_path, "truncating")
    runner = FakeRunner(state, evidence, output_quota_bytes=16)
    try:
        request = ticket()
        runner.submit(request)
        record = settled(runner, request.call_id, "completed")
        assert any(event.type == "execution_output_truncated" for event in record.events)
        assert record.result is not None and record.result.truncated
        assert record.result.evidence[0].truncated
    finally:
        runner.close()


def test_an_archive_that_cannot_be_written_blocks_the_affected_execution(tmp_path: Path) -> None:
    """The call ends *blocked* with the archive's reason, and no result is invented for it."""
    state, evidence = workspace(tmp_path, "full")
    runner = _FullVolumeRunner(state, evidence)
    try:
        request = ticket()
        runner.submit(request)
        record = settled(runner, request.call_id, "failed")
        assert record.reason_code == "evidence_storage_full"
        # No result: an execution whose archive is missing is not a confirmed action.
        assert record.result is None
        refused = [event for event in record.events if event.type == "execution_evidence_refused"]
        assert refused and refused[0].payload["reason_code"] == "evidence_storage_full"
    finally:
        runner.close()


class _FullVolumeRunner(FakeRunner):
    """A demonstration executor whose archive refuses everything, to reach the storage-full path.

    The refusal comes from the same archive writer the real side uses; only the free-space check is
    replaced, which is exactly the state (a full volume) the criterion is about.
    """

    def __init__(self, state_dir: Path, evidence_dir: Path):
        super().__init__(state_dir, evidence_dir)

        class Full(EvidenceArchive):
            def _require_space(self, incoming: int) -> None:
                raise ArchiveRejected("evidence_storage_full")

        self.archive_writer = Full(evidence_dir, artifact_quota_bytes=1024)


def test_the_ledger_keeps_a_call_s_runtime_statement_across_restart(tmp_path: Path) -> None:
    """What an old call ran must not change when the deployment's profile moves on."""
    state, evidence = workspace(tmp_path, "restart")
    runner = FakeRunner(state, evidence)
    request = ticket()
    runner.submit(request)
    record = settled(runner, request.call_id, "completed")
    runner.close()
    reopened = FakeRunner(state, evidence)
    try:
        again = reopened.query(request.call_id)
        assert again is not None and again.runtime == record.runtime
    finally:
        reopened.close()
