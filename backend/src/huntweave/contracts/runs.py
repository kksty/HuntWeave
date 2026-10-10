from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    computed_field,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# The version of the authorized-execution policy a ticket is fenced to. It is recorded inside
# the authorization snapshot, so every Run keeps the policy it was authorized under. Bump it
# when the action set, the profiles, the protected-address rules or the exit rules change in a
# way that is not interchangeable with the previous version.
EXECUTION_POLICY_VERSION = 1

# The version of the physical-execution resource policy a Run's slots are measured against: the
# global execution quota, the per-address limit and the control dispatch slots. It is recorded
# inside the authorization snapshot for the same reason `EXECUTION_POLICY_VERSION` is, and bumping
# it is what `PROJECT.md` section 12 asks for when one of those defaults changes. Bump it when the
# slot counts or their meaning change; the contract and the rule that reads them live in
# `huntweave.contracts.resources`.
RESOURCE_POLICY_VERSION = 1


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
    # The per-Run limit this preview was measured against, and how many rows crossed it. Sent with
    # the row-level reasons so the console can point at the offending lines instead of showing an
    # operator a count and a 422.
    target_limit: int = Field(ge=1, strict=True)
    over_limit: int = Field(default=0, ge=0, strict=True)


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
    # The authorization itself says whether it permits real execution. A Run inherits this and is
    # refused if it asks for a different side than the scope it acts under.
    mode: Literal["demonstration", "real"] = "demonstration"
    execution_profile: Literal["fake-p0-v1", "real-lab-v1"] = "fake-p0-v1"
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
    mode: Literal["demonstration", "real"] = "demonstration"
    execution_profile: Literal["fake-p0-v1", "real-lab-v1"] = "fake-p0-v1"
    config_version: Literal["p0-b-v1"] = "p0-b-v1"
    # Recorded at authorization, never re-read from the current build: an active Run must not
    # silently change policy while it still holds tickets minted under the old one.
    policy_version: int = Field(default=EXECUTION_POLICY_VERSION, ge=1, strict=True)
    # Recorded at authorization beside the execution policy version, and never re-read from the
    # current build: a Run keeps the quota it was opened under, so changing a deployment's slot
    # counts mid-flight is a versioned act rather than a silent re-pricing of a live Run. The
    # version identifies the numbers; the numbers themselves are the deployment's, read once per
    # process from `HUNTWEAVE_GLOBAL_EXECUTION_SLOTS` / `HUNTWEAVE_PER_IP_EXECUTION_SLOTS`.
    resource_policy_version: int = Field(default=RESOURCE_POLICY_VERSION, ge=1, strict=True)


class ScopeView(Contract):
    id: UUID
    project_id: UUID
    version: int
    snapshot: ScopeSnapshot
    created_at: datetime


class RunCreate(Contract):
    scope_id: UUID
    scope_version: int = Field(ge=1, strict=True)
    # What this Run will really do. Demonstration is the default, and asking for real execution is
    # only accepted while the execution side reports it is ready (ADR-0010's four gates); a Run
    # fixes its mode here and never switches sides afterwards.
    execution_profile: Literal["fake-p0-v1", "real-lab-v1"] = "fake-p0-v1"
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
    # Derived from the Run's own execution profile, never assumed: a real Run is not a
    # demonstration, and the interface says which one this is.
    execution_profile: Literal["fake-p0-v1", "real-lab-v1"] = "fake-p0-v1"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def demonstration(self) -> bool:
        return self.execution_profile == "fake-p0-v1"
    # Observed per response, never assumed: a view that has not asked the execution side must not
    # claim its chain is ready.
    execution_ready: bool = False
    demonstration_scenario: Literal["positive", "negative", "failure"] = "positive"
    started_at: datetime | None = None
    reason_code: str | None = None
    demonstration_duration_ms: int = 1500
