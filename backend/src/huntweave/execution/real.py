"""Real executor: one call, one sandbox instance, one archived command.

The demonstration side writes fixed output; this side runs the action the ticket names *inside* the
sandbox the trusted manager created, under the permits the gateway holds and as the unprivileged
user the profile names. It shares the call ledger with the demonstration side, so acceptance,
fencing, the control lease, cancellation, the separate stop confirmation and the evidence-integrity
read are the same rules for both — only what running a call means differs.

Everything a call produces is archived before the record claims a result: stdout and stderr as
their own evidence files, the combined transcript the console reads, and the summary fields the
action declared. The instance is revoked, stopped and reclaimed on every path, including
cancellation and failure, because a call that is over must not leave a permit behind.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from huntweave.contracts.execution import (
    REAL_ACTIONS,
    CallRuntime,
    DiscoverTcpParameters,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
    ProbeHttpParameters,
    ShellExecParameters,
)
from huntweave.execution.ledger import (
    ArchiveRejected,
    CallLedger,
    RunnerRejected,
    RunnerUnavailable,
    StopOutcome,
)
from huntweave.execution.retention import RetentionStore
from huntweave.execution.sandbox import (
    AuthorizedEndpoint,
    CommandResult,
    EnvironmentManifest,
    HaltRequest,
    InstanceRecord,
    LeaseRenewal,
    RuntimeUnavailable,
    SandboxInstanceRequest,
    SandboxManager,
    SandboxRejected,
    SandboxSessionRequest,
)
from huntweave.execution.sandboxprofile import SandboxProfile

__all__ = ["RealRunner", "action_command", "RunnerRejected", "RunnerUnavailable"]

# Fixed programs for the scripted actions. A deployment's tool image decides which interpreter
# runs them; nothing here comes from a caller, and the arguments are the ticket's own binding.
DISCOVERY_SCRIPT = """
import json, socket, sys
address, ports = sys.argv[1], [int(value) for value in sys.argv[2].split(",") if value]
opened, closed = [], []
for port in ports:
    with socket.socket() as probe:
        probe.settimeout(2)
        try:
            (opened if probe.connect_ex((address, port)) == 0 else closed).append(port)
        except OSError:
            closed.append(port)
print(json.dumps({"open_ports": opened, "closed_ports": closed}))
"""

PROBE_SCRIPT = """
import json, sys, urllib.error, urllib.request
method, url = sys.argv[1], sys.argv[2]
request = urllib.request.Request(url, method=method)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        summary = {
            "status_code": response.status,
            "server": response.headers.get("Server", ""),
            "content_type": response.headers.get("Content-Type", ""),
        }
except urllib.error.HTTPError as error:
    summary = {
        "status_code": error.code,
        "server": error.headers.get("Server", ""),
        "content_type": error.headers.get("Content-Type", ""),
    }
except Exception as error:
    print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
    raise SystemExit(2)
print(json.dumps(summary))
"""


def action_command(request: ExecutionRequest, timeout_seconds: int) -> list[str]:
    """The argv one action runs, composed only from the ticket's own binding and parameters."""
    parameters = request.typed_parameters
    if request.action_id == "shell.exec":
        assert isinstance(parameters, ShellExecParameters)
        return ["sh", "-c", parameters.command]
    if request.action_id == "discover_tcp_services":
        assert isinstance(parameters, DiscoverTcpParameters)
        ports = ",".join(str(port) for port in parameters.ports)
        return ["python", "-c", DISCOVERY_SCRIPT, str(request.target_ip), ports]
    if request.action_id == "probe_http":
        assert isinstance(parameters, ProbeHttpParameters)
        url = f"{parameters.scheme}://{request.target_ip}:{request.target_port}{parameters.path}"
        return ["python", "-c", PROBE_SCRIPT, parameters.method, url]
    raise RunnerRejected("action_not_real")


