from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# The version of the authorized-execution policy a ticket is fenced to. It is recorded inside
# the authorization snapshot, so every Run keeps the policy it was authorized under. Bump it
# when the action set, the profiles, the protected-address rules or the exit rules change in a
# way that is not interchangeable with the previous version.
EXECUTION_POLICY_VERSION = 1


class LoginRequest(Contract):
    access_key: SecretStr = Field(min_length=1, max_length=512)


class TargetInput(Contract):
    text: str = Field(max_length=200_000)


class TargetRow(Contract):
    line: int
    value: str
    normalized: str | None = None
    reason_code: str | None = None
    duplicate_of: int | None = None


class TargetPreview(Contract):
    targets: list[str]
    rows: list[TargetRow]
    valid: bool
    ipv6_enabled: Literal[False] = False


class PortInput(Contract):
    profile: Literal["common-tcp-v1", "custom-tcp-v1", "all-tcp-v1"] = "common-tcp-v1"
    custom: str = Field(default="", max_length=200_000)


class Budget(Contract):
    max_tool_calls: int = Field(default=50, ge=1, le=1000, strict=True)
    max_tokens: int = Field(default=100_000, ge=1, le=1_000_000, strict=True)
    max_wall_seconds: int = Field(default=3600, ge=1, le=86400, strict=True)
    max_output_bytes: int = Field(default=33_554_432, ge=1, le=1_073_741_824, strict=True)
    max_concurrency: int = Field(default=4, ge=1, le=32, strict=True)


class ProjectCreate(Contract):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class ProjectView(ProjectCreate):
    id: UUID
    created_at: datetime


class ScopeCreate(Contract):
    project_id: UUID
    targets_text: str = Field(min_length=1, max_length=200_000)
    ports: PortInput = Field(default_factory=PortInput)
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    authorization: str = Field(min_length=1, max_length=2000)
    budget: Budget = Field(default_factory=Budget)
    mode: Literal["demonstration"] = "demonstration"
    execution_profile: Literal["fake-p0-v1"] = "fake-p0-v1"
    config_version: Literal["p0-b-v1"] = "p0-b-v1"


class ScopeSnapshot(Contract):
    targets: list[str]
    transport: Literal["tcp"] = "tcp"
    ports: list[int]
    port_profile: str
    starts_at: datetime
    expires_at: datetime
    authorization: str
    budget: Budget
    mode: Literal["demonstration"] = "demonstration"
    execution_profile: Literal["fake-p0-v1"] = "fake-p0-v1"
    config_version: Literal["p0-b-v1"] = "p0-b-v1"
    # Recorded at authorization, never re-read from the current build: an active Run must not
    # silently change policy while it still holds tickets minted under the old one.
    policy_version: int = Field(default=EXECUTION_POLICY_VERSION, ge=1, strict=True)


class ScopeView(Contract):
    id: UUID
    project_id: UUID
    version: int
    snapshot: ScopeSnapshot
    created_at: datetime


class RunCreate(Contract):
    scope_id: UUID
    scope_version: int = Field(ge=1, strict=True)
    demonstration_scenario: Literal["positive", "negative", "failure"] = "positive"
    demonstration_duration_ms: int = Field(default=1500, ge=0, le=30000, strict=True)


class VersionRequest(Contract):
    version: int = Field(ge=1, strict=True)


class RunView(Contract):
    id: UUID
    project_id: UUID
    scope_id: UUID
    scope_version: int
    scope_snapshot: ScopeSnapshot
    status: Literal[
        "draft",
        "queued",
        "running",
        "waiting",
        "pausing",
        "paused",
        "recovering",
        "cancelling",
        "closed",
        "cancelled",
        "failed",
    ]
    phase: Literal["collecting", "researching", "reviewing", "awaiting_human"] | None = None
    version: int
    created_at: datetime
    demonstration: Literal[True] = True
    # Observed per response, never assumed: a view that has not asked the execution side must not
    # claim its chain is ready.
    execution_ready: bool = False
    demonstration_scenario: Literal["positive", "negative", "failure"] = "positive"
    started_at: datetime | None = None
    reason_code: str | None = None
    demonstration_duration_ms: int = 1500
