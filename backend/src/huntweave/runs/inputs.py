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
    targets: dict[str, int] = {}
    rows: list[TargetRow] = []
    invalid = False
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
    if len(targets) > TARGET_LIMIT:
        raise ServiceError("target_limit_exceeded", 422)
    return TargetPreview(targets=list(targets), rows=rows, valid=bool(targets) and not invalid)


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
