"""Versioned execution contracts: one ticket shape, an action registry, and per-action schemas.

P1 lets the platform act for real, so the action and profile identifiers stop being a fixed set of
literals and become a registry: each action declares its own parameters, default and hard timeouts,
the summary it is expected to produce and whether a retry is even meaningful. Fake and real actions
share this one contract — the same submit, query, cancel, renew and reconcile path — which is what
makes replacing the execution side something the checks can see rather than something a caller
hopes for. A real action is never a wrapper around a fake one: it declares its own schema here.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, IPvAnyAddress, StrictInt, model_validator

from huntweave.contracts.runs import Contract

ActionId = Literal[
    "fake.collect",
    "fake.verify",
    "fake.review",
    "shell.exec",
    "discover_tcp_services",
    "probe_http",
]
ExecutionProfile = Literal["fake-p0-v1", "real-lab-v1"]

FAKE_ACTIONS: tuple[ActionId, ...] = ("fake.collect", "fake.verify", "fake.review")
REAL_ACTIONS: tuple[ActionId, ...] = ("shell.exec", "discover_tcp_services", "probe_http")

# The profile decides how far a call may reach; keeping the two apart means a deployment cannot
# present a fake call as a real one by editing an action name.
PROFILE_ACTIONS: dict[ExecutionProfile, tuple[ActionId, ...]] = {
    "fake-p0-v1": FAKE_ACTIONS,
    "real-lab-v1": REAL_ACTIONS,
}


class FakeParameters(Contract):
    """Parameters of the fixed demonstration actions. They never reach a target."""

    scenario: Literal["success", "failure", "needs_evidence"] = "success"
    duration_ms: int = Field(default=1500, ge=0, le=30000, strict=True)


class ShellExecParameters(Contract):
    """A command the research decision asked to run inside the tool container.

    The command is the product's purpose, not an escalation: it runs as the tool user in the
    session container, bounded by the ticket's authorized target, its deadline, the control lease
    and the action's hard timeout, and everything it prints is archived as evidence.
    """

    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: int = Field(default=60, ge=1, le=300, strict=True)


class DiscoverTcpParameters(Contract):
    """Check whether the authorized TCP endpoints are listening, and report which are."""

    ports: list[StrictInt] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=30, ge=1, le=120, strict=True)

    @model_validator(mode="after")
    def _ports_are_real(self) -> "DiscoverTcpParameters":
        if any(not 1 <= port <= 65535 for port in self.ports):
            raise ValueError("Ports must be integers in 1..65535")
        if len(set(self.ports)) != len(self.ports):
            raise ValueError("Ports must be unique")
        return self


class ProbeHttpParameters(Contract):
    """One HTTP request to the authorized target, kept to a safe method and a bounded path."""

    scheme: Literal["http", "https"] = "http"
    method: Literal["GET", "HEAD"] = "GET"
    path: str = Field(default="/", max_length=200)
    timeout_seconds: int = Field(default=20, ge=1, le=120, strict=True)

    @model_validator(mode="after")
    def _path_stays_on_the_target(self) -> "ProbeHttpParameters":
        # A leading "//" or an embedded "//" would let the path name another authority, which is
        # a different target from the one this ticket is bound to.
        if not self.path.startswith("/") or "//" in self.path:
            raise ValueError("Path must be an absolute path on the authorized target")
        return self


@dataclass(frozen=True)
class ActionSpec:
    """Everything one action declares about itself, in one place."""

    action: ActionId
    profile: ExecutionProfile
    parameters: type[Contract]
    default_timeout_seconds: int
    hard_timeout_seconds: int
    # What the execution side is expected to summarise from the raw output, and what a caller may
    # branch on. An empty mapping means the action produces raw output only.
    summary_fields: tuple[str, ...]
    # Whether repeating an action whose outcome is unknown could even be meaningful. Nothing here
    # authorises a retry: a retry needs a reconciliation verdict (spec 0002 section 3.3).
    retryable: bool


ACTIONS: dict[ActionId, ActionSpec] = {
    "fake.collect": ActionSpec(
        action="fake.collect",
        profile="fake-p0-v1",
        parameters=FakeParameters,
        default_timeout_seconds=30,
        hard_timeout_seconds=30,
        summary_fields=(),
        retryable=False,
    ),
    "fake.verify": ActionSpec(
        action="fake.verify",
        profile="fake-p0-v1",
        parameters=FakeParameters,
        default_timeout_seconds=30,
        hard_timeout_seconds=30,
        summary_fields=(),
        retryable=False,
    ),
    "fake.review": ActionSpec(
        action="fake.review",
        profile="fake-p0-v1",
        parameters=FakeParameters,
        default_timeout_seconds=30,
        hard_timeout_seconds=30,
        summary_fields=(),
        retryable=False,
    ),
    "shell.exec": ActionSpec(
        action="shell.exec",
        profile="real-lab-v1",
        parameters=ShellExecParameters,
        default_timeout_seconds=60,
        hard_timeout_seconds=300,
        summary_fields=("exit_code",),
        retryable=True,
    ),
    "discover_tcp_services": ActionSpec(
        action="discover_tcp_services",
        profile="real-lab-v1",
        parameters=DiscoverTcpParameters,
        default_timeout_seconds=30,
        hard_timeout_seconds=120,
        summary_fields=("open_ports", "closed_ports"),
        retryable=True,
    ),
    "probe_http": ActionSpec(
        action="probe_http",
        profile="real-lab-v1",
        parameters=ProbeHttpParameters,
        default_timeout_seconds=20,
        hard_timeout_seconds=120,
        summary_fields=("status_code", "server", "content_type"),
        retryable=True,
    ),
}


def action_spec(action_id: str) -> ActionSpec:
    """The spec of one action. An identifier nobody registered is refused, never guessed at."""
    try:
        return ACTIONS[action_id]  # type: ignore[index]
    except KeyError:
        raise ValueError(f"Unregistered action: {action_id}") from None


def parameters_hash(parameters: Contract | dict[str, Any]) -> str:
    value = parameters.model_dump() if isinstance(parameters, Contract) else parameters
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ExecutionRequest(Contract):
    protocol_version: Literal["1"] = "1"
    call_id: UUID = Field()
    run_id: UUID
    session_id: UUID
    decision_id: UUID
    scope_id: UUID
    budget_reservation_id: UUID
    action_id: ActionId
    # Validated against the action's own schema in `_parameters_fit_the_action`.
    parameters: dict[str, Any]
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_version: int = Field(ge=1, strict=True)
    policy_version: int = Field(ge=1, strict=True)
    lease_generation: int = Field(ge=1, strict=True)
    lease_expires_at: AwareDatetime
    deadline_at: AwareDatetime
    authorized_until: AwareDatetime
    execution_profile: ExecutionProfile = "fake-p0-v1"
    target_ip: IPvAnyAddress
    target_port: int = Field(ge=1, le=65535, strict=True)

    @model_validator(mode="after")
    def _parameters_fit_the_action(self) -> "ExecutionRequest":
        spec = action_spec(self.action_id)
        if spec.profile != self.execution_profile:
            # A ticket may not name an action from another profile: that is exactly how a real
            # call would end up being served by the demonstration side, or the reverse.
            raise ValueError("Action does not belong to the ticket's execution profile")
        spec.parameters.model_validate(self.parameters)
        return self

    @property
    def spec(self) -> ActionSpec:
        return action_spec(self.action_id)

    @property
    def typed_parameters(self) -> Contract:
        """The parameters as the action's own model, for code that has to read a field."""
        return self.spec.parameters.model_validate(self.parameters)


