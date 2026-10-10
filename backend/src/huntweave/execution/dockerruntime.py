"""The Docker adapter behind the fixed operation set the trusted manager is allowed to perform.

This is the only module that imports the Docker SDK, and it is reachable only from the Runner
process (ADR-0010: no service share of the socket beyond the trusted manager, and the app gets no
container management at all). Each method implements one operation from ``ContainerRuntime`` and
takes a spec the manager already composed out of the fixed profile, so nothing a caller sends can
become a container option. There is no generic request method and no ``**options`` passthrough.
"""

from collections.abc import Mapping
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from huntweave.config import SandboxSettings
from huntweave.execution.sandbox import (
    ContainerFacts,
    LogRead,
    ResourceNotFound,
    RuntimeFacts,
    RuntimeResource,
    RuntimeUnavailable,
    SandboxManager,
    VolumeMount,
)
from huntweave.execution.sandboxprofile import ContainerSpec, SandboxProfile


class DockerRuntime:
    """A narrow Docker client: fixed operations, and a refusal reported as a named reason."""

    def __init__(self, client: Any | None = None) -> None:
        # The client is built here and nowhere else. `from_env` negotiates the API version with the
        # daemon as it is constructed, so a missing or unreadable socket fails here rather than at
        # import: that failure is reported as an unreachable runtime, never as a broken request.
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

    def _container(self, container_id: str) -> Any:
        try:
            return self.client.containers.get(container_id)
        except NotFound as error:
            raise ResourceNotFound(container_id) from error
        except DockerException as error:
            raise RuntimeUnavailable(str(error)) from error


def _labels_of(value: object) -> dict[str, str]:
    """Docker labels arrive as an arbitrary mapping; this project only ever writes strings."""
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def open_sandbox_manager(settings: SandboxSettings) -> SandboxManager:
    """Build the trusted manager from deployment settings.

    The profile is read from the image's committed profiles directory, and the socket is opened
    here and nowhere else. This function is only called when sandbox management is explicitly
    enabled, so a default deployment never constructs a Docker client at all.
    """
    profile = SandboxProfile.load(settings.profile_id, settings.profile_dir)
    return SandboxManager(
        runtime=DockerRuntime(),
        profile=profile,
        state_dir=settings.state_dir,
        evidence_dir=settings.evidence_dir,
    )


__all__ = ["DockerRuntime", "open_sandbox_manager"]
