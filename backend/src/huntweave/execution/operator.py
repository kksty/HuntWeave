"""The operator's read of the execution side: containers, gateways and reclaim confirmations.

The console must be able to say "this Run currently has a tool container, it may reach these
endpoints through this gateway, and its stop is/is not confirmed" without gaining any execution
capability. That is what this module is: a projection of the trusted manager's records into the
versioned views in `contracts/execution.py`. It reads; it never starts, stops or reclaims anything.

Two rules shape the projection, and both of them exist to keep a statement honest:

* A stop is reported **confirmed** only where the manager recorded `stop_confirmed_at`. An instance
  that is `reclaimed` without one is *not* a stop — nobody proved what it left behind.
* A resource the runtime has but the manager's ledger does not is reported **unaccounted**, and one
  the ledger has but the runtime lost is reported **missing**. Neither is silently dropped, because
  both are cases where "nothing is running" would be a guess.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from huntweave.contracts.execution import (
    CallStopState,
    RunRuntimeView,
    SessionRuntimeView,
    ToolRuntimeView,
)
from huntweave.execution.sandbox import (
    InstanceRecord,
    ManagedResource,
    SandboxRunState,
    SessionRecord,
)

__all__ = ["project_run_state"]


def _stop_state(record: InstanceRecord) -> CallStopState:
    """The manager's own answer to "is this instance's execution accounted for".

    Only `stop_confirmed_at` makes a stop confirmed. An instance whose creation never finished and
    one whose record was reclaimed without a confirmation both answer "unconfirmed" — in the first
    case because nobody may act on resources held by an unfinished create, in the second because
    removing containers is not proof about what they left behind.
    """
    if record.state in {"creating", "interrupted"}:
        return "unconfirmed"
    if record.state in {"stopped", "reclaimed"}:
        return "confirmed" if record.stop_confirmed_at is not None else "unconfirmed"
    return "running"


def _resource(resource: ManagedResource) -> dict[str, object]:
    return {
        "kind": resource.kind,
        "role": resource.role,
        "name": resource.name,
        "running": resource.running,
    }


def _instance_view(record: InstanceRecord) -> ToolRuntimeView:
    return ToolRuntimeView(
        instance_id=record.instance_id,
        session_id=record.session_id,
        run_id=record.run_id,
        state=record.state,
        stop_state=_stop_state(record),
        environment=record.environment.model_dump(mode="json"),
        egress={
            **record.egress.model_dump(mode="json"),
            # The changes are kept beside the current rules: "what may this reach now" and "when
            # did that change, and why" are two different questions an operator asks in sequence.
            "changes": [change.model_dump(mode="json") for change in record.egress_changes],
            "observation": record.egress_observation,
        },
        lease_expires_at=record.lease_expires_at,
        halt_reason=record.halt_reason,
        resources=[_resource(item) for item in record.resources],
    )


def _session_view(record: SessionRecord) -> SessionRuntimeView:
    return SessionRuntimeView(
        session_id=record.session_id,
        agent_session_id=record.agent_session_id,
        run_id=record.run_id,
        scope_version=record.scope_version,
        policy_version=record.policy_version,
        created_at=record.created_at,
    )


def _reclaimable(records: list[InstanceRecord]) -> list[dict[str, object]]:
    """What a reclaim would touch, so the console previews the act instead of asserting it.

    This is a *preview*: it says which instances have resources left to remove and which are already
    fully accounted for. Nothing here performs a reclaim, and an instance whose stop is unconfirmed
    is listed as outstanding rather than as something a reclaim would safely clear.
    """
    preview: list[dict[str, object]] = []
    for record in records:
        removable = [item for item in record.resources if item.kind != "network"]
        if record.state == "reclaimed":
            continue
        preview.append(
            {
                "instance_id": str(record.instance_id),
                "state": record.state,
                "stop_state": _stop_state(record),
                "resources": [_resource(item) for item in removable],
                "confirmation_required": _stop_state(record) != "confirmed",
            }
        )
    return preview


def project_run_state(
    state: SandboxRunState, *, profile_id: str | None, reason_code: str | None = None
) -> RunRuntimeView:
    """One Run's containers and gateways as the execution side records them right now."""
    instances = sorted(state.instances, key=lambda item: item.created_at)
    sessions: list[SessionRuntimeView] = []
    for session in state.sessions:
        view = _session_view(session)
        view = view.model_copy(
            update={
                "instances": [
                    _instance_view(record)
                    for record in instances
                    if record.session_id == session.session_id
                ]
            }
        )
        sessions.append(view)
    audit = state.audit
    available = audit is not None
    return RunRuntimeView(
        available=available,
        reason_code=None if available else (reason_code or "sandbox_runtime_unreachable"),
        sandbox_management="ready",
        profile_id=profile_id,
        observed_at=datetime.now(UTC),
        run_id=state.run_id,
        sessions=sessions,
        unaccounted=[_resource(item) for item in audit.unaccounted] if audit else [],
        missing=[_resource(item) for item in audit.missing] if audit else [],
        interrupted=list(audit.interrupted) if audit else [],
        reclaimable=_reclaimable(instances),
    )


def unavailable_run_state(
    run_id: UUID,
    *,
    management: Literal["disabled", "ready", "unavailable"],
    reason_code: str | None,
    profile_id: str | None,
) -> RunRuntimeView:
    """The answer for a deployment that has no management capability, or cannot be asked.

    It is a real answer and it is *not* "nothing is running": the console shows this as an
    observation gap, so an operator never reads a missing answer as an empty Run.
    """
    return RunRuntimeView(
        available=False,
        reason_code=reason_code or "sandbox_management_disabled",
        sandbox_management=management,
        profile_id=profile_id,
        observed_at=datetime.now(UTC),
        run_id=run_id,
    )
