"""The physical-execution quota rule, checked at the value level, and where it cannot be.

Two things are being defended here. The first is the *release rule*: a physical slot comes back only
on a statement from the execution side that the process and the connection are accounted for and no
control lease is live. A settled result, a timeout and an expired lease are none of those, and the
acceptance table of spec 0006 section 11 lists reading any of them as a stop as something that must
be prevented. The second is that the Python rule and the SQL predicate the occupancy query counts
with say the same thing: two spellings of one decision, and the check that matters evaluates both on
the same constructed records rather than comparing their text.

The value-level comparison needs a database to evaluate `LIVE_SLOT_SQL`, so it runs where a
disposable PostgreSQL is available (the `integration` runs). A structural assertion beside it covers
the one inversion that is visible without one — a flipped SQL operator. Field *names* are not
compared because there is only one copy of them: `STOP_FIELDS` builds both spellings.
"""

import inspect
import json
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from huntweave.contracts.resources import (
    ACCEPTANCE_THRESHOLDS_VERSION,
    LIVE_SLOT_SQL,
    STOP_FIELDS,
    ConcurrencyAcceptanceThresholds,
    ExecutionQuota,
    ResourcePolicy,
    holds_physical_slot,
    slot_wait,
    stop_confirmed,
)
from huntweave.contracts.runs import RESOURCE_POLICY_VERSION, Budget, ScopeSnapshot
from huntweave.storage.database import connect_engine

REPOSITORY = Path(__file__).resolve().parents[2]

CONFIRMED_STOP = {
    "started": True,
    "process_active": False,
    "connection_open": False,
    "lease_active": False,
    "observed_at": "2026-10-11T00:00:00+00:00",
}


def observation(**changes: object) -> dict[str, object]:
    return {**CONFIRMED_STOP, **changes}


def test_only_a_confirmed_stop_returns_a_physical_slot() -> None:
    assert stop_confirmed(CONFIRMED_STOP) is True
    assert holds_physical_slot(CONFIRMED_STOP) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("process_active", True),
        ("process_active", None),
        ("connection_open", True),
        ("connection_open", None),
        ("lease_active", True),
        ("lease_active", None),
    ],
)
def test_an_unstated_or_live_field_is_not_a_stop(field: str, value: object) -> None:
    """`None` is the ledger saying it cannot confirm, which is not the same as confirming."""
    assert holds_physical_slot(observation(**{field: value})) is True


def test_a_record_with_no_observation_holds_its_slot() -> None:
    """A call nobody has said anything about is a call that may be running."""
    assert holds_physical_slot(None) is True
    assert holds_physical_slot({}) is True
    assert holds_physical_slot({"started": False, "process_active": False}) is True


@pytest.mark.parametrize(
    "outcome,ended",
    [
        ("succeeded", True),
        ("failed", True),
        ("cancelled", True),
        ("unknown", True),
    ],
)
def test_settling_a_result_does_not_release_capacity(outcome: str, ended: bool) -> None:
    """The outcome and the stop are separate facts: only the second one returns a slot.

    A call that ended with an outcome while its process is unaccounted for is the case the
    acceptance table calls out by name ("结果 succeeded 但停止未确认也不能释放物理额度"), and an
    `unknown` outcome with a confirmed stop is the one case that does release it.
    """
    assert ended
    unaccounted = observation(process_active=None, connection_open=None)
    assert holds_physical_slot(unaccounted) is True
    assert holds_physical_slot(observation()) is False


def test_the_python_rule_reads_the_one_shared_field_list() -> None:
    """The field list has one definition, so there is no second copy for a name check to compare.

    `stop_confirmed` and `LIVE_SLOT_SQL` are both built from `STOP_FIELDS`. What can still drift is
    the *operator* on each field, which is what the value-level comparison below is for; this keeps
    the field list itself from being re-inlined into one of the two spellings.
    """
    source = inspect.getsource(stop_confirmed)
    assert "STOP_FIELDS" in source
    assert not re.search(r'\.get\("[a-z_]+"\)', source)
    assert sorted(re.findall(r"observation->>'([a-z_]+)'", LIVE_SLOT_SQL)) == sorted(STOP_FIELDS)
    # `<>` would treat a missing field as NULL and quietly drop the row; `IS NOT DISTINCT FROM`
    # would invert the whole rule. Both are caught by value below where a database is available;
    # this is the spelling that is caught even where it is not.
    assert "<>" not in LIVE_SLOT_SQL
    assert "IS NOT DISTINCT FROM" not in LIVE_SLOT_SQL


