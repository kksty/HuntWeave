"""Strict, versioned control-plane tickets for demonstration execution."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, IPvAnyAddress

from huntweave.contracts.runs import Contract


class FakeParameters(Contract):
    scenario: Literal["success", "failure", "needs_evidence"] = "success"
    duration_ms: int = Field(default=1500, ge=0, le=30000, strict=True)


def parameters_hash(parameters: FakeParameters | dict[str, Any]) -> str:
    value = parameters.model_dump() if isinstance(parameters, FakeParameters) else parameters
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ExecutionRequest(Contract):
    protocol_version: Literal["1"] = "1"
    call_id: UUID
    run_id: UUID
    session_id: UUID
    decision_id: UUID
    scope_id: UUID
    budget_reservation_id: UUID
    action_id: Literal["fake.collect", "fake.verify", "fake.review"]
    parameters: FakeParameters
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_version: int = Field(ge=1, strict=True)
    policy_version: int = Field(ge=1, strict=True)
    lease_generation: int = Field(ge=1, strict=True)
    lease_expires_at: AwareDatetime
    deadline_at: AwareDatetime
    authorized_until: AwareDatetime
    profile: Literal["fake-p0-v1"] = "fake-p0-v1"
    target_ip: IPvAnyAddress
    target_port: int = Field(ge=1, le=65535, strict=True)


class ExecutionEvidence(Contract):
    id: UUID
    relative_path: str
    sha256: str
    size_bytes: int
    available: bool
    truncated: bool = False
    redacted: bool = False
    missing_reason: str | None = None


class ExecutionResult(Contract):
    output: str
    exit_code: int | None
    evidence: list[ExecutionEvidence]


class ExecutionEvent(Contract):
    source_event_id: str
    type: str
    payload: dict[str, Any]
    created_at: AwareDatetime


class ExecutionRecord(Contract):
    request: ExecutionRequest
    status: Literal["accepted", "running", "completed", "failed", "cancelled", "unknown"]
    reason_code: str | None = None
    result: ExecutionResult | None = None
    events: list[ExecutionEvent] = Field(default_factory=list)


class LeaseRenewal(Contract):
    lease_generation: int = Field(ge=1, strict=True)
    lease_expires_at: AwareDatetime


class CallCancellation(Contract):
    lease_generation: int = Field(ge=1, strict=True)
