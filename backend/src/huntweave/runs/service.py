import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.runs import (
    ProjectCreate,
    ProjectView,
    RunCreate,
    RunView,
    ScopeCreate,
    ScopeSnapshot,
    ScopeView,
)
from huntweave.runs.inputs import expand_ports, preview_targets
from huntweave.storage.database import database_now
from huntweave.storage.models import AuthorizationScope, Project, Run


class RunService:
    def __init__(
        self,
        engine: Callable[[], Engine],
        readiness: Callable[[], bool] | None = None,
    ):
        self.engine = engine
        # Read fresh when a Run asks to act for real: the four gates are a precondition of opening
        # the Run, not a label the deployment keeps once and never checks again.
        self.readiness = readiness

    def _require_real_readiness(self) -> None:
        if self.readiness is None or not self.readiness():
            raise ServiceError("real_execution_not_ready", 409)

    def create_project(self, request: ProjectCreate) -> ProjectView:
        name = request.name.strip()
        if not name:
            raise ServiceError("project_name_required", 422)
        with Session(self.engine()) as session, session.begin():
            record = Project(
                name=name, description=request.description, created_at=database_now(session)
            )
            session.add(record)
            session.flush()
            return self._project(record)

    def projects(self) -> list[ProjectView]:
        with Session(self.engine()) as session:
            return [
                self._project(item)
                for item in session.scalars(
                    select(Project).order_by(Project.created_at.desc()).limit(200)
                )
            ]

    def create_scope(self, request: ScopeCreate) -> ScopeView:
        preview = preview_targets(request.targets_text)
        if not preview.valid:
            # Name the reason the operator has to act on: input over the per-Run limit is a
            # different problem from input the platform cannot parse, and the console shows the
            # offending lines from the same preview either way.
            raise ServiceError(
                "target_limit_exceeded" if preview.over_limit else "invalid_targets", 422
            )
        if not request.authorization.strip():
            raise ServiceError("authorization_required", 422)
        snapshot = ScopeSnapshot(
            targets=sorted(preview.targets),
            ports=expand_ports(request.ports),
            port_profile=request.ports.profile,
            starts_at=request.starts_at.astimezone(UTC),
            expires_at=request.expires_at.astimezone(UTC),
            authorization=request.authorization.strip(),
            budget=request.budget,
        )
        with Session(self.engine()) as session, session.begin():
            now = database_now(session)
            if session.get(Project, request.project_id) is None:
                raise ServiceError("project_not_found", 404)
            if snapshot.starts_at >= snapshot.expires_at:
                raise ServiceError("invalid_authorization_window", 422)
            if snapshot.expires_at <= now:
                raise ServiceError("authorization_expired", 409)
            record = AuthorizationScope(
                project_id=request.project_id,
                version=1,
                snapshot=snapshot.model_dump(mode="json"),
                created_at=now,
            )
            session.add(record)
            session.flush()
            return self._scope(record)

    def scope(self, scope_id: UUID) -> ScopeView:
        with Session(self.engine()) as session:
            record = session.get(AuthorizationScope, scope_id)
            if record is None:
                raise ServiceError("scope_not_found", 404)
            return self._scope(record)

    def create_run(self, request: RunCreate, key: str) -> RunView:
        if request.execution_profile != "fake-p0-v1":
            self._require_real_readiness()
        if not 1 <= len(key) <= 128 or not key.isascii() or any(ord(c) < 33 for c in key):
            raise ServiceError("invalid_idempotency_key", 422)
        canonical = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        lock = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], signed=True)
        with Session(self.engine()) as session, session.begin():
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            existing = session.scalar(select(Run).where(Run.idempotency_key == key))
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ServiceError("idempotency_conflict", 409)
                return self._run(existing)
            scope = session.get(AuthorizationScope, request.scope_id)
            if scope is None:
                raise ServiceError("scope_not_found", 404)
            if scope.version != request.scope_version:
                raise ServiceError("version_conflict", 409)
            snapshot = ScopeSnapshot.model_validate(scope.snapshot)
            if snapshot.execution_profile != request.execution_profile:
                # The authorization decides what may be done with its targets. A Run cannot act
                # for real under a demonstration authorization, or the reverse.
                raise ServiceError("execution_profile_mismatch", 409)
            now = database_now(session)
            self._check_window(snapshot, now)
            record = Run(
                project_id=scope.project_id,
                scope_id=scope.id,
                scope_version=scope.version,
                scope_snapshot=snapshot.model_dump(mode="json"),
                idempotency_key=key,
                request_hash=request_hash,
                status="draft",
                phase=None,
                version=1,
                created_at=now,
                demonstration_scenario=request.demonstration_scenario,
                demonstration_duration_ms=request.demonstration_duration_ms,
                execution_profile=request.execution_profile,
            )
            session.add(record)
            session.flush()
            return self._run(record)

    def run(self, run_id: UUID) -> RunView:
        with Session(self.engine()) as session:
            record = session.get(Run, run_id)
            if record is None:
                raise ServiceError("run_not_found", 404)
            return self._run(record)

    def runs(self) -> list[RunView]:
        with Session(self.engine()) as session:
            return [
                self._run(item)
                for item in session.scalars(select(Run).order_by(Run.created_at.desc()).limit(200))
            ]

    def start(self, run_id: UUID, version: int) -> RunView:
        with Session(self.engine()) as session, session.begin():
            record = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if record is None:
                raise ServiceError("run_not_found", 404)
            if record.version != version:
                raise ServiceError("version_conflict", 409)
            if record.status != "draft":
                raise ServiceError("invalid_run_state", 409)
            self._check_window(
                ScopeSnapshot.model_validate(record.scope_snapshot), database_now(session)
            )
            record.status = "queued"
            record.version += 1
            from huntweave.runs.orchestration import OrchestrationService

            OrchestrationService._event(session, record, "run_queued", {"version": record.version})
            return self._run(record)

    @staticmethod
    def _check_window(snapshot: ScopeSnapshot, now: datetime) -> None:
        if snapshot.expires_at <= now:
            raise ServiceError("authorization_expired", 409)
        if snapshot.starts_at > now:
            raise ServiceError("authorization_not_started", 409)

    @staticmethod
    def _project(record: Project) -> ProjectView:
        return ProjectView.model_validate(record, from_attributes=True)

    @staticmethod
    def _scope(record: AuthorizationScope) -> ScopeView:
        return ScopeView.model_validate(record, from_attributes=True)

    @staticmethod
    def _run(record: Run) -> RunView:
        return RunView.model_validate(record, from_attributes=True)