# Observations chosen for what each one does to the *rule*, not for what a ledger usually says: a
# record nobody wrote, an empty record, a record missing exactly the field that is easiest to
# forget, and records that are half, wholly and not at all stopped.
DRIFT_CASES: list[tuple[str, dict[str, object] | None]] = [
    ("no statement at all", None),
    ("empty statement", {}),
    ("lease field missing", {key: value for key, value in CONFIRMED_STOP.items()
                            if key != "lease_active"}),
    ("process cannot confirm", observation(process_active=None)),
    ("connection cannot confirm", observation(connection_open=None)),
    ("lease still live", observation(lease_active=True)),
    ("every field live", observation(
        process_active=True, connection_open=True, lease_active=True
    )),
    ("confirmed stop", observation()),
]


def test_the_sql_predicate_and_the_python_rule_agree_case_by_case() -> None:
    """The two spellings evaluated on the same records, rather than compared as text.

    A field-name comparison cannot see `is False` becoming `is not True`, nor `IS DISTINCT FROM`
    becoming `IS NOT DISTINCT FROM`: both keep every name and every literal in place and invert the
    answer. This evaluates the rule the database really runs (`LIVE_SLOT_SQL`, over a `VALUES` list,
    so no table is created) beside the rule Python really runs, and requires the same verdict on
    every record. Mutation-verified: flipping either operator alone turns this red — see the
    validation record's 更正 section.
    """
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database to evaluate the predicate in SQL")
    parameters: dict[str, object] = {}
    rows = []
    for index, (label, record) in enumerate(DRIFT_CASES):
        rows.append(f"(:label{index}, CAST(:json{index} AS jsonb))")
        parameters[f"label{index}"] = label
        parameters[f"json{index}"] = None if record is None else json.dumps(record)
    statement = text(
        f"SELECT v.label, {LIVE_SLOT_SQL} FROM (VALUES {', '.join(rows)}) AS v(label, observation)"
    )
    engine = connect_engine()
    try:
        with engine.begin() as connection:
            held_in_sql = {
                str(row[0]): bool(row[1]) for row in connection.execute(statement, parameters)
            }
    finally:
        engine.dispose()
    held_in_python = {label: holds_physical_slot(record) for label, record in DRIFT_CASES}
    assert held_in_sql == held_in_python

    # And the rule has a consumer on both sides, so neither spelling can become dead text that a
    # later edit is free to change: `stop_confirmed` is read by the call's outstanding conditions,
    # `holds_physical_slot` by the same place, and `LIVE_SLOT_SQL` by the occupancy query.
    orchestration = (REPOSITORY / "backend" / "src" / "huntweave" / "runs" / "orchestration.py")
    text_of = orchestration.read_text(encoding="utf-8")
    assert "LIVE_SLOT_SQL" in text_of
    assert "holds_physical_slot(" in text_of
    assert "stop_confirmed(" in text_of


def quota(global_used: int, per_ip: dict[str, int], *, global_limit: int = 4) -> ExecutionQuota:
    return ExecutionQuota(
        policy_version=RESOURCE_POLICY_VERSION,
        global_limit=global_limit,
        per_ip_limit=1,
        global_used=global_used,
        per_ip_used=per_ip,
    )


def test_a_free_pool_admits_the_next_action() -> None:
    assert slot_wait(quota(2, {"192.0.2.1": 1}), ["192.0.2.2"]) is None


def test_the_global_limit_is_answered_before_any_target() -> None:
    """An operator acts on the shared limit first; a per-address answer would misdirect them."""
    refused = slot_wait(quota(4, {"192.0.2.1": 1}), ["192.0.2.1"])
    assert refused is not None
    assert refused.category == "global_execution_slot"
    assert refused.resource is None
    assert (refused.holders, refused.limit) == (4, 4)
    assert "confirmed stopped" in refused.recovery_condition


def test_an_address_that_already_runs_an_action_refuses_a_second_one() -> None:
    refused = slot_wait(quota(1, {"192.0.2.1": 1}), ["192.0.2.1"])
    assert refused is not None
    assert refused.category == "target_execution_slot"
    assert refused.resource == "192.0.2.1"
    assert (refused.holders, refused.limit) == (1, 1)


