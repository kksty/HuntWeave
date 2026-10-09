"""Evaluate ADR-0010's four readiness gates from facts this build and deployment really have.

Nothing here writes the answer down: `real_execution_ready` is computed from the gates, and the
two gates that are properties of this revision are checked against the artifacts that decide them
(the contract model and the served console bundle) instead of being declared. Every unmet
condition carries a reason code, so an operator sees which one is missing rather than a flat
"unsupported".
"""

from datetime import datetime
from functools import lru_cache
from typing import get_args

from huntweave.config import CONSOLE_BUILD
from huntweave.contracts.capabilities import Capabilities, ReadinessGate, ReadinessGateName

FAKE_PROFILE = "fake-p0-v1"
CONSOLE_CAPABILITY_ENDPOINT = "/api/v1/system/capabilities"

# Gate 1: the real execution profile is revalidated across tickets, leases, cancellation, resource
# reclamation and evidence archival. That revalidation is P1 work (issues #16 and #18); until a
# recorded revalidation exists, no host may claim the gate. These two placeholders are the facts
# those slices replace, each with the record that will hold the answer; neither is a deployment
# flag, so no environment variable can open real execution on its own.
PROFILE_REVALIDATED = False

# Gate 4: a revert entry that stops new real calls, reclaims and reconciles them before the
# management capability is withdrawn (spec 0002 section 3.5). The default deployment keeps fake
# execution today; the revert entry arrives with the real execution slices.
REVERT_ENTRY_AVAILABLE = False


def _contract_is_expressible(model: type[Capabilities] = Capabilities) -> bool:
    """Gate 2: the contract can say "not ready" without pinning the state itself.

    Checked against the model, so re-pinning `real_execution_ready` to a literal or making the
    reason mandatory fails this gate instead of passing quietly.
    """
    ready = model.model_fields["real_execution_ready"].annotation is bool
    reason = model.model_fields["reason_code"].annotation
    return ready and reason is not None and type(None) in get_args(reason)


@lru_cache(maxsize=1)
def _console_consumes_capabilities() -> bool:
    """Gate 3: the console served by this build asks for this answer.

    The bundle is the artifact that has to consume the endpoint, so the gate reads it instead of
    asserting the intent. A missing build leaves the gate unmet, which keeps the closed state.
    """
    return any(
        CONSOLE_CAPABILITY_ENDPOINT in bundle.read_text(encoding="utf-8", errors="ignore")
        for bundle in sorted(CONSOLE_BUILD.glob("assets/*.js"))
    )


def _gate(name: ReadinessGateName, ready: bool, reason_code: str) -> ReadinessGate:
    return ReadinessGate(gate=name, ready=ready, reason_code=None if ready else reason_code)


def evaluate(*, fake_execution_ready: bool, now: datetime) -> Capabilities:
    """Report readiness from the four gates rather than from a constant.

    Gates 2 and 3 are properties of this revision, not of the running host, so they are checked
    against the contract model and the shipped console bundle. Real execution also runs through
    the same execution side as the fixed fake chain, so an unusable chain withholds readiness even
    when every gate holds (ADR-0010 makes the four gates a requirement, not the only one).

    The platform stays in demonstration mode whenever anything is unmet, and `reason_code` names
    the first blocker in the order an operator has to care about: an unusable execution chain
    before an unopened real path (ADR-0010 keeps `environment_unsupported` as the P1 conclusion,
    with the gates carrying the specific missing condition).
    """
    gates = [
        _gate("profile_revalidation", PROFILE_REVALIDATED, "profile_unvalidated"),
        _gate("contract_expressiveness", _contract_is_expressible(), "contract_not_expressible"),
        _gate("console_consumption", _console_consumes_capabilities(), "console_not_consuming"),
        _gate("deployment_revert", REVERT_ENTRY_AVAILABLE, "revert_path_missing"),
    ]
    real_ready = fake_execution_ready and all(gate.ready for gate in gates)
    if not fake_execution_ready:
        reason = "runner_state_unavailable"
    elif not real_ready:
        reason = "environment_unsupported"
    else:
        reason = None
    return Capabilities(
        mode="real" if real_ready else "demonstration",
        execution_profile=FAKE_PROFILE,
        fake_execution_ready=fake_execution_ready,
        real_execution_ready=real_ready,
        reason_code=reason,
        observed_at=now,
        gates=gates,
    )
