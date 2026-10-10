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
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from huntweave.contracts.runs import Contract
from huntweave.execution.durable import atomic_write
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
    "ContainerFacts",
    "ContainerRuntime",
    "ContainerSpec",
    "EnvironmentManifest",
    "EvidenceRef",
    "InstanceRecord",
    "LogRead",
    "LogSlice",
    "ManagedResource",
    "ReclamationReport",
    "ResourceAudit",
    "ResourceNotFound",
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

SandboxRole = Literal["gateway", "tool"]
ResourceRole = Literal["gateway", "tool", "workspace", "session"]
ResourceKind = Literal["container", "network", "volume"]
InstanceState = Literal["creating", "ready", "stopped", "reclaimed", "interrupted"]

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


@dataclass(frozen=True)
class RuntimeFacts:
    engine: str
    architecture: str


@dataclass(frozen=True)
class LogRead:
    """A bounded log read, and whether either bound may have cut the output."""

    text: str
    lines_returned: int
    line_bound: int


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
    remove the per-session gateway, network and workspace volume, read this project's labeled
    containers and processes, read bounded logs, and resolve an image. There is deliberately no
    generic request method, no command execution and no ``**options`` passthrough, so a caller
    cannot ask the daemon for anything this interface does not name.
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

    def container_facts(self, container_id: str) -> ContainerFacts: ...

    def container_processes(self, container_id: str, limit: int) -> tuple[str, ...]: ...

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
    """Create a new execution instance for an existing session. It carries no configuration."""

    session_id: UUID


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
    """Trusted lifecycle owner: creates, observes and reclaims this project's sandbox resources.

    The manager never starts a target action and never installs an egress rule — that is the next
    slice — so a session it creates has no path to any target network.
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
        """
        with self.lock:
            session = self._session(request.session_id)
            for record in self._instances_of(session):
                if record.state == "interrupted":
                    raise SandboxRejected("sandbox_instance_interrupted")
                if record.state in {"creating", "ready"}:
                    raise SandboxRejected("sandbox_instance_active")
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
        resource records and the environment manifest stay, and the workspace is untouched."""
        with self.lock:
            record = self._instance(instance_id)
            if record.state == "reclaimed":
                raise SandboxRejected("sandbox_instance_reclaimed")
            resources = self._stop_containers(record)
            # The confirmation time is read after the stop it certifies, never before.
            stopped = record.model_copy(
                update={
                    "state": "stopped",
                    "stop_confirmed_at": datetime.now(UTC),
                    "resources": resources,
                }
            )
            return self._store(stopped)

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
        self.runtime.close()

    # -- internals ----------------------------------------------------------------------------

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
        """Stop in reverse creation order and confirm it. The workspace volume stays."""
        containers = [item for item in record.resources if item.kind == "container"]
        for resource in reversed(containers):
            try:
                self.runtime.stop_container(resource.id, CONTAINER_STOP_SECONDS)
                facts = self.runtime.container_facts(resource.id)
            except ResourceNotFound:
                # A container that no longer exists cannot be running.
                continue
            if facts.running:
                # An unconfirmed stop is reported, never softened into a stopped state: whatever
                # the parent Shell returning suggests, the processes may still be running.
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

    def _container_of(self, record: InstanceRecord, role: SandboxRole) -> ManagedResource:
        for resource in record.resources:
            if resource.kind == "container" and resource.role == role:
                return resource
        raise SandboxRejected("sandbox_resource_missing")
