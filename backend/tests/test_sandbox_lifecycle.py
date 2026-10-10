"""The trusted sandbox manager's contract: fixed profile, narrow runtime, honest lifecycle.

These checks never start a container. The real lifecycle is verified by
``deploy/verify_lifecycle.py`` against the lab; here the subject is the decisions — which values
reach the runtime, what a request may say, what the ledger proves after a crash, and what a
reclaim is allowed to remove.
"""

import inspect
import json
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from huntweave.config import SandboxSettings
from huntweave.contracts.capabilities import Capabilities
from huntweave.execution.network_policy import NetworkPolicy
from huntweave.execution.sandbox import (
    LABEL_NAMESPACE,
    PROJECT_NAME,
    AuthorizedEndpoint,
    ContainerFacts,
    ContainerRuntime,
    ContainerSpec,
    EgressUpdate,
    HaltRequest,
    LeaseRenewal,
    LogRead,
    NetworkFacts,
    ResourceNotFound,
    RuntimeFacts,
    RuntimeResource,
    RuntimeUnavailable,
    SandboxInstanceRequest,
    SandboxManager,
    SandboxProfile,
    SandboxRejected,
    SandboxSessionRequest,
    VolumeMount,
)
from huntweave.execution.server import create_runner

REPOSITORY = Path(__file__).resolve().parents[2]
PROFILES = REPOSITORY / "profiles"
LIFECYCLE_PROFILE = "sandbox-lifecycle-v1"
EGRESS_PROFILE = "sandbox-egress-v1"

# Every operation the trusted component may reach the runtime with (spec 0002 section 3.1). A
# name added to the protocol without this list changing is a widening of the Docker surface.
FIXED_OPERATIONS = {
    "probe",
    "resolve_image",
    "create_network",
    "remove_network",
    "create_volume",
    "remove_volume",
    "create_container",
    "start_container",
    "stop_container",
    "remove_container",
    "connect_network",
    "network_facts",
    "own_networks",
    "apply_gateway_policy",
    "read_gateway_policy",
    "container_facts",
    "container_processes",
    "container_logs",
    "list_resources",
    "close",
}


