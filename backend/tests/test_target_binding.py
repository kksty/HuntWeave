"""Every ticket is bound to the endpoint its own action was authorized for."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.runs import EXECUTION_POLICY_VERSION, Budget, ScopeSnapshot
from huntweave.runs.orchestration import resolve_target


def scope_snapshot(targets: list[str], ports: list[int]) -> ScopeSnapshot:
    now = datetime.now(UTC)
    return ScopeSnapshot(
        targets=targets,
        ports=ports,
        port_profile="custom-tcp-v1",
        starts_at=now,
        expires_at=now + timedelta(hours=1),
        authorization="Fixed fake actions only",
        budget=Budget(),
    )


def refusal(decision: dict, scope: ScopeSnapshot, ordinal: int) -> str:
    with pytest.raises(ServiceError) as error:
        resolve_target(decision, scope, ordinal)
    return error.value.reason_code


def test_a_plan_naming_an_authorized_endpoint_binds_exactly_that_endpoint() -> None:
    scope = scope_snapshot(["192.0.2.7", "192.0.2.8"], [80, 443])
    # Not the first entry of either list: the binding follows the plan, not the snapshot order.
    assert resolve_target(
        {"action": "fake.verify", "target_ip": "192.0.2.8", "target_port": 443}, scope, 0
    ) == ("192.0.2.8", 443)


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "fake.verify", "target_ip": "192.0.2.99", "target_port": 80},
        {"action": "fake.verify", "target_ip": "192.0.2.7", "target_port": 8080},
        {"action": "fake.verify", "target_ip": "192.0.2.7"},
        {"action": "fake.verify", "target_port": 80},
        {"action": "fake.verify", "target_ip": "not-an-address", "target_port": 80},
        {"action": "fake.verify", "target_ip": "192.0.2.7", "target_port": "80"},
    ],
)
def test_a_plan_aimed_outside_the_snapshot_is_refused_rather_than_retargeted(decision) -> None:
    scope = scope_snapshot(["192.0.2.7", "192.0.2.8"], [80, 443])
    assert refusal(decision, scope, 0) == "scope_denied"


def test_a_plan_naming_no_target_takes_the_runs_next_authorized_endpoint() -> None:
    scope = scope_snapshot(["192.0.2.7", "192.0.2.8"], [80, 443])
    bound = [resolve_target({"action": "fake.collect"}, scope, ordinal) for ordinal in range(4)]
    assert bound == [
        ("192.0.2.7", 80),
        ("192.0.2.7", 443),
        ("192.0.2.8", 80),
        ("192.0.2.8", 443),
    ]
    # Successive actions of one Run are distinguishable, and the walk restarts at the head.
    assert len(set(bound)) == 4
    assert resolve_target({"action": "fake.collect"}, scope, 4) == ("192.0.2.7", 80)


def test_a_snapshot_records_the_policy_version_its_tickets_are_fenced_to() -> None:
    scope = scope_snapshot(["192.0.2.7"], [80])
    assert scope.policy_version == EXECUTION_POLICY_VERSION
    # A stored snapshot keeps the policy it was authorized under; the current build does not
    # overwrite it, so an active Run never changes policy underneath its tickets.
    stored = ScopeSnapshot.model_validate({**scope.model_dump(), "policy_version": 1})
    assert stored.policy_version == 1
    with pytest.raises(ValidationError):
        ScopeSnapshot.model_validate({**scope.model_dump(), "policy_version": 0})
