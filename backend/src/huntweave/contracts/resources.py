"""Versioned physical-execution quota, and the thresholds a concurrency fixture is fixed to.

ADR-0016 separates the control dispatch slots from the physical execution quota. Cancelling,
renewing, reconciling and reclaiming never queue behind a target execution, while a global quota
(4 by default) and a per-target-address limit (1 by default) constrain active actions together,
across Runs. `PROJECT.md` section 12 calls those numbers adjustable conservative defaults and
requires a version record when they change, so they are one versioned contract here rather than
literals spread over the scheduler.

The same module carries the thresholds issue #21 is judged against. They are here for one reason:
the limits on response, waiting and growth have to be fixed *before* a load fixture runs, so a
result cannot be met by moving the standard afterwards. A fixture reads them from here and reports
the version it measured against; nothing in the product's behaviour depends on them.
"""

import os
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from huntweave.contracts.runs import RESOURCE_POLICY_VERSION, Contract

__all__ = [
    "ACCEPTANCE_THRESHOLDS_VERSION",
    "LIVE_SLOT_SQL",
    "STOP_FIELDS",
    "ConcurrencyAcceptanceThresholds",
    "ExecutionQuota",
    "ResourcePolicy",
    "SlotWait",
    "WaitCategory",
    "holds_physical_slot",
    "slot_wait",
    "stop_confirmed",
]

# Bump when a threshold below changes. A validation record names the version it measured against,
# which is what makes "the standard did not move" a checkable claim rather than a promise.
ACCEPTANCE_THRESHOLDS_VERSION = 1

# Why a planned action is not being offered to the execution side yet. These are ADR-0016 wait
# categories, not refusals: the Run keeps its place, its budget and its lease, and the same action
# is offered again as soon as the slot it needs is returned. They travel in `waiting_category` and
# never in `reason_code`, because nothing was refused and no recovery condition was missed.
WaitCategory = Literal["global_execution_slot", "target_execution_slot"]

# The three fields that together answer "is anything this call started still running". They are the
# single definition of that question: `stop_confirmed` and `LIVE_SLOT_SQL` are both built from this
# tuple, so adding a field is one edit rather than two. What cannot be shared between Python and SQL
# is the *operator* on each field, which is why the two spellings are compared record by record in
# `tests/test_execution_quota.py` rather than by reading them.
STOP_FIELDS: tuple[str, ...] = ("process_active", "connection_open", "lease_active")

# The "still holds a physical slot" rule of `holds_physical_slot`, as text the database evaluates.
# Read by `OrchestrationService._execution_quota()` — the occupancy query — and by nothing else.
# `IS DISTINCT FROM` rather than `<>` on purpose: a missing or null field is *not* the execution
# side saying `false`, and reading it as one would release a slot on the strength of a field
# nobody wrote.
LIVE_SLOT_SQL = "(" + " OR ".join(
    [
        "observation IS NULL",
        *(f"observation->>'{field}' IS DISTINCT FROM 'false'" for field in STOP_FIELDS),
    ]
) + ")"


def stop_confirmed(observation: Mapping[str, Any] | None) -> bool:
    """Whether the execution side proved that nothing this call started is still running.

    The process, the connection and the old control lease are one answer: a record that reports
    ``None`` for any of them is saying it cannot confirm, which is not a stop. A shell may have left
    descendants behind, and a live lease can still authorise the call the Run is trying to leave
    behind. An operator's verdict never stands in for this fact.

    Read in production through `OrchestrationService._stop_confirmed` (a re-dispatch's precondition
    and a reconciliation verdict's basis), through `holds_physical_slot` where a call's outstanding
    conditions are decided, and by the record-by-record comparison against `LIVE_SLOT_SQL`.
    """
    if observation is None:
        return False
    return all(observation.get(field) is False for field in STOP_FIELDS)


def holds_physical_slot(observation: Mapping[str, Any] | None) -> bool:
    """Whether a call still occupies a physical execution slot.

    A settled result, a timeout and an expired lease are all in this set: none of them is a fact
    about the process. Only a stop the execution side itself confirmed returns the slot, which is
    also why a call whose outcome is ``unknown`` keeps holding it — and why the run that ended with
    such a call is limited rather than free.

    Read in production where a call's outstanding conditions are decided
    (`OrchestrationService._call_conditions`, where a call that keeps a physical slot is exactly a
    call whose stop is unconfirmed) and by the record-by-record comparison against `LIVE_SLOT_SQL`.
    """
    return not stop_confirmed(observation)


