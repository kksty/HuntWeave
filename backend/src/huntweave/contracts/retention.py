"""Versioned views for selective retention (spec 0002 section 3.8, PROJECT.md section 10.5).

P1 keeps one private, project-managed artifact per execution instance: the workspace volume the
only tool container mounted. When a call ends, that workspace is not destroyed on the spot — it
becomes *private reproduction material* under a retention policy with a TTL, a capacity, explicit
pins and a reclaim preview. This module is what the console reads and what the execution side
answers with; it carries no capability of its own.

Two things these contracts deliberately cannot say:

* Nothing here publishes. ``shared_tool_version`` exists so a reader can see that no artifact was
  promoted into a shared tool version, and it is never true in P1 — promoting requires rebuilding
  from a fixed source in a clean preparation environment (P2).
* A size may be ``None``. The engine does not always report what a volume holds; an unmeasured
  artifact is counted as unmeasured rather than as zero, and capacity is never claimed on it.
"""

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from huntweave.contracts.capabilities import SandboxManagement
from huntweave.contracts.runs import Contract

ArtifactKind = Literal["workspace"]
ArtifactState = Literal["retained", "deleted"]
RetentionAction = Literal["pin", "unpin", "delete", "sweep"]
RetentionTarget = Literal["version", "artifact", "deployment"]

# The thresholds of PROJECT.md section 10.5, versioned here so a view states the rule it was
# computed under instead of leaving a reader to guess which numbers produced a candidate.
DEFAULT_CANDIDATE_WINDOW_DAYS = 30
DEFAULT_CANDIDATE_RUNS = 3
DEFAULT_CACHE_TTL_DAYS = 7
DEFAULT_CACHE_CAPACITY_BYTES = 10 * 1024**3


class RetentionLimits(Contract):
    """The policy one retention view was computed under, carried with it."""

    candidate_window_days: int = Field(default=DEFAULT_CANDIDATE_WINDOW_DAYS, ge=1, strict=True)
    candidate_runs: int = Field(default=DEFAULT_CANDIDATE_RUNS, ge=1, strict=True)
    cache_ttl_days: int = Field(default=DEFAULT_CACHE_TTL_DAYS, ge=1, strict=True)
    cache_capacity_bytes: int = Field(default=DEFAULT_CACHE_CAPACITY_BYTES, ge=0, strict=True)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RetentionLimits":
        """The policy this deployment runs under, defaulting to PROJECT.md section 10.5.

        The numbers are the conservative development defaults, and PROJECT.md allows adjusting them
        per machine; a deployment states its own in the environment, and both the Execution side and
        the console read the same names so the view always says which policy produced it.
        """
        values = os.environ if environ is None else environ

        def number(name: str, default: int) -> int:
            raw = values.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None

        return cls(
            candidate_window_days=number(
                "HUNTWEAVE_RETENTION_WINDOW_DAYS", DEFAULT_CANDIDATE_WINDOW_DAYS
            ),
            candidate_runs=number("HUNTWEAVE_RETENTION_CANDIDATE_RUNS", DEFAULT_CANDIDATE_RUNS),
            cache_ttl_days=number("HUNTWEAVE_RETENTION_TTL_DAYS", DEFAULT_CACHE_TTL_DAYS),
            cache_capacity_bytes=number(
                "HUNTWEAVE_RETENTION_CAPACITY_BYTES", DEFAULT_CACHE_CAPACITY_BYTES
            ),
        )


class RetainedArtifact(Contract):
    """One private artifact the platform still holds for a finished execution instance."""

    artifact_id: UUID
    environment_key: str
    run_id: UUID
    session_id: UUID
    instance_id: UUID
    kind: ArtifactKind = "workspace"
    resource_name: str
    # ``None`` means the engine did not report a size. It is not a zero and is never summed as one.
    size_bytes: int | None = Field(default=None, ge=0)
    retained_at: AwareDatetime
    expires_at: AwareDatetime
    pinned: bool = False
    state: ArtifactState = "retained"
    deleted_at: AwareDatetime | None = None
    # Why it went away: an operator's delete, the TTL, or a capacity reclaim.
    delete_reason: str | None = None


