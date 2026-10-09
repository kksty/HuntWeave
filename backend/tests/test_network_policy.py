from ipaddress import IPv4Address, IPv4Network

import pytest

from huntweave.execution.network_policy import Endpoint, NetworkPolicy, ScopeDenied


def test_explicit_scope_cannot_override_metadata_or_control_protection() -> None:
    for address in ("169.254.169.254", "127.0.0.1", "10.80.0.4"):
        with pytest.raises(ScopeDenied):
            NetworkPolicy(
                permissions=(Endpoint(IPv4Address(address), 7000),),
                protected=(IPv4Network("10.80.0.0/24"),),
            )


def test_private_authorized_target_is_valid() -> None:
    policy = NetworkPolicy(
        permissions=(Endpoint(IPv4Address("10.82.0.5"), 7000),),
        protected=(IPv4Network("10.80.0.0/24"),),
    )
    assert policy.permits(IPv4Address("10.82.0.5"), 7000)
    assert not policy.permits(IPv4Address("10.82.0.5"), 7001)
    assert not policy.permits(IPv4Address("10.82.0.6"), 7000)


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_invalid_ports_are_rejected(port: int) -> None:
    with pytest.raises(ValueError):
        Endpoint(IPv4Address("10.82.0.5"), port)