def _slot_count(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


class ResourcePolicy(Contract):
    """How many physical executions this deployment admits at once, and which version says so.

    Read once per process from the environment, and deliberately **not** frozen per Run: the limits
    in force are whatever this deployment resolves now, so a restart with different slot counts
    re-prices Runs that are already running. `ScopeSnapshot.resource_policy_version` records the
    version a Run was opened under as an audit trace, not as a value anything reads back. See
    `docs/validation/0022-concurrency-quota.md` for why that is accepted here and when it would have
    to become a per-Run freeze instead.
    """

    policy_version: int = Field(default=RESOURCE_POLICY_VERSION, ge=1, strict=True)
    # Booked separately and deliberately not derived from the physical quota: a deployment whose
    # targets are all occupied must still be able to cancel, renew, reconcile and reclaim. There is
    # no dispatch queue in this build — those paths are direct calls — so nothing enforces this
    # number today; it records the design ADR-0016 fixes, and the property it stands for is checked
    # by measuring those paths while every physical slot is held.
    control_dispatch_slots: int = Field(default=4, ge=1, strict=True)
    global_execution_slots: int = Field(default=4, ge=1, strict=True)
    per_ip_execution_slots: int = Field(default=1, ge=1, strict=True)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "ResourcePolicy":
        """The deployment's slot counts, read once per process.

        A bad value is refused rather than clamped: a deployment that asked for a negative quota has
        a configuration mistake, and silently serving a different one would hide it.
        """
        source = os.environ if environ is None else environ
        return cls(
            control_dispatch_slots=_slot_count(
                source, "HUNTWEAVE_CONTROL_DISPATCH_SLOTS", 4
            ),
            global_execution_slots=_slot_count(source, "HUNTWEAVE_GLOBAL_EXECUTION_SLOTS", 4),
            per_ip_execution_slots=_slot_count(
                source, "HUNTWEAVE_PER_IP_EXECUTION_SLOTS", 1
            ),
        )


class ExecutionQuota(Contract):
    """How much physical execution capacity is free right now, and where it is being used."""

    policy_version: int = Field(ge=1, strict=True)
    global_limit: int = Field(ge=1, strict=True)
    per_ip_limit: int = Field(ge=1, strict=True)
    global_used: int = Field(ge=0, strict=True)
    # Held slots per authorized address. Only the addresses that hold one appear, so an empty
    # mapping is the deployment saying "nothing is running", not "nothing was counted".
    per_ip_used: dict[str, int] = Field(default_factory=dict)

    @property
    def global_available(self) -> int:
        return max(0, self.global_limit - self.global_used)


class SlotWait(Contract):
    """Which slot refuses one more action, and what has to happen before it is offered again."""

    category: WaitCategory
    # The address whose slot is wanted, for a per-target wait. `None` means the whole pool is full,
    # which is not about any one address.
    resource: str | None = None
    holders: int = Field(ge=0, strict=True)
    limit: int = Field(ge=1, strict=True)
    recovery_condition: str


def slot_wait(quota: ExecutionQuota, targets: Sequence[str]) -> SlotWait | None:
    """The first slot that refuses one more action on these targets, or ``None`` if none does.

    The global quota is checked before any address: it is shared by every Run, so it is the limit an
    operator would act on first, and a Run must not be told to wait for one address while the whole
    pool is full. Every actual target is then checked rather than a navigation anchor, in a stable
    order, so two callers that touch the same set cannot disagree about which of them is refused.
    A repeated address is checked once, and a caller offering several addresses gets one refusal
    rather than several: the Run waits for the first slot it cannot have.
    """
    if quota.global_used >= quota.global_limit:
        return SlotWait(
            category="global_execution_slot",
            holders=quota.global_used,
            limit=quota.global_limit,
            recovery_condition=(
                "Wait for a running call to be confirmed stopped. A settled result, a timeout or "
                "an expired lease does not return a physical slot, so this is not a queue the Run "
                "can leave by ending its own call."
            ),
        )
    for address in sorted(set(targets)):
        used = quota.per_ip_used.get(address, 0)
        if used >= quota.per_ip_limit:
            return SlotWait(
                category="target_execution_slot",
                resource=address,
                holders=used,
                limit=quota.per_ip_limit,
                recovery_condition=(
                    f"Wait for the call holding {address} to be confirmed stopped. One address "
                    "runs one active action at a time across every Run, controlled reproduction "
                    "included."
                ),
            )
    return None


class ConcurrencyAcceptanceThresholds(Contract):
    """The limits issue #21's load fixture is judged against, fixed before it runs.

    Verification thresholds, not product behaviour: nothing in the control plane reads them. They
    live in a versioned contract so a result cannot be met by editing the standard afterwards — the
    fixture prints ``thresholds_version`` beside its numbers, and the validation record names the
    same version.
    """

    thresholds_version: int = Field(default=ACCEPTANCE_THRESHOLDS_VERSION, ge=1, strict=True)
    # A control action — a cancel request, a lease renewal, a reconciliation pass — has to answer
    # within this many seconds while every physical slot is held by an unconfirmed stop.
    control_path_seconds: float = Field(default=5.0, gt=0)
    # A Run with work ready and a free slot has to be claimed within this many seconds while another
    # Run holds a long call. This is the bound on "one Run must not starve the others": the fixture
    # measures the wall-clock wait of a ready Run behind `admitted` holders and asserts this.
    scheduling_wait_seconds: float = Field(default=3.0, gt=0)
    # Growth per unit of ledger volume rather than an absolute budget: the scheduler's read of the
    # ledger may get no more than this many milliseconds slower per 1000 calls already recorded. An
    # absolute ceiling would be met by a fast machine with a bad shape, and what the bound is for is
    # catching super-linear growth rather than certifying a speed. Two microseconds per historical
    # call is loose for a scan and would still catch anything worse than linear.
    pass_growth_ms_per_1000_calls: float = Field(default=2.0, gt=0)
    # Neither of these is a budget to stay inside: issue #21 names both as hard zeros. An unknown
    # outcome acted on again is a wrong retry; a slot given back without the execution side saying
    # the call stopped is a wrong release. The fixture measures both and asserts equality.
    unknown_retries_max: int = Field(default=0, ge=0, strict=True)
    wrong_releases_max: int = Field(default=0, ge=0, strict=True)
