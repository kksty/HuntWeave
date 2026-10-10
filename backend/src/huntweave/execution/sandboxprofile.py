"""The fixed sandbox profile and the container specs it produces.

A profile is a committed, versioned artifact: the deployment selects one by id, and no request can
override any part of it. Everything a container may become — image, user, mount, network mode,
capability, limit, label-free name — is decided here, so the lifecycle in ``sandbox`` has nothing
left to choose. The loader refuses anything that would widen the sandbox instead of accepting it
and hoping the caller behaves.
"""

import ipaddress
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_PROFILE_VERSION = 1
GATEWAY_CAPABILITIES = ("NET_ADMIN",)


class SandboxRejected(Exception):
    """A refused sandbox operation. The reason code names the precondition that failed."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ResourceNotFound(Exception):
    """The runtime no longer has this resource. A resource that does not exist cannot be running."""


class RuntimeUnavailable(Exception):
    """The container runtime could not be reached, or refused the operation outright."""


@dataclass(frozen=True)
class VolumeMount:
    volume: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class ContainerSpec:
    """What the manager asks the runtime to create. Built only from the profile."""

    image: str
    user: str
    command: tuple[str, ...] | None
    capabilities_add: tuple[str, ...]
    network_name: str | None
    network_mode: str | None
    dns: tuple[str, ...]
    read_only_rootfs: bool
    capabilities_drop: tuple[str, ...]
    security_options: tuple[str, ...]
    tmpfs: tuple[tuple[str, str], ...]
    sysctls: tuple[tuple[str, str], ...]
    mounts: tuple[VolumeMount, ...]
    pids_limit: int
    memory_limit: str
    cpu_limit: float
    # Empty means no health probe. The gateway's is what the manager waits on before it lets a
    # tool exist, so a gateway without one is refused at load time.
    healthcheck: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleProfile:
    """One container role, fully decided before any request arrives."""

    image: str
    command: tuple[str, ...] | None
    user: str
    capabilities_add: tuple[str, ...]
    # How the role reports that it finished preparing itself. The gateway installs its
    # default-deny rules before it reports ready, which is what lets the manager hold the tool
    # back until the rules exist.
    healthcheck: tuple[str, ...]


@dataclass(frozen=True)
class SandboxLimits:
    pids: int
    memory: str
    cpus: float
    read_only_rootfs: bool
    capabilities_drop: tuple[str, ...]
    no_new_privileges: bool
    tmpfs: tuple[tuple[str, str], ...]
    log_tail_bytes: int
    log_line_bound: int
    evidence_max_bytes: int
    # How long the manager waits for the gateway to report ready, and what "within the lease
    # allows" means when a revocation has to take effect.
    readiness_seconds: int
    revocation_seconds: int


@dataclass(frozen=True)
class EgressProfile:
    """The fixed commands the manager uses to install and read the gateway's egress rules.

    Only the rule body travels; the program that installs it is decided here, and the manager
    refuses to install anything at all when this profile names no program. A caller cannot name a
    program, and the adapter runs nothing else.
    """

    policy_command: tuple[str, ...]
    read_command: tuple[str, ...]
    policy_user: str


@dataclass(frozen=True)
class SandboxProfile:
    """A committed description of the sandbox, including the specs its roles produce."""

    profile_id: str
    profile_version: int
    status: str
    network_driver: str
    network_internal: bool
    network_gateway_mode: str
    dns: tuple[str, ...]
    # The network the gateway uses to reach authorized targets. Null means this profile has no
    # target egress at all: a session it creates cannot reach anything but itself.
    target_network: str | None
    # Ranges the deployment declares off-limits on top of what the manager discovers for itself
    # (its own networks and the bridges' host-side addresses).
    protected_networks: tuple[str, ...]
    workspace_mount: str
    tool_inventory: tuple[str, ...]
    egress: EgressProfile
    gateway: RoleProfile
    tool: RoleProfile
    limits: SandboxLimits

    @classmethod
    def load(cls, profile_id: str, directory: Path) -> "SandboxProfile":
        """Read one profile from disk, refusing anything that would widen the sandbox."""
        if not profile_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in profile_id
        ):
            raise SandboxRejected("sandbox_profile_invalid")
        path = directory / f"{profile_id}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SandboxRejected("sandbox_profile_unknown") from None
        except (OSError, ValueError):
            raise SandboxRejected("sandbox_profile_invalid") from None
        if not isinstance(document, dict):
            raise SandboxRejected("sandbox_profile_invalid")
        _exact_keys(
            document,
            {
                "profile_version",
                "profile_id",
                "status",
                "network",
                "workspace",
                "tool_inventory",
                "egress",
                "gateway",
                "tool",
                "limits",
            },
        )
        if document["profile_id"] != profile_id:
            raise SandboxRejected("sandbox_profile_invalid")
        version = _integer(document["profile_version"], minimum=1)
        if version != SUPPORTED_PROFILE_VERSION:
            raise SandboxRejected("sandbox_profile_unsupported_version")
        network = _mapping(
            document["network"],
            {
                "driver",
                "internal",
                "ipv6",
                "gateway_mode_ipv4",
                "dns",
                "target_network",
                "protected",
            },
        )
        # The session network is where the tool runs. A profile that lets it reach outside the
        # sandbox is not a narrower deployment choice, it is a different product.
        gateway_mode = _string(network["gateway_mode_ipv4"])
        dns = tuple(_string(value) for value in _list(network["dns"]))
        if network["internal"] is not True or network["ipv6"] is not False:
            raise SandboxRejected("sandbox_profile_not_isolated")
        if gateway_mode != "isolated" or any(value != "127.0.0.1" for value in dns):
            raise SandboxRejected("sandbox_profile_not_isolated")
        target_network = network["target_network"]
        if target_network is not None:
            target_network = _string(target_network)
        workspace = _mapping(document["workspace"], {"mount"})
        egress = _mapping(
            document["egress"], {"policy_command", "read_command", "policy_user"}
        )
        gateway = _role(document["gateway"], privileged=True)
        policy_command = _command(egress["policy_command"])
        read_command = _command(egress["read_command"])
        policy_user = _string(egress["policy_user"])
        if _user_identifier(policy_user) != _user_identifier(gateway.user):
            # The rules are installed as the gateway's own user: two sources for one decision is
            # one source too many.
            raise SandboxRejected("sandbox_profile_invalid")
        return cls(
            profile_id=profile_id,
            profile_version=version,
            status=_string(document["status"]),
            network_driver=_string(network["driver"]),
            network_internal=True,
            network_gateway_mode=gateway_mode,
            dns=dns,
            target_network=target_network,
            protected_networks=tuple(
                _network(value) for value in _strings(network["protected"])
            ),
            workspace_mount=_string(workspace["mount"]),
            tool_inventory=_strings(document["tool_inventory"]),
            egress=EgressProfile(
                policy_command=policy_command,
                read_command=read_command,
                policy_user=policy_user,
            ),
            gateway=gateway,
            tool=_role(document["tool"], privileged=False),
            limits=_limits(document["limits"]),
        )

    # -- the specs this profile produces ------------------------------------------------------

    def gateway_spec(self, network_id: str) -> ContainerSpec:
        """The gateway holds the only added capability and installs the default-deny rules.

        It carries no mount at all: the policy it enforces and the evidence of it live outside the
        container that reaches targets.
        """
        return ContainerSpec(
            image=self.gateway.image,
            user=self.gateway.user,
            command=self.gateway.command,
            capabilities_add=self.gateway.capabilities_add,
            network_name=network_id,
            network_mode=None,
            dns=self.dns,
            read_only_rootfs=self.limits.read_only_rootfs,
            capabilities_drop=self.limits.capabilities_drop,
            security_options=self._security_options(),
            tmpfs=self.limits.tmpfs,
            sysctls=(
                ("net.ipv6.conf.all.disable_ipv6", "1"),
                ("net.ipv6.conf.default.disable_ipv6", "1"),
                ("net.ipv4.ip_forward", "0"),
            ),
            mounts=(),
            pids_limit=self.limits.pids,
            memory_limit=self.limits.memory,
            cpu_limit=self.limits.cpus,
            healthcheck=self.gateway.healthcheck,
        )

    def tool_spec(self, gateway_id: str, volume_id: str) -> ContainerSpec:
        """The tool shares the gateway's network namespace and owns only its private workspace."""
        return ContainerSpec(
            image=self.tool.image,
            user=self.tool.user,
            command=self.tool.command,
            capabilities_add=self.tool.capabilities_add,
            network_name=None,
            # The tool runs inside the gateway's network namespace: it cannot pick another route,
            # and its egress is whatever the gateway's rules allow.
            network_mode=f"container:{gateway_id}",
            dns=(),
            read_only_rootfs=self.limits.read_only_rootfs,
            capabilities_drop=self.limits.capabilities_drop,
            security_options=self._security_options(),
            tmpfs=self.limits.tmpfs,
            sysctls=(),
            mounts=(
                VolumeMount(volume=volume_id, target=self.workspace_mount, read_only=False),
            ),
            pids_limit=self.limits.pids,
            memory_limit=self.limits.memory,
            cpu_limit=self.limits.cpus,
            healthcheck=self.tool.healthcheck,
        )

    def _security_options(self) -> tuple[str, ...]:
        return ("no-new-privileges:true",) if self.limits.no_new_privileges else ()


