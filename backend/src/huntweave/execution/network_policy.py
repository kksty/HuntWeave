"""IPv4/TCP gateway policy, also consumed by the isolated P0 lab helper.

Installing these rules requires the trusted gateway's NET_ADMIN capability.
This module never grants an Agent a network management interface.
"""

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

PROTECTED_DEFAULTS = tuple(
    IPv4Network(value)
    for value in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)


class ScopeDenied(ValueError):
    """An explicit target conflicts with protected infrastructure."""


@dataclass(frozen=True)
class Endpoint:
    address: IPv4Address
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.address, IPv4Address):
            raise ValueError("This profile supports IPv4 only")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Port must be an integer in 1..65535")


@dataclass(frozen=True)
class NetworkPolicy:
    permissions: tuple[Endpoint, ...] = ()
    protected: tuple[IPv4Network, ...] = ()

    def __post_init__(self) -> None:
        for endpoint in self.permissions:
            if any(endpoint.address in network for network in PROTECTED_DEFAULTS + self.protected):
                raise ScopeDenied("Target conflicts with protected infrastructure")

    def permits(self, address: IPv4Address, port: int) -> bool:
        return Endpoint(address, port) in self.permissions

    def gateway_rules(self) -> str:
        lines = ["*filter", ":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"]
        for network in PROTECTED_DEFAULTS + self.protected:
            lines.append(f"-A OUTPUT -d {network} -j DROP")
        for endpoint in self.permissions:
            lines.extend(
                [
                    f"-A OUTPUT -d {endpoint.address}/32 "
                    f"-p tcp --dport {endpoint.port} "
                    "-m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT",
                    f"-A INPUT -s {endpoint.address}/32 "
                    f"-p tcp --sport {endpoint.port} -m conntrack --ctstate ESTABLISHED -j ACCEPT",
                ]
            )
        # No blanket ESTABLISHED rule: revocation must remove both directions of a flow.
        return "\n".join([*lines, "COMMIT", ""])
