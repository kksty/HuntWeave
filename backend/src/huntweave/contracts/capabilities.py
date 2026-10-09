"""What the execution side can actually do, and what still blocks real execution.

ADR-0010 allows `real_execution_ready=true` only while four gates hold at once. This contract
carries the gates themselves, so the state is derived from its conditions instead of being
written into the model: `real_execution_ready` is a real boolean, `reason_code` is nullable, and
a response without a trusted observation says so rather than implying readiness.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReadinessGateName = Literal[
    "profile_revalidation",
    "contract_expressiveness",
    "console_consumption",
    "deployment_revert",
]


class ReadinessGate(BaseModel):
    """One ADR-0010 gate, with the condition that still blocks it when it is unmet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: ReadinessGateName
    ready: bool
    reason_code: str | None = None


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1"] = "1"
    # What this deployment will really do, not what it could do.
    mode: Literal["demonstration", "real"] = "demonstration"
    execution_profile: str = "fake-p0-v1"
    fake_execution_ready: bool = False
    # True only while every ADR-0010 gate holds.
    real_execution_ready: bool = False
    # Null when nothing blocks the platform; otherwise why it is not in its ready state. The
    # per-gate detail below names what a given gate is still missing.
    reason_code: str | None = None
    # Null when no trusted answer was obtained: a missing observation is never readiness.
    observed_at: datetime | None = None
    gates: list[ReadinessGate] = Field(default_factory=list)

    @classmethod
    def unobserved(cls, reason_code: str) -> "Capabilities":
        """No answer from the execution side: report the gap instead of an assumed ready state."""
        return cls(reason_code=reason_code)
