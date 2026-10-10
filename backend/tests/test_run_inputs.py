import pytest

from huntweave.config import AppSettings
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.runs import PortInput
from huntweave.runs.inputs import expand_ports, preview_targets


def test_preview_preserves_bad_lines_and_deduplicates_normalized_addresses() -> None:
    result = preview_targets(
        "\n 192.0.2.10 \n192.0.2.10\nhttps://192.0.2.10\n192.0.2.10:80\n192.0.2.0/24\nexample.com\n"
    )
    assert result.targets == ["192.0.2.10"]
    assert result.rows[0].line == 2
    assert result.rows[1].duplicate_of == 2
    assert [row.line for row in result.rows if row.reason_code] == [4, 5, 6, 7]
    assert not result.valid


def test_ipv6_is_normalized_but_not_executable_on_current_profile() -> None:
    result = preview_targets("2001:0db8:0:0:0:0:0:1\n2001:db8::1\nfe80::1%eth0\nfe80::2")
    assert result.targets == ["2001:db8::1"]
    assert result.rows[1].duplicate_of == 1
    assert result.rows[0].reason_code == "ipv6_environment_unsupported"
    assert result.rows[2].reason_code == "invalid_ip"
    assert result.rows[3].reason_code == "protected_address"
    assert not result.valid


def test_private_addresses_allowed_and_empty_input_cannot_be_submitted() -> None:
    assert preview_targets("10.30.0.1\n192.168.8.2").valid
    assert not preview_targets(" \n ").valid
    assert not preview_targets("127.0.0.1\n0.0.0.0\n169.254.169.254\n224.0.0.1").valid


def _targets_at_limit() -> list[str]:
    """恰好等于文档上限的清单（`PROJECT.md` §12：单 Run 100 个 IP）。"""
    return [f"192.0.2.{index}" for index in range(1, 101)]


def test_target_limit_names_the_rows_that_crossed_it() -> None:
    """The 101st distinct target is refused by line, not by a bare 422 (issue #19).

    A count in a response body tells an operator nothing about which rows to remove, so the limit
    is reported the same way every other input problem is: on the line that caused it.
    """
    targets = _targets_at_limit()
    assert preview_targets("\n".join(targets)).valid
    over = preview_targets("\n".join([*targets, "192.0.2.101"]))
    assert not over.valid
    assert over.target_limit == 100
    assert over.over_limit == 1
    assert [row.line for row in over.rows if row.reason_code] == [101]
    assert over.rows[-1].reason_code == "target_limit_exceeded"
    assert over.rows[-1].normalized == "192.0.2.101"


def test_target_limit_counts_distinct_targets_rather_than_input_lines() -> None:
    targets = _targets_at_limit()
    preview = preview_targets("\n".join([*targets, *targets]))
    assert len(preview.targets) == 100
    assert preview.valid
    assert preview.over_limit == 0


def test_an_in_executable_ipv6_target_is_reported_before_the_limit() -> None:
    """An IPv6 target inside the quota must say why it cannot run, not be counted out first.

    Both refusals are true of this input; the one the operator can act on is the one where the
    address can never run on this deployment, so it is reported on the row itself and the count
    never pre-empts it.
    """
    targets = [*_targets_at_limit()[:99], "2001:db8::5"]
    preview = preview_targets("\n".join([*targets, "192.0.2.101"]))
    reasons = {row.line: row.reason_code for row in preview.rows if row.reason_code}
    assert reasons[100] == "ipv6_environment_unsupported"
    assert reasons[101] == "target_limit_exceeded"


def test_ports_expand_explicit_profile_custom_ranges_and_full_tcp() -> None:
    common = expand_ports(PortInput())
    assert 80 in common and 443 in common and 6379 in common
    assert len(common) < 65535
    assert expand_ports(PortInput(profile="custom-tcp-v1", custom="443,80,8000-8002,80")) == [
        80,
        443,
        8000,
        8001,
        8002,
    ]
    all_ports = expand_ports(PortInput(profile="all-tcp-v1"))
    assert all_ports == list(range(1, 65536))


@pytest.mark.parametrize(
    "custom", ["", "0", "65536", "80-1", "-1", "80;id", "1.5", "1,,2", "80/udp"]
)
def test_invalid_ports_fail_closed(custom: str) -> None:
    with pytest.raises(ServiceError):
        expand_ports(PortInput(profile="custom-tcp-v1", custom=custom))


def test_non_custom_profile_rejects_hidden_port_overrides() -> None:
    with pytest.raises(ServiceError):
        expand_ports(PortInput(custom="22"))


def test_remote_http_cannot_silently_use_development_cookie() -> None:
    with pytest.raises(ValueError):
        AppSettings(public_origin="http://example.com")
    secure = AppSettings(public_origin="https://example.com", cookie_secure=True)
    assert secure.cookie_name == "__Host-huntweave"
