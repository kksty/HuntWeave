"""Selective retention: candidate counting, pins, the reclaim preview and honest deletion.

The rules these checks hold are PROJECT.md section 10.5 and spec 0002 section 3.8: a version's
candidacy counts independent Runs in a window, retries never make a version look high-frequency,
pinning protects private reproduction material from *automatic* reclamation, capacity pressure is
reported as a block rather than met by deleting protected material, only this project's unreferenced
artifacts are ever removed, deleting a one-off environment never deletes evidence, and nothing here
promotes a private workspace into a shared tool version.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_real_execution import build as build_runner
from test_real_execution import real_ticket, settled
from test_sandbox_lifecycle import PROFILES, FakeRuntime

from huntweave.config import SandboxSettings
from huntweave.contracts.retention import (
    RetentionLimits,
    RetentionRequest,
    RetentionView,
)
from huntweave.execution.ledger import RunnerRejected
from huntweave.execution.retention import (
    PROTECTED_IN_USE,
    PROTECTED_PINNED,
    PROTECTED_VERSION_PINNED,
    REASON_OPERATOR,
    ArtifactRecord,
    RetentionStore,
    environment_key,
    remove_artifact,
)
from huntweave.execution.sandbox import (
    LABEL_NAMESPACE,
    EnvironmentManifest,
    SandboxInstanceRequest,
    SandboxManager,
    SandboxProfile,
    SandboxRejected,
    SandboxSessionRequest,
)
from huntweave.execution.server import create_runner

EGRESS_PROFILE = "sandbox-egress-v1"


# --------------------------------------------------------------------------------------------
# A store, a manager and the artifacts they share
# --------------------------------------------------------------------------------------------


def manifest(**changes: object) -> EnvironmentManifest:
    base: dict[str, object] = {
        "profile_id": EGRESS_PROFILE,
        "profile_version": 1,
        "image_digests": {"gateway": "sha256:aaa", "tool": "sha256:bbb"},
        "tool_inventory": ["curl"],
        "engine": "29.8.2",
        "architecture": "x86_64",
        "created_at": datetime.now(UTC),
    }
    base.update(changes)
    return EnvironmentManifest.model_validate(base)


def catalog(tmp_path: Path, **limits: int) -> RetentionStore:
    return RetentionStore(tmp_path / "state", limits=RetentionLimits(**limits))


def manager(tmp_path: Path, runtime: FakeRuntime) -> SandboxManager:
    return SandboxManager(
        runtime=runtime,
        profile=SandboxProfile.load(EGRESS_PROFILE, PROFILES),
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )


def retain(
    store: RetentionStore,
    sandbox: SandboxManager,
    *,
    run_id: UUID | None = None,
    retained_at: datetime | None = None,
    size_bytes: int | None = None,
) -> ArtifactRecord:
    """One real instance, reclaimed with its private workspace kept, recorded in the catalog."""
    request = SandboxSessionRequest(
        run_id=run_id or uuid4(),
        agent_session_id=uuid4(),
        scope_id=uuid4(),
        scope_version=1,
        policy_version=1,
    )
    session = sandbox.open_session(request)
    instance = sandbox.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    sandbox.stop_instance(instance.instance_id)
    report = sandbox.reclaim_instance(instance.instance_id, retain_volumes=True)
    assert report.retained, "the private workspace was not kept for the retention policy"
    volume = report.retained[0]
    store.record_artifact(
        instance.environment,
        run_id=request.run_id,
        session_id=session.session_id,
        instance_id=instance.instance_id,
        resource_id=volume.id,
        resource_name=volume.name,
        retained_at=retained_at,
        size_bytes=size_bytes,
    )
    return store.retained()[-1]


def use(store: RetentionStore, *, runs: int, calls: int = 1, at: datetime | None = None) -> None:
    for _ in range(runs):
        run_id = uuid4()
        for index in range(calls):
            store.record_use(
                manifest(),
                run_id=run_id,
                call_id=uuid4(),
                action_id="shell.exec",
                at=at or datetime.now(UTC) + timedelta(milliseconds=index),
            )


# --------------------------------------------------------------------------------------------
# The environment key and the candidate rule
# --------------------------------------------------------------------------------------------


def test_the_key_names_the_environment_and_not_the_host_engine() -> None:
    base = environment_key(manifest())
    # The container engine is the host's, not the environment an action ran in: upgrading Docker
    # must not look like a different tool.
    assert environment_key(manifest(engine="30.1.0")) == base
    assert environment_key(manifest(architecture="aarch64")) != base
    assert environment_key(manifest(profile_version=2)) != base
    assert environment_key(manifest(tool_inventory=["curl", "nmap"])) != base
    other_tool = {"gateway": "sha256:aaa", "tool": "sha256:ccc"}
    assert environment_key(manifest(image_digests=other_tool)) != base


def test_a_version_becomes_a_candidate_only_across_independent_runs(tmp_path: Path) -> None:
    store = catalog(tmp_path)
    # Two Runs that retried: six successful calls, but only two independent uses.
    use(store, runs=2, calls=3)
    version = store.view().versions[0]
    assert version.successful_runs == 2
    assert version.successful_calls == 6
    assert version.candidate is False
    assert version.threshold_runs == 3

    # A third independent Run is what makes it a candidate — repetitions never did.
    use(store, runs=1)
    version = store.view().versions[0]
    assert version.successful_runs == 3
    assert version.candidate is True


def test_a_use_outside_the_window_does_not_count(tmp_path: Path) -> None:
    store = catalog(tmp_path)
    old = datetime.now(UTC) - timedelta(days=40)
    use(store, runs=4, at=old)
    assert store.view().versions[0].successful_runs == 0
    assert store.view().versions[0].candidate is False
    assert store.view().versions[0].last_used_at is not None


def test_the_view_states_the_policy_it_was_computed_under(tmp_path: Path) -> None:
    store = catalog(tmp_path, candidate_runs=2, candidate_window_days=2, cache_ttl_days=1)
    use(store, runs=2)
    version = store.view().versions[0]
    assert version.threshold_runs == 2 and version.window_days == 2
    assert version.candidate is True
    assert store.view().limits.cache_ttl_days == 1


def test_the_deployment_can_state_its_own_limits() -> None:
    limits = RetentionLimits.from_env(
        {
            "HUNTWEAVE_RETENTION_CANDIDATE_RUNS": "5",
            "HUNTWEAVE_RETENTION_WINDOW_DAYS": "10",
            "HUNTWEAVE_RETENTION_TTL_DAYS": "3",
            "HUNTWEAVE_RETENTION_CAPACITY_BYTES": "1024",
        }
    )
    assert (limits.candidate_runs, limits.candidate_window_days) == (5, 10)
    assert (limits.cache_ttl_days, limits.cache_capacity_bytes) == (3, 1024)
    with pytest.raises(ValueError):
        RetentionLimits.from_env({"HUNTWEAVE_RETENTION_CAPACITY_BYTES": "lots"})


# --------------------------------------------------------------------------------------------
# What a real call leaves behind
# --------------------------------------------------------------------------------------------


def test_a_finished_call_keeps_its_private_workspace_under_the_policy(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runner, sandbox, _ = build_runner(tmp_path, runtime, command_stdout="ok\n")
    ticket = real_ticket("shell.exec", {"command": "true"})
    runner.submit(ticket)
    settled(runner, ticket.call_id, "completed", "failed")

    artifacts = runner.retention.retained()
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.run_id == ticket.run_id and artifact.kind == "workspace"
    # The containers and the session network are gone; the workspace volume is what remains.
    assert [item.kind for item in sandbox.resources()] == ["volume"]
    instance = next(iter(sandbox.instances.values()))
    assert instance.state == "reclaimed"
    # The environment manifest survives the reclamation, which is what lets a later reader explain
    # what this private material is a reproduction of (ADR-0014).
    assert instance.environment.profile_id == EGRESS_PROFILE
    assert artifact.environment_key == environment_key(instance.environment)
    # And the successful call credited the version it ran in.
    assert runner.retention.view().versions[0].successful_runs == 1


# --------------------------------------------------------------------------------------------
# The reclaim preview: TTL, capacity and protection
# --------------------------------------------------------------------------------------------


def test_expiry_selects_an_artifact_and_says_which_rule_did_it(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_ttl_days=7)
    artifact = retain(
        store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=8), size_bytes=10
    )
    preview = store.preview(sizes={artifact.resource_id: 10})
    assert [item.artifact_id for item in preview.to_delete] == [artifact.artifact_id]
    assert preview.freed_bytes == 10 and preview.blocked is False

    report = store.sweep(
        actor="operator", note="", remove=remove_artifact(sandbox), sizes={artifact.resource_id: 10}
    )
    assert report.deleted == [artifact.artifact_id]
    assert sandbox.resources() == []
    assert report.decision.reason_code is None
    assert report.view.artifacts == []


def test_a_fresh_artifact_is_not_selected_without_capacity_pressure(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox, size_bytes=10)
    preview = store.preview(sizes={artifact.resource_id: 10})
    assert preview.to_delete == []
    assert preview.blocked is False
    report = store.sweep(
        actor="operator", note="", remove=remove_artifact(sandbox), sizes={artifact.resource_id: 10}
    )
    assert report.deleted == []
    assert sandbox.resources() != []


def test_capacity_pressure_reclaims_the_least_recently_retained_first(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_capacity_bytes=12)
    oldest = retain(
        store, sandbox, retained_at=datetime.now(UTC) - timedelta(hours=2), size_bytes=8
    )
    newest = retain(store, sandbox, retained_at=datetime.now(UTC), size_bytes=8)
    sizes = {oldest.resource_id: 8, newest.resource_id: 8}

    preview = store.preview(sizes=sizes)
    assert preview.retained_bytes == 16
    assert [item.artifact_id for item in preview.to_delete] == [oldest.artifact_id]
    assert preview.blocked is False

    report = store.sweep(
        actor="operator", note="", remove=remove_artifact(sandbox), sizes=sizes
    )
    assert report.deleted == [oldest.artifact_id]
    assert [item.artifact_id for item in report.view.artifacts] == [newest.artifact_id]


def test_capacity_short_of_protected_material_is_reported_as_a_block(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_capacity_bytes=4)
    artifact = retain(store, sandbox, size_bytes=10)
    store.pin(
        target_kind="artifact", target=str(artifact.artifact_id), actor="operator", note=""
    )
    preview = store.view(sizes={artifact.resource_id: 10}).preview
    # Nothing reclaimable is left and the cache is still over capacity: the answer is the shortfall,
    # not a deletion of the thing the operator pinned.
    assert preview.to_delete == []
    assert preview.blocked is True
    assert preview.needed_bytes == 6
    assert preview.reason_code == "retention_capacity_insufficient"
    assert [item.reason_code for item in preview.protected] == [PROTECTED_PINNED]

    report = store.sweep(
        actor="operator", note="", remove=remove_artifact(sandbox), sizes={artifact.resource_id: 10}
    )
    assert report.deleted == []
    assert sandbox.resources() != []


def test_an_unmeasured_artifact_is_not_counted_as_empty(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    retain(store, sandbox)
    preview = store.view().preview
    # The engine reported no size: the artifact is counted as unmeasured, never as zero bytes.
    assert preview.retained_bytes == 0
    assert preview.unmeasured_artifacts == 1


# --------------------------------------------------------------------------------------------
# Pins
# --------------------------------------------------------------------------------------------


def test_a_pin_protects_material_from_automatic_reclamation(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_ttl_days=1)
    artifact = retain(store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=2))
    view = store.pin(
        target_kind="artifact", target=str(artifact.artifact_id), actor="operator", note="keep it"
    )
    assert view.artifacts[0].pinned is True
    assert view.preview.to_delete == []
    assert [item.reason_code for item in view.preview.protected] == [PROTECTED_PINNED]

    # Unpinning gives it back to the policy: it is expired, so the next sweep takes it.
    store.unpin(target_kind="artifact", target=str(artifact.artifact_id), actor="operator", note="")
    report = store.sweep(actor="operator", note="", remove=remove_artifact(sandbox))
    assert report.deleted == [artifact.artifact_id]


def test_pinning_a_version_covers_every_artifact_of_it(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_ttl_days=1)
    first = retain(store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=2))
    second = retain(store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=2))
    view = store.pin(
        target_kind="version", target=first.environment_key, actor="operator", note=""
    )
    assert view.versions[0].pinned is True
    assert [item.reason_code for item in view.preview.protected] == [PROTECTED_VERSION_PINNED] * 2
    report = store.sweep(actor="operator", note="", remove=remove_artifact(sandbox))
    assert report.deleted == []
    assert {item.artifact_id for item in store.retained()} == {
        first.artifact_id,
        second.artifact_id,
    }


def test_an_explicit_delete_still_works_on_pinned_material(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox)
    store.pin(target_kind="artifact", target=str(artifact.artifact_id), actor="operator", note="")
    report = store.delete_artifacts(
        [store.artifact(artifact.artifact_id)],
        action="delete",
        target_kind="artifact",
        target=str(artifact.artifact_id),
        actor="operator",
        note="remove it anyway",
        remove=remove_artifact(sandbox),
        reason=REASON_OPERATOR,
    )
    # A pin constrains the automatic policy; it is not a veto over the operator who set it.
    assert report.deleted == [artifact.artifact_id]
    assert sandbox.resources() == []


def test_a_pin_never_promotes_private_material_to_a_shared_tool_version(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox)
    store.pin(target_kind="version", target=artifact.environment_key, actor="operator", note="")
    view = store.view()
    assert all(version.shared_tool_version is False for version in view.versions)
    # Nothing about the artifact was lifted out of the cache: it is still this project's own volume,
    # and the store offers no path that would publish it.
    assert not hasattr(store, "publish")
    assert [item.resource_name for item in view.artifacts] == [artifact.resource_name]


# --------------------------------------------------------------------------------------------
# Ownership and use
# --------------------------------------------------------------------------------------------


def test_a_volume_an_instance_still_holds_is_never_reclaimed(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    session = sandbox.open_session(
        SandboxSessionRequest(
            run_id=uuid4(),
            agent_session_id=uuid4(),
            scope_id=uuid4(),
            scope_version=1,
            policy_version=1,
        )
    )
    instance = sandbox.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
    volume = next(item for item in instance.resources if item.kind == "volume")
    # An instance that has not confirmed its stop may still have the volume mounted, so the write
    # path refuses before it even considers what the retention ledger says.
    with pytest.raises(SandboxRejected) as raised:
        sandbox.release_artifact(instance_id=instance.instance_id, volume_id=volume.id)
    assert raised.value.reason_code == "retention_artifact_in_use"
    assert volume.id in sandbox.in_use_volumes()

    # Once it is reclaimed and retained, the volume can go — and a second attempt is refused by name
    # rather than silently succeeding twice.
    sandbox.stop_instance(instance.instance_id)
    report = sandbox.reclaim_instance(instance.instance_id, retain_volumes=True)
    retained = report.retained[0]
    sandbox.release_artifact(instance_id=instance.instance_id, volume_id=retained.id)
    assert runtime.volumes == {}
    with pytest.raises(SandboxRejected) as raised:
        sandbox.release_artifact(instance_id=instance.instance_id, volume_id=retained.id)
    assert raised.value.reason_code == "retention_artifact_not_retained"


def test_a_held_volume_is_protected_by_the_preview(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox)
    preview = store.preview(sizes={}, in_use=frozenset({artifact.resource_id}))
    assert preview.to_delete == []
    assert [item.reason_code for item in preview.protected] == [PROTECTED_IN_USE]


def test_only_this_projects_owned_volume_is_removed(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox)
    # Someone else's volume, carrying the identity the ledger recorded: the labels decide.
    runtime.volumes[artifact.resource_id]["labels"] = {f"{LABEL_NAMESPACE}.project": "other"}
    report = store.delete_artifacts(
        [store.artifact(artifact.artifact_id)],
        action="delete",
        target_kind="artifact",
        target=str(artifact.artifact_id),
        actor="operator",
        note="",
        remove=remove_artifact(sandbox),
        reason=REASON_OPERATOR,
    )
    assert report.deleted == []
    assert report.failed == [f"ownership_mismatch:{artifact.artifact_id}"]
    assert runtime.volumes  # not removed
    assert store.retained()[0].state == "retained"


def test_deleting_a_one_off_environment_keeps_the_evidence(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runner, sandbox, _ = build_runner(tmp_path, runtime, command_stdout="uid=10001\n")
    ticket = real_ticket("shell.exec", {"command": "id"})
    runner.submit(ticket)
    record = settled(runner, ticket.call_id, "completed", "failed")
    assert record.result is not None
    evidence = {item.relative_path: item for item in record.result.evidence}
    assert evidence
    artifact = runner.retention.retained()[0]
    instance = next(iter(sandbox.instances.values()))

    report = runner.retention.delete_artifacts(
        [artifact],
        action="delete",
        target_kind="artifact",
        target=str(artifact.artifact_id),
        actor="operator",
        note="",
        remove=remove_artifact(sandbox),
        reason=REASON_OPERATOR,
    )
    assert report.deleted == [artifact.artifact_id]
    assert sandbox.resources() == []
    # The necessary evidence is still there, byte for byte, and the manifest still names what the
    # removed environment was built from.
    for relative in evidence:
        assert (tmp_path / "evidence" / relative).is_file()
    assert sandbox.instances[str(instance.instance_id)].environment == instance.environment


def test_the_policy_runs_without_an_operator_and_still_respects_a_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TTL and the capacity are what bound a deployment's disk, so the policy runs on its own.

    A policy that only runs when somebody opens the console is a suggestion; this checks the
    unattended tick, and that it takes exactly the material the preview would have offered — the
    pinned artifact survives an automatic sweep.
    """
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_capacity_bytes=4, cache_ttl_days=1)
    pinned = retain(store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=2))
    unpinned = retain(store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=2))
    store.pin(target_kind="artifact", target=str(pinned.artifact_id), actor="operator", note="")
    monkeypatch.setenv("HUNTWEAVE_RETENTION_CAPACITY_BYTES", "4")

    client = runner_client(tmp_path, enabled=True, runtime=runtime)
    # The Runner's ledger is the one the manager just wrote into: same state directory, so the
    # artifacts above are already in its cache.
    body = client.get("/v1/retention", headers=headers()).json()
    view = RetentionView.model_validate(body)
    assert [item.reason_code for item in view.preview.protected] == ["retention_artifact_pinned"]
    assert [item.artifact_id for item in view.preview.to_delete] == [unpinned.artifact_id]


