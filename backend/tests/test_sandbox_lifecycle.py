"""The trusted sandbox manager's contract: fixed profile, narrow runtime, honest lifecycle.

These checks never start a container. The real lifecycle is verified by
``deploy/verify_lifecycle.py`` against the lab; here the subject is the decisions — which values
reach the runtime, what a request may say, what the ledger proves after a crash, and what a
reclaim is allowed to remove.
"""

import inspect
import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from huntweave.config import SandboxSettings
from huntweave.contracts.capabilities import Capabilities
from huntweave.execution.sandbox import (
    LABEL_NAMESPACE,
    PROJECT_NAME,
    ContainerFacts,
    ContainerRuntime,
    ContainerSpec,
    LogRead,
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
        )

    def container_processes(self, container_id: str, limit: int) -> tuple[str, ...]:
        self._record("container_processes")
        if container_id not in self.containers:
            raise ResourceNotFound(container_id)
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