class ToolVersionView(Contract):
    """One tool/environment version and how many independent Runs really used it.

    ``successful_runs`` counts distinct Runs inside the window, so retrying one action — or running
    it ten times in the same Run — never makes a version look high-frequency (PROJECT.md section
    10.5, ADR-0005).
    """

    environment_key: str
    profile_id: str
    profile_version: int
    image_digests: dict[str, str] = Field(default_factory=dict)
    tool_inventory: list[str] = Field(default_factory=list)
    engine: str
    architecture: str
    successful_runs: int = Field(default=0, ge=0, strict=True)
    # Every successfully finished call of the window, retries included. Shown beside the distinct
    # count so a reader can see that repetitions were counted but never made a version a candidate.
    successful_calls: int = Field(default=0, ge=0, strict=True)
    window_days: int
    threshold_runs: int
    candidate: bool = False
    last_used_at: AwareDatetime | None = None
    retained_artifacts: int = Field(default=0, ge=0, strict=True)
    retained_bytes: int = Field(default=0, ge=0, strict=True)
    unmeasured_artifacts: int = Field(default=0, ge=0, strict=True)
    pinned: bool = False
    # P1 never publishes a private workspace as a shared tool version. Kept explicit so the answer
    # to "was this promoted?" is on the record rather than inferred from a missing field.
    shared_tool_version: bool = False


class ProtectedArtifact(Contract):
    """One artifact a reclaim would refuse to touch, and the rule that protects it."""

    artifact_id: UUID
    resource_name: str
    run_id: UUID
    reason_code: str


class RetentionPreview(Contract):
    """What a reclaim would delete, what it would leave, and whether capacity can be met at all."""

    capacity_bytes: int = Field(ge=0, strict=True)
    ttl_days: int = Field(ge=1, strict=True)
    retained_bytes: int = Field(default=0, ge=0, strict=True)
    unmeasured_artifacts: int = Field(default=0, ge=0, strict=True)
    to_delete: list[RetainedArtifact] = Field(default_factory=list)
    protected: list[ProtectedArtifact] = Field(default_factory=list)
    freed_bytes: int = Field(default=0, ge=0, strict=True)
    # True when even reclaiming everything reclaimable leaves the cache over capacity: the answer
    # is then a block with the shortfall, not a partial delete dressed up as success.
    blocked: bool = False
    reason_code: str | None = None
    needed_bytes: int = Field(default=0, ge=0, strict=True)


class RetentionDecisionView(Contract):
    """One recorded decision about retention, business-side and durable.

    The execution ledger keeps its own record of what was removed; this is the operator-facing
    trace: who asked, for what, and which Runs it touched.
    """

    decision_id: UUID
    action: RetentionAction
    target_kind: RetentionTarget
    target: str
    at: AwareDatetime
    actor: str | None = None
    note: str | None = None
    reason_code: str | None = None
    affected_runs: list[UUID] = Field(default_factory=list)
    freed_bytes: int = Field(default=0, ge=0, strict=True)
    deleted_artifacts: list[UUID] = Field(default_factory=list)
    # Removals the execution side refused, as ``<reason_code>:<artifact_id>``, so a decision never
    # reads as more successful than it was.
    failed: list[str] = Field(default_factory=list)


class RetentionView(Contract):
    """Everything the execution side can say about retention right now.

    ``available=false`` with a reason is a real answer: a deployment with no management capability,
    or a Runner that cannot be reached, must not be rendered as a cache with nothing in it.
    """

    available: bool
    reason_code: str | None = None
    sandbox_management: SandboxManagement = "disabled"
    observed_at: AwareDatetime
    limits: RetentionLimits = Field(default_factory=RetentionLimits)
    versions: list[ToolVersionView] = Field(default_factory=list)
    artifacts: list[RetainedArtifact] = Field(default_factory=list)
    preview: RetentionPreview
    decisions: list[RetentionDecisionView] = Field(default_factory=list)

    @classmethod
    def unavailable(
        cls,
        reason_code: str | None,
        *,
        management: SandboxManagement = "unavailable",
        limits: RetentionLimits | None = None,
    ) -> "RetentionView":
        """No trusted retention observation: state the gap, never an empty cache.

        The limits stay honest — they are the policy this deployment runs under, not the execution
        side's answer — while every list is empty, so "we could not ask" cannot be read as "nothing
        is retained".
        """
        policy = limits or RetentionLimits.from_env()
        return cls(
            available=False,
            reason_code=reason_code,
            sandbox_management=management,
            observed_at=datetime.now(UTC),
            limits=policy,
            preview=RetentionPreview(
                capacity_bytes=policy.cache_capacity_bytes, ttl_days=policy.cache_ttl_days
            ),
        )


class RetentionRequest(Contract):
    """The operator's own words about one retention action. It authorizes nothing by itself."""

    note: str = Field(default="", max_length=500)
    actor: str | None = Field(default=None, max_length=120)


class RetentionReport(Contract):
    """What one retention action did, plus the view it leaves behind."""

    decision: RetentionDecisionView
    deleted: list[UUID] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    view: RetentionView
