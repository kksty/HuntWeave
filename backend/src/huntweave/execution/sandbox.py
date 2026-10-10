"""Trusted sandbox lifecycle: the Runner's only, narrowly scoped path to the container runtime.

ADR-0010 keeps container management inside the Runner and narrows it to a fixed operation set;
ADR-0014 keeps three identities apart — the logical ``SandboxSession``, each execution instance,
and the ToolCall — and requires the old instance's facts to survive a rebuild. This module holds
the lifecycle; ``sandboxprofile`` holds the fixed description it must obey. ``SandboxManager``
performs exactly the operations named in ``ContainerRuntime`` and nothing else: a request carries
identities and the authorization it was opened under, never configuration, and the request
contracts forbid extra fields so no caller can smuggle any in.

Creation is recorded before it starts and again after every resource it creates, so a crash leaves
a ledger entry next to labeled resources instead of an invisible container. An instance is only
reported ready after its facts have been read back and matched against the profile, an instance
that did not finish is never presented as ready, and a new instance is refused while an earlier
instance of the same session is unaccounted for.
"""

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from huntweave.contracts.runs import Contract
from huntweave.execution.durable import atomic_write
from huntweave.execution.network_policy import Endpoint, NetworkPolicy, ScopeDenied
from huntweave.execution.sandboxprofile import (
    ContainerSpec,
    ResourceNotFound,
    RoleProfile,
    RuntimeUnavailable,
    SandboxLimits,
    SandboxProfile,
    SandboxRejected,
    VolumeMount,
)

__all__ = [
    "AuthorizedEndpoint",
    "ContainerFacts",
    "ContainerRuntime",
    "ContainerSpec",
    "EgressState",
    "EnvironmentManifest",
    "EvidenceRef",
    "HaltReason",
    "InstanceRecord",
    "LogRead",
    "LogSlice",
    "ManagedResource",
    "NetworkFacts",
    "ReclamationReport",
    "ResourceAudit",
    "ResourceNotFound",
    "RevertReport",
    "RoleProfile",
    "RuntimeFacts",
    "RuntimeResource",
    "RuntimeUnavailable",
    "SandboxInstanceRequest",
    "SandboxLimits",
    "SandboxManager",
    "SandboxObservation",
    "SandboxProfile",
    "SandboxRejected",
    "SandboxSessionRequest",
    "SessionRecord",
    "VolumeMount",
]

LABEL_NAMESPACE = "com.huntweave"
PROJECT_NAME = "huntweave"
CONTAINER_STOP_SECONDS = 10
# A control lease is short by design (spec 0002 section 3.7): the manager, not the control plane,
# is what bounds a tool whose conversation partner went away.
MAX_LEASE_SECONDS = 15
LEASE_POLL_SECONDS = 0.5
# How often the watchdog re-reads a running instance's rules. Reading them is an exec in the
# gateway, so it is bounded in time rather than done on every tick.
EGRESS_VERIFY_SECONDS = 2.0
# How the manager turns a profile's readiness command into a Docker health probe, and how often it
# reads the answer while a launch waits for it.
HEALTHCHECK_POLL_SECONDS = 0.2
HEALTHCHECK_INTERVAL_NS = 200_000_000
HEALTHCHECK_TIMEOUT_NS = 2_000_000_000
HEALTHCHECK_RETRIES = 3
# A platform whose own networks cannot be identified cannot promise to keep a tool out of them,
# so a launch refuses instead of assuming they are unreachable.
PLATFORM_LABEL = "com.docker.compose.project"

SandboxRole = Literal["gateway", "tool"]
ResourceRole = Literal["gateway", "tool", "workspace", "session"]
ResourceKind = Literal["container", "network", "volume"]
InstanceState = Literal["creating", "ready", "stopped", "reclaimed", "interrupted"]
# Why an instance stopped reaching its targets. Every path that ends an execution names itself,
# so a stop is never attributed to the wrong reason.
HaltReason = Literal[
    "operator_cancelled",
    "execution_timeout",
    "control_lease_expired",
    "scope_revoked",
    "egress_unverified",
    "revert",
]

ROLES: tuple[str, ...] = ("gateway", "tool", "workspace", "session")


# --------------------------------------------------------------------------------------------
# The fixed operation set
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerFacts:
    """What the daemon says a container really is, read back after it was created."""

    id: str
    name: str
    running: bool
    labels: Mapping[str, str]
    user: str
    network_mode: str
    capabilities_add: tuple[str, ...]
    capabilities_drop: tuple[str, ...]
    security_options: tuple[str, ...]
    read_only_rootfs: bool
    mounts: tuple[VolumeMount, ...]
    health: str


@dataclass(frozen=True)
class RuntimeFacts:
    engine: str
    architecture: str


@dataclass(frozen=True)
class NetworkFacts:
    """A network the runtime knows about, with the addresses that identify its reach.

    ``gateway`` is the host-side interface of a bridge network: it is the host's own address on
    that bridge, which is exactly the kind of management interface a tool must not reach.
    """

    id: str
    name: str
    subnet: str | None
    internal: bool
    gateway: str | None = None


@dataclass(frozen=True)
class LogRead:
    """A bounded log read, and whether either bound may have cut the output."""

    text: str
    lines_returned: int
    line_bound: int