class ExecutionEvidence(Contract):
    id: UUID
    relative_path: str
    sha256: str
    size_bytes: int
    available: bool
    truncated: bool = False
    redacted: bool = False
    missing_reason: str | None = None


class ExecutionResult(Contract):
    output: str
    exit_code: int | None
    evidence: list[ExecutionEvidence]
    # What the execution side could read out of the raw output, in the fields its action declared.
    # A caller may branch on this; it is never a substitute for the raw output, which stays the
    # evidence, and an action that declares no summary fields leaves it empty.
    summary: dict[str, Any] = Field(default_factory=dict)


class ExecutionEvent(Contract):
    source_event_id: str
    type: str
    payload: dict[str, Any]
    created_at: AwareDatetime


class ExecutionObservation(Contract):
    """What the durable ledger can prove about one call, separately from its outcome.

    ``started`` is written before any side effect, so a ledger that proves it absent proves
    the action never ran. A ``None`` process, connection or lease state means the ledger
    cannot confirm either way: reconciliation must read that as "not stopped", never as a
    stop, because an operator's verdict never releases the execution side's resources.
    """

    started: bool
    process_active: bool | None
    connection_open: bool | None
    lease_active: bool | None = None
    observed_at: AwareDatetime


class ExecutionRecord(Contract):
    request: ExecutionRequest
    status: Literal["accepted", "running", "completed", "failed", "cancelled", "unknown"]
    reason_code: str | None = None
    result: ExecutionResult | None = None
    events: list[ExecutionEvent] = Field(default_factory=list)
    # Absent only on records the control plane states without asking the ledger; an
    # unproven absence of observation never authorises declaring a call not executed.
    observation: ExecutionObservation | None = None


class LeaseRenewal(Contract):
    lease_generation: int = Field(ge=1, strict=True)
    lease_expires_at: AwareDatetime


class CallCancellation(Contract):
    lease_generation: int = Field(ge=1, strict=True)