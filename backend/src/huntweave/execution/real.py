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
from pathlib import Path
from typing import Any
from uuid import UUID

from huntweave.contracts.execution import (
    REAL_ACTIONS,
    DiscoverTcpParameters,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
    ProbeHttpParameters,
    ShellExecParameters,
)
from huntweave.execution.ledger import CallLedger, RunnerRejected, RunnerUnavailable
from huntweave.execution.sandbox import (
    AuthorizedEndpoint,
    CommandResult,
    HaltRequest,
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
    ):
        super().__init__(state_dir, evidence_dir)
        self.manager = manager
        self.profile = profile

    # -- submission ---------------------------------------------------------------------------

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
        instance_id: UUID | None = None
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
            self._announce(record, request, instance_id)
            command = action_command(request, timeout)
            result = self.manager.run_command(instance_id, command, timeout)
        except SandboxRejected as refusal:
            self._settle_failure(call_id, refusal.reason_code)
            return
        except (RuntimeUnavailable, OSError, ValueError):
            self._settle_failure(call_id, "sandbox_instance_creation_failed")
            return
        finally:
            if instance_id is not None:
                self._release(call_id, instance_id)

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
            except OSError:
                self._failure(current, "evidence_storage_failed")
                return
            reason = None
            if result.timed_out:
                reason = "execution_timeout"
            elif result.exit_code != 0:
                reason = "action_failed"
            self._change(
                current,
                "completed" if result.exit_code == 0 else "failed",
                reason,
                settled,
            )

    def _halt_requested(self, call_id: UUID) -> bool:
        """Whether an operator ended this call while it was still being prepared."""
        record = self._record(call_id)
        return (
            self._noted(call_id, "halt_requested") == "1"
            or record is None
            or record.status != "running"
        )

    def _stop_call(self, record: ExecutionRecord) -> ExecutionResult | None:
        """Cancel: end the instance now and hand back whatever evidence already exists.

        Stopping the container ends the command that is running in it, so the call stops where it
        is rather than at its own deadline — and the transcript that was already archived stays
        the call's partial result. The request is recorded before it is acted on, so a cancel that
        arrives while the instance is still being prepared is honoured instead of racing it.
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
        return self._result(record, None)

    # -- helpers ------------------------------------------------------------------------------

    def _announce(
        self, record: ExecutionRecord, request: ExecutionRequest, instance_id: UUID
    ) -> None:
        """Record where the call is going to act, before it acts."""
        self._emit(
            record,
            "execution_instance",
            {
                "instance_id": str(instance_id),
                "execution_profile": request.execution_profile,
                "profile_id": self.profile.profile_id,
                "action": request.action_id,
                "target": f"{request.target_ip}:{request.target_port}",
            },
        )

    def _settle_failure(self, call_id: UUID, reason_code: str) -> None:
        with self.lock:
            current = self._record(call_id)
            if current is None or current.status != "running":
                return
            self._failure(current, reason_code)

    def _release(self, call_id: UUID, instance_id: UUID) -> None:
        """Revoke, stop and reclaim the instance this call used, whatever the outcome was."""
        try:
            halt = self.manager.halt_instance(
                HaltRequest(instance_id=instance_id, reason="operator_cancelled")
            )
            self.manager.reclaim_instance(halt.instance_id)
        except (SandboxRejected, RuntimeUnavailable, ValueError):
            # A release that could not complete stays visible on the instance record and in the
            # manager's ledger; the call's own outcome is not rewritten to hide it.
            return
        finally:
            self._note(call_id, "instance_id", "")

    def _archive_result(
        self, record: ExecutionRecord, request: ExecutionRequest, result: CommandResult
    ) -> ExecutionResult:
        """Archive what the call produced, then describe it.

        The transcript is appended first (it is what the console shows), then stdout and stderr
        as their own files, each hashed from the bytes that were really written.
        """
        text = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
        if text:
            record = self._output(record, text)
        stdout_evidence = self._archive(record, "stdout.txt", result.stdout.encode())
        stderr_evidence = self._archive(record, "stderr.txt", result.stderr.encode())
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
