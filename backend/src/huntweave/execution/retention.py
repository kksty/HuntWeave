"""Selective retention: candidate statistics, manual pins, and a reclaim preview.

P1 produces exactly one private artifact per execution instance — the workspace volume the tool
container mounted — and PROJECT.md section 10.5 gives that artifact a lifecycle of its own: it is a
one-off task environment, it stays private whatever an operator pins, and it is removed by a
*cleanup policy* rather than the moment a call ends. This module is that policy's bookkeeping:

* **Candidate statistics.** A tool/environment version is named by the key its manifest hashes to.
  A use is recorded once per successfully finished call, and candidacy counts *distinct Runs*
  inside a 30-day window, so retrying an action never makes a version look high-frequency.
* **Manual pins.** An operator can pin a version or one artifact, unpin it, or delete it outright.
  A pin protects material from *automatic* reclamation; an explicit delete still has to pass the
  ownership and in-use checks the trusted manager performs.
* **A reclaim preview.** What a reclaim would delete (TTL expiry first, then least-recently-retained
  while the cache is over capacity), what it would not touch and why, and whether capacity can be
  met at all.

Nothing here removes anything by itself: the caller supplies the removal — the trusted manager — and
every removal it refuses is reported as a failure rather than papered over. Nor does anything here
publish: a private workspace is never promoted into a shared tool version.
"""

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from huntweave.contracts.retention import (
    ArtifactKind,
    ArtifactState,
    ProtectedArtifact,
    RetainedArtifact,
    RetentionAction,
    RetentionDecisionView,
    RetentionLimits,
    RetentionPreview,
    RetentionReport,
    RetentionTarget,
    RetentionView,
    ToolVersionView,
)
from huntweave.contracts.runs import Contract
from huntweave.execution.durable import atomic_write
from huntweave.execution.ledger import RunnerRejected
from huntweave.execution.sandbox import (
    EnvironmentManifest,
    RuntimeUnavailable,
    SandboxManager,
    SandboxRejected,
)

__all__ = [
    "ArtifactRecord",
    "DecisionRecord",
    "PinRecord",
    "RetentionStore",
    "UseRecord",
    "VersionRecord",
    "environment_key",
    "remove_artifact",
]

# How many decision records the ledger keeps, and how many of them a view carries: the ledger is
# the durable trace, the view is a panel somebody reads.
DECISION_LIMIT = 200
DECISIONS_IN_VIEW = 50
# Deleted artifact records are kept for audit but bounded: a long-running deployment must not grow
# its retention ledger without limit over artifacts nothing holds any more.
DELETED_ARTIFACT_LIMIT = 2000

# Why an artifact left the cache. Each names the rule that removed it, so a record never has to be
# read as "something deleted it".
REASON_TTL_EXPIRED = "retention_ttl_expired"
REASON_CAPACITY = "retention_capacity_reclaim"
REASON_OPERATOR = "retention_operator_deleted"
# The reason a preview is blocked: reclaiming everything reclaimable still leaves the cache over.
REASON_CAPACITY_INSUFFICIENT = "retention_capacity_insufficient"
# Why an artifact was left alone by an automatic reclaim.
PROTECTED_PINNED = "retention_artifact_pinned"
PROTECTED_VERSION_PINNED = "retention_version_pinned"
PROTECTED_IN_USE = "retention_artifact_in_use"

# Every reason this side can state about retention, in one place: why an artifact was removed, why
# an automatic reclaim left it alone, why a preview is blocked, and why an action was refused.
# Declared as a Literal so the console's reason vocabulary check reads this list rather than
# collecting the values from scattered literals (backend/tests/test_reason_codes.py).
RetentionReason = Literal[
    "retention_ttl_expired",
    "retention_capacity_reclaim",
    "retention_operator_deleted",
    "retention_capacity_insufficient",
    "retention_artifact_pinned",
    "retention_version_pinned",
    "retention_artifact_in_use",
    "retention_artifact_unknown",
    "retention_version_unknown",
    "retention_artifact_not_retained",
    "retention_delete_failed",
    "retention_not_pinned",
    # A Runner that does not serve the retention routes at all: a deployment fact, stated as itself
    # rather than as an empty cache or as a read-interface gap.
    "retention_unsupported",
]