@dataclass(frozen=True)
class CommandResult:
    """What running one command inside the tool container produced.

    ``exit_code`` is the command's own status (124, the shell's "timed out", is reported as a
    timeout instead), and both streams come back whole so the caller can archive them as evidence
    rather than summarising them away.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class RuntimeResource:
    kind: ResourceKind
    id: str
    name: str
    labels: Mapping[str, str]
    running: bool | None = None


@runtime_checkable
class ContainerRuntime(Protocol):
    """The trusted component's whole reach into the container runtime.

    These are fixed operations: create, start, stop and remove a session container, create and
    remove the per-session gateway, network and workspace volume, attach the gateway to the
    deployment's target network, install and read the gateway's egress rules through the profile's
    fixed command, read this project's labeled containers and processes, read bounded logs, and
    resolve an image. There is deliberately no generic request method, no command execution and no
    ``**options`` passthrough, so a caller cannot ask the daemon for anything this interface does
    not name.
    """

    def probe(self) -> RuntimeFacts: ...

    def resolve_image(self, reference: str) -> str: ...

    def create_network(
        self,
        *,
        name: str,
        labels: Mapping[str, str],
        internal: bool,
        driver: str,
        gateway_mode: str,
    ) -> str: ...

    def remove_network(self, network_id: str) -> None: ...

    def create_volume(self, *, name: str, labels: Mapping[str, str]) -> str: ...

    def remove_volume(self, volume_id: str) -> None: ...

    def create_container(
        self, *, name: str, labels: Mapping[str, str], spec: ContainerSpec
    ) -> str: ...

    def start_container(self, container_id: str) -> None: ...

    def stop_container(self, container_id: str, timeout_seconds: int) -> None: ...

    def remove_container(self, container_id: str, force: bool) -> None: ...

    def connect_network(self, container_id: str, network_name: str) -> None: ...

    def network_facts(self, network_id: str) -> NetworkFacts: ...

    def own_networks(self) -> tuple[NetworkFacts, ...]: ...

    def apply_gateway_policy(self, gateway_id: str, payload: str) -> None: ...

    def read_gateway_policy(self, gateway_id: str) -> str: ...

    def container_facts(self, container_id: str) -> ContainerFacts: ...

    def container_processes(self, container_id: str, limit: int) -> tuple[str, ...]: ...

    def exec_in_tool(
        self, container_id: str, argv: Sequence[str], timeout_seconds: int
    ) -> CommandResult: ...

    def container_logs(
        self, container_id: str, *, tail_lines: int, tail_bytes: int
    ) -> LogRead: ...

    def list_resources(self, labels: Mapping[str, str]) -> tuple[RuntimeResource, ...]: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


class EnvironmentManifest(Contract):
    """The immutable environment an instance was created from (ADR-0014, spec 0002 section 3.6).

    It carries what this slice can really observe: the profile version, the resolved image
    digests, the tool inventory the profile declares, and the engine and architecture of the host
    that ran it. It is not a snapshot of the private workspace, holds no target credential, and
    says nothing about current isolation health.
    """

    profile_id: str
    profile_version: int
    image_digests: dict[str, str]
    tool_inventory: list[str]
    engine: str
    architecture: str
    created_at: AwareDatetime


class ManagedResource(Contract):
    kind: ResourceKind
    role: ResourceRole | None = None
    id: str
    name: str
    labels: dict[str, str]
    running: bool | None = None


class AuthorizedEndpoint(Contract):
    """One IPv4/TCP endpoint an instance is authorized to reach."""

    address: str
    port: int = Field(ge=1, le=65535, strict=True)


class EgressChange(Contract):
    """One change to what an instance may reach, with the moment it took effect."""

    authorized: list[AuthorizedEndpoint] = Field(default_factory=list)
    at: AwareDatetime
    reason: str


class EgressState(Contract):
    """What the gateway was last told an instance may reach, and when that changed.

    The timings are recorded because "the scope shrank, so the call stopped" is only explainable
    with the moments: when the rules were applied, when they were revoked, and why.
    """

    authorized: list[AuthorizedEndpoint] = Field(default_factory=list)
    applied_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None
    revocation_reason: str | None = None


class InstanceRecord(Contract):
    instance_id: UUID
    session_id: UUID
    run_id: UUID
    scope_id: UUID
    scope_version: int
    policy_version: int
    state: InstanceState
    environment: EnvironmentManifest
    created_at: AwareDatetime
    egress: EgressState = Field(default_factory=EgressState)
    egress_changes: list[EgressChange] = Field(default_factory=list)
    # The rules the gateway held when a verification failed, kept so the halt is explicable.
    egress_observation: str | None = None
    # While a lease is set, an instance that stops renewing it is halted by the manager itself:
    # a control plane that goes away must not leave a tool running against a target.
    lease_expires_at: AwareDatetime | None = None
    halt_reason: HaltReason | None = None
    stop_confirmed_at: AwareDatetime | None = None
    reclaimed_at: AwareDatetime | None = None
    resources: list[ManagedResource] = Field(default_factory=list)


class SessionRecord(Contract):
    session_id: UUID
    run_id: UUID
    agent_session_id: UUID
    scope_id: UUID
    scope_version: int
    policy_version: int
    created_at: AwareDatetime
    instances: list[UUID] = Field(default_factory=list)


class SandboxSessionRequest(Contract):
    """Open (or reuse) the execution session of one model session under one authorization.

    The authorization identity is part of the request because an execution session is bound to
    the authorization it was opened under: the same model session under a different scope version
    or policy version is a different thing, not a session to reuse.
    """

    run_id: UUID
    agent_session_id: UUID
    scope_id: UUID
    scope_version: int = Field(ge=1, strict=True)
    policy_version: int = Field(ge=1, strict=True)


class SandboxInstanceRequest(Contract):
    """Create a new execution instance for an existing session.

    It carries the authorization this instance may act under — the endpoints it may reach — and
    no configuration: images, mounts, networks and limits stay the profile's business.
    """

    session_id: UUID
    authorized: list[AuthorizedEndpoint] = Field(default_factory=list)


class EgressUpdate(Contract):
    """A changed authorization for a live instance: a narrower scope or a revocation."""

    instance_id: UUID
    authorized: list[AuthorizedEndpoint] = Field(default_factory=list)
    reason: Literal["initial", "scope_shrunk", "scope_revoked"]


class HaltRequest(Contract):
    """End an instance now: cancel, timeout, scope revocation or revert."""

    instance_id: UUID
    reason: HaltReason


class LeaseRenewal(Contract):
    """Keep one instance's control lease alive, or let the manager stop it when it lapses."""

    instance_id: UUID
    lease_expires_at: AwareDatetime


class LogSlice(Contract):
    role: SandboxRole
    text: str
    truncated: bool


class EvidenceRef(Contract):
    relative_path: str
    sha256: str
    size_bytes: int


class ReclamationReport(Contract):
    instance_ids: list[UUID]
    removed: list[ManagedResource]
    failed: list[str]

    @property
    def complete(self) -> bool:
        return not self.failed


class RevertReport(Contract):
    """The execution side's answer to a deployment revert request.

    `withdrew` is the only thing that authorises withdrawing the management capability: while
    anything is outstanding the deployment keeps it, because something is still out there that
    the platform has not accounted for.
    """

    state: Literal["complete", "blocked"]
    reason_code: str | None = None
    halted: list[UUID] = Field(default_factory=list)
    reclaimed: list[ManagedResource] = Field(default_factory=list)
    # Where the reconciliation record of this revert was archived.
    archived: list[str] = Field(default_factory=list)
    outstanding: list[str] = Field(default_factory=list)
    withdrew: bool = False


class ResourceAudit(Contract):
    """What the ledger and the runtime disagree about. Neither side is trusted alone."""

    unaccounted: list[ManagedResource]
    missing: list[ManagedResource]
    interrupted: list[UUID]


class SandboxObservation(Contract):
    available: bool
    profile_id: str
    reason_code: str | None = None
    observed_at: AwareDatetime | None = None


# --------------------------------------------------------------------------------------------
# Labels and names
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceNames:
    network: str
    volume: str
    gateway: str
    tool: str


def _labels(
    *,
    run_id: UUID,
    profile_id: str,
    session_id: UUID | None = None,
    instance_id: UUID | None = None,
    role: str | None = None,
) -> dict[str, str]:
    """The only label set this component writes, and the only one it selects by."""
    values: dict[str, str | None] = {
        "project": PROJECT_NAME,
        "run_id": str(run_id),
        "session_id": None if session_id is None else str(session_id),
        "instance_id": None if instance_id is None else str(instance_id),
        "role": role,
        "profile": profile_id,
    }
    return {
        f"{LABEL_NAMESPACE}.{key}": value for key, value in values.items() if value is not None
    }


