"""The Docker adapter behind the fixed operation set the trusted manager is allowed to perform.

This is the only module that imports the Docker SDK, and it is reachable only from the Runner
process (ADR-0010: no service share of the socket beyond the trusted manager, and the app gets no
container management at all). Each method implements one operation from ``ContainerRuntime`` and
takes a spec the manager already composed out of the fixed profile, so nothing a caller sends can
become a container option. There is no generic request method and no ``**options`` passthrough.
"""

import socket
from collections.abc import Mapping, Sequence
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from huntweave.config import SandboxSettings
from huntweave.execution.sandbox import (
    HEALTHCHECK_INTERVAL_NS,
    HEALTHCHECK_RETRIES,
    HEALTHCHECK_TIMEOUT_NS,
    LABEL_NAMESPACE,
    PROJECT_NAME,
    CommandResult,
    ContainerFacts,
    LogRead,
    NetworkFacts,
    ResourceNotFound,
    RuntimeFacts,
    RuntimeResource,
    RuntimeUnavailable,
    SandboxManager,
    VolumeMount,
)
from huntweave.execution.sandboxprofile import ContainerSpec, EgressProfile, SandboxProfile


class DockerRuntime:
    """A narrow Docker client: fixed operations, and a refusal reported as a named reason."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        egress: EgressProfile | None = None,
        tool_user: str | None = None,
    ) -> None:
        # The client is built here and nowhere else. `from_env` negotiates the API version with the
        # daemon as it is constructed, so a missing or unreadable socket fails here rather than at
        # import: that failure is reported as an unreachable runtime, never as a broken request.
        # The policy commands, their user and the tool user come from the fixed profile, so no
        # caller can name a program or an identity.
        self.egress = egress
        self.tool_user = tool_user
        try:
            self.client: Any = docker.from_env(timeout=15) if client is None else client
        except DockerException as error:
            raise RuntimeUnavailable("container runtime unreachable") from error

    # -- observation --------------------------------------------------------------------------

    def probe(self) -> RuntimeFacts:
        try:
            self.client.ping()
            version = self.client.version()
            info = self.client.info()
        except DockerException as error:
            raise RuntimeUnavailable("container runtime unreachable") from error
        return RuntimeFacts(
            engine=str(version.get("Version") or "unknown"),
            architecture=str(info.get("Architecture") or "unknown"),
        )

    def resolve_image(self, reference: str) -> str:
        try:
            image = self.client.images.get(reference)
        except ImageNotFound as error:
            raise ResourceNotFound(reference) from error
        except DockerException as error:
            raise RuntimeUnavailable("container runtime unreachable") from error
        digests = image.attrs.get("RepoDigests") or []
        # Either the registry digest or the local image ID identifies the exact bytes that will be
        # launched; a local build has no registry digest yet, and the manifest records what it has.
        return str(digests[0]) if digests else str(image.id)

    # -- networks, volumes, containers --------------------------------------------------------

    def create_network(
        self,
        *,
        name: str,
        labels: Mapping[str, str],
        internal: bool,
        driver: str,
        gateway_mode: str,
    ) -> str:
        try:
            network = self.client.networks.create(
                name,
                driver=driver,
                internal=internal,
                labels=dict(labels),
                options={"com.docker.network.bridge.gateway_mode_ipv4": gateway_mode},
                enable_ipv6=False,
            )
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        return str(network.id)

    def remove_network(self, network_id: str) -> None:
        try:
            self.client.networks.get(network_id).remove()
        except NotFound as error:
            raise ResourceNotFound(network_id) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error

    def create_volume(self, *, name: str, labels: Mapping[str, str]) -> str:
        try:
            volume = self.client.volumes.create(name=name, labels=dict(labels))
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        return str(volume.name)

    def remove_volume(self, volume_id: str) -> None:
        try:
            self.client.volumes.get(volume_id).remove(force=True)
        except NotFound as error:
            raise ResourceNotFound(volume_id) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error

    def create_container(
        self, *, name: str, labels: Mapping[str, str], spec: ContainerSpec
    ) -> str:
        options: dict[str, Any] = {
            "image": spec.image,
            "name": name,
            "labels": dict(labels),
            "detach": True,
            "user": spec.user,
            "init": True,
            "read_only": spec.read_only_rootfs,
            "cap_drop": list(spec.capabilities_drop),
            "cap_add": list(spec.capabilities_add),
            "security_opt": list(spec.security_options),
            "pids_limit": spec.pids_limit,
            "mem_limit": spec.memory_limit,
            "nano_cpus": int(spec.cpu_limit * 1_000_000_000),
            "tmpfs": dict(spec.tmpfs),
        }
        if spec.healthcheck:
            # The profile's readiness command becomes the container's health probe, which is how
            # the manager learns that the gateway finished installing its rules.
            options["healthcheck"] = {
                "test": list(spec.healthcheck),
                "interval": HEALTHCHECK_INTERVAL_NS,
                "timeout": HEALTHCHECK_TIMEOUT_NS,
                "retries": HEALTHCHECK_RETRIES,
            }
        if spec.command is not None:
            options["command"] = list(spec.command)
        if spec.network_name is not None:
            options["network"] = spec.network_name
        else:
            options["network_mode"] = spec.network_mode or "none"
        if spec.dns:
            options["dns"] = list(spec.dns)
        if spec.sysctls:
            options["sysctls"] = dict(spec.sysctls)
        if spec.mounts:
            options["volumes"] = {
                mount.volume: {
                    "bind": mount.target,
                    "mode": "ro" if mount.read_only else "rw",
                }
                for mount in spec.mounts
            }
        try:
            container = self.client.containers.create(**options)
        except ImageNotFound as error:
            raise ResourceNotFound(str(error)) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        return str(container.id)

    def start_container(self, container_id: str) -> None:
        try:
            self._container(container_id).start()
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error

    def stop_container(self, container_id: str, timeout_seconds: int) -> None:
        try:
            self._container(container_id).stop(timeout=timeout_seconds)
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error

    def remove_container(self, container_id: str, force: bool) -> None:
        try:
            self._container(container_id).remove(force=force)
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error

    # -- network attachment and egress policy -------------------------------------------------

    def connect_network(self, container_id: str, network_name: str) -> None:
        try:
            network = self.client.networks.get(network_name)
            network.connect(container_id)
        except NotFound as error:
            raise ResourceNotFound(network_name) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error

    def network_facts(self, network_id: str) -> NetworkFacts:
        return _network_facts(self._network(network_id))

    def own_networks(self) -> tuple[NetworkFacts, ...]:
        """The networks this Runner container is attached to.

        Discovered from the container itself rather than passed in, so a deployment cannot forget
        to protect the control plane it is part of. An unrecognisable container id yields no
        networks, and the manager refuses the launch instead of assuming they are unreachable.
        """
        try:
            container = self.client.containers.get(socket.gethostname())
            container.reload()
        except (NotFound, DockerException):
            return ()
        found = []
        attached = (container.attrs.get("NetworkSettings", {}).get("Networks") or {})
        for name, record in attached.items():
            network_id = record.get("NetworkID")
            facts = (
                _network_facts(self._network(str(network_id)))
                if network_id
                else NetworkFacts(id=name, name=str(name), subnet=None, internal=False)
            )
            ipam = record.get("IPAMConfig") or {}
            found.append(
                NetworkFacts(
                    id=facts.id,
                    name=str(name),
                    subnet=ipam.get("Subnet") or facts.subnet,
                    internal=facts.internal,
                    gateway=facts.gateway,
                )
            )
        return tuple(found)

    def apply_gateway_policy(self, gateway_id: str, payload: str) -> None:
        """Run the profile's fixed policy command inside the gateway with this rule body.

        No command comes from the caller: the program and the user are the profile's, and only the
        JSON body (permissions and protected networks this project already validated) travels. The
        container has to carry this project's labels, so the trusted component can only ever exec
        in a container it owns — and with no program configured, nothing runs at all.
        """
        egress = self._egress()
        egress = self._egress()
        container = self._container(gateway_id, require_project=True)
        try:
            result = container.exec_run(
                [*egress.policy_command, payload], user=egress.policy_user, demux=False
            )
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        if result.exit_code != 0:
            # The rule set is replaced as a whole by the helper, so a refusal leaves the previous
            # rules standing; the manager reads them back and decides what that means.
            raise RuntimeUnavailable("gateway policy rejected")

    def read_gateway_policy(self, gateway_id: str) -> str:
        egress = self._egress()
        container = self._container(gateway_id, require_project=True)
        try:
            result = container.exec_run(list(egress.read_command), user=egress.policy_user)
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        output: bytes = result.output
        return output.decode("utf-8", errors="replace")

    def exec_in_tool(
        self, container_id: str, argv: Sequence[str], timeout_seconds: int
    ) -> CommandResult:
        """Run one command inside this project's tool container, as the profile's tool user.

        The command is wrapped in a bounded supervisor inside the container, so an action that
        hangs cannot outlive its ticket even if this process is busy: the timeout belongs to the
        call, not to the caller's patience.
        """
        container = self._container(container_id, require_project=True)
        user = self._tool_user()
        supervised = ["timeout", "-k", str(TOOL_KILL_GRACE_SECONDS), str(timeout_seconds), *argv]
        try:
            result = container.exec_run(supervised, user=user, demux=True)
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        streams = result.output if isinstance(result.output, tuple) else (result.output, None)
        raw_stdout, raw_stderr = streams
        stdout = (raw_stdout or b"").decode("utf-8", errors="replace")
        stderr = (raw_stderr or b"").decode("utf-8", errors="replace")
        exit_code = int(result.exit_code or 0)
        return CommandResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            # 124 is what `timeout` returns when the deadline was reached.
            timed_out=exit_code == 124,
        )

    def _tool_user(self) -> str:
        if self.tool_user is None:
            raise RuntimeUnavailable("no tool user is configured")
        return self.tool_user

    def _egress(self) -> EgressProfile:
        if self.egress is None or not self.egress.policy_command:
            # Without a fixed program there is nothing this adapter may run, and running the
            # caller's payload as a program is exactly what must never happen.
            raise RuntimeUnavailable("no egress policy command is configured")
        return self.egress

    # -- facts about this project's resources -------------------------------------------------

    def container_facts(self, container_id: str) -> ContainerFacts:
        attrs = self._container(container_id).attrs
        host = attrs["HostConfig"]
        mounts = tuple(
            VolumeMount(
                volume=str(entry["Name"]),
                target=str(entry["Destination"]),
                read_only=not bool(entry["RW"]),
            )
            for entry in attrs.get("Mounts", [])
            if entry.get("Type") == "volume"
        )
        return ContainerFacts(
            id=str(attrs["Id"]),
            name=str(attrs["Name"]).lstrip("/"),
            running=bool(attrs["State"]["Running"]),
            labels=_labels_of(attrs["Config"].get("Labels")),
            user=str(attrs["Config"].get("User") or ""),
            network_mode=str(host.get("NetworkMode") or ""),
            capabilities_add=tuple(host.get("CapAdd") or ()),
            capabilities_drop=tuple(host.get("CapDrop") or ()),
            security_options=tuple(host.get("SecurityOpt") or ()),
            read_only_rootfs=bool(host.get("ReadonlyRootfs")),
            mounts=mounts,
            health=str((attrs.get("State", {}).get("Health") or {}).get("Status") or "none"),
        )

    def container_processes(self, container_id: str, limit: int) -> tuple[str, ...]:
        container = self._container(container_id)
        try:
            rows = container.top().get("Processes") or []
        except APIError as error:
            if error.response is not None and error.response.status_code == 409:
                # A stopped container runs no processes; that is a fact, not a failure.
                return ()
            raise RuntimeUnavailable(str(error)) from error
        return tuple(" ".join(str(cell) for cell in row) for row in rows[:limit])

    def container_logs(
        self, container_id: str, *, tail_lines: int, tail_bytes: int
    ) -> LogRead:
        container = self._container(container_id)
        try:
            raw = container.logs(stdout=True, stderr=True, tail=tail_lines, timestamps=False)
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        payload: bytes = raw
        # Two bounds: the daemon returns the last lines, and only the last bytes of those are
        # kept. The line count is reported so the caller can tell that the daemon cut output.
        return LogRead(
            text=payload[-tail_bytes:].decode("utf-8", errors="replace"),
            lines_returned=payload.count(b"\n") or (1 if payload else 0),
            line_bound=tail_lines,
        )

    def list_resources(self, labels: Mapping[str, str]) -> tuple[RuntimeResource, ...]:
        selector = [f"{key}={value}" for key, value in sorted(labels.items())]
        try:
            containers = self.client.containers.list(all=True, filters={"label": selector})
            networks = self.client.networks.list(filters={"label": selector})
            volumes = self.client.volumes.list(filters={"label": selector})
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        resources = [
            RuntimeResource(
                kind="container",
                id=str(container.id),
                name=str(container.name),
                labels=_labels_of(container.labels),
                running=str(container.status) == "running",
            )
            for container in containers
        ]
        resources.extend(
            RuntimeResource(
                kind="network",
                id=str(network.id),
                name=str(network.name),
                labels=_labels_of(network.attrs.get("Labels")),
            )
            for network in networks
        )
        resources.extend(
            RuntimeResource(
                kind="volume",
                id=str(volume.name),
                name=str(volume.name),
                labels=_labels_of(volume.attrs.get("Labels")),
            )
            for volume in volumes
        )
        return tuple(resources)

    def close(self) -> None:
        self.client.close()

    def _container(self, container_id: str, *, require_project: bool = False) -> Any:
        try:
            container = self.client.containers.get(container_id)
        except NotFound as error:
            raise ResourceNotFound(container_id) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error
        if require_project and not str(
            _labels_of(container.labels).get(f"{LABEL_NAMESPACE}.project", "")
        ) == PROJECT_NAME:
            # An operation that runs a program inside a container is only ever aimed at one this
            # project created: a bare container id from a caller is not enough.
            raise ResourceNotFound(container_id)
        return container

    def _network(self, network_id: str) -> Any:
        try:
            return self.client.networks.get(network_id)
        except NotFound as error:
            raise ResourceNotFound(network_id) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error


def _network_facts(network: Any) -> NetworkFacts:
    try:
        network.reload()
    except DockerException as error:
        raise RuntimeUnavailable(str(error)) from error
    attrs: Mapping[str, Any] = network.attrs
    return NetworkFacts(
        id=str(network.id),
        name=str(network.name),
        subnet=_subnet_of(attrs),
        internal=bool(attrs.get("Internal")),
        gateway=_gateway_of(attrs),
    )


def _labels_of(value: object) -> dict[str, str]:
    """Docker labels arrive as an arbitrary mapping; this project only ever writes strings."""
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _subnet_of(attrs: Mapping[str, Any]) -> str | None:
    config = (attrs.get("IPAM") or {}).get("Config") or []
    for entry in config:
        subnet = entry.get("Subnet")
        if isinstance(subnet, str) and ":" not in subnet:
            return subnet
    return None


def _gateway_of(attrs: Mapping[str, Any]) -> str | None:
    config = (attrs.get("IPAM") or {}).get("Config") or []
    for entry in config:
        gateway = entry.get("Gateway")
        if isinstance(gateway, str) and ":" not in gateway:
            return gateway
    return None


# How long a command may keep running after its timeout before it is killed outright.
TOOL_KILL_GRACE_SECONDS = 5


def open_sandbox_manager(settings: SandboxSettings) -> SandboxManager:
    """Build the trusted manager from deployment settings.

    The profile is read from the image's committed profiles directory, and the socket is opened
    here and nowhere else. This function is only called when sandbox management is explicitly
    enabled, so a default deployment never constructs a Docker client at all.
    """
    profile = SandboxProfile.load(settings.profile_id, settings.profile_dir)
    return SandboxManager(
        runtime=DockerRuntime(egress=profile.egress, tool_user=profile.tool.user),
        profile=profile,
        state_dir=settings.state_dir,
        evidence_dir=settings.evidence_dir,
    )


__all__ = ["DockerRuntime", "open_sandbox_manager"]