class RealRunner(CallLedger):
    """Runs real actions through the trusted manager, on the same ledger as the fake ones."""

    ledger_name = "real"

    def __init__(
        self,
        state_dir: Path,
        evidence_dir: Path,
        *,
        manager: SandboxManager,
        profile: SandboxProfile,
        retention: RetentionStore | None = None,
    ):
        self.manager = manager
        self.profile = profile
        # The retention ledger this executor hands a call's private workspace to (issue #20). One
        # ledger per state directory, opened on first use: a Runner whose state directory cannot be
        # written still reports its readiness instead of failing to start.
        self._retention = retention
        self._retention_dir = state_dir
        super().__init__(
            state_dir,
            evidence_dir,
            # The profile decides how much of one artifact this deployment may keep. A profile is
            # already the fixed, reviewed place these limits live (spec 0002 section 3.6), so the
            # archive reads it from there instead of carrying a second default.
            artifact_quota_bytes=profile.limits.evidence_max_bytes,
            output_quota_bytes=profile.limits.evidence_max_bytes,
        )

    # -- submission ---------------------------------------------------------------------------

    @property
    def retention(self) -> RetentionStore:
        """The retention ledger, opened the first time a workspace is handed to it."""
        if self._retention is None:
            self._retention = RetentionStore(self._retention_dir)
        return self._retention

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        if request.action_id not in REAL_ACTIONS:
            # The demonstration side is not a fallback for a real deployment: a ticket that names
            # a fake action here is a wiring mistake, not something to serve by pretending.
            raise RunnerRejected("action_not_real")
        if not self.manager.observe().available:
            # Transient: the control plane should hold the intent and try later rather than
            # settle the call as denied.
            raise RunnerUnavailable("sandbox_runtime_unreachable")
        return super().submit(request)

    # -- running one call ---------------------------------------------------------------------

    def _run_call(self, call_id: UUID) -> None:
        record = self._record(call_id)
        if record is None or record.status != "running":
            return
        request = record.request
        parameters = request.typed_parameters
        timeout = min(
            int(getattr(parameters, "timeout_seconds", request.spec.default_timeout_seconds)),
            request.spec.hard_timeout_seconds,
        )
        argv = action_command(request, timeout)
        began = datetime.now(UTC)
        instance_id: UUID | None = None
        environment: EnvironmentManifest | None = None
        try:
            session = self.manager.open_session(
                SandboxSessionRequest(
                    run_id=request.run_id,
                    agent_session_id=request.session_id,
                    scope_id=request.scope_id,
                    scope_version=request.scope_version,
                    policy_version=request.policy_version,
                )
            )
            instance = self.manager.launch_instance(
                SandboxInstanceRequest(
                    session_id=session.session_id,
                    authorized=[
                        AuthorizedEndpoint(
                            address=str(request.target_ip), port=request.target_port
                        )
                    ],
                )
            )
            instance_id = instance.instance_id
            environment = instance.environment
            self._note(call_id, "instance_id", str(instance_id))
            # The call's own control lease bounds the instance too: if the control plane stops
            # renewing, the manager's watchdog revokes and stops this execution instead of letting
            # it run on (issue #17, criterion 8).
            self.manager.renew_instance_lease(
                LeaseRenewal(instance_id=instance_id, lease_expires_at=request.lease_expires_at)
            )
            if self._halt_requested(call_id):
                # The operator cancelled while the instance was being prepared. The command never
                # starts, and the instance this call created is released by the same path.
                return
            record = self._announce(record, request, instance, argv)
            result = self.manager.run_command(instance_id, argv, timeout)
        except SandboxRejected as refusal:
            self._settle_failure(call_id, refusal.reason_code)
            return
        except (RuntimeUnavailable, OSError, ValueError):
            self._settle_failure(call_id, "sandbox_instance_creation_failed")
            return
        finally:
            if instance_id is not None:
                self._release(instance_id, environment)

        with self.lock:
            current = self._record(call_id)
            if current is None:
                return
            if current.status != "running":
                # Cancelled or expired while the command ran: that path owns the record now, and
                # it must not be overwritten with an outcome it did not observe. Whatever the
                # command had already produced is still archived as this call's evidence.
                if current.result is None and (result.stdout or result.stderr):
                    try:
                        partial = self._archive_result(current, request, result)
                    except OSError:
                        return
                    self._store(current.model_copy(update={"result": partial}))
                return
            # The lease may have lapsed while the command ran — that is precisely the case the
            # instance watchdog exists for. The call is then cancelled with what it produced, not
            # completed as if the control plane had still been watching.
            expired = self._expired_reason(current.request)
            if expired is not None:
                self._change(
                    current, "cancelled", expired, self._result(current, result.exit_code)
                )
                return
            try:
                settled = self._archive_result(current, request, result)
            except ArchiveRejected as refusal:
                # The archive refused this call's output. No result is claimed, and the call ends
                # blocked with the archive's own reason instead of a fabricated success.
                raise refusal
            except OSError:
                self._failure(current, "evidence_storage_failed")
                return
            reason = None
            if result.timed_out:
                reason = "execution_timeout"
            elif result.exit_code != 0:
                reason = "action_failed"
            settled = settled.model_copy(
                update={"duration_ms": result.duration_ms, "truncated": self._truncated(current)}
            )
            # Every one of these transitions takes its claim about processes from this executor's
            # own stop fact, read after the release above: the ledger asks whether the instance
            # this call used is confirmed stopped, instead of this path asserting that it is.
            finished = self._change(
                current,
                "completed" if result.exit_code == 0 else "failed",
                reason,
                settled,
            )
            # The end of the command is recorded with the end of the call, so a reader that sees
            # the terminal status already sees the whole call rather than racing the last event.
            self._announce_completion(finished, began, result)
            if finished.status == "completed" and environment is not None:
                # A version is credited with a use only when the call really finished well: the
                # candidate rule counts successful uses, and a retry must not be able to look like
                # a second Run (PROJECT.md section 10.5).
                self._record_use(finished, request, environment)

    def _record_use(
        self, record: ExecutionRecord, request: ExecutionRequest, environment: EnvironmentManifest
    ) -> None:
        """Credit the environment version this call ran in, without letting that fail the call.

        The command already succeeded and its evidence is archived; a retention ledger that cannot
        be written is a bookkeeping gap, not a reason to rewrite the call's outcome. It is reported
        as its own event so the gap is visible instead of being inferred from a missing count.
        """
        try:
            self.retention.record_use(
                environment,
                run_id=request.run_id,
                call_id=request.call_id,
                action_id=request.action_id,
                at=datetime.now(UTC),
            )
        except OSError:
            self._emit(record, "retention_use_unrecorded", {"call_id": str(request.call_id)})

    def _halt_requested(self, call_id: UUID) -> bool:
        """Whether an operator ended this call while it was still being prepared."""
        record = self._record(call_id)
        return (
            self._noted(call_id, "halt_requested") == "1"
            or record is None
            or record.status != "running"
        )

    def _stop_call(self, record: ExecutionRecord) -> StopOutcome:
        """Cancel: end the instance now and hand back whatever evidence already exists.

        Stopping the container ends the command that is running in it, so the call stops where it
        is rather than at its own deadline — and the transcript that was already archived stays
        the call's partial result. The request is recorded before it is acted on, so a cancel that
        arrives while the instance is still being prepared is honoured instead of racing it.

        A halt the manager could not confirm is reported as exactly that: the call is cancelled,
        but nothing claims its processes are gone.
        """
        call_id = record.request.call_id
        self._note(call_id, "halt_requested", "1")
        instance_id = self._noted(call_id, "instance_id")
        if instance_id:
            try:
                self.manager.halt_instance(
                    HaltRequest(instance_id=UUID(instance_id), reason="operator_cancelled")
                )
            except (SandboxRejected, RuntimeUnavailable, ValueError):
                pass
        return StopOutcome(
            result=self._result(record, None),
            stopped=self._stop_confirmed(instance_id),
        )

    def _stop_confirmed(self, instance_id: str | None) -> bool:
        """Whether the manager can prove this call's instance stopped.

        The manager owns that fact and answers it through its own interface: only it knows when a
        stop was confirmed, and reading its instance ledger here would put a second copy of that
        judgement beside the one the operator's projection already reads. An instance the manager
        cannot speak for — still running, or no longer part of its record at all — answers "cannot
        confirm", never "stopped". The only "yes" that does not come from the manager is the call
        that never had an instance: it cannot have left anything.
        """
        if not instance_id:
            return True
        try:
            return self.manager.stop_fact(UUID(instance_id)).state == "confirmed"
        except (SandboxRejected, ValueError):
            return False

    def _stop_fact(self, record: ExecutionRecord) -> bool:
        """What the manager can say about the instance this call used, if it used one."""
        return self._stop_confirmed(self._noted(record.request.call_id, "instance_id"))

    # -- helpers ------------------------------------------------------------------------------

    def _announce(
        self,
        record: ExecutionRecord,
        request: ExecutionRequest,
        instance: InstanceRecord,
        argv: list[str],
    ) -> ExecutionRecord:
        """Record where the call is going to act, before it acts.

        ``cwd`` is the profile's workspace mount — the directory the manager passes on the exec
        itself — so a reader sees where the command ran as a decision of this deployment rather
        than as whatever the tool image left as its default. The rest is the same kind of fact:
        the user the container runs as, the image digests the instance was built from, and the
        gateway that holds this instance's permits. All of it is written here, once, from the
        instance the manager just read back (spec 0002 section 2 clause 11, issue #19).
        """
        runtime = CallRuntime(
            action_id=request.action_id,
            execution_profile=request.execution_profile,
            target_ip=str(request.target_ip),
            target_port=request.target_port,
            parameters_hash=request.parameters_hash,
            argv=argv,
            cwd=self.profile.workspace_mount,
            user=self.profile.tool_user,
            instance_id=instance.instance_id,
            session_id=instance.session_id,
            image_digests=dict(instance.environment.image_digests),
            tool_inventory=list(instance.environment.tool_inventory),
            engine=instance.environment.engine,
            architecture=instance.environment.architecture,
            profile_version=instance.environment.profile_version,
            network_mode=self.profile.network_mode,
            gateway_ids=[
                resource.id for resource in instance.resources if resource.role == "gateway"
            ],
            authorized=[
                f"{endpoint.address}:{endpoint.port}" for endpoint in instance.egress.authorized
            ],
            observed_at=datetime.now(UTC),
        )
        record = self._runtime(record, runtime)
        return self._emit(
            record,
            "execution_instance",
            {
                "instance_id": str(instance.instance_id),
                "execution_profile": request.execution_profile,
                "profile_id": self.profile.profile_id,
                "action": request.action_id,
                "target": f"{request.target_ip}:{request.target_port}",
                "cwd": self.profile.workspace_mount,
                "user": self.profile.tool_user,
                "argv": argv,
                "network_mode": self.profile.network_mode,
            },
        )

    def _announce_completion(
        self, record: ExecutionRecord, began: datetime, result: CommandResult
    ) -> None:
        """Record that the action ended, with the wall time it really took.

        This is what lets the console separate "the platform never said anything" from "the action
        is still silent": the end is a fact of its own, written by the side that ran it. It is
        written from the record the terminal transition just returned, so the event lands with the
        outcome instead of racing a reader that already saw the call finish.
        """
        with self.lock:
            current = self._record(record.request.call_id)
            if current is None or current.status != record.status:
                return
            ended = datetime.now(UTC)
            self._emit(
                record,
                "execution_command_completed",
                {
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms
                    if result.duration_ms is not None
                    else int((ended - began).total_seconds() * 1000),
                    "started_at": began.isoformat(),
                    "ended_at": ended.isoformat(),
                },
            )

    def _truncated(self, record: ExecutionRecord) -> bool:
        """Whether any part of this call's transcript was cut or emptied by redaction."""
        return any(
            event.type in {"execution_output_truncated", "execution_output_redacted"}
            for event in record.events
        )

    def _settle_failure(self, call_id: UUID, reason_code: str) -> None:
        with self.lock:
            current = self._record(call_id)
            if current is None or current.status != "running":
                return
            self._failure(current, reason_code)

    def _release(self, instance_id: UUID, environment: EnvironmentManifest | None) -> None:
        """Revoke, stop and settle the instance this call used, whatever the outcome was.

        The private workspace of a call that ran in a real environment is handed to the retention
        ledger instead of being destroyed on the spot: it is that call's private reproduction
        material, and its cleanup follows the policy (TTL, capacity, pins) rather than this method
        (issue #20). A volume is kept only once the ledger names it: a ledger that cannot be written
        gives the volume back to the manager instead of leaving behind something nothing accounts
        for, and a volume that cannot even be given back is reclaimed like any other resource rather
        than left on the host unnamed.

        The call keeps naming that instance afterwards, even when the release failed: the note is
        how any later stop finds it, and erasing it would turn "this call still has an instance
        running" into "this call never had one" — the opposite of what the manager is saying.
        """
        try:
            halt = self.manager.halt_instance(
                HaltRequest(instance_id=instance_id, reason="operator_cancelled")
            )
        except (SandboxRejected, RuntimeUnavailable, ValueError):
            return
        try:
            report = self.manager.reclaim_instance(
                halt.instance_id, retain_volumes=environment is not None
            )
        except (SandboxRejected, RuntimeUnavailable, ValueError):
            # A release that could not complete stays visible on the instance record and in the
            # manager's ledger; the call's own outcome is not rewritten to hide it.
            return
        if environment is None:
            return
        # The record the halt returned is the manager's own statement about this instance, so the
        # retained material is attributed from the answer to the request rather than from a second
        # read of the manager's ledger.
        sizes = self.manager.volume_usage([item.id for item in report.retained])
        for volume in report.retained:
            try:
                self.retention.record_artifact(
                    environment,
                    run_id=halt.run_id,
                    session_id=halt.session_id,
                    instance_id=halt.instance_id,
                    resource_id=volume.id,
                    resource_name=volume.name,
                    size_bytes=sizes.get(volume.id),
                )
            except OSError:
                self._discard(halt.instance_id, volume.id)

    def _discard(self, instance_id: UUID, volume_id: str) -> None:
        """Remove a volume the retention ledger could not name, so nothing stays unnamed.

        The first attempt gives the volume back to the manager (which re-checks ownership), which is
        the ordinary "already gone" outcome. If even that refuses, the instance is reclaimed without
        the retention policy: this call's reproduction material is lost, which is the honest
        degradation, but the host does not keep a resource that nothing can name or reclaim.
        """
        try:
            self.manager.release_artifact(instance_id=instance_id, volume_id=volume_id)
            return
        except (SandboxRejected, RuntimeUnavailable, ValueError):
            pass
        try:
            self.manager.reclaim_instance(instance_id, retain_volumes=False)
        except (SandboxRejected, RuntimeUnavailable, ValueError):
            # The volume stays in the manager's retained list, where its ledger names it and the
            # Run's own resource view still reports it.
            return

    def _archive_result(
        self, record: ExecutionRecord, request: ExecutionRequest, result: CommandResult
    ) -> ExecutionResult:
        """Archive what the call produced, then describe it.

        The transcript is appended first (it is what the console shows), then stdout and stderr as
        their own files, each hashed from the bytes that were really written. A refusal from the
        archive propagates: this method never returns a result for output that was not kept.
        """
        text = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
        if text:
            record = self._output(record, text)
        stdout_evidence = self._archive(record, "stdout.txt", result.stdout)
        stderr_evidence = self._archive(record, "stderr.txt", result.stderr)
        transcript = self._result(record, result.exit_code)
        evidence = [
            *(transcript.evidence if transcript is not None else []),
            stdout_evidence,
            stderr_evidence,
        ]
        return ExecutionResult(
            output=result.stdout,
            exit_code=result.exit_code,
            evidence=evidence,
            summary=_summary(request, result),
            duration_ms=result.duration_ms,
            truncated=self._truncated(record),
            redacted=any(item.redacted for item in evidence),
        )


def _summary(request: ExecutionRequest, result: CommandResult) -> dict[str, Any]:
    """The fields the action declared, read from the JSON line it printed.

    Only declared fields are kept, so one action cannot smuggle another's facts into the summary
    a later decision branches on.
    """
    fields = request.spec.summary_fields
    if not fields:
        return {}
    summary: dict[str, Any] = {}
    if "exit_code" in fields:
        summary["exit_code"] = result.exit_code
    parsed: dict[str, Any] | None = None
    for line in reversed(result.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
            break
    for field in fields:
        if parsed is not None and field in parsed:
            summary[field] = parsed[field]
    return summary
