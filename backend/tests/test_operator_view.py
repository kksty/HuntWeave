"""What the operator's read of the execution side may and may not say.

The console's isolation panel is only worth anything if it never overstates. Two statements it must
not make are "this instance is reclaimed" when nobody confirmed the stop, and "nothing is running"
when the answer is really "we could not ask". Both are checked here against the manager's own
records, and against a manager whose runtime has gone away.
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from _fakeruntime import FakeRuntime

from huntweave.contracts.execution import RunRuntimeView
from huntweave.execution.operator import project_run_state, unavailable_run_state
from huntweave.execution.sandbox import (
    AuthorizedEndpoint,
    EgressUpdate,
    HaltRequest,
    InstanceRecord,
    SandboxInstanceRequest,
    SandboxManager,
    SandboxRejected,
    SessionRecord,
)
from huntweave.execution.sandboxprofile import SandboxProfile

PROFILES = Path(__file__).resolve().parents[2] / "profiles"


def live_runtime() -> FakeRuntime:
    """An in-memory runtime standing in for the container engine (see `_fakeruntime`)."""
    return FakeRuntime()


def lifecycle_profile() -> SandboxProfile:
    return SandboxProfile.load("sandbox-lifecycle-v1", PROFILES)


def opened_manager(tmp_path: Path) -> SandboxManager:
    return SandboxManager(
        runtime=live_runtime(),
        profile=lifecycle_profile(),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )


def launched(
    manager: SandboxManager, run_id: UUID, address: str = "192.0.2.10"
) -> tuple[SessionRecord, InstanceRecord]:
    """One live instance under one authorization, opened the way a real call opens it."""
    from huntweave.execution.sandbox import SandboxSessionRequest

    session = manager.open_session(
        SandboxSessionRequest(
            run_id=run_id,
            agent_session_id=uuid4(),
            scope_id=uuid4(),
            scope_version=1,
            policy_version=1,
        )
    )
    instance = manager.launch_instance(
        SandboxInstanceRequest(
            session_id=session.session_id,
            authorized=[AuthorizedEndpoint(address=address, port=80)],
        )
    )
    return session, instance


def test_a_live_instance_is_reported_as_running_with_its_permits(tmp_path: Path) -> None:
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    assert view.available and view.sandbox_management == "ready"
    assert view.profile_id == manager.profile.profile_id
    assert len(view.sessions) == 1 and len(view.sessions[0].instances) == 1
    reported = view.sessions[0].instances[0]
    assert reported.instance_id == instance.instance_id
    assert reported.state == "ready"
    # A running instance's stop is not confirmed — it is the opposite of a stop.
    assert reported.stop_state == "running"
    assert reported.egress["authorized"] == [{"address": "192.0.2.10", "port": 80}]
    # The image digests and the tool inventory come from the instance's own manifest.
    assert reported.environment["image_digests"]
    assert reported.environment["profile_version"] == 1


def test_a_confirmed_stop_is_the_only_thing_reported_as_confirmed(tmp_path: Path) -> None:
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    manager.halt_instance(
        HaltRequest(instance_id=instance.instance_id, reason="operator_cancelled")
    )
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    reported = view.sessions[0].instances[0]
    assert reported.state == "stopped"
    # The manager recorded the confirmation, so the projection may say so.
    assert reported.stop_state == "confirmed"
    assert reported.halt_reason == "operator_cancelled"


def test_an_unconfirmed_stop_is_never_reported_as_reclaimed(tmp_path: Path) -> None:
    """The one thing the console must not soften: a stop nobody proved releases nothing."""
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    assert isinstance(manager.runtime, FakeRuntime)
    # The engine refuses to stop the container, which is the state the criterion is about: the
    # halt attempt fails and the instance stays where it was rather than being reported stopped.
    manager.runtime.refuse_stop = True
    with pytest.raises(SandboxRejected):
        manager.halt_instance(
            HaltRequest(instance_id=instance.instance_id, reason="operator_cancelled")
        )
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    reported = view.sessions[0].instances[0]
    assert reported.state == "ready"
    assert reported.stop_state == "running"
    # No confirmation is claimed, and the reclaim preview says one is required rather than
    # promising a clean removal.
    preview = next(
        item for item in view.reclaimable if item["instance_id"] == str(instance.instance_id)
    )
    assert preview["confirmation_required"] is True


def test_a_record_without_a_confirmation_is_never_reported_as_stopped(tmp_path: Path) -> None:
    """`reclaimed` without `stop_confirmed_at` is not a stop: nobody proved what it left behind."""
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    with manager.lock:
        manager.instances[str(instance.instance_id)] = instance.model_copy(
            update={"state": "reclaimed", "stop_confirmed_at": None, "reclaimed_at": None}
        )
        manager._persist()
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    assert view.sessions[0].instances[0].stop_state == "unconfirmed"


def test_the_reclaim_preview_lists_what_would_be_removed_without_performing_it(
    tmp_path: Path,
) -> None:
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    before = len(manager.instances)
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    preview = next(
        item for item in view.reclaimable if item["instance_id"] == str(instance.instance_id)
    )
    assert {item["role"] for item in preview["resources"]} <= {"gateway", "tool", "workspace"}
    # Reading is not acting: the manager still holds the same instances afterwards.
    assert len(manager.instances) == before
    assert manager.instances[str(instance.instance_id)].state == "ready"


def test_a_closed_permit_stays_visible_after_it_was_revoked(tmp_path: Path) -> None:
    """A revoked rule is shown as revoked, with the permits it withdrew still named.

    An empty authorization beside a live container would be an explanation nobody could give: the
    console has to say that the rules were there and were withdrawn, and why.
    """
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    manager.revoke_egress(instance.instance_id, "scope_revoked")
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    egress = view.sessions[0].instances[0].egress
    assert egress["revoked_at"] is not None
    assert egress["revocation_reason"] == "scope_revoked"
    assert egress["authorized"] == []
    # The change log is what explains the empty set: the permits were applied first.
    assert [change["reason"] for change in egress["changes"]] == ["initial", "scope_revoked"]


def test_a_narrowed_scope_keeps_both_the_old_and_the_new_authorization(tmp_path: Path) -> None:
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    _, instance = launched(manager, run_id)
    manager.authorize_egress(
        EgressUpdate(
            instance_id=instance.instance_id,
            authorized=[AuthorizedEndpoint(address="192.0.2.10", port=80)],
            reason="scope_shrunk",
        )
    )
    view = project_run_state(manager.run_state(run_id), profile_id=manager.profile.profile_id)
    egress = view.sessions[0].instances[0].egress
    assert egress["applied_at"] is not None
    assert [change["reason"] for change in egress["changes"]] == ["initial", "scope_shrunk"]


def test_an_unreachable_runtime_leaves_the_ledger_standing_and_says_so(tmp_path: Path) -> None:
    """An audit that could not be taken must not be presented as "nothing is out there"."""
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    launched(manager, run_id)
    assert isinstance(manager.runtime, FakeRuntime)
    # The runtime cannot be listed at all: the audit is not "clean", it is absent.
    manager.runtime.fail_on = "list_resources"
    state = manager.run_state(run_id)
    # The ledger still names the instance, and the missing audit is reported as missing.
    assert len(state.instances) == 1
    assert state.audit is None
    view = project_run_state(state, profile_id=manager.profile.profile_id)
    assert view.available is False
    assert view.reason_code == "sandbox_runtime_unreachable"
    assert len(view.sessions[0].instances) == 1


def test_a_deployment_without_management_states_that_instead_of_an_empty_run() -> None:
    run_id = uuid4()
    view = unavailable_run_state(run_id, management="disabled", reason_code=None, profile_id=None)
    assert view == RunRuntimeView(
        available=False,
        reason_code="sandbox_management_disabled",
        sandbox_management="disabled",
        profile_id=None,
        observed_at=view.observed_at,
        run_id=run_id,
    )


def test_an_instance_still_being_created_is_not_offered_for_reclaim(tmp_path: Path) -> None:
    """A create that never finished holds resources nobody may touch yet."""
    manager = opened_manager(tmp_path)
    run_id = uuid4()
    assert isinstance(manager.runtime, FakeRuntime)
    manager.runtime.fail_creates_after = 1
    with pytest.raises(SandboxRejected):
        launched(manager, run_id)
    state = manager.run_state(run_id)
    assert state.instances and state.instances[0].state == "interrupted"
    view = project_run_state(state, profile_id=manager.profile.profile_id)
    # An unfinished create is neither a stop nor a healthy running instance: it is unaccounted for,
    # so any reclaim of it needs a confirmation nobody has.
    assert view.sessions[0].instances[0].stop_state == "unconfirmed"
    assert state.audit is not None and state.audit.interrupted