def _project_selector(**extra: str) -> dict[str, str]:
    return {f"{LABEL_NAMESPACE}.project": PROJECT_NAME, **extra}


def _endpoints(authorized: Sequence[AuthorizedEndpoint]) -> tuple[Endpoint, ...]:
    """Turn an authorization into the policy's endpoints.

    This profile is IPv4/TCP only, so an address it cannot express — an IPv6 literal, a name, a
    malformed value — is a scope the sandbox cannot honour: the request is refused rather than
    quietly narrowed to something else.
    """
    endpoints: list[Endpoint] = []
    for item in authorized:
        try:
            endpoints.append(Endpoint(IPv4Address(item.address), item.port))
        except ValueError:
            # A distinct cause gets a distinct code: this is not a scope decision the control
            # plane made, it is an endpoint this profile cannot express at all.
            raise SandboxRejected("endpoint_not_expressible") from None
    return tuple(endpoints)


def _names(run_id: UUID, session_id: UUID, instance_id: UUID) -> ResourceNames:
    """Predictable names, derived from identities only: a human sees which session and instance a
    container belongs to, and a reclaimer can check the prefix as a second ownership signal."""
    stem = f"huntweave-{str(run_id)[:8]}-{str(session_id)[:8]}-{str(instance_id)[:8]}"
    return ResourceNames(
        network=f"{stem}-session",
        volume=f"{stem}-workspace",
        gateway=f"{stem}-gateway",
        tool=f"{stem}-tool",
    )


def _without_running(resources: Sequence[ManagedResource]) -> list[ManagedResource]:
    return [
        item.model_copy(update={"running": False}) if item.kind == "container" else item
        for item in resources
    ]


def _owned_by(resource: ManagedResource, record: InstanceRecord) -> bool:
    """Ownership is not a label alone: the labels this project writes *and* the name derived from
    the same identities must both agree before anything is removed."""
    return _owned_by_session(
        resource, record.run_id, record.session_id
    ) and resource.labels.get(f"{LABEL_NAMESPACE}.instance_id") == str(record.instance_id)


def _owned_by_session(resource: ManagedResource, run_id: UUID, session_id: UUID) -> bool:
    labels = resource.labels
    stem = f"huntweave-{str(run_id)[:8]}-{str(session_id)[:8]}-"
    return (
        labels.get(f"{LABEL_NAMESPACE}.project") == PROJECT_NAME
        and labels.get(f"{LABEL_NAMESPACE}.run_id") == str(run_id)
        and labels.get(f"{LABEL_NAMESPACE}.session_id") == str(session_id)
        and resource.name.startswith(stem)
    )


def _as_resource(resource: RuntimeResource) -> ManagedResource:
    return ManagedResource(
        kind=resource.kind,
        role=_role_of(resource.labels),
        id=resource.id,
        name=resource.name,
        labels=dict(resource.labels),
        running=resource.running,
    )


def _role_of(labels: Mapping[str, str]) -> ResourceRole | None:
    """A label this component did not write is not a role: unknown values read as "no role"."""
    value = labels.get(f"{LABEL_NAMESPACE}.role")
    return cast(ResourceRole, value) if value in ROLES else None


def _permits_match(rules: str, authorized: Sequence[Endpoint]) -> bool:
    """Whether the gateway's live rules permit exactly the authorized endpoints.

    Counted by endpoint, not by distinct address: two ports on one target are two permits. Two
    directions matter: a missing permit is an execution that cannot work, and an extra permit is
    an execution that reaches somewhere nobody authorized.
    """
    if ":OUTPUT DROP" not in rules or ":FORWARD DROP" not in rules:
        return False
    grants = [line for line in rules.splitlines() if line.rstrip().endswith("-j ACCEPT")]
    if len(grants) != 2 * len(authorized):
        return False
    mentioned = {
        token
        for line in grants
        for token in re.findall(r"-(?:d|s) (\d+\.\d+\.\d+\.\d+)", line)
    }
    return mentioned == {str(item.address) for item in authorized}


def _write_ledger(
    path: Path, sessions: Mapping[str, SessionRecord], instances: Mapping[str, InstanceRecord]
) -> None:
    """One durable write of the whole ledger: a reader sees a consistent pair of maps."""
    document = {
        "sessions": {key: record.model_dump(mode="json") for key, record in sessions.items()},
        "instances": {key: record.model_dump(mode="json") for key, record in instances.items()},
    }
    atomic_write(path, json.dumps(document, sort_keys=True).encode())


# --------------------------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------------------------