def environment_key(environment: EnvironmentManifest) -> str:
    """The compatibility key of one tool/environment version.

    PROJECT.md section 10.5 makes this key the thing that decides whether two executions used "the
    same capability": the fixed profile and its version, the resolved base and tool image digests,
    the declared tool inventory and the CPU architecture. The engine version is deliberately *not*
    part of it — that is the host's container engine, not the environment an action ran in, and
    including it would make every engine upgrade look like a different tool.
    """
    material = {
        "profile_id": environment.profile_id,
        "profile_version": environment.profile_version,
        "image_digests": dict(sorted(environment.image_digests.items())),
        "tool_inventory": sorted(environment.tool_inventory),
        "architecture": environment.architecture,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # A readable prefix: the whole digest identifies nothing a human ever compares by eye.
    return digest[:32]


class UseRecord(Contract):
    """One environment version as one Run really used it. ``calls`` never drives candidacy."""

    environment_key: str
    run_id: UUID
    calls: int = Field(default=1, ge=1, strict=True)
    first_at: AwareDatetime
    last_at: AwareDatetime
    last_action: str | None = None
    last_call_id: UUID | None = None


class VersionRecord(Contract):
    """The immutable description of a version, taken from the manifest that created an instance."""

    environment_key: str
    profile_id: str
    profile_version: int
    image_digests: dict[str, str] = Field(default_factory=dict)
    tool_inventory: list[str] = Field(default_factory=list)
    engine: str
    architecture: str


class ArtifactRecord(Contract):
    """One private artifact the platform holds, or held and removed."""

    artifact_id: UUID
    environment_key: str
    run_id: UUID
    session_id: UUID
    instance_id: UUID
    kind: ArtifactKind = "workspace"
    resource_id: str
    resource_name: str
    size_bytes: int | None = Field(default=None, ge=0)
    retained_at: AwareDatetime
    expires_at: AwareDatetime
    state: ArtifactState = "retained"
    deleted_at: AwareDatetime | None = None
    delete_reason: str | None = None


class PinRecord(Contract):
    """An operator's pin, with who asked and when."""

    target_kind: Literal["version", "artifact"]
    target: str
    at: AwareDatetime
    actor: str | None = None
    note: str | None = None


class DecisionRecord(Contract):
    """One retention decision: what was asked, what happened, and which Runs it touched."""

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
    failed: list[str] = Field(default_factory=list)


class RetentionStore:
    """The retention ledger: durable, single-writer, and the only place a pin is remembered."""

    def __init__(self, state_dir: Path, *, limits: RetentionLimits | None = None) -> None:
        self.limits = limits or RetentionLimits.from_env()
        self.path = state_dir / "retention.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        (
            self.uses,
            self.versions,
            self.artifacts,
            self.pins,
            self.decisions,
        ) = self._load()

    # -- durability ---------------------------------------------------------------------------

    def _load(
        self,
    ) -> tuple[
        dict[str, UseRecord],
        dict[str, VersionRecord],
        dict[str, ArtifactRecord],
        dict[str, PinRecord],
        list[DecisionRecord],
    ]:
        if not self.path.exists():
            return {}, {}, {}, {}, []
        document = json.loads(self.path.read_text(encoding="utf-8"))
        return (
            {
                key: UseRecord.model_validate(value)
                for key, value in document.get("uses", {}).items()
            },
            {
                key: VersionRecord.model_validate(value)
                for key, value in document.get("versions", {}).items()
            },
            {
                key: ArtifactRecord.model_validate(value)
                for key, value in document.get("artifacts", {}).items()
            },
            {
                key: PinRecord.model_validate(value)
                for key, value in document.get("pins", {}).items()
            },
            [DecisionRecord.model_validate(value) for value in document.get("decisions", [])],
        )

    def _persist(self) -> None:
        document = {
            "uses": {key: value.model_dump(mode="json") for key, value in self.uses.items()},
            "versions": {
                key: value.model_dump(mode="json") for key, value in self.versions.items()
            },
            "artifacts": {
                key: value.model_dump(mode="json") for key, value in self.artifacts.items()
            },
            "pins": {key: value.model_dump(mode="json") for key, value in self.pins.items()},
            "decisions": [value.model_dump(mode="json") for value in self.decisions],
        }
        atomic_write(self.path, json.dumps(document, sort_keys=True).encode())

    # -- recording facts ----------------------------------------------------------------------

    def record_use(
        self,
        environment: EnvironmentManifest,
        *,
        run_id: UUID,
        call_id: UUID | None = None,
        action_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record one *successfully finished* call against the version it ran in.

        The record is keyed by version and Run, which is what makes the candidate count a count of
        independent Runs: a retry, or ten calls inside one Run, raises ``calls`` without raising the
        number of Runs that used the version.
        """
        moment = at or datetime.now(UTC)
        key = environment_key(environment)
        with self.lock:
            self.versions[key] = _version_record(environment)
            identifier = f"{key}|{run_id}"
            existing = self.uses.get(identifier)
            if existing is None:
                self.uses[identifier] = UseRecord(
                    environment_key=key,
                    run_id=run_id,
                    calls=1,
                    first_at=moment,
                    last_at=moment,
                    last_action=action_id,
                    last_call_id=call_id,
                )
            else:
                self.uses[identifier] = existing.model_copy(
                    update={
                        "calls": existing.calls + 1,
                        "last_at": moment,
                        "last_action": action_id or existing.last_action,
                        "last_call_id": call_id or existing.last_call_id,
                    }
                )
            self._persist()

    def record_artifact(
        self,
        environment: EnvironmentManifest,
        *,
        run_id: UUID,
        session_id: UUID,
        instance_id: UUID,
        resource_id: str,
        resource_name: str,
        retained_at: datetime | None = None,
        size_bytes: int | None = None,
        kind: ArtifactKind = "workspace",
    ) -> RetainedArtifact:
        """Take one private artifact into the cache, with the TTL the policy gives it."""
        moment = retained_at or datetime.now(UTC)
        key = environment_key(environment)
        record = ArtifactRecord(
            artifact_id=uuid4(),
            environment_key=key,
            run_id=run_id,
            session_id=session_id,
            instance_id=instance_id,
            kind=kind,
            resource_id=resource_id,
            resource_name=resource_name,
            size_bytes=size_bytes,
            retained_at=moment,
            expires_at=moment + timedelta(days=self.limits.cache_ttl_days),
        )
        with self.lock:
            self.versions[key] = _version_record(environment)
            self.artifacts[str(record.artifact_id)] = record
            self._prune_deleted()
            self._persist()
        return _artifact_view(record, pinned=False)

    # -- reading ------------------------------------------------------------------------------

    def artifact(self, artifact_id: UUID) -> ArtifactRecord:
        with self.lock:
            record = self.artifacts.get(str(artifact_id))
        if record is None:
            raise RunnerRejected("retention_artifact_unknown")
        return record

    def by_version(self, key: str) -> list[ArtifactRecord]:
        """The retained artifacts of one version, failing loudly for a version nobody recorded."""
        with self.lock:
            if key not in self.versions:
                raise RunnerRejected("retention_version_unknown")
            found = [
                record
                for record in self.artifacts.values()
                if record.environment_key == key and record.state == "retained"
            ]
        return sorted(found, key=lambda item: (item.retained_at, str(item.artifact_id)))

    def retained(self) -> list[ArtifactRecord]:
        """Every artifact the cache still holds, oldest first — the order a reclaim works in."""
        with self.lock:
            live = [record for record in self.artifacts.values() if record.state == "retained"]
        return sorted(live, key=lambda item: (item.retained_at, str(item.artifact_id)))

    def pin_of(self, record: ArtifactRecord) -> PinRecord | None:
        """The pin protecting one artifact: pinned directly, or through its whole version."""
        with self.lock:
            return self.pins.get(f"artifact|{record.artifact_id}") or self.pins.get(
                f"version|{record.environment_key}"
            )

    # -- preview ------------------------------------------------------------------------------

    def preview(
        self,
        *,
        sizes: Mapping[str, int | None] | None = None,
        in_use: frozenset[str] = frozenset(),
        now: datetime | None = None,
    ) -> RetentionPreview:
        """What an automatic reclaim would delete right now, and what it would leave.

        Two rules select the set, in this order: an artifact past its TTL is expired and goes; and
        while the cache is over capacity the least recently retained reclaimable artifacts go too.
        Pinned artifacts, and artifacts an instance still holds, are never selected — they are
        reported as protected with the rule that protects them.
        """
        moment = now or datetime.now(UTC)
        capacity = self.limits.cache_capacity_bytes
        held = self.retained()
        protected: list[ProtectedArtifact] = []
        reclaimable: list[ArtifactRecord] = []
        for record in held:
            pin = self.pin_of(record)
            if pin is not None:
                protected.append(
                    _protected(
                        record,
                        PROTECTED_PINNED
                        if pin.target_kind == "artifact"
                        else PROTECTED_VERSION_PINNED,
                    )
                )
            elif record.resource_id in in_use:
                protected.append(_protected(record, PROTECTED_IN_USE))
            else:
                reclaimable.append(record)

        total = sum(_size(record, sizes) or 0 for record in held)
        unmeasured = sum(1 for record in held if _size(record, sizes) is None)
        chosen = [record for record in reclaimable if record.expires_at <= moment]
        chosen_ids = {str(record.artifact_id) for record in chosen}
        remaining = total - sum(_size(record, sizes) or 0 for record in chosen)
        for record in reclaimable:
            if remaining <= capacity:
                break
            if str(record.artifact_id) in chosen_ids:
                continue
            chosen.append(record)
            chosen_ids.add(str(record.artifact_id))
            remaining -= _size(record, sizes) or 0
        freed = sum(_size(record, sizes) or 0 for record in chosen)
        blocked = remaining > capacity
        return RetentionPreview(
            capacity_bytes=capacity,
            ttl_days=self.limits.cache_ttl_days,
            retained_bytes=total,
            unmeasured_artifacts=unmeasured,
            to_delete=[_artifact_view(record, pinned=False) for record in chosen],
            protected=protected,
            freed_bytes=freed,
            blocked=blocked,
            reason_code=REASON_CAPACITY_INSUFFICIENT if blocked else None,
            needed_bytes=max(0, remaining - capacity) if blocked else 0,
        )

    def view(
        self,
        *,
        sizes: Mapping[str, int | None] | None = None,
        in_use: frozenset[str] = frozenset(),
        now: datetime | None = None,
        available: bool = True,
        reason_code: str | None = None,
        sandbox_management: Literal["disabled", "ready", "unavailable"] = "ready",
    ) -> RetentionView:
        """The whole retention answer: versions, held artifacts, the reclaim preview, the trace."""
        moment = now or datetime.now(UTC)
        held = self.retained()
        artifacts = [
            _artifact_view(record, pinned=self.pin_of(record) is not None) for record in held
        ]
        with self.lock:
            decisions = [
                _decision_view(record) for record in reversed(self.decisions[-DECISIONS_IN_VIEW:])
            ]
        return RetentionView(
            available=available,
            reason_code=reason_code,
            sandbox_management=sandbox_management,
            observed_at=moment,
            limits=self.limits,
            versions=self._versions(sizes=sizes, now=moment),
            artifacts=artifacts,
            preview=self.preview(sizes=sizes, in_use=in_use, now=moment),
            decisions=decisions,
        )

    def _versions(
        self, *, sizes: Mapping[str, int | None] | None, now: datetime
    ) -> list[ToolVersionView]:
        window_start = now - timedelta(days=self.limits.candidate_window_days)
        held = self.retained()
        versions: list[ToolVersionView] = []
        for key, record in self.versions.items():
            uses = [item for item in self.uses.values() if item.environment_key == key]
            inside = [item for item in uses if item.last_at >= window_start]
            mine = [item for item in held if item.environment_key == key]
            versions.append(
                ToolVersionView(
                    environment_key=key,
                    profile_id=record.profile_id,
                    profile_version=record.profile_version,
                    image_digests=dict(record.image_digests),
                    tool_inventory=list(record.tool_inventory),
                    engine=record.engine,
                    architecture=record.architecture,
                    successful_runs=len(inside),
                    successful_calls=sum(item.calls for item in inside),
                    window_days=self.limits.candidate_window_days,
                    threshold_runs=self.limits.candidate_runs,
                    candidate=len(inside) >= self.limits.candidate_runs,
                    last_used_at=max((item.last_at for item in uses), default=None),
                    retained_artifacts=len(mine),
                    retained_bytes=sum(_size(item, sizes) or 0 for item in mine),
                    unmeasured_artifacts=sum(1 for item in mine if _size(item, sizes) is None),
                    pinned=f"version|{key}" in self.pins,
                )
            )
        return sorted(
            versions,
            key=lambda item: (not item.candidate, -(item.last_used_at or now).timestamp()),
        )

    # -- pins ---------------------------------------------------------------------------------

    def pin(
        self,
        *,
        target_kind: Literal["version", "artifact"],
        target: str,
        actor: str | None,
        note: str,
        sizes: Mapping[str, int | None] | None = None,
        in_use: frozenset[str] = frozenset(),
        at: datetime | None = None,
    ) -> RetentionView:
        """Pin a version or one artifact, so an automatic reclaim leaves it alone."""
        moment = at or datetime.now(UTC)
        with self.lock:
            self._require_target(target_kind, target)
            self.pins[f"{target_kind}|{target}"] = PinRecord(
                target_kind=target_kind, target=target, at=moment, actor=actor, note=note or None
            )
            self._record_decision(
                action="pin",
                target_kind=target_kind,
                target=target,
                at=moment,
                actor=actor,
                note=note,
                affected=self._affected_runs(target_kind, target),
            )
            self._persist()
        return self.view(sizes=sizes, in_use=in_use, now=moment)

    def unpin(
        self,
        *,
        target_kind: Literal["version", "artifact"],
        target: str,
        actor: str | None,
        note: str,
        sizes: Mapping[str, int | None] | None = None,
        in_use: frozenset[str] = frozenset(),
        at: datetime | None = None,
    ) -> RetentionView:
        """Remove a pin. Unpinning something that was not pinned is a no-op, and recorded as one."""
        moment = at or datetime.now(UTC)
        with self.lock:
            self._require_target(target_kind, target)
            removed = self.pins.pop(f"{target_kind}|{target}", None)
            self._record_decision(
                action="unpin",
                target_kind=target_kind,
                target=target,
                at=moment,
                actor=actor,
                note=note,
                reason_code=None if removed is not None else "retention_not_pinned",
                affected=self._affected_runs(target_kind, target),
            )
            self._persist()
        return self.view(sizes=sizes, in_use=in_use, now=moment)

    def _require_target(self, target_kind: Literal["version", "artifact"], target: str) -> None:
        if target_kind == "version":
            if target not in self.versions:
                raise RunnerRejected("retention_version_unknown")
        elif target not in self.artifacts:
            raise RunnerRejected("retention_artifact_unknown")

    def _affected_runs(self, target_kind: str, target: str) -> list[UUID]:
        """The Runs a retention decision concerns, so its trace can reach their timelines."""
        if target_kind == "version":
            runs = {
                record.run_id
                for record in self.artifacts.values()
                if record.environment_key == target
            }
            runs.update(
                item.run_id for item in self.uses.values() if item.environment_key == target
            )
            return sorted(runs, key=str)
        record = self.artifacts.get(target)
        return [] if record is None else [record.run_id]

    # -- deletion -----------------------------------------------------------------------------

    def delete_artifacts(
        self,
        records: Sequence[ArtifactRecord],
        *,
        action: RetentionAction,
        target_kind: RetentionTarget,
        target: str,
        actor: str | None,
        note: str,
        remove: Callable[[ArtifactRecord], None],
        reason: str,
        sizes: Mapping[str, int | None] | None = None,
        in_use: frozenset[str] = frozenset(),
        at: datetime | None = None,
    ) -> RetentionReport:
        """Remove artifacts through the caller's removal, then record what really happened.

        The removal is the trusted manager's: it refuses anything this project does not own, and
        anything an instance is still holding. A refusal leaves the artifact in the cache and is
        reported as a failure, so the ledger never claims a removal that did not happen.
        """
        moment = at or datetime.now(UTC)
        deleted: list[UUID] = []
        failed: list[str] = []
        freed = 0
        with self.lock:
            for record in records:
                if record.state != "retained":
                    failed.append(f"retention_artifact_not_retained:{record.artifact_id}")
                    continue
                try:
                    remove(record)
                except (SandboxRejected, RuntimeUnavailable, RunnerRejected) as refusal:
                    failed.append(f"{_reason_of(refusal)}:{record.artifact_id}")
                    continue
                self.artifacts[str(record.artifact_id)] = record.model_copy(
                    update={"state": "deleted", "deleted_at": moment, "delete_reason": reason}
                )
                deleted.append(record.artifact_id)
                freed += _size(record, sizes) or 0
            self._record_decision(
                action=action,
                target_kind=target_kind,
                target=target,
                at=moment,
                actor=actor,
                note=note,
                reason_code=None if not failed else "retention_delete_failed",
                affected=sorted({record.run_id for record in records}, key=str),
                deleted=deleted,
                failed=failed,
                freed=freed,
            )
            self._prune_deleted()
            self._persist()
            decision = self.decisions[-1]
            view = self.view(sizes=sizes, in_use=in_use, now=moment)
        return RetentionReport(
            decision=_decision_view(decision),
            deleted=deleted,
            failed=failed,
            view=view,
        )

    def sweep(
        self,
        *,
        actor: str | None,
        note: str,
        remove: Callable[[ArtifactRecord], None],
        sizes: Mapping[str, int | None] | None = None,
        in_use: frozenset[str] = frozenset(),
        at: datetime | None = None,
    ) -> RetentionReport:
        """Apply the preview: delete what the policy selects, and say whether that was enough.

        A blocked preview is not a reason to delete protected material: the sweep removes exactly
        the reclaimable set, and the resulting view keeps reporting the shortfall.
        """
        moment = at or datetime.now(UTC)
        preview = self.preview(sizes=sizes, in_use=in_use, now=moment)
        chosen = [
            self.artifacts[str(item.artifact_id)]
            for item in preview.to_delete
            if str(item.artifact_id) in self.artifacts
        ]
        expired_only = bool(preview.to_delete) and all(
            item.expires_at <= moment for item in preview.to_delete
        )
        return self.delete_artifacts(
            chosen,
            action="sweep",
            target_kind="deployment",
            target="preview",
            actor=actor,
            note=note,
            remove=remove,
            reason=REASON_TTL_EXPIRED if expired_only else REASON_CAPACITY,
            sizes=sizes,
            in_use=in_use,
            at=moment,
        )

    # -- internals ----------------------------------------------------------------------------

    def _record_decision(
        self,
        *,
        action: RetentionAction,
        target_kind: RetentionTarget,
        target: str,
        at: datetime,
        actor: str | None,
        note: str,
        reason_code: str | None = None,
        affected: Sequence[UUID] = (),
        deleted: Sequence[UUID] = (),
        failed: Sequence[str] = (),
        freed: int = 0,
    ) -> None:
        self.decisions.append(
            DecisionRecord(
                decision_id=uuid4(),
                action=action,
                target_kind=target_kind,
                target=target,
                at=at,
                actor=actor,
                note=note or None,
                reason_code=reason_code,
                affected_runs=list(dict.fromkeys(affected)),
                freed_bytes=freed,
                deleted_artifacts=list(deleted),
                failed=list(failed),
            )
        )
        del self.decisions[:-DECISION_LIMIT]

    def _prune_deleted(self) -> None:
        gone = [record for record in self.artifacts.values() if record.state == "deleted"]
        if len(gone) <= DELETED_ARTIFACT_LIMIT:
            return
        gone.sort(key=lambda item: (item.deleted_at or item.retained_at))
        for record in gone[: len(gone) - DELETED_ARTIFACT_LIMIT]:
            del self.artifacts[str(record.artifact_id)]


def remove_artifact(manager: SandboxManager) -> Callable[[ArtifactRecord], None]:
    """The removal a retention action uses: the trusted manager's own, by identity.

    The manager re-reads the volume from the runtime and refuses one this project does not own or
    one an instance still holds, so a retention record alone never authorizes deleting anything.
    """

    def remove(record: ArtifactRecord) -> None:
        manager.release_artifact(
            instance_id=record.instance_id, volume_id=record.resource_id
        )

    return remove


def _version_record(environment: EnvironmentManifest) -> VersionRecord:
    return VersionRecord(
        environment_key=environment_key(environment),
        profile_id=environment.profile_id,
        profile_version=environment.profile_version,
        image_digests=dict(environment.image_digests),
        tool_inventory=list(environment.tool_inventory),
        engine=environment.engine,
        architecture=environment.architecture,
    )


def _size(record: ArtifactRecord, sizes: Mapping[str, int | None] | None) -> int | None:
    """The freshest size known for one artifact.

    A fresh reading of ``None`` means the engine did not report one this time, which is not the
    same as the artifact being empty: the recorded size is preferred to inventing a zero.
    """
    if sizes is not None:
        fresh = sizes.get(record.resource_id)
        if isinstance(fresh, int):
            return fresh
    return record.size_bytes


def _artifact_view(record: ArtifactRecord, *, pinned: bool) -> RetainedArtifact:
    return RetainedArtifact(
        artifact_id=record.artifact_id,
        environment_key=record.environment_key,
        run_id=record.run_id,
        session_id=record.session_id,
        instance_id=record.instance_id,
        kind=record.kind,
        resource_name=record.resource_name,
        size_bytes=record.size_bytes,
        retained_at=record.retained_at,
        expires_at=record.expires_at,
        pinned=pinned,
        state=record.state,
        deleted_at=record.deleted_at,
        delete_reason=record.delete_reason,
    )


def _protected(record: ArtifactRecord, reason_code: str) -> ProtectedArtifact:
    return ProtectedArtifact(
        artifact_id=record.artifact_id,
        resource_name=record.resource_name,
        run_id=record.run_id,
        reason_code=reason_code,
    )


def _decision_view(record: DecisionRecord) -> RetentionDecisionView:
    return RetentionDecisionView(
        decision_id=record.decision_id,
        action=record.action,
        target_kind=record.target_kind,
        target=record.target,
        at=record.at,
        actor=record.actor,
        note=record.note,
        reason_code=record.reason_code,
        affected_runs=list(record.affected_runs),
        freed_bytes=record.freed_bytes,
        deleted_artifacts=list(record.deleted_artifacts),
        failed=list(record.failed),
    )


def _reason_of(refusal: Exception) -> str:
    reason = getattr(refusal, "reason_code", None)
    return str(reason) if reason else type(refusal).__name__