def _exact_keys(document: Mapping[str, object], expected: set[str]) -> None:
    if set(document) != expected:
        raise SandboxRejected("sandbox_profile_invalid")


def _mapping(value: object, expected: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise SandboxRejected("sandbox_profile_invalid")
    _exact_keys(value, expected)
    return value


def _list(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise SandboxRejected("sandbox_profile_invalid")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SandboxRejected("sandbox_profile_invalid")
    return value


def _integer(value: object, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise SandboxRejected("sandbox_profile_invalid")
    return value


def _number(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SandboxRejected("sandbox_profile_invalid")
    if not minimum <= float(value) <= maximum:
        raise SandboxRejected("sandbox_profile_invalid")
    return float(value)


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _list(value))


def _command(value: object) -> tuple[str, ...]:
    """A command a profile names is never empty: an empty one would make a caller's payload the
    program, which is exactly the widening this component exists to prevent."""
    command = _strings(value)
    if not command:
        raise SandboxRejected("sandbox_profile_invalid")
    return command


def _network(value: object) -> str:
    """A declared protected range must be an IPv4 network literal."""
    text = _string(value)
    try:
        network = ipaddress.IPv4Network(text, strict=False)
    except ValueError:
        raise SandboxRejected("sandbox_profile_invalid") from None
    return str(network)


def _user_identifier(value: object) -> int:
    """A container user is ``uid`` or ``uid:gid``; only the numeric form is accepted."""
    head = _string(value).split(":", 1)[0]
    if not head.isdigit():
        raise SandboxRejected("sandbox_profile_invalid")
    return int(head)


def _image(value: object) -> str:
    """An image reference must name a tag or a digest, and never a floating ``latest``."""
    text = _string(value)
    if "@sha256:" in text:
        digest = text.split("@sha256:", 1)[1]
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise SandboxRejected("sandbox_profile_image_not_pinned")
        return text
    if ":" not in text.split("/")[-1]:
        raise SandboxRejected("sandbox_profile_image_not_pinned")
    if text.rsplit(":", 1)[1] == "latest":
        raise SandboxRejected("sandbox_profile_image_not_pinned")
    return text


def _role(value: object, *, privileged: bool) -> RoleProfile:
    document = _mapping(
        value, {"image", "command", "user", "capabilities_add", "healthcheck"}
    )
    command = (
        None
        if document["command"] is None
        else tuple(_string(part) for part in _list(document["command"]))
    )
    image, user = _image(document["image"]), _string(document["user"])
    uid, capabilities = _user_identifier(user), _strings(document["capabilities_add"])
    if not privileged and (uid == 0 or capabilities):
        # The tool container is the one that reaches authorized targets: it runs as a normal user
        # with no added capability, in every profile.
        raise SandboxRejected("sandbox_profile_privileged_tool")
    if privileged and (uid != 0 or any(item not in GATEWAY_CAPABILITIES for item in capabilities)):
        raise SandboxRejected("sandbox_profile_invalid")
    healthcheck: tuple[str, ...] = ()
    if document["healthcheck"] is not None:
        healthcheck = _command(document["healthcheck"])
        if healthcheck[0] not in {"CMD", "CMD-SHELL"}:
            # A readiness probe the daemon cannot run would leave the manager waiting for a
            # gateway that never reports, which is a refusal at launch, not a silent pass.
            raise SandboxRejected("sandbox_profile_invalid")
    if privileged and not healthcheck:
        # The manager holds the tool back until the gateway reports that its rules are installed.
        # A gateway nobody can ask is a gateway the manager would have to guess about.
        raise SandboxRejected("sandbox_profile_invalid")
    return RoleProfile(
        image=image,
        command=command,
        user=user,
        capabilities_add=capabilities,
        healthcheck=healthcheck,
    )


def _limits(value: object) -> SandboxLimits:
    document = _mapping(
        value,
        {
            "pids",
            "memory",
            "cpus",
            "read_only_rootfs",
            "capabilities_drop",
            "no_new_privileges",
            "tmpfs",
            "log_tail_bytes",
            "log_line_bound",
            "evidence_max_bytes",
            "readiness_seconds",
            "revocation_seconds",
        },
    )
    if document["read_only_rootfs"] is not True or document["no_new_privileges"] is not True:
        raise SandboxRejected("sandbox_profile_invalid")
    if _strings(document["capabilities_drop"]) != ("ALL",):
        raise SandboxRejected("sandbox_profile_invalid")
    tmpfs = document["tmpfs"]
    if not isinstance(tmpfs, dict):
        raise SandboxRejected("sandbox_profile_invalid")
    return SandboxLimits(
        pids=_integer(document["pids"], minimum=8, maximum=1024),
        memory=_string(document["memory"]),
        cpus=_number(document["cpus"], minimum=0.1, maximum=8.0),
        read_only_rootfs=True,
        capabilities_drop=("ALL",),
        no_new_privileges=True,
        tmpfs=tuple((_string(key), _string(tmpfs[key])) for key in sorted(tmpfs)),
        log_tail_bytes=_integer(document["log_tail_bytes"], minimum=256, maximum=1_048_576),
        log_line_bound=_integer(document["log_line_bound"], minimum=8, maximum=10_000),
        evidence_max_bytes=_integer(
            document["evidence_max_bytes"], minimum=1, maximum=20 * 1024 * 1024
        ),
        readiness_seconds=_integer(document["readiness_seconds"], minimum=1, maximum=120),
        revocation_seconds=_integer(document["revocation_seconds"], minimum=1, maximum=60),
    )