def test_every_actual_target_is_checked_not_only_the_first() -> None:
    """A call that reaches several addresses is refused by any one of them being occupied."""
    refused = slot_wait(quota(2, {"192.0.2.9": 1}), ["192.0.2.8", "192.0.2.9"])
    assert refused is not None and refused.resource == "192.0.2.9"
    # ...and the answer does not depend on the order the caller happened to name them in.
    assert slot_wait(quota(2, {"192.0.2.9": 1}), ["192.0.2.9", "192.0.2.8"]) == refused


def test_a_repeated_address_is_one_address() -> None:
    assert slot_wait(quota(1, {"192.0.2.1": 1}), ["192.0.2.1", "192.0.2.1"]) is not None
    assert slot_wait(quota(1, {"192.0.2.1": 1}), ["192.0.2.2", "192.0.2.2"]) is None


def test_the_limit_is_reached_not_exceeded() -> None:
    assert slot_wait(quota(3, {}, global_limit=4), ["192.0.2.1"]) is None
    assert slot_wait(quota(4, {}, global_limit=4), ["192.0.2.1"]) is not None


def test_available_slots_are_reported_without_going_negative() -> None:
    assert quota(1, {}).global_available == 3
    assert quota(9, {}).global_available == 0


def test_the_deployment_defaults_are_the_documented_ones() -> None:
    """PROJECT.md section 12 fixes the numbers; this is what makes editing one a visible act."""
    text = (REPOSITORY / "PROJECT.md").read_text(encoding="utf-8")
    policy = ResourcePolicy.from_environment({})
    rows = {
        "global_execution_slots": "| 全局工具执行并发 |",
        "per_ip_execution_slots": "| 同 IP 主动执行并发 |",
        "control_dispatch_slots": "| 控制派发额度 |",
    }
    for field, marker in rows.items():
        line = next(item for item in text.splitlines() if item.startswith(marker))
        documented = int(re.search(r"\d+", line).group())  # type: ignore[union-attr]
        assert documented == getattr(policy, field), (field, line)


def test_the_slot_counts_are_configurable_and_a_bad_value_is_refused() -> None:
    configured = ResourcePolicy.from_environment(
        {
            "HUNTWEAVE_GLOBAL_EXECUTION_SLOTS": "6",
            "HUNTWEAVE_PER_IP_EXECUTION_SLOTS": "2",
            "HUNTWEAVE_CONTROL_DISPATCH_SLOTS": "8",
        }
    )
    assert (configured.global_execution_slots, configured.per_ip_execution_slots) == (6, 2)
    assert configured.control_dispatch_slots == 8
    assert configured.policy_version == RESOURCE_POLICY_VERSION
    for bad in ("0", "-1", "four"):
        with pytest.raises(ValueError):
            ResourcePolicy.from_environment({"HUNTWEAVE_GLOBAL_EXECUTION_SLOTS": bad})


def test_a_run_records_the_resource_policy_it_was_authorized_under() -> None:
    """The version travels with the authorization, so a live Run is not silently re-priced."""
    assert RESOURCE_POLICY_VERSION >= 1
    snapshot = ScopeSnapshot(
        targets=["192.0.2.1"],
        ports=[80],
        port_profile="common-tcp-v1",
        starts_at="2026-10-11T00:00:00+00:00",
        expires_at="2026-10-11T01:00:00+00:00",
        authorization="fixed fake actions only",
        budget=Budget(),
    )
    assert snapshot.resource_policy_version == RESOURCE_POLICY_VERSION


def test_the_acceptance_thresholds_are_versioned_and_fixed_before_a_fixture_runs() -> None:
    thresholds = ConcurrencyAcceptanceThresholds()
    assert thresholds.thresholds_version == ACCEPTANCE_THRESHOLDS_VERSION >= 1
    assert thresholds.control_path_seconds > 0
    assert thresholds.scheduling_wait_seconds > 0
    assert thresholds.pass_growth_ms_per_1000_calls > 0
    # Issue #21 names both of these as hard zeros, not as budgets to stay inside.
    assert thresholds.unknown_retries_max == 0
    assert thresholds.wrong_releases_max == 0
