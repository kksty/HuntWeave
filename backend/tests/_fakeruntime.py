"""An in-memory container runtime, shared by the checks that need a manager without Docker.

It records what the trusted manager asked the runtime for and can fail on purpose, which is how the
lifecycle, egress and operator-read checks reach branches a real daemon will not produce on demand.
"""

import json
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from huntweave.execution.sandbox import (
    LABEL_NAMESPACE,
    CommandResult,
    ContainerFacts,
    ContainerSpec,
    LogRead,
    NetworkFacts,
    ResourceNotFound,
    RuntimeFacts,
    RuntimeResource,
    RuntimeUnavailable,
)


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
        # What running a command inside the tool container produces.
        self.commands: list[list[str]] = []
        self.command_workdirs: list[str | None] = []
        self.command_stdout = ""
        self.command_stderr = ""
        self.command_exit_code = 0
        self.command_timed_out = False
        self.command_gate: threading.Event | None = None

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

    def exec_in_tool(
        self,
        container_id: str,
        argv: Sequence[str],
        timeout_seconds: int,
        *,
        workdir: str | None = None,
    ) -> CommandResult:
        self._record("exec_in_tool")
        if container_id not in self.containers:
            raise ResourceNotFound(container_id)
        self.commands.append(list(argv))
        self.command_workdirs.append(workdir)
        if self.command_gate is not None:
            # Stands in for a command that is still running while the control plane reacts.
            self.command_gate.wait(timeout=10)
        return CommandResult(
            exit_code=self.command_exit_code,
            stdout=self.command_stdout,
            stderr=self.command_stderr,
            timed_out=self.command_timed_out,
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