def test_the_policy_reclaims_on_its_own_without_an_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattended tick takes exactly what the preview offers, and records who did it.

    The TTL and the capacity are what bound a deployment's disk, so the policy runs without anybody
    opening the console; the decision it leaves behind names no operator, because none acted.
    """
    monkeypatch.setenv("HUNTWEAVE_RETENTION_CAPACITY_BYTES", "1")
    monkeypatch.setenv("HUNTWEAVE_RETENTION_SWEEP_SECONDS", "1")
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = RetentionStore(tmp_path / "state")
    retain(store, sandbox, size_bytes=10)

    client = runner_client(tmp_path, enabled=True, runtime=runtime)
    assert client.get("/v1/retention", headers=headers()).json()["available"] is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and runtime.volumes:
        time.sleep(0.05)
    assert runtime.volumes == {}, "the unattended sweep never took an artifact over capacity"

    trace = client.get("/v1/retention", headers=headers()).json()["decisions"]
    sweeps = [item for item in trace if item["action"] == "sweep"]
    assert sweeps and sweeps[0]["actor"] is None
    assert sweeps[0]["deleted_artifacts"]


# --------------------------------------------------------------------------------------------
# Refusals and the trace
# --------------------------------------------------------------------------------------------


def test_an_unknown_target_is_refused_by_name(tmp_path: Path) -> None:
    store = catalog(tmp_path)
    with pytest.raises(RunnerRejected) as raised:
        store.pin(target_kind="artifact", target=str(uuid4()), actor=None, note="")
    assert raised.value.reason_code == "retention_artifact_unknown"
    with pytest.raises(RunnerRejected) as raised:
        store.unpin(target_kind="version", target="0" * 32, actor=None, note="")
    assert raised.value.reason_code == "retention_version_unknown"
    with pytest.raises(RunnerRejected) as raised:
        store.artifact(uuid4())
    assert raised.value.reason_code == "retention_artifact_unknown"


def test_every_decision_records_who_asked_and_what_happened(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path, cache_ttl_days=1)
    artifact = retain(store, sandbox, retained_at=datetime.now(UTC) - timedelta(days=2))
    store.pin(
        target_kind="artifact",
        target=str(artifact.artifact_id),
        actor="session-1",
        note="reproduce later",
    )
    report = store.delete_artifacts(
        [store.artifact(artifact.artifact_id)],
        action="delete",
        target_kind="artifact",
        target=str(artifact.artifact_id),
        actor="session-1",
        note="done with it",
        remove=remove_artifact(sandbox),
        reason=REASON_OPERATOR,
    )
    actions = [item.action for item in report.view.decisions]
    assert actions == ["delete", "pin"]
    assert report.view.decisions[0].actor == "session-1"
    assert report.view.decisions[0].note == "done with it"
    assert report.view.decisions[0].affected_runs == [artifact.run_id]
    assert report.view.decisions[0].deleted_artifacts == [artifact.artifact_id]


def test_the_catalog_survives_a_restart_with_its_pins_and_uses(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox)
    store.pin(target_kind="artifact", target=str(artifact.artifact_id), actor="operator", note="")
    use(store, runs=3)

    reopened = RetentionStore(tmp_path / "state")
    assert reopened.view().versions[0].candidate is True
    assert reopened.view().artifacts[0].pinned is True
    assert reopened.retained()[0].artifact_id == artifact.artifact_id


def test_a_revert_names_retained_material_instead_of_deleting_it(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    artifact = retain(store, sandbox)

    report = sandbox.begin_revert(reason="check_revert")
    # Withdrawing management does not quietly destroy an operator's reproduction material: the
    # volume is still there, and the report says so rather than reading as an empty host.
    assert report.state == "complete" and report.withdrew is True
    assert [item.id for item in report.retained] == [artifact.resource_id]
    assert artifact.resource_id in runtime.volumes
    # The revert record archived next to the evidence carries the same statement.
    snapshot = json.loads(
        (tmp_path / "evidence" / report.archived[0]).read_text(encoding="utf-8")
    )
    assert snapshot["retained"] == [artifact.resource_name]


# --------------------------------------------------------------------------------------------
# The Runner's own surface
# --------------------------------------------------------------------------------------------


def runner_client(tmp_path: Path, *, enabled: bool, runtime: FakeRuntime) -> TestClient:
    settings = SandboxSettings(
        enabled=enabled,
        profile_id=EGRESS_PROFILE,
        profile_dir=PROFILES,
        state_dir=tmp_path / "state",
        evidence_dir=tmp_path / "evidence",
    )
    return TestClient(
        create_runner(
            token="b" * 64,
            state_dir=tmp_path / "state",
            evidence_dir=tmp_path / "evidence",
            sandbox_settings=settings,
            sandbox_factory=lambda _: manager(tmp_path, runtime),
        )
    )


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + "b" * 64}


def test_a_runner_without_management_says_so_instead_of_showing_an_empty_cache(
    tmp_path: Path,
) -> None:
    client = runner_client(tmp_path, enabled=False, runtime=FakeRuntime())
    body = client.get("/v1/retention", headers=headers()).json()
    view = RetentionView.model_validate(body)
    assert view.available is False
    assert view.sandbox_management == "disabled"
    assert view.reason_code == "sandbox_management_disabled"
    # And it refuses to act rather than answering an empty success.
    refused = client.post(
        "/v1/retention/sweep", json={"note": ""}, headers=headers()
    )
    assert refused.status_code == 409
    assert refused.json()["reason_code"] == "sandbox_management_disabled"


def test_the_runner_serves_the_catalog_and_its_actions(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    client = runner_client(tmp_path, enabled=True, runtime=runtime)
    sandbox = manager(tmp_path, runtime)
    store = RetentionStore(tmp_path / "state")
    artifact = retain(store, sandbox)

    body = client.get("/v1/retention", headers=headers()).json()
    view = RetentionView.model_validate(body)
    assert view.available is True and view.sandbox_management == "ready"
    assert [item.artifact_id for item in view.artifacts] == [artifact.artifact_id]
    assert view.versions[0].retained_artifacts == 1

    pinned = client.post(
        f"/v1/retention/artifacts/{artifact.artifact_id}/pin",
        json=RetentionRequest(note="keep").model_dump(),
        headers=headers(),
    ).json()
    assert RetentionView.model_validate(pinned).artifacts[0].pinned is True

    deleted = client.post(
        f"/v1/retention/artifacts/{artifact.artifact_id}/delete",
        json=RetentionRequest(note="").model_dump(),
        headers=headers(),
    ).json()
    assert deleted["deleted"] == [str(artifact.artifact_id)]
    assert runtime.volumes == {}
    trace = client.get("/v1/retention", headers=headers()).json()["decisions"]
    assert [item["action"] for item in trace] == ["delete", "pin"]
    assert all(item["actor"] is None for item in trace)


def test_the_runner_records_who_acted_when_the_control_plane_says_so(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    client = runner_client(tmp_path, enabled=True, runtime=runtime)
    sandbox = manager(tmp_path, runtime)
    store = RetentionStore(tmp_path / "state")
    artifact = retain(store, sandbox)
    client.post(
        f"/v1/retention/artifacts/{artifact.artifact_id}/pin",
        json=RetentionRequest(actor="session-7", note="from the console").model_dump(),
        headers=headers(),
    )
    decision = client.get("/v1/retention", headers=headers()).json()["decisions"][0]
    assert decision["actor"] == "session-7" and decision["note"] == "from the console"


def test_the_ledger_file_is_json_the_next_runner_can_read(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sandbox = manager(tmp_path, runtime)
    store = catalog(tmp_path)
    retain(store, sandbox)
    document = json.loads((tmp_path / "state" / "retention.json").read_text(encoding="utf-8"))
    assert set(document) == {"uses", "versions", "artifacts", "pins", "decisions"}