class FakeRuntime:
    """An in-memory runtime: it records what the manager asked for, and can fail on purpose."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_on: str | None = None
        self.fail_creates_after: int | None = None
        self.creates = 0
        self.refuse_stop = False
        self.reachable = True
        self.reported_user: str | None = None
        self.stops: list[datetime] = []
        self.log_text = "hold\n"
        self.log_lines_returned = 1
        self.process_lines = ("1 python /lab/fixture.py hold",)
        self.sequence = 0
        # The manager's view of the platform: the runner container is attached to this network,
        # so a launch refuses to proceed unless it is discoverable. The deployment's target
        # network exists before a launch, which is how its host-side address is known in time.
        self.platform = [
            NetworkFacts(id="net-control", name="control", subnet="172.28.0.0/16", internal=True)
        ]
        self.networks = {
            "net-target": {
                "name": "huntweave-lab-egress-targets",
                "labels": {},
                "internal": False,
            }
        }
        self.network_subnets: dict[str, str] = {"net-target": "172.29.0.0/16"}
        self.network_gateways: dict[str, str] = {"huntweave-lab-egress-targets": "172.29.0.1"}
        self.policies: list[str] = []
        self.policy_output = ""
        self.drifted_policy: str | None = None
        self.reject_policy = False
        self.health = "healthy"

    # -- recording ----------------------------------------------------------------------------

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise RuntimeError(f"injected failure in {name}")

    def _identifier(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence:04d}"

    def created_spec(self, role: str) -> ContainerSpec:
        for identifier in self.containers:
            if self._labels_of(identifier).get(f"{LABEL_NAMESPACE}.role") == role:
                spec: ContainerSpec = self.containers[identifier]["spec"]
                return spec
        raise AssertionError(f"no {role} container was created")

    def _labels_of(self, container_id: str) -> dict[str, str]:
        return dict(self.containers[container_id]["labels"])

    # -- the fixed operation set --------------------------------------------------------------

    def probe(self) -> RuntimeFacts:
        self._record("probe")
        if not self.reachable:
            raise RuntimeUnavailable("no socket")
        return RuntimeFacts(engine="29.7.2", architecture="x86_64")

    def resolve_image(self, reference: str) -> str:
        self._record("resolve_image")
        if self.fail_on == "resolve_image":
            raise ResourceNotFound(reference)
        return f"{reference}@sha256:{'a' * 64}"

    def create_network(
        self,
        *,
        name: str,
        labels: Mapping[str, str],
        internal: bool,
        driver: str,
        gateway_mode: str,
    ) -> str:
        self._record("create_network")
        assert internal is True, "a session network that is not internal is not a sandbox"
        assert gateway_mode == "isolated"
        identifier = self._identifier("network")
        self.networks[identifier] = {"name": name, "labels": dict(labels), "driver": driver}
        return identifier

    def remove_network(self, network_id: str) -> None:
        self._record("remove_network")
        if self.networks.pop(network_id, None) is None:
            raise ResourceNotFound(network_id)

    def create_volume(self, *, name: str, labels: Mapping[str, str]) -> str:
        self._record("create_volume")
        identifier = self._identifier("volume")
        self.volumes[identifier] = {"name": name, "labels": dict(labels)}
        return identifier

    def remove_volume(self, volume_id: str) -> None:
        self._record("remove_volume")
        if self.volumes.pop(volume_id, None) is None:
            raise ResourceNotFound(volume_id)

    def create_container(
        self, *, name: str, labels: Mapping[str, str], spec: ContainerSpec
    ) -> str:
        self._record("create_container")
        self.creates += 1
        if self.fail_creates_after is not None and self.creates > self.fail_creates_after:
            raise RuntimeError("injected failure while creating a container")
        identifier = self._identifier("container")
        self.containers[identifier] = {
            "name": name,
            "labels": dict(labels),
            "spec": spec,
            "running": False,
        }
        return identifier

    def start_container(self, container_id: str) -> None:
        self._record("start_container")
        if container_id not in self.containers:
            raise ResourceNotFound(container_id)
        self.containers[container_id]["running"] = True

    def stop_container(self, container_id: str, timeout_seconds: int) -> None:
        self._record("stop_container")
        if container_id not in self.containers:
            raise ResourceNotFound(container_id)
        self.stops.append(datetime.now(UTC))
        self.containers[container_id]["running"] = self.refuse_stop

    def remove_container(self, container_id: str, force: bool) -> None:
        self._record("remove_container")
        if self.containers.pop(container_id, None) is None:
            raise ResourceNotFound(container_id)

    def connect_network(self, container_id: str, network_name: str) -> None:
        self._record("connect_network")
        if container_id not in self.containers:
            raise ResourceNotFound(container_id)
        self.containers[container_id]["target_network"] = network_name

    def network_facts(self, network_id: str) -> NetworkFacts:
        self._record("network_facts")
        entry = self.networks.get(network_id)
        if entry is None:
            # The manager also asks by name for the deployment's target network.
            entry = next(
                (item for item in self.networks.values() if item["name"] == network_id), None
            )
        if entry is None:
            raise ResourceNotFound(network_id)
        return NetworkFacts(
            id=network_id,
            name=entry["name"],
            subnet=self.network_subnets.get(network_id, "172.29.0.0/16"),
            internal=bool(entry.get("internal")),
            gateway=self.network_gateways.get(entry["name"], "172.29.0.1"),
        )

    def own_networks(self) -> tuple[NetworkFacts, ...]:
        self._record("own_networks")
        return tuple(self.platform)

    def apply_gateway_policy(self, gateway_id: str, payload: str) -> None:
        self._record("apply_gateway_policy")
        if gateway_id not in self.containers:
            raise ResourceNotFound(gateway_id)
        if self.reject_policy:
            raise RuntimeUnavailable("gateway policy rejected")
        self.policies.append(payload)
        # A gateway that installed exactly what it was told, as `iptables-save` would print it.
        permissions = json.loads(payload)["permissions"]
        lines = ["*filter", ":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"]
        for item in permissions:
            lines.append(
                f"[0:0] -A OUTPUT -d {item['address']}/32 -p tcp --dport {item['port']} "
                "-m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT"
            )
            lines.append(
                f"[0:0] -A INPUT -s {item['address']}/32 -p tcp --sport {item['port']} "
                "-m conntrack --ctstate ESTABLISHED -j ACCEPT"
            )
        self.policy_output = "\n".join([*lines, "COMMIT", ""])

    def read_gateway_policy(self, gateway_id: str) -> str:
        self._record("read_gateway_policy")
        if gateway_id not in self.containers:
            raise ResourceNotFound(gateway_id)
        return self.drifted_policy or self.policy_output

    def container_facts(self, container_id: str) -> ContainerFacts:
        self._record("container_facts")
        entry = self.containers.get(container_id)
        if entry is None:
            raise ResourceNotFound(container_id)
        spec: ContainerSpec = entry["spec"]
        return ContainerFacts(
            id=container_id,
            name=entry["name"],
            running=bool(entry["running"]),
            labels=dict(entry["labels"]),
            user=self.reported_user or spec.user,
            # Docker reports the network a container was created on as its network mode; a
            # container created inside another's namespace reports that container instead.
            network_mode=spec.network_mode or spec.network_name or "default",
            capabilities_add=spec.capabilities_add,
            capabilities_drop=spec.capabilities_drop,
            security_options=spec.security_options,
            read_only_rootfs=spec.read_only_rootfs,
            mounts=spec.mounts,
            health=self.health,
        )

    def container_processes(self, container_id: str, limit: int) -> tuple[str, ...]:
        self._record("container_processes")
        entry = self.containers.get(container_id)
        if entry is None:
            raise ResourceNotFound(container_id)
        if not entry["running"]:
            # Docker refuses to list processes in a container that is not running.
            return ()
        return self.process_lines[:limit]

    def container_logs(
        self, container_id: str, *, tail_lines: int, tail_bytes: int
    ) -> LogRead:
        self._record("container_logs")
        if container_id not in self.containers:
            raise ResourceNotFound(container_id)
        return LogRead(
            text=self.log_text[-tail_bytes:],
            lines_returned=self.log_lines_returned,
            line_bound=tail_lines,
        )

    def list_resources(self, labels: Mapping[str, str]) -> tuple[RuntimeResource, ...]:
        self._record("list_resources")
        found: list[RuntimeResource] = []
        for kind, store in (
            ("container", self.containers),
            ("network", self.networks),
            ("volume", self.volumes),
        ):
            for identifier, entry in store.items():
                if all(entry["labels"].get(key) == value for key, value in labels.items()):
                    found.append(
                        RuntimeResource(
                            kind=kind,  # type: ignore[arg-type]
                            id=identifier,
                            name=entry["name"],
                            labels=dict(entry["labels"]),
                            running=entry.get("running") if kind == "container" else None,
                        )
                    )
        return tuple(found)

    def close(self) -> None:
        self.calls.append("close")


def profile_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (PROFILES / f"{LIFECYCLE_PROFILE}.json").read_text(encoding="utf-8")
    )
    return document


def written_profile(directory: Path, identifier: str, document: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{identifier}.json").write_text(json.dumps(document), encoding="utf-8")
    return directory


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def profile() -> SandboxProfile:
    return SandboxProfile.load(LIFECYCLE_PROFILE, PROFILES)


@pytest.fixture
def manager(tmp_path: Path, runtime: FakeRuntime, profile: SandboxProfile) -> SandboxManager:
    return SandboxManager(
        runtime=runtime,
        profile=profile,
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )


@pytest.fixture
def egress_manager(tmp_path: Path, runtime: FakeRuntime) -> SandboxManager:
    """The profile that declares a target network: the one egress control is checked against."""
    return SandboxManager(
        runtime=runtime,
        profile=SandboxProfile.load(EGRESS_PROFILE, PROFILES),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )


# --------------------------------------------------------------------------------------------
# The Docker surface stays fixed and narrow
# --------------------------------------------------------------------------------------------


def test_the_runtime_protocol_exposes_exactly_the_fixed_operations() -> None:
    declared = {
        name
        for name, value in vars(ContainerRuntime).items()
        if callable(value) and not name.startswith("_")
    }
    assert declared == FIXED_OPERATIONS


def test_no_fixed_operation_accepts_arbitrary_runtime_options() -> None:
    for name in FIXED_OPERATIONS:
        signature = inspect.signature(getattr(ContainerRuntime, name))
        assert not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ), f"{name} accepts **kwargs, which would be a generic Docker passthrough"


def test_a_request_cannot_carry_its_own_container_configuration() -> None:
    session = {
        "run_id": str(uuid4()),
        "agent_session_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "scope_version": 1,
        "policy_version": 1,
    }
    with pytest.raises(ValidationError):
        SandboxSessionRequest.model_validate({**session, "profile": "other"})
    for smuggled in ("image", "mounts", "network_mode", "cap_add", "labels", "privileged"):
        with pytest.raises(ValidationError):
            SandboxSessionRequest.model_validate({**session, smuggled: "anything"})
        with pytest.raises(ValidationError):
            SandboxInstanceRequest.model_validate(
                {"session_id": str(uuid4()), smuggled: "anything"}
            )


def test_a_session_belongs_to_the_authorization_it_was_opened_under(
    manager: SandboxManager,
) -> None:
    request = _session_request()
    opened = manager.open_session(request)
    # The same request — the same run, model session, scope version and policy — reuses it.
    assert manager.open_session(request).session_id == opened.session_id

    # A re-authorized scope or a new policy is a different session, never an adopted one.
    with pytest.raises(SandboxRejected) as raised:
        manager.open_session(request.model_copy(update={"scope_version": 2}))
    assert raised.value.reason_code == "sandbox_authorization_mismatch"
    with pytest.raises(SandboxRejected) as raised:
        manager.open_session(request.model_copy(update={"scope_id": uuid4()}))
    assert raised.value.reason_code == "sandbox_authorization_mismatch"

    instance = manager.launch_instance(SandboxInstanceRequest(session_id=opened.session_id))
    assert instance.scope_id == opened.scope_id
    assert instance.scope_version == opened.scope_version
    assert instance.policy_version == opened.policy_version
    assert instance.environment.tool_inventory == []


def test_an_instance_whose_facts_do_not_match_the_profile_is_interrupted(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    # The daemon reports a container that came up as root even though the profile says otherwise:
    # creating a container is not evidence that the profile took effect.
    runtime.reported_user = "0:0"
    session = manager.open_session(_session_request())
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_profile_not_applied"
    assert [
        record.state
        for record in manager.instances.values()
        if record.session_id == session.session_id
    ] == ["interrupted"]
    # The containers it did create stay named, so they can be reclaimed instead of leaking.
    assert manager.reclaim_session(session.session_id).complete
    assert manager.resources(session_id=session.session_id) == []


# --------------------------------------------------------------------------------------------
# The fixed profile decides every container option
# --------------------------------------------------------------------------------------------


def test_the_shipped_lifecycle_profile_loads_and_keeps_the_sandbox_closed() -> None:
    loaded = SandboxProfile.load(LIFECYCLE_PROFILE, PROFILES)
    assert loaded.profile_version == 1
    assert loaded.network_internal is True
    assert loaded.tool.user.split(":")[0] != "0"
    assert loaded.tool.capabilities_add == ()
    assert loaded.limits.read_only_rootfs and loaded.limits.no_new_privileges
    assert loaded.limits.capabilities_drop == ("ALL",)


def test_an_unknown_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxRejected) as raised:
        SandboxProfile.load("sandbox-absent-v1", tmp_path)
    assert raised.value.reason_code == "sandbox_profile_unknown"


@pytest.mark.parametrize(
    ("change", "reason_code"),
    [
        (lambda document: document.update(unknown="value"), "sandbox_profile_invalid"),
        (
            lambda document: document["tool"].update(user="0:0"),
            "sandbox_profile_privileged_tool",
        ),
        (
            lambda document: document["tool"].update(capabilities_add=["NET_RAW"]),
            "sandbox_profile_privileged_tool",
        ),
        (
            lambda document: document["gateway"].update(capabilities_add=["SYS_ADMIN"]),
            "sandbox_profile_invalid",
        ),
        (
            lambda document: document["network"].update(internal=False),
            "sandbox_profile_not_isolated",
        ),
        (lambda document: document["network"].update(ipv6=True), "sandbox_profile_not_isolated"),
        (
            lambda document: document["network"].update(dns=["8.8.8.8"]),
            "sandbox_profile_not_isolated",
        ),
        (
            lambda document: document["tool"].update(image="huntweave/tool:latest"),
            "sandbox_profile_image_not_pinned",
        ),
        (
            lambda document: document["limits"].update(read_only_rootfs=False),
            "sandbox_profile_invalid",
        ),
        (
            lambda document: document["limits"].update(capabilities_drop=[]),
            "sandbox_profile_invalid",
        ),
    ],
)
def test_a_profile_that_widens_the_sandbox_is_refused(
    tmp_path: Path, change: Any, reason_code: str
) -> None:
    document = profile_document()
    change(document)
    directory = written_profile(tmp_path, LIFECYCLE_PROFILE, document)
    with pytest.raises(SandboxRejected) as raised:
        SandboxProfile.load(LIFECYCLE_PROFILE, directory)
    assert raised.value.reason_code == reason_code


def test_an_unsupported_profile_version_is_refused(tmp_path: Path) -> None:
    document = profile_document()
    document["profile_version"] = 99
    directory = written_profile(tmp_path, LIFECYCLE_PROFILE, document)
    with pytest.raises(SandboxRejected) as raised:
        SandboxProfile.load(LIFECYCLE_PROFILE, directory)
    assert raised.value.reason_code == "sandbox_profile_unsupported_version"


def test_every_container_option_comes_from_the_profile(
    manager: SandboxManager, runtime: FakeRuntime, profile: SandboxProfile
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))

    tool = runtime.created_spec("tool")
    assert tool.image == profile.tool.image
    assert tool.user == profile.tool.user
    assert tool.command == profile.tool.command
    assert tool.capabilities_add == ()
    assert tool.capabilities_drop == ("ALL",)
    assert tool.security_options == ("no-new-privileges:true",)
    assert tool.read_only_rootfs is True
    assert tool.tmpfs == profile.limits.tmpfs
    assert tool.pids_limit == profile.limits.pids
    assert tool.memory_limit == profile.limits.memory
    assert tool.cpu_limit == profile.limits.cpus
    assert tool.network_name is None
    gateway = next(
        item for item in instance.resources if item.role == "gateway"
    )
    assert tool.network_mode == f"container:{gateway.id}"
    workspace = next(item for item in instance.resources if item.kind == "volume")
    assert tool.mounts == (
        VolumeMount(volume=workspace.id, target=profile.workspace_mount, read_only=False),
    )

    gateway_spec = runtime.created_spec("gateway")
    assert gateway_spec.capabilities_add == ("NET_ADMIN",)
    assert gateway_spec.mounts == ()
    network = next(item for item in instance.resources if item.kind == "network")
    assert gateway_spec.network_name == network.id
    assert gateway_spec.network_mode is None


def test_the_gateway_is_started_before_the_tool_joins_its_namespace(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    order = [
        call
        for call in runtime.calls
        if call in {"create_network", "create_container", "start_container"}
    ]
    assert order == [
        "create_network",
        "create_container",
        "start_container",
        "create_container",
        "start_container",
    ]


def test_another_sessions_container_is_not_reachable_through_this_one(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    first = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=first.session_id))
    second = manager.open_session(_session_request())
    assert manager.resources(session_id=second.session_id) == []
    assert len(manager.resources(session_id=first.session_id)) == 4
    assert manager.resources(run_id=instance.run_id) == manager.resources(
        session_id=first.session_id
    )


# --------------------------------------------------------------------------------------------
# Identities, crash recovery and stop confirmation
# --------------------------------------------------------------------------------------------


def test_a_rebuild_is_a_new_instance_that_keeps_the_old_one(
    manager: SandboxManager,
) -> None:
    session = manager.open_session(_session_request())
    first = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_instance_active"

    manager.stop_instance(first.instance_id)
    manager.reclaim_instance(first.instance_id)
    second = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert second.instance_id != first.instance_id
    kept = manager.instances[str(first.instance_id)]
    assert kept.instance_id == first.instance_id
    assert kept.environment == first.environment
    assert kept.state == "reclaimed"
    assert kept.reclaimed_at is not None


def test_a_creation_that_fails_mid_way_is_recorded_and_blocks_the_next_launch(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    # The gateway is created, the tool container is not: a crash halfway through a launch.
    runtime.fail_creates_after = 1
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_instance_creation_failed"

    interrupted = [
        record for record in manager.instances.values() if record.session_id == session.session_id
    ]
    assert [record.state for record in interrupted] == ["interrupted"]
    # The resources it did create are recorded, so the leftovers have names.
    assert [item.kind for item in interrupted[0].resources] == ["volume", "network", "container"]

    # The interrupted instance is not cleared by asking again, and reclaim is what clears it.
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_instance_interrupted"
    runtime.fail_creates_after = None
    report = manager.reclaim_instance(interrupted[0].instance_id)
    assert report.complete
    assert manager.resources(session_id=session.session_id) == []
    assert manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id)
    ).state == "ready"


def test_a_ledger_left_creating_by_a_crash_becomes_interrupted(
    tmp_path: Path, runtime: FakeRuntime, profile: SandboxProfile
) -> None:
    state_dir = tmp_path / "state"
    first = SandboxManager(
        runtime=runtime, profile=profile, state_dir=state_dir, evidence_dir=tmp_path / "evidence"
    )
    session = first.open_session(_session_request())
    instance = first.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    # What a process that died between "write the record" and "finish the launch" leaves behind.
    first.instances[str(instance.instance_id)] = instance.model_copy(update={"state": "creating"})
    first._persist()

    reopened = SandboxManager(
        runtime=runtime, profile=profile, state_dir=state_dir, evidence_dir=tmp_path / "evidence"
    )
    assert reopened.instances[str(instance.instance_id)].state == "interrupted"
    # The new process refuses to build on the old instance it cannot account for, and reclaiming
    # the old one is what makes a fresh instance possible.
    with pytest.raises(SandboxRejected) as raised:
        reopened.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_instance_interrupted"
    assert reopened.reclaim_session(session.session_id).complete
    assert reopened.resources(session_id=session.session_id) == []
    assert reopened.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id)
    ).instance_id != instance.instance_id


def test_a_labeled_resource_the_ledger_never_saw_blocks_a_new_instance(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    # What a crashed launch leaves behind: a resource named and labeled exactly as this project
    # names them, but with no ledger entry that could account for it.
    orphan_instance = uuid4()
    leftover = runtime.create_container(
        name=(
            f"huntweave-{str(session.run_id)[:8]}-{str(session.session_id)[:8]}-"
            f"{str(orphan_instance)[:8]}-tool"
        ),
        labels={
            f"{LABEL_NAMESPACE}.project": PROJECT_NAME,
            f"{LABEL_NAMESPACE}.run_id": str(session.run_id),
            f"{LABEL_NAMESPACE}.session_id": str(session.session_id),
            f"{LABEL_NAMESPACE}.instance_id": str(orphan_instance),
        },
        spec=_dummy_spec(),
    )
    audit = manager.audit()
    assert [item.id for item in audit.unaccounted] == [leftover]
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_resources_unaccounted"
    # A session-level reclaim sweeps by label, so the round can end with nothing left.
    assert manager.reclaim_session(session.session_id).complete
    assert manager.resources() == []


def test_an_unconfirmed_stop_is_refused_rather_than_reported_as_stopped(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    runtime.refuse_stop = True
    with pytest.raises(SandboxRejected) as raised:
        manager.stop_instance(instance.instance_id)
    assert raised.value.reason_code == "sandbox_stop_unconfirmed"
    assert manager.instances[str(instance.instance_id)].state == "ready"

    runtime.refuse_stop = False
    stopped = manager.stop_instance(instance.instance_id)
    assert stopped.state == "stopped"
    assert stopped.stop_confirmed_at is not None
    # The confirmation is timestamped after the stop it certifies, not before it.
    assert runtime.stops and stopped.stop_confirmed_at >= runtime.stops[-1]
    assert all(item.running is False for item in stopped.resources if item.kind == "container")
    assert manager.audit().missing == []


def test_unknown_instances_are_refused_by_every_entry_point(manager: SandboxManager) -> None:
    absent = uuid4()
    for call in (
        lambda: manager.stop_instance(absent),
        lambda: manager.reclaim_instance(absent),
        lambda: manager.processes(absent),
        lambda: manager.logs(absent, "tool"),
        lambda: manager.archive_evidence(
            run_id=uuid4(), instance_id=absent, name="report.txt", payload=b"x"
        ),
    ):
        with pytest.raises(SandboxRejected) as raised:
            call()
        assert raised.value.reason_code == "sandbox_instance_unknown"


# --------------------------------------------------------------------------------------------
# Reclaim touches this project's resources only
# --------------------------------------------------------------------------------------------


def test_reclaim_removes_every_owned_resource_and_leaves_nothing_behind(
    manager: SandboxManager,
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    report = manager.reclaim_instance(instance.instance_id)
    assert report.complete
    assert sorted(item.kind for item in report.removed) == [
        "container",
        "container",
        "network",
        "volume",
    ]
    assert manager.resources(session_id=session.session_id) == []
    reclaimed = manager.instances[str(instance.instance_id)]
    assert reclaimed.state == "reclaimed"
    assert reclaimed.environment.profile_id == LIFECYCLE_PROFILE


def test_reclaim_refuses_a_resource_this_project_does_not_own(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    foreign = runtime.create_container(
        name="someone-elses-container",
        labels={f"{LABEL_NAMESPACE}.project": "another-product"},
        spec=_dummy_spec(),
    )

    report = manager.reclaim_instance(instance.instance_id)
    assert report.complete
    assert foreign in runtime.containers
    assert manager.resources(session_id=session.session_id) == []


def test_reclaim_reports_ownership_conflicts_instead_of_forcing_them(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    # A resource that carries this instance's labels but not the name its identities imply.
    impostor = runtime.create_container(
        name="huntweave-00000000-00000000-00000000-tool",
        labels={
            f"{LABEL_NAMESPACE}.project": PROJECT_NAME,
            f"{LABEL_NAMESPACE}.run_id": str(instance.run_id),
            f"{LABEL_NAMESPACE}.session_id": str(instance.session_id),
            f"{LABEL_NAMESPACE}.instance_id": str(instance.instance_id),
        },
        spec=_dummy_spec(),
    )
    report = manager.reclaim_instance(instance.instance_id)
    assert report.complete is False
    assert "ownership_mismatch" in report.failed
    assert impostor in runtime.containers
    # A reclaim that could not finish leaves the instance unreclaimed rather than claiming it.
    assert manager.instances[str(instance.instance_id)].state == "stopped"


def test_a_session_reclaim_ends_the_round_with_no_labeled_resources(
    manager: SandboxManager,
) -> None:
    session = manager.open_session(_session_request())
    manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    manager.reclaim_session(session.session_id)
    assert manager.resources(run_id=session.run_id) == []
    assert manager.audit().unaccounted == []


# --------------------------------------------------------------------------------------------
# Observation, logs, processes and evidence
# --------------------------------------------------------------------------------------------


def test_logs_are_bounded_by_the_profile(manager: SandboxManager, runtime: FakeRuntime) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    runtime.log_text = "x" * 5000
    bounded = manager.logs(instance.instance_id, "tool", tail_bytes=1000)
    assert bounded.role == "tool"
    assert len(bounded.text) == 1000
    assert bounded.truncated is True

    runtime.log_text = "short\n"
    assert manager.logs(instance.instance_id, "tool").truncated is False
    assert manager.logs(instance.instance_id, "gateway", tail_bytes=10).role == "gateway"
    # The daemon keeps only the last lines of a chatty container: that cut is reported too, even
    # though the text that came back fits inside the byte bound.
    runtime.log_lines_returned = manager.profile.limits.log_line_bound
    assert manager.logs(instance.instance_id, "tool").truncated is True
    runtime.log_lines_returned = 1
    # A reclaimed instance has no containers left to read from, and says so rather than
    # returning an empty log as if the container had simply been quiet.
    manager.reclaim_instance(instance.instance_id)
    with pytest.raises(SandboxRejected) as raised:
        manager.logs(instance.instance_id, "tool")
    assert raised.value.reason_code == "sandbox_resource_missing"


def test_processes_come_from_the_tool_container(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert manager.processes(instance.instance_id) == list(runtime.process_lines)
    assert manager.processes(instance.instance_id, limit=0) == []


def test_evidence_is_archived_bounded_and_bound_to_its_instance(
    manager: SandboxManager, tmp_path: Path
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    with pytest.raises(SandboxRejected) as raised:
        manager.archive_evidence(
            run_id=uuid4(), instance_id=instance.instance_id, name="report.txt", payload=b"x"
        )
    assert raised.value.reason_code == "sandbox_instance_mismatch"

    for name in ("../escape.txt", "nested/report.txt", "", "x" * 65):
        with pytest.raises(SandboxRejected) as raised:
            manager.archive_evidence(
                run_id=instance.run_id,
                instance_id=instance.instance_id,
                name=name,
                payload=b"x",
            )
        assert raised.value.reason_code == "evidence_name_rejected"

    with pytest.raises(SandboxRejected) as raised:
        manager.archive_evidence(
            run_id=instance.run_id,
            instance_id=instance.instance_id,
            name="big.txt",
            payload=b"x" * (manager.profile.limits.evidence_max_bytes + 1),
        )
    assert raised.value.reason_code == "evidence_too_large"

    reference = manager.archive_evidence(
        run_id=instance.run_id,
        instance_id=instance.instance_id,
        name="report.json",
        payload=b'{"observed": true}',
    )
    assert reference.relative_path == f"{instance.run_id}/{instance.instance_id}/report.json"
    assert reference.size_bytes == len(b'{"observed": true}')
    assert (tmp_path / "evidence" / reference.relative_path).read_bytes() == b'{"observed": true}'
    assert (tmp_path / "evidence" / reference.relative_path).parent.parent == (
        tmp_path / "evidence" / str(instance.run_id)
    )


def test_observation_reports_an_unreachable_runtime(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    directory = written_profile(tmp_path, LIFECYCLE_PROFILE, profile_document())
    manager = SandboxManager(
        runtime=runtime,
        profile=SandboxProfile.load(LIFECYCLE_PROFILE, directory),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    assert manager.observe().available is True
    runtime.reachable = False
    unavailable = manager.observe()
    assert unavailable.available is False
    assert unavailable.reason_code == "sandbox_runtime_unreachable"
    assert unavailable.observed_at is not None


# --------------------------------------------------------------------------------------------
# The Runner's deployment gate
# --------------------------------------------------------------------------------------------


def test_management_is_disabled_unless_the_deployment_says_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("", "true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("HUNTWEAVE_SANDBOX_MANAGEMENT", value)
        assert SandboxSettings.from_env().enabled is False
    monkeypatch.setenv("HUNTWEAVE_SANDBOX_MANAGEMENT", "enabled")
    assert SandboxSettings.from_env().enabled is True


def test_a_default_runner_reports_no_container_management(tmp_path: Path) -> None:
    client = TestClient(
        create_runner(
            token="b" * 64,
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
            sandbox_settings=SandboxSettings(
                enabled=False, state_dir=tmp_path / "state", evidence_dir=tmp_path / "evidence"
            ),
        )
    )
    body = client.get("/v1/capabilities", headers={"Authorization": "Bearer " + "b" * 64}).json()
    assert body["sandbox_management"] == "disabled"
    assert body["sandbox_reason_code"] is None


def test_enabled_management_is_reported_with_its_own_reason(tmp_path: Path) -> None:
    settings = SandboxSettings(
        enabled=True,
        profile_id=LIFECYCLE_PROFILE,
        profile_dir=PROFILES,
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    headers = {"Authorization": "Bearer " + "b" * 64}

    def ready(_: SandboxSettings) -> SandboxManager:
        return SandboxManager(
            runtime=FakeRuntime(),
            profile=SandboxProfile.load(LIFECYCLE_PROFILE, PROFILES),
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
        )

    ready_client = TestClient(
        create_runner(
            token="b" * 64,
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
            sandbox_settings=settings,
            sandbox_factory=ready,
        )
    )
    body = ready_client.get("/v1/capabilities", headers=headers).json()
    assert body["sandbox_management"] == "ready"
    assert body["sandbox_reason_code"] is None
    # Enabling the trusted manager does not open real execution: the gates decide that, and
    # profile revalidation stays unmet until the slices that cover it are delivered.
    assert body["real_execution_ready"] is False
    assert body["mode"] == "demonstration"
    profile_gate = next(gate for gate in body["gates"] if gate["gate"] == "profile_revalidation")
    assert profile_gate["ready"] is False
    assert profile_gate["reason_code"] == "profile_unvalidated"

    def unknown_profile(_: SandboxSettings) -> SandboxManager:
        raise SandboxRejected("sandbox_profile_unknown")

    broken_client = TestClient(
        create_runner(
            token="b" * 64,
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
            sandbox_settings=settings,
            sandbox_factory=unknown_profile,
        )
    )
    body = broken_client.get("/v1/capabilities", headers=headers).json()
    assert body["sandbox_management"] == "unavailable"
    assert body["sandbox_reason_code"] == "sandbox_profile_unknown"
    assert body["real_execution_ready"] is False


def test_capabilities_stay_parseable_without_the_new_fields() -> None:
    # The app and the Runner share the control image, but a contract that could not express the
    # default would make an older answer unreadable; `disabled` is what an absent field means.
    state = Capabilities.model_validate({"protocol_version": "1"})
    assert state.sandbox_management == "disabled"
    assert state.sandbox_reason_code is None


def test_the_sandbox_manager_serializes_its_ledger(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    ledger = tmp_path / "state" / "sandboxes.json"
    manager = SandboxManager(
        runtime=runtime,
        profile=SandboxProfile.load(LIFECYCLE_PROFILE, PROFILES),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    session = manager.open_session(_session_request())
    manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    document = json.loads(ledger.read_text(encoding="utf-8"))
    assert set(document) == {"sessions", "instances"}
    assert str(session.session_id) in document["sessions"]
    assert len(document["instances"]) == 1


def test_concurrent_session_open_returns_one_session(tmp_path: Path) -> None:
    manager = SandboxManager(
        runtime=FakeRuntime(),
        profile=SandboxProfile.load(LIFECYCLE_PROFILE, PROFILES),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    request = _session_request()
    seen: list[UUID] = []

    def open_once() -> None:
        seen.append(manager.open_session(request).session_id)

    threads = [threading.Thread(target=open_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(seen)) == 1


# --------------------------------------------------------------------------------------------
# Egress: rules before the process, scope, revocation
# --------------------------------------------------------------------------------------------


def endpoint(address: str, port: int = 7000) -> AuthorizedEndpoint:
    return AuthorizedEndpoint(address=address, port=port)


def test_the_rules_exist_before_the_tool_process_does(
    egress_manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = egress_manager.open_session(_session_request())
    egress_manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    calls = runtime.calls
    gateway_created = calls.index("create_container")
    attached = calls.index("connect_network")
    gateway_started = calls.index("start_container")
    policy_applied = calls.index("apply_gateway_policy")
    tool_created = calls.index("create_container", gateway_created + 1)
    # Attach, start, wait for the gateway to report ready, apply the authorization — and only
    # then may a tool container exist whose process could send anything.
    assert gateway_created < attached < gateway_started
    assert gateway_started < policy_applied < tool_created
    assert "container_facts" in calls[gateway_started:policy_applied]


def test_a_gateway_that_never_reports_ready_ends_the_launch(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    runtime.health = "unhealthy"
    session = manager.open_session(_session_request())
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_gateway_not_ready"
    assert runtime.calls.count("create_container") == 1, "the tool container was never created"
    runtime.health = "healthy"
    assert manager.reclaim_session(session.session_id).complete


def test_the_policy_carries_the_authorization_and_the_platform_protection(
    egress_manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = egress_manager.open_session(_session_request())
    egress_manager.launch_instance(
        SandboxInstanceRequest(
            session_id=session.session_id,
            authorized=[endpoint("10.20.0.5"), endpoint("10.20.0.6", 443)],
        )
    )
    payload = json.loads(runtime.policies[-1])
    assert payload["permissions"] == [
        {"address": "10.20.0.5", "port": 7000},
        {"address": "10.20.0.6", "port": 443},
    ]
    # The platform's own network is protected, and so is the host's address on each bridge the
    # session uses — that address is the host's own interface, not a target.
    protected = payload["protected"]
    assert "172.28.0.0/16" in protected
    assert "172.29.0.1/32" in protected
    rules = NetworkPolicy(
        (), tuple(IPv4Network(item) for item in protected)
    ).gateway_rules()
    for network in ("169.254.0.0/16", "127.0.0.0/8", "0.0.0.0/8"):
        assert f"-A OUTPUT -d {network} -j DROP" in rules


@pytest.mark.parametrize(
    ("authorized", "reason_code"),
    [
        ([endpoint("172.28.0.9")], "scope_denied"),  # inside the platform's own control network
        ([endpoint("172.29.0.1")], "scope_denied"),  # the host's own address on the target bridge
        ([endpoint("169.254.169.254")], "scope_denied"),  # the metadata address
        ([endpoint("127.0.0.1")], "scope_denied"),  # loopback
        ([endpoint("::1")], "endpoint_not_expressible"),  # IPv6 is not a scope this profile holds
        ([endpoint("example.invalid")], "endpoint_not_expressible"),  # a name is not an endpoint
    ],
)
def test_an_authorization_that_names_infrastructure_is_refused(
    egress_manager: SandboxManager,
    runtime: FakeRuntime,
    authorized: list[AuthorizedEndpoint],
    reason_code: str,
) -> None:
    session = egress_manager.open_session(_session_request())
    with pytest.raises(SandboxRejected) as raised:
        egress_manager.launch_instance(
            SandboxInstanceRequest(session_id=session.session_id, authorized=authorized)
        )
    assert raised.value.reason_code == reason_code
    # Refused before anything exists: an authorization naming infrastructure never becomes a
    # container that has to be cleaned up afterwards.
    assert runtime.containers == {}


def test_a_platform_whose_networks_are_unknown_is_refused_a_launch(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    runtime.platform = []
    session = manager.open_session(_session_request())
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_platform_networks_unknown"
    assert runtime.containers == {}


def test_a_narrower_scope_replaces_the_previous_one(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(
            session_id=session.session_id, authorized=[endpoint("10.20.0.5"), endpoint("10.20.0.7")]
        )
    )
    shrunk = manager.authorize_egress(
        EgressUpdate(
            instance_id=instance.instance_id,
            authorized=[endpoint("10.20.0.7")],
            reason="scope_shrunk",
        )
    )
    assert json.loads(runtime.policies[-1])["permissions"] == [
        {"address": "10.20.0.7", "port": 7000}
    ]
    assert [item.address for item in shrunk.egress.authorized] == ["10.20.0.7"]
    assert shrunk.egress.applied_at is not None
    assert shrunk.egress.revoked_at is None
    # Both changes are kept, in order, with the reason each one happened.
    assert [change.reason for change in shrunk.egress_changes] == ["initial", "scope_shrunk"]
    assert shrunk.egress_changes[0].at <= shrunk.egress_changes[1].at


def test_revocation_closes_egress_without_stopping_the_containers(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    revoked = manager.revoke_egress(instance.instance_id, "scope_revoked")
    assert json.loads(runtime.policies[-1])["permissions"] == []
    assert revoked.egress.authorized == []
    assert revoked.egress.revoked_at is not None
    assert revoked.egress.revocation_reason == "scope_revoked"
    # The containers are still there: revocation is not a stop, and the stop is its own fact.
    assert revoked.state == "ready"
    running = [
        runtime.containers[item.id]["running"]
        for item in revoked.resources
        if item.kind == "container"
    ]
    assert all(running)


def test_halting_revokes_before_it_stops(manager: SandboxManager, runtime: FakeRuntime) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    before = len(runtime.calls)
    halted = manager.halt_instance(
        HaltRequest(instance_id=instance.instance_id, reason="operator_cancelled")
    )
    assert halted.state == "stopped"
    assert halted.halt_reason == "operator_cancelled"
    sequence = [
        call
        for call in runtime.calls[before:]
        if call in {"apply_gateway_policy", "stop_container"}
    ]
    assert sequence == ["apply_gateway_policy", "stop_container", "stop_container"]
    assert json.loads(runtime.policies[-1])["permissions"] == []


# --------------------------------------------------------------------------------------------
# Control lease
# --------------------------------------------------------------------------------------------


def test_a_lease_beyond_the_documented_bound_is_refused(
    manager: SandboxManager,
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    now = datetime.now(UTC)
    with pytest.raises(SandboxRejected) as raised:
        manager.renew_instance_lease(
            LeaseRenewal(
                instance_id=instance.instance_id,
                lease_expires_at=now + timedelta(seconds=30),
            )
        )
    assert raised.value.reason_code == "control_lease_too_long"
    with pytest.raises(SandboxRejected) as raised:
        manager.renew_instance_lease(
            LeaseRenewal(
                instance_id=instance.instance_id,
                lease_expires_at=now - timedelta(seconds=1),
            )
        )
    assert raised.value.reason_code == "invalid_control_lease"
    renewed = manager.renew_instance_lease(
        LeaseRenewal(instance_id=instance.instance_id, lease_expires_at=now + timedelta(seconds=10))
    )
    assert renewed.lease_expires_at is not None


def test_a_lapsed_lease_is_halted_by_the_manager_itself(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    manager.renew_instance_lease(
        LeaseRenewal(
            instance_id=instance.instance_id,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
        )
    )
    # The control plane stops answering: nobody renews, and the manager's own watchdog ends it.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if manager.instances[str(instance.instance_id)].state == "stopped":
            break
        time.sleep(0.2)
    halted = manager.instances[str(instance.instance_id)]
    assert halted.state == "stopped"
    assert halted.halt_reason == "control_lease_expired"
    assert json.loads(runtime.policies[-1])["permissions"] == []
    assert all(
        runtime.containers[item.id]["running"] is False
        for item in halted.resources
        if item.kind == "container"
    )


# --------------------------------------------------------------------------------------------
# Revert
# --------------------------------------------------------------------------------------------


def test_a_revert_revokes_stops_reclaims_and_then_allows_withdrawal(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    report = manager.begin_revert()
    assert report.state == "complete"
    assert report.withdrew is True
    assert report.halted == [instance.instance_id]
    assert sorted(item.kind for item in report.reclaimed) == [
        "container",
        "container",
        "network",
        "volume",
    ]
    assert manager.instances[str(instance.instance_id)].state == "reclaimed"
    assert manager.resources() == []
    # Asking again is a no-op, not a second teardown.
    again = manager.begin_revert()
    assert again.state == "complete" and again.halted == []
    # New executions are refused from the moment the revert began, and stay refused.
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    assert raised.value.reason_code == "sandbox_reverting"


def test_a_revert_that_cannot_confirm_a_stop_keeps_management_in_place(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    runtime.refuse_stop = True
    report = manager.begin_revert()
    assert report.state == "blocked"
    assert report.withdrew is False
    assert report.reason_code == "sandbox_revert_blocked"
    assert any(item.startswith("sandbox_stop_unconfirmed") for item in report.outstanding)
    # The containers are not removed while the stop is unconfirmed, and the instance is still
    # there for the operator to reconcile.
    assert manager.resources(session_id=session.session_id) != []
    runtime.refuse_stop = False
    resolved = manager.begin_revert()
    assert resolved.state == "complete" and resolved.withdrew is True
    assert manager.instances[str(instance.instance_id)].state == "reclaimed"


def test_a_policy_the_gateway_did_not_really_take_ends_the_launch(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    runtime.reject_policy = True
    session = manager.open_session(_session_request())
    with pytest.raises(SandboxRejected) as raised:
        manager.launch_instance(
            SandboxInstanceRequest(
                session_id=session.session_id, authorized=[endpoint("10.20.0.5")]
            )
        )
    assert raised.value.reason_code == "sandbox_gateway_policy_failed"
    assert runtime.calls.count("create_container") == 1, "no tool container was created"
    runtime.reject_policy = False
    assert manager.reclaim_session(session.session_id).complete


def test_a_permit_the_gateway_no_longer_holds_is_not_renewed(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    # The kernel's rules changed under the manager: the ledger's claim is no longer true, so
    # neither a renewal nor a running execution may be assumed.
    runtime.drifted_policy = (
        "*filter\n:INPUT DROP [0:0]\n:FORWARD DROP [0:0]\n:OUTPUT DROP [0:0]\n"
        "[0:0] -A OUTPUT -d 10.20.0.9/32 -p tcp --dport 7000 -j ACCEPT\n"
        "[0:0] -A INPUT -s 10.20.0.9/32 -p tcp --sport 7000 -j ACCEPT\nCOMMIT\n"
    )
    with pytest.raises(SandboxRejected) as raised:
        manager.renew_instance_lease(
            LeaseRenewal(
                instance_id=instance.instance_id,
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=10),
            )
        )
    assert raised.value.reason_code == "sandbox_egress_unverified"
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if manager.instances[str(instance.instance_id)].state == "stopped":
            break
        time.sleep(0.3)
    drifted = manager.instances[str(instance.instance_id)]
    assert drifted.state == "stopped"
    assert drifted.halt_reason == "egress_unverified"
    assert drifted.egress.authorized == []


def test_a_stop_withdraws_the_permit_and_the_lease(
    manager: SandboxManager,
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    manager.renew_instance_lease(
        LeaseRenewal(
            instance_id=instance.instance_id,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=10),
        )
    )
    stopped = manager.stop_instance(instance.instance_id)
    assert stopped.egress.authorized == []
    assert stopped.egress.revoked_at is not None
    assert stopped.lease_expires_at is None, "a stopped instance holds no live claim"


def test_a_revert_archives_what_it_reconciled(
    manager: SandboxManager, tmp_path: Path
) -> None:
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    report = manager.begin_revert()
    assert report.state == "complete"
    assert len(report.archived) == 1
    archived = json.loads((tmp_path / "evidence" / report.archived[0]).read_text(encoding="utf-8"))
    assert archived["reason"] == "operator_revert"
    assert archived["halted"] == [str(instance.instance_id)]
    assert archived["outstanding"] == []


def test_the_adapter_refuses_to_run_a_program_anywhere_it_does_not_own() -> None:
    from huntweave.execution.dockerruntime import DockerRuntime
    from huntweave.execution.sandboxprofile import EgressProfile

    class Container:
        labels: dict[str, str] = {}

    class Containers:
        def get(self, identifier: str) -> Container:
            return Container()

    class Client:
        containers = Containers()

    runtime = DockerRuntime(
        client=Client(),
        egress=EgressProfile(
            policy_command=("python", "/lab/network.py", "policy"),
            read_command=("iptables-save", "-c"),
            policy_user="0:0",
        ),
    )
    for call in (
        lambda: runtime.apply_gateway_policy("someone-elses-container", "{}"),
        lambda: runtime.read_gateway_policy("someone-elses-container"),
    ):
        with pytest.raises(ResourceNotFound):
            call()
    # And a runtime configured with no program at all runs nothing, rather than treating a
    # caller's payload as the program.
    unconfigured = DockerRuntime(client=Client())
    with pytest.raises(RuntimeUnavailable):
        unconfigured.apply_gateway_policy("any", "{}")


def test_a_revert_that_cannot_account_for_a_resource_blocks(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    manager.begin_revert()
    runtime.create_container(
        name="huntweave-deadbeef-deadbeef-deadbeef-tool",
        labels={
            f"{LABEL_NAMESPACE}.project": PROJECT_NAME,
            f"{LABEL_NAMESPACE}.instance_id": str(uuid4()),
        },
        spec=_dummy_spec(),
    )
    report = manager.begin_revert()
    assert report.state == "blocked" and report.withdrew is False
    assert any(
        item.startswith("sandbox_resources_unaccounted") for item in report.outstanding
    ), report.outstanding


def test_a_rebuild_after_a_halt_starts_from_an_empty_session(
    egress_manager: SandboxManager, runtime: FakeRuntime
) -> None:
    session = egress_manager.open_session(_session_request())
    first = egress_manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    egress_manager.halt_instance(
        HaltRequest(instance_id=first.instance_id, reason="operator_cancelled")
    )
    second = egress_manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    assert second.instance_id != first.instance_id
    assert second.state == "ready"
    assert second.egress.applied_at is not None
    # The halted instance's resources are still recorded — stopping is not reclaiming — and the
    # rebuild has its own four.
    assert sorted(item.kind for item in second.resources) == [
        "container",
        "container",
        "network",
        "volume",
    ]
    assert egress_manager.instances[str(first.instance_id)].state == "stopped"


def test_a_whole_lifecycle_only_ever_asks_the_runtime_for_fixed_operations(
    manager: SandboxManager, runtime: FakeRuntime
) -> None:
    """The check that keeps the Docker surface narrow: whatever a full round does, the runtime is
    only ever asked for operations that are named in the fixed set."""
    session = manager.open_session(_session_request())
    instance = manager.launch_instance(
        SandboxInstanceRequest(session_id=session.session_id, authorized=[endpoint("10.20.0.5")])
    )
    manager.authorize_egress(
        EgressUpdate(
            instance_id=instance.instance_id,
            authorized=[endpoint("10.20.0.6")],
            reason="scope_shrunk",
        )
    )
    manager.processes(instance.instance_id)
    manager.logs(instance.instance_id, "tool")
    manager.audit()
    manager.halt_instance(
        HaltRequest(instance_id=instance.instance_id, reason="operator_cancelled")
    )
    manager.reclaim_instance(instance.instance_id)
    manager.close()

    assert set(runtime.calls) <= FIXED_OPERATIONS
    # And the round really did use the operations this slice added, rather than the set being
    # asserted from a list nobody exercises.
    assert {
        "connect_network" if manager.profile.target_network else "create_network",
        "apply_gateway_policy",
        "read_gateway_policy",
        "own_networks",
        "network_facts",
    } <= set(runtime.calls)


def _session_request(
    *, run_id: UUID | None = None, agent_session_id: UUID | None = None, scope_version: int = 1
) -> SandboxSessionRequest:
    return SandboxSessionRequest(
        run_id=run_id or uuid4(),
        agent_session_id=agent_session_id or uuid4(),
        scope_id=uuid4(),
        scope_version=scope_version,
        policy_version=1,
    )


def _dummy_spec() -> ContainerSpec:
    return ContainerSpec(
        image="huntweave-isolation-probe:p0",
        user="10001:10001",
        command=None,
        capabilities_add=(),
        network_name=None,
        network_mode="none",
        dns=(),
        read_only_rootfs=True,
        capabilities_drop=("ALL",),
        security_options=("no-new-privileges:true",),
        tmpfs=(),
        sysctls=(),
        mounts=(),
        pids_limit=128,
        memory_limit="512m",
        cpu_limit=1.0,
    )