class SandboxManager:
    """Trusted lifecycle owner: creates, observes, bounds and reclaims this project's sandboxes.

    The manager creates the containers, installs the gateway's egress policy, keeps each instance
    inside its control lease, and reclaims what a round used. It never starts a target action
    itself: the tool container is where actions happen, and the rules the gateway holds are what
    decide whether they can leave.
    """

    def __init__(
        self,
        *,
        runtime: ContainerRuntime,
        profile: SandboxProfile,
        state_dir: Path,
        evidence_dir: Path,
    ):
        self.runtime = runtime
        self.profile = profile
        self.state_dir, self.evidence_dir = state_dir, evidence_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = state_dir / "sandboxes.json"
        self.sessions, self.instances = self._load()
        # A revert lives for this process: the durable act is the deployment withdrawing the
        # management capability, and after that restart nothing may open real execution again.
        self.reverting: str | None = None
        self.closed = threading.Event()
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()

    # -- durability ---------------------------------------------------------------------------

    def _load(self) -> tuple[dict[str, SessionRecord], dict[str, InstanceRecord]]:
        if not self.path.exists():
            return {}, {}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        sessions = {
            key: SessionRecord.model_validate(value) for key, value in document["sessions"].items()
        }
        instances = {
            key: InstanceRecord.model_validate(value)
            for key, value in document["instances"].items()
        }
        # A creation that did not finish is not a ready instance. It stays in the ledger as
        # interrupted, so the next launch has to account for it before it may create anything.
        repaired = False
        for key, record in list(instances.items()):
            if record.state == "creating":
                instances[key] = record.model_copy(update={"state": "interrupted"})
                repaired = True
        if repaired:
            _write_ledger(self.path, sessions, instances)
        return sessions, instances

    def _persist(self) -> None:
        _write_ledger(self.path, self.sessions, self.instances)

    def _store(self, record: InstanceRecord) -> InstanceRecord:
        self.instances[str(record.instance_id)] = record
        self._persist()
        return record

    def _store_session(self, record: SessionRecord) -> SessionRecord:
        self.sessions[str(record.session_id)] = record
        self._persist()
        return record

    # -- observation --------------------------------------------------------------------------

    def observe(self) -> SandboxObservation:
        """Report whether this deployment can really manage sandboxes right now."""
        try:
            self.runtime.probe()
        except Exception:
            # Any failure to answer is the same fact to an operator: no trusted observation.
            return SandboxObservation(
                available=False,
                profile_id=self.profile.profile_id,
                reason_code="sandbox_runtime_unreachable",
                observed_at=datetime.now(UTC),
            )
        return SandboxObservation(
            available=True,
            profile_id=self.profile.profile_id,
            observed_at=datetime.now(UTC),
        )

    def resources(
        self, *, run_id: UUID | None = None, session_id: UUID | None = None
    ) -> list[ManagedResource]:
        selector = _project_selector()
        if run_id is not None:
            selector[f"{LABEL_NAMESPACE}.run_id"] = str(run_id)
        if session_id is not None:
            selector[f"{LABEL_NAMESPACE}.session_id"] = str(session_id)
        return [_as_resource(resource) for resource in self.runtime.list_resources(selector)]

    def audit(self) -> ResourceAudit:
        """Compare the ledger with the runtime. Both directions matter: a resource the ledger
        never recorded is unaccounted, and a resource the runtime lost is missing rather than
        silently forgotten."""
        with self.lock:
            live = self.runtime.list_resources(_project_selector())
            unaccounted = [
                _as_resource(resource)
                for resource in live
                if resource.labels.get(f"{LABEL_NAMESPACE}.instance_id") not in self.instances
            ]
            present = {resource.id for resource in live}
            missing = [
                resource
                for record in self.instances.values()
                if record.state not in {"reclaimed"}
                for resource in record.resources
                if resource.id not in present
            ]
            interrupted = [
                record.instance_id
                for record in self.instances.values()
                if record.state == "interrupted"
            ]
            return ResourceAudit(unaccounted=unaccounted, missing=missing, interrupted=interrupted)

    def processes(self, instance_id: UUID, limit: int = 64) -> list[str]:
        container = self._container_of(self._instance(instance_id), "tool")
        try:
            return list(self.runtime.container_processes(container.id, limit))
        except ResourceNotFound:
            raise SandboxRejected("sandbox_resource_missing") from None

    def run_command(
        self, instance_id: UUID, argv: Sequence[str], timeout_seconds: int
    ) -> CommandResult:
        """Run one command inside this instance's tool container, as the tool user.

        This is the only place a target action happens: inside the container the profile created,
        under the permit the gateway holds, as the unprivileged user the profile names. The
        command itself is the caller's — that is the product's purpose — and everything else about
        where and how it runs was decided before the call arrived.
        """
        record = self._instance(instance_id)
        if record.state != "ready":
            raise SandboxRejected("sandbox_instance_not_running")
        container = self._container_of(record, "tool")
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise SandboxRejected("action_command_invalid")
        try:
            return self.runtime.exec_in_tool(container.id, list(argv), timeout_seconds)
        except ResourceNotFound:
            raise SandboxRejected("sandbox_resource_missing") from None

    def logs(self, instance_id: UUID, role: SandboxRole, tail_bytes: int | None = None) -> LogSlice:
        container = self._container_of(self._instance(instance_id), role)
        bound = self.profile.limits.log_tail_bytes
        requested = bound if tail_bytes is None else min(tail_bytes, bound)
        try:
            read = self.runtime.container_logs(
                container.id,
                tail_lines=self.profile.limits.log_line_bound,
                tail_bytes=requested,
            )
        except ResourceNotFound:
            raise SandboxRejected("sandbox_resource_missing") from None
        return LogSlice(
            role=role,
            text=read.text,
            # Either bound may have cut the output: the daemon keeps only the last lines, and only
            # the last bytes of those are returned. Both facts are reported, not just the one the
            # manager applied itself.
            truncated=read.lines_returned >= read.line_bound
            or len(read.text.encode()) >= requested,
        )

    def archive_evidence(
        self, *, run_id: UUID, instance_id: UUID, name: str, payload: bytes
    ) -> EvidenceRef:
        """Archive one already-collected file for this instance, bounded and attributable.

        The caller names a file, not a path: the manager composes the path from the Run and the
        instance, so no argument can address anything outside the evidence root.
        """
        record = self._instance(instance_id)
        if record.run_id != run_id:
            raise SandboxRejected("sandbox_instance_mismatch")
        if not name or len(name) > 64 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in name
        ):
            raise SandboxRejected("evidence_name_rejected")
        if len(payload) > self.profile.limits.evidence_max_bytes:
            raise SandboxRejected("evidence_too_large")
        relative = f"{run_id}/{instance_id}/{name}"
        try:
            destination = self.evidence_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, payload)
        except OSError:
            raise SandboxRejected("evidence_storage_failed") from None
        return EvidenceRef(
            relative_path=relative,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )

    # -- lifecycle ----------------------------------------------------------------------------

    def open_session(self, request: SandboxSessionRequest) -> SessionRecord:
        with self.lock:
            for record in self.sessions.values():
                if (
                    record.run_id == request.run_id
                    and record.agent_session_id == request.agent_session_id
                ):
                    if (
                        record.scope_id,
                        record.scope_version,
                        record.policy_version,
                    ) != (request.scope_id, request.scope_version, request.policy_version):
                        # The session belongs to the authorization it was opened under. A
                        # different scope or policy is a different session, never an adoption.
                        raise SandboxRejected("sandbox_authorization_mismatch")
                    return record
            return self._store_session(
                SessionRecord(
                    session_id=uuid4(),
                    run_id=request.run_id,
                    agent_session_id=request.agent_session_id,
                    scope_id=request.scope_id,
                    scope_version=request.scope_version,
                    policy_version=request.policy_version,
                    created_at=datetime.now(UTC),
                )
            )

    def launch_instance(self, request: SandboxInstanceRequest) -> InstanceRecord:
        """Create one new execution instance for a session, with its own identity.

        A rebuild is a new instance: the previous one keeps its identity, environment and
        resource history. The new instance may only be created once nothing of the previous one
        is unaccounted for, because an un-cleared old instance must never be masked by the new
        one's health.

        The order is part of the guarantee: the gateway is created, attached to the target
        network and *started* first, the manager waits until it reports its default-deny rules
        installed, then the allow-list is applied, and only after that is the tool container
        created. A tool process therefore never exists before the rules that bound it.

        An endpoint inside the platform's own networks, the host or the metadata range is refused
        before any container exists: an authorization that names infrastructure is not an
        authorization the sandbox can honour.
        """
        with self.lock:
            if self.reverting is not None:
                raise SandboxRejected("sandbox_reverting")
            session = self._session(request.session_id)
            for record in self._instances_of(session):
                if record.state == "interrupted":
                    raise SandboxRejected("sandbox_instance_interrupted")
                if record.state in {"creating", "ready"}:
                    raise SandboxRejected("sandbox_instance_active")
            try:
                endpoints = _endpoints(request.authorized)
                NetworkPolicy(endpoints, self._protected_networks())
            except ScopeDenied:
                raise SandboxRejected("scope_denied") from None
            leftovers = self.runtime.list_resources(
                _project_selector(**{f"{LABEL_NAMESPACE}.session_id": str(session.session_id)})
            )
            # Only resources this session's ledger cannot explain block a launch. A stopped
            # instance's containers are accounted for; a container whose ledger write never
            # happened is not, and it has to be reclaimed before a new instance may exist.
            accounted = {
                resource.id
                for record in self._instances_of(session)
                for resource in record.resources
            }
            if any(resource.id not in accounted for resource in leftovers):
                raise SandboxRejected("sandbox_resources_unaccounted")

            instance_id = uuid4()
            try:
                environment = self._manifest()
            except ResourceNotFound:
                raise SandboxRejected("sandbox_image_unavailable") from None
            except RuntimeUnavailable:
                raise SandboxRejected("sandbox_runtime_unreachable") from None
            record = self._store(
                InstanceRecord(
                    instance_id=instance_id,
                    session_id=session.session_id,
                    run_id=session.run_id,
                    scope_id=session.scope_id,
                    scope_version=session.scope_version,
                    policy_version=session.policy_version,
                    state="creating",
                    environment=environment,
                    created_at=datetime.now(UTC),
                    egress=EgressState(authorized=list(request.authorized)),
                )
            )
            # The session knows about the instance as soon as its identity exists: an attempt that
            # fails halfway is still an attempt the session has to account for.
            session = self._store_session(
                session.model_copy(update={"instances": [*session.instances, instance_id]})
            )
            names = _names(session.run_id, session.session_id, instance_id)
            labels = _labels(
                run_id=session.run_id,
                session_id=session.session_id,
                instance_id=instance_id,
                profile_id=self.profile.profile_id,
            )
            try:
                volume = self.runtime.create_volume(
                    name=names.volume,
                    labels={**labels, f"{LABEL_NAMESPACE}.role": "workspace"},
                )
                record = self._record_resource(
                    record,
                    ManagedResource(
                        kind="volume",
                        role="workspace",
                        id=volume,
                        name=names.volume,
                        labels=dict(labels),
                    ),
                )
                network = self.runtime.create_network(
                    name=names.network,
                    labels={**labels, f"{LABEL_NAMESPACE}.role": "session"},
                    internal=self.profile.network_internal,
                    driver=self.profile.network_driver,
                    gateway_mode=self.profile.network_gateway_mode,
                )
                record = self._record_resource(
                    record,
                    ManagedResource(
                        kind="network",
                        role="session",
                        id=network,
                        name=names.network,
                        labels=dict(labels),
                    ),
                )
                gateway_spec = self.profile.gateway_spec(network)
                gateway = self.runtime.create_container(
                    name=names.gateway,
                    labels={**labels, f"{LABEL_NAMESPACE}.role": "gateway"},
                    spec=gateway_spec,
                )
                if self.profile.target_network is not None:
                    self.runtime.connect_network(gateway, self.profile.target_network)
                self.runtime.start_container(gateway)
                record = self._record_resource(
                    record,
                    ManagedResource(
                        kind="container",
                        role="gateway",
                        id=gateway,
                        name=names.gateway,
                        labels=dict(labels),
                        running=True,
                    ),
                )
                # The bridges' host-side addresses are only knowable once the session network
                # exists, so the policy the gateway really gets is built here, with them included.
                try:
                    policy = NetworkPolicy(
                        endpoints, self._protected_networks(session_network=network)
                    )
                except ScopeDenied:
                    raise SandboxRejected("scope_denied") from None
                # The gateway's own startup installs the default-deny rules; nothing may reach a
                # target until it has reported them, and nothing else starts before that either.
                self._await_gateway_ready(gateway)
                record = self._apply_policy(record, gateway, policy, "initial")
                tool_spec = self.profile.tool_spec(gateway, volume)
                tool = self.runtime.create_container(
                    name=names.tool,
                    labels={**labels, f"{LABEL_NAMESPACE}.role": "tool"},
                    spec=tool_spec,
                )
                self.runtime.start_container(tool)
                record = self._record_resource(
                    record,
                    ManagedResource(
                        kind="container",
                        role="tool",
                        id=tool,
                        name=names.tool,
                        labels=dict(labels),
                        running=True,
                    ),
                )
                self._verify_instance(gateway, gateway_spec, tool, tool_spec)
            except SandboxRejected:
                self._store(record.model_copy(update={"state": "interrupted"}))
                raise
            except Exception:
                # The resources created so far stay in the ledger: a failure here is an
                # interrupted instance with named leftovers, not an invisible container.
                self._store(record.model_copy(update={"state": "interrupted"}))
                raise SandboxRejected("sandbox_instance_creation_failed") from None
            return self._store(record.model_copy(update={"state": "ready"}))

    def stop_instance(self, instance_id: UUID) -> InstanceRecord:
        """Stop the instance's containers and confirm the stop. Stopping is not reclaiming: the
        resource records and the environment manifest stay, and the workspace is untouched.

        A stop withdraws what the instance was still allowed to reach and clears its lease: an
        instance that is not running holds no permit and no live claim.
        """
        with self.lock:
            record = self._instance(instance_id)
            if record.state == "reclaimed":
                raise SandboxRejected("sandbox_instance_reclaimed")
            record = self._revoke_if_permitted(record, "stopped")
            resources = self._stop_containers(record)
            stopped = record.model_copy(
                update={
                    "state": "stopped",
                    "stop_confirmed_at": datetime.now(UTC),
                    "lease_expires_at": None,
                    "egress": record.egress.model_copy(
                        update={
                            "authorized": [],
                            "revoked_at": record.egress.revoked_at or datetime.now(UTC),
                        }
                    ),
                    "resources": resources,
                }
            )
            return self._store(stopped)

    def _revoke_if_permitted(self, record: InstanceRecord, reason: str) -> InstanceRecord:
        """Withdraw the permits a record still carries, tolerating a gateway that refuses.

        A stop must end the execution whether or not the rule change could be installed, so a
        failed revocation is recorded and the stop proceeds: the containers are what can still
        send, and they are about to be stopped.
        """
        if not record.egress.authorized:
            return record
        try:
            return self.revoke_egress(record.instance_id, reason)
        except SandboxRejected:
            return self._store(
                record.model_copy(
                    update={
                        "egress": record.egress.model_copy(
                            update={
                                "authorized": [],
                                "revoked_at": datetime.now(UTC),
                                "revocation_reason": "revocation_failed",
                            }
                        )
                    }
                )
            )

    # -- egress -------------------------------------------------------------------------------

    def authorize_egress(self, update: EgressUpdate) -> InstanceRecord:
        """Replace the instance's authorization with a narrower one, immediately.

        The rules are replaced, not added to: an endpoint the new authorization omits loses its
        permit at the same moment, and any flow still using it is revoked by the rule change
        rather than by waiting for a timeout.
        """
        with self.lock:
            record = self._instance(update.instance_id)
            if record.state != "ready":
                raise SandboxRejected("sandbox_instance_not_running")
            gateway = self._container_of(record, "gateway")
            try:
                policy = NetworkPolicy(
                    _endpoints(update.authorized),
                    self._protected_networks(session_network=self._session_network(record)),
                )
            except ScopeDenied:
                raise SandboxRejected("scope_denied") from None
            return self._apply_policy(record, gateway.id, policy, update.reason)

    def revoke_egress(self, instance_id: UUID, reason: str) -> InstanceRecord:
        """Close the instance's egress now, keeping its containers and workspace.

        Revoking is a separate act from stopping: the gateway keeps refusing packets while the
        stop path confirms the processes and connections that were already open.
        """
        with self.lock:
            record = self._instance(instance_id)
            if record.state in {"reclaimed", "interrupted"}:
                raise SandboxRejected("sandbox_instance_not_running")
            gateway = self._container_of(record, "gateway")
            return self._apply_policy(record, gateway.id, NetworkPolicy(), reason)

    def halt_instance(self, request: HaltRequest) -> InstanceRecord:
        """End an instance for a named reason: revoke, stop, and confirm both.

        The order matters for the target's sake: the permit is withdrawn first, so nothing new
        leaves, and only then are the containers stopped and the stop confirmed. A revocation the
        gateway refused does not skip the stop — it is recorded, and the stop still ends the
        execution.
        """
        with self.lock:
            record = self._revoke_if_permitted(
                self._instance(request.instance_id), request.reason
            )
            if record.state != "stopped":
                record = self.stop_instance(request.instance_id)
            return self._store(record.model_copy(update={"halt_reason": request.reason}))

    # -- control lease ------------------------------------------------------------------------

    def renew_instance_lease(self, renewal: LeaseRenewal) -> InstanceRecord:
        """Extend an instance's control lease.

        A lease bounds how long a tool may keep running while nobody is watching: the manager
        halts an instance whose lease lapses instead of trusting a control plane that stopped
        answering. Renewing therefore re-checks what the run still depends on — the execution side
        is reachable, the instance is still running and its permit is still the verified one — and
        refuses instead of extending a lease it cannot stand behind. Authorization and budget are
        re-checked by the control plane before it asks, and by the ticket path once calls are
        bound to instances.
        """
        with self.lock:
            record = self._instance(renewal.instance_id)
            if record.state != "ready":
                raise SandboxRejected("sandbox_instance_not_running")
            try:
                self.runtime.probe()
            except RuntimeUnavailable:
                raise SandboxRejected("sandbox_runtime_unreachable") from None
            try:
                gateway = self._container_of(record, "gateway")
                rules = self.runtime.read_gateway_policy(gateway.id)
            except (ResourceNotFound, RuntimeUnavailable):
                raise SandboxRejected("sandbox_egress_unverified") from None
            if not _permits_match(rules, _endpoints(record.egress.authorized)):
                raise SandboxRejected("sandbox_egress_unverified")
            now = datetime.now(UTC)
            if renewal.lease_expires_at <= now:
                raise SandboxRejected("invalid_control_lease")
            if (renewal.lease_expires_at - now).total_seconds() > MAX_LEASE_SECONDS:
                raise SandboxRejected("control_lease_too_long")
            return self._store(
                record.model_copy(update={"lease_expires_at": renewal.lease_expires_at})
            )

    def _expire_leases(self) -> None:
        """Halt every instance whose lease lapsed, on the watchdog's own initiative."""
        now = datetime.now(UTC)
        expired = [
            record
            for record in list(self.instances.values())
            if record.state == "ready"
            and record.lease_expires_at is not None
            and record.lease_expires_at <= now
        ]
        for record in expired:
            try:
                self.halt_instance(
                    HaltRequest(instance_id=record.instance_id, reason="control_lease_expired")
                )
            except SandboxRejected:
                # A stop that cannot be confirmed stays visible as an outstanding instance: the
                # watchdog retries it rather than pretending the call is over.
                continue

    def _await_egress(self, gateway_id: str, authorized: Sequence[Endpoint]) -> bool:
        """Read the gateway's rules back until they are exactly the permit just asked for.

        The wait is bounded by the profile's revocation bound: a permit that is supposed to be
        gone has to be gone within it, and a permit that was supposed to appear has to appear.
        """
        deadline = time.monotonic() + self.profile.limits.revocation_seconds
        while True:
            try:
                rules = self.runtime.read_gateway_policy(gateway_id)
            except (ResourceNotFound, RuntimeUnavailable):
                return False
            if _permits_match(rules, authorized):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(HEALTHCHECK_POLL_SECONDS)

    def _verify_egress_drift(self) -> None:
        """Re-read each running instance's rules and end any execution whose permit changed.

        A gateway that restarted, or a rule that was altered any other way, would otherwise leave
        the ledger asserting a permit the kernel no longer holds. The manager trusts the kernel,
        so a mismatch halts the instance rather than being recorded as a healthy state.
        """
        for record in list(self.instances.values()):
            if record.state != "ready":
                continue
            gateway = self._container_of(record, "gateway")
            try:
                rules = self.runtime.read_gateway_policy(gateway.id)
            except (ResourceNotFound, RuntimeUnavailable):
                continue
            if not _permits_match(rules, _endpoints(record.egress.authorized)):
                # What the gateway really holds is recorded before the execution ends: a halt
                # nobody can explain afterwards is not a reconciled fact.
                self._store(
                    record.model_copy(update={"egress_observation": rules[:2000]})
                )
                try:
                    self.halt_instance(
                        HaltRequest(
                            instance_id=record.instance_id, reason="egress_unverified"
                        )
                    )
                except SandboxRejected:
                    continue

    def _watch(self) -> None:
        verified_at = time.monotonic()
        while not self.closed.wait(LEASE_POLL_SECONDS):
            try:
                # The ledger has one writer at a time: the watchdog takes the same lock every
                # caller does, so a tick can never interleave with a launch or a reclaim.
                with self.lock:
                    self._expire_leases()
                    if time.monotonic() - verified_at >= EGRESS_VERIFY_SECONDS:
                        verified_at = time.monotonic()
                        self._verify_egress_drift()
            except Exception:
                # The watchdog exists to bound a lapsed lease and to catch a permit that changed
                # under it; a failure to do so once is not a reason for the thread to die and
                # stop bounding anything at all. The next tick reads the ledger again.
                continue

    # -- revert -------------------------------------------------------------------------------

    def begin_revert(self, reason: str = "operator_revert") -> RevertReport:
        """Stop opening new executions, then revoke, stop, reclaim and audit what is running.

        This is the execution side's part of the deployment revert in spec 0002 section 3.5: new
        instances are refused from the first moment, in-flight egress is revoked before the
        containers are stopped, and nothing is reported as withdrawn while something is
        unaccounted for — an unconfirmed stop, an ownership conflict, or a resource the ledger
        cannot explain. The management capability itself is only withdrawn by the deployment,
        after this report says it may be. Asking again after a completed revert is a no-op.
        """
        with self.lock:
            self.reverting = reason
            halted: list[UUID] = []
            reclaimed: list[ManagedResource] = []
            outstanding: list[str] = []
            for record in list(self.instances.values()):
                if record.state == "reclaimed":
                    continue
                record = self._revoke_if_permitted(record, "revert")
                if record.state == "ready":
                    try:
                        self.halt_instance(
                            HaltRequest(instance_id=record.instance_id, reason="revert")
                        )
                        halted.append(record.instance_id)
                    except SandboxRejected as refusal:
                        # An unconfirmed stop keeps its resources and keeps management in place.
                        outstanding.append(f"{refusal.reason_code}:{record.instance_id}")
                        continue
                report = self.reclaim_instance(record.instance_id)
                reclaimed.extend(report.removed)
                outstanding.extend(f"{failure}:{record.instance_id}" for failure in report.failed)
            audit = self.audit()
            outstanding.extend(
                f"sandbox_resources_unaccounted:{item.name}" for item in audit.unaccounted
            )
            outstanding.extend(f"sandbox_resource_missing:{item.name}" for item in audit.missing)
            # What was running, what was removed and what could not be accounted for is archived
            # where evidence lives, so a later review reads the reconciliation rather than being
            # told one happened.
            archived: list[str] = []
            relative = f"reverts/{reason}-{str(uuid4())[:8]}.json"
            snapshot = {
                "reason": reason,
                "at": datetime.now(UTC).isoformat(),
                "halted": [str(value) for value in halted],
                "reclaimed": [item.name for item in reclaimed],
                "outstanding": outstanding,
            }
            try:
                destination = self.evidence_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(destination, json.dumps(snapshot, indent=2).encode())
                archived.append(relative)
            except OSError:
                outstanding.append("evidence_storage_failed")
            return RevertReport(
                state="blocked" if outstanding else "complete",
                reason_code="sandbox_revert_blocked" if outstanding else None,
                halted=halted,
                reclaimed=reclaimed,
                archived=archived,
                outstanding=outstanding,
                withdrew=not outstanding,
            )

    def reclaim_instance(self, instance_id: UUID) -> ReclamationReport:
        """Remove every resource this instance owns, selected only by this project's labels."""
        with self.lock:
            record = self._instance(instance_id)
            if record.state in {"creating", "ready"}:
                record = self.stop_instance(instance_id)
            selector = _project_selector(
                **{
                    f"{LABEL_NAMESPACE}.run_id": str(record.run_id),
                    f"{LABEL_NAMESPACE}.session_id": str(record.session_id),
                    f"{LABEL_NAMESPACE}.instance_id": str(record.instance_id),
                }
            )
            removed, failed = self._remove(selector, lambda item: _owned_by(item, record))
            self._store(
                record.model_copy(
                    update={
                        "state": "reclaimed" if not failed else record.state,
                        "reclaimed_at": datetime.now(UTC) if not failed else record.reclaimed_at,
                        "resources": _without_running(record.resources),
                    }
                )
            )
            return ReclamationReport(instance_ids=[instance_id], removed=removed, failed=failed)

    def reclaim_session(self, session_id: UUID) -> ReclamationReport:
        """Reclaim every instance of a session, then sweep the session's labels.

        The sweep is what makes a round end with nothing left: a resource whose ledger write
        never happened is still labeled for this session, and it is removed by label too.
        """
        with self.lock:
            session = self._session(session_id)
            removed: list[ManagedResource] = []
            failed: list[str] = []
            reclaimed: list[UUID] = []
            for record in self._instances_of(session):
                report = self.reclaim_instance(record.instance_id)
                removed.extend(report.removed)
                failed.extend(report.failed)
                reclaimed.append(record.instance_id)
            selector = _project_selector(
                **{
                    f"{LABEL_NAMESPACE}.run_id": str(session.run_id),
                    f"{LABEL_NAMESPACE}.session_id": str(session.session_id),
                }
            )
            extra, extra_failed = self._remove(
                selector,
                lambda item: _owned_by_session(item, session.run_id, session.session_id),
            )
            return ReclamationReport(
                instance_ids=reclaimed,
                removed=[*removed, *extra],
                failed=[*failed, *extra_failed],
            )

    def close(self) -> None:
        """Stop the lease watchdog and release the runtime, in that order."""
        self.closed.set()
        self.watcher.join(timeout=2)
        self.runtime.close()

    # -- internals ----------------------------------------------------------------------------

    def _await_gateway_ready(self, gateway_id: str) -> None:
        """Wait until the gateway itself reports that its rules are installed.

        The gateway's own startup installs the default-deny rules and only then reports ready, so
        this wait is what makes "the rules exist before any tool process does" a fact rather than
        a hope. A gateway that never reports, or reports unhealthy, ends the launch.
        """
        deadline = time.monotonic() + self.profile.limits.readiness_seconds
        while time.monotonic() < deadline:
            try:
                facts = self.runtime.container_facts(gateway_id)
            except ResourceNotFound:
                raise SandboxRejected("sandbox_resource_missing") from None
            if not facts.running or facts.health == "unhealthy":
                raise SandboxRejected("sandbox_gateway_not_ready")
            if facts.health == "healthy":
                return
            time.sleep(HEALTHCHECK_POLL_SECONDS)
        raise SandboxRejected("sandbox_gateway_not_ready")

    def _protected_networks(self, session_network: str | None = None) -> tuple[IPv4Network, ...]:
        """The networks a tool must never reach, whatever authorization it was given.

        Three sources, none of them a request: the profile's declared protected ranges, the
        networks the Runner itself is attached to (the control plane it talks to is exactly what a
        tool must not reach), and the host-side gateway address of each bridge the session uses —
        that address is the host's own interface on the bridge, i.e. a management interface.
        A deployment whose own networks cannot be identified is refused a launch instead of being
        trusted to have protected them some other way.
        """
        networks = [IPv4Network(value) for value in self.profile.protected_networks]
        discovered = [item for item in self.runtime.own_networks() if item.subnet]
        if not discovered:
            raise SandboxRejected("sandbox_platform_networks_unknown")
        networks.extend(IPv4Network(item.subnet) for item in discovered if item.subnet)
        for source in (session_network, self.profile.target_network):
            if source is None:
                continue
            try:
                facts = self.runtime.network_facts(source)
            except ResourceNotFound:
                continue
            if source == session_network and facts.subnet:
                # The session bridge's own subnet is where the host's interface on it lives, and a
                # tool has no business reaching anything there: its targets are elsewhere.
                networks.append(IPv4Network(facts.subnet))
            if facts.gateway:
                networks.append(IPv4Network(f"{facts.gateway}/32"))
        return tuple(networks)

    def _apply_policy(
        self,
        record: InstanceRecord,
        gateway_id: str,
        policy: NetworkPolicy,
        reason: str,
    ) -> InstanceRecord:
        """Install one policy through the profile's fixed command and record when it took effect.

        Only the rule body travels: the program that installs it comes from the profile, and the
        endpoints inside the body were validated against the protected networks when the policy
        was built.
        """
        payload = json.dumps(
            {
                "permissions": [
                    {"address": str(item.address), "port": item.port}
                    for item in policy.permissions
                ],
                "protected": [str(item) for item in policy.protected],
            }
        )
        try:
            self.runtime.apply_gateway_policy(gateway_id, payload)
        except ResourceNotFound:
            raise SandboxRejected("sandbox_resource_missing") from None
        except RuntimeUnavailable:
            raise SandboxRejected("sandbox_gateway_policy_failed") from None
        if not self._await_egress(gateway_id, policy.permissions):
            # Installing rules is an assertion; the rules themselves are the fact. A permit the
            # gateway did not really take (or did not really drop) ends the execution instead of
            # being recorded as applied.
            raise SandboxRejected("sandbox_egress_unverified")
        now = datetime.now(UTC)
        authorized = [
            AuthorizedEndpoint(address=str(item.address), port=item.port)
            for item in policy.permissions
        ]
        revoked = not authorized
        egress = EgressState(
            authorized=authorized,
            applied_at=now,
            revoked_at=now if revoked else record.egress.revoked_at,
            revocation_reason=reason if revoked else record.egress.revocation_reason,
        )
        change = EgressChange(authorized=authorized, at=now, reason=reason)
        return self._store(
            record.model_copy(
                update={
                    "egress": egress,
                    "egress_changes": [*record.egress_changes, change],
                }
            )
        )

    def _manifest(self) -> EnvironmentManifest:
        facts = self.runtime.probe()
        return EnvironmentManifest(
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            image_digests={
                "gateway": self.runtime.resolve_image(self.profile.gateway.image),
                "tool": self.runtime.resolve_image(self.profile.tool.image),
            },
            tool_inventory=list(self.profile.tool_inventory),
            engine=facts.engine,
            architecture=facts.architecture,
            created_at=datetime.now(UTC),
        )

    def _verify_instance(
        self,
        gateway_id: str,
        gateway_spec: ContainerSpec,
        tool_id: str,
        tool_spec: ContainerSpec,
    ) -> None:
        """The instance is only ready once its facts match the profile it was created from.

        Creating a container is not evidence that the profile took effect: the daemon is asked
        what really runs. A tool that came up privileged, writable or outside the gateway's
        namespace is an interruption to clean up, never a ready instance.
        """
        gateway = self.runtime.container_facts(gateway_id)
        tool = self.runtime.container_facts(tool_id)
        matches = (
            gateway.user == gateway_spec.user
            and gateway.capabilities_add == gateway_spec.capabilities_add
            and gateway.capabilities_drop == gateway_spec.capabilities_drop
            and set(gateway_spec.security_options) <= set(gateway.security_options)
            and gateway.read_only_rootfs is gateway_spec.read_only_rootfs
            and gateway.network_mode == gateway_spec.network_name
            and gateway.running
            and tool.user == tool_spec.user
            and tool.capabilities_add == tool_spec.capabilities_add
            and tool.capabilities_drop == tool_spec.capabilities_drop
            and set(tool_spec.security_options) <= set(tool.security_options)
            and tool.read_only_rootfs is tool_spec.read_only_rootfs
            and tool.network_mode == f"container:{gateway_id}"
            and tool.mounts == tool_spec.mounts
            and tool.running
        )
        if not matches:
            raise SandboxRejected("sandbox_profile_not_applied")

    def _record_resource(
        self, record: InstanceRecord, resource: ManagedResource
    ) -> InstanceRecord:
        return self._store(record.model_copy(update={"resources": [*record.resources, resource]}))

    def _stop_containers(self, record: InstanceRecord) -> list[ManagedResource]:
        """Stop in reverse creation order and confirm it, processes included.

        Stopping is not reclaiming: the workspace and the resource records stay.
        """
        containers = [item for item in record.resources if item.kind == "container"]
        for resource in reversed(containers):
            try:
                self.runtime.stop_container(resource.id, CONTAINER_STOP_SECONDS)
                facts = self.runtime.container_facts(resource.id)
                processes = self.runtime.container_processes(resource.id, 64)
            except ResourceNotFound:
                # A container that no longer exists cannot be running.
                continue
            if facts.running:
                # An unconfirmed stop is reported, never softened into a stopped state: whatever
                # the parent Shell returning suggests, the processes may still be running.
                raise SandboxRejected("sandbox_stop_unconfirmed")
            if processes:
                # A stopped container with processes left in it contradicts the stop, and the
                # contradiction is reported rather than averaged away.
                raise SandboxRejected("sandbox_stop_unconfirmed")
        return _without_running(record.resources)

    def _remove(
        self, selector: Mapping[str, str], owned: Callable[[ManagedResource], bool]
    ) -> tuple[list[ManagedResource], list[str]]:
        """Remove labeled resources, containers first, and never one this project does not own."""
        live = {item.id: _as_resource(item) for item in self.runtime.list_resources(selector)}
        removed: list[ManagedResource] = []
        failed: list[str] = []
        for kind in ("container", "network", "volume"):
            for identifier, item in sorted(live.items()):
                if item.kind != kind:
                    continue
                if not owned(item):
                    # Another component's resource is never removed, whatever its labels say.
                    failed.append("ownership_mismatch")
                    continue
                try:
                    if kind == "container":
                        self.runtime.remove_container(identifier, force=True)
                    elif kind == "network":
                        self.runtime.remove_network(identifier)
                    else:
                        self.runtime.remove_volume(identifier)
                except ResourceNotFound:
                    pass
                except Exception as error:
                    failed.append(type(error).__name__)
                    continue
                removed.append(
                    item.model_copy(update={"running": False if kind == "container" else None})
                )
        return removed, failed

    def _instance(self, instance_id: UUID) -> InstanceRecord:
        record = self.instances.get(str(instance_id))
        if record is None:
            raise SandboxRejected("sandbox_instance_unknown")
        return record

    def _session(self, session_id: UUID) -> SessionRecord:
        record = self.sessions.get(str(session_id))
        if record is None:
            raise SandboxRejected("sandbox_session_unknown")
        return record

    def _instances_of(self, session: SessionRecord) -> list[InstanceRecord]:
        return [
            self.instances[str(value)]
            for value in session.instances
            if str(value) in self.instances
        ]

    def _session_network(self, record: InstanceRecord) -> str | None:
        for resource in record.resources:
            if resource.kind == "network" and resource.role == "session":
                return resource.id
        return None

    def _container_of(self, record: InstanceRecord, role: SandboxRole) -> ManagedResource:
        for resource in record.resources:
            if resource.kind == "container" and resource.role == role:
                return resource
        raise SandboxRejected("sandbox_resource_missing")
