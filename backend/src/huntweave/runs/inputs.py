import ipaddress
import json
import re
from pathlib import Path

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.runs import PortInput, TargetPreview, TargetRow

PROFILE = Path(__file__).resolve().parents[4] / "profiles" / "common-tcp-v1.json"

# PROJECT.md 第 12 节的保守开发默认值：单 Run 导入上限 100 个 IP。
# 计数口径是去重后的不同目标，重复行不占额度。
TARGET_LIMIT = 100


def preview_targets(text: str) -> TargetPreview:
    """Read a pasted list of targets and report, per line, what the platform will and will not take.

    The order of the two refusals matters and is deliberate. Every line is classified first — which
    is where an IPv6 target is told it cannot run here at all — and only a list that is otherwise
    acceptable is measured against the per-Run limit. Reporting `target_limit_exceeded` for input
    that also contains an IPv6 target would hide the reason the operator can actually act on behind
    a count, and no per-line feedback would be shown for either.
    """
    targets: dict[str, int] = {}
    rows: list[TargetRow] = []
    invalid = False
    overflow = 0
    for line, raw in enumerate(text.splitlines(), start=1):
        value = raw.strip()
        if not value:
            continue
        normalized, reason, duplicate = None, None, None
        try:
            if "%" in value:
                raise ValueError("zone ID")
            address = ipaddress.ip_address(value)
            if (
                address.is_link_local
                or address.is_loopback
                or address.is_unspecified
                or address.is_multicast
            ):
                reason = "protected_address"
            else:
                normalized = str(address)
                duplicate = targets.get(normalized)
                targets.setdefault(normalized, line)
                if address.version == 6:
                    reason = "ipv6_environment_unsupported"
        except ValueError:
            reason = "invalid_ip"
        if reason is None and duplicate is None and len(targets) > TARGET_LIMIT:
            # Over the per-Run limit: named on the line that crossed it, so the operator is told
            # which rows to remove instead of only that something 422ed.
            reason = "target_limit_exceeded"
            overflow += 1
        invalid = invalid or reason is not None
        rows.append(
            TargetRow(
                line=line,
                value=value,
                normalized=normalized,
                reason_code=reason,
                duplicate_of=duplicate,
            )
        )
    return TargetPreview(
        targets=list(targets),
        rows=rows,
        valid=bool(targets) and not invalid,
        target_limit=TARGET_LIMIT,
        over_limit=overflow,
    )


def expand_ports(value: PortInput) -> list[int]:
    if value.profile != "custom-tcp-v1" and value.custom.strip():
        raise ServiceError("unexpected_custom_ports", 422)
    if value.profile == "common-tcp-v1":
        data = json.loads(PROFILE.read_text(encoding="utf-8"))
        return list(data["ports"])
    if value.profile == "all-tcp-v1":
        return list(range(1, 65536))
    ports: set[int] = set()
    for part in value.custom.split(","):
        if not re.fullmatch(r"[0-9]{1,5}(?:\s*-\s*[0-9]{1,5})?", part.strip()):
            raise ServiceError("invalid_ports", 422)
        bounds = [int(item) for item in part.strip().split("-")]
        start, end = bounds[0], bounds[-1]
        if not 1 <= start <= end <= 65535:
            raise ServiceError("invalid_ports", 422)
        ports.update(range(start, end + 1))
    return sorted(ports)
