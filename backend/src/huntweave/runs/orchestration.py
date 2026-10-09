"""Business Interface for durable execution and human controls.

All mutations serialize on the Run row. No Runner or graph call occurs inside a
business transaction; stable IDs and the transactional outbox survive replay.
"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import (
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
    parameters_hash,
)
from huntweave.contracts.runs import ScopeSnapshot
from huntweave.harness.model import DeterministicModel
from huntweave.runs.service import RunService
from huntweave.storage.database import database_now
from huntweave.storage.models import (
    AgentSession,
    AuditEvent,
    BudgetReservation,
    Decision,
    EventCursor,
    Evidence,
    InterruptionRecord,
    Outbox,
    ResearchTask,
    Run,
    ToolCall,
    ToolResult,
)

TERMINAL_CALLS = {"succeeded", "failed", "cancelled", "denied"}
ACTIVE_RUNS = {"queued", "running", "recovering", "pausing", "cancelling", "waiting"}
ROLES = ["collector", "worker", "reviewer"]
# The demonstration scenario is a fixed mapping; no user text reaches the fake adapter.
SCENARIOS: dict[str, Literal["success", "failure", "needs_evidence"]] = {
    "positive": "success",
    "negative": "needs_evidence",
    "failure": "failure",
}


def stable(run_id: UUID, kind: str) -> UUID:
    return uuid5(run_id, kind)


class OrchestrationService:
    def __init__(self, engine: Callable[[], Engine], evidence_root: Path = Path("/evidence")):
        self.engine = engine
        self.owner = uuid4()
        self.evidence_root = evidence_root
        self.model = DeterministicModel()

    @staticmethod
    def _run(session: Session, run_id: UUID) -> Run:
        record = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if record is None:
            raise ServiceError("run_not_found", 404)
        return record

    @staticmethod
    def _event(
        session: Session, run: Run, kind: str, payload: dict[str, Any], source: str | None = None
    ) -> None:
        if (
            source
            and session.scalar(
                select(AuditEvent.cursor).where(
                    AuditEvent.run_id == run.id, AuditEvent.source_event_id == source
                )
            )
            is not None
        ):
            return
        cursor = session.get(EventCursor, run.id, with_for_update=True)
        if cursor is None:
            cursor = EventCursor(run_id=run.id, cursor=0)
            session.add(cursor)
        cursor.cursor += 1
        session.add(
            AuditEvent(
                run_id=run.id,
                cursor=cursor.cursor,
                type=kind,
                payload=payload,
                source_event_id=source,
                created_at=database_now(session),
            )
        )
        session.flush()

    def _interrupt(self, session: Session, run: Run, reason: str, condition: str) -> None:
        if run.reason_code != reason:
            session.add(
                InterruptionRecord(
                    run_id=run.id,
                    reason_code=reason,
                    recovery_condition=condition,
                    created_at=database_now(session),
                )
            )
            self._event(
                session,
                run,
                "interrupted",
                {"reason_code": reason, "recovery_condition": condition},
            )
        run.reason_code = reason

    def _new_task(self, session: Session, run: Run, role: str) -> ResearchTask:
        task_id = stable(run.id, "task:" + role)
        task = session.get(ResearchTask, task_id)
        if task is None:
            task = ResearchTask(
                id=task_id,
                run_id=run.id,
                role=role,
                status="queued",
                step=0,
                version=1,
                lease_generation=0,
            )
            session.add(task)
            session.flush()
            evidence_ids = [
                str(x)
                for x in session.scalars(select(Evidence.id).where(Evidence.run_id == run.id))
            ]
            # Reviewer only receives evidence references, never another role's transcript.
            context = {"evidence_ids": evidence_ids, "demonstration": True}
            if role != "reviewer":
                context["scenario"] = run.demonstration_scenario
            session.add(
                AgentSession(
                    id=stable(run.id, "session:" + role),
                    run_id=run.id,
                    task_id=task.id,
                    role=role,
                    status="queued",
                    context=context,
                )
            )
            self._event(
                session,
                run,
                "task_created",
                {"task_id": str(task.id), "role": role, "demonstration": True},
            )
        return task

    def claim(self, run_id: UUID | None = None) -> dict[str, Any] | None:
        with Session(self.engine()) as session, session.begin():
            now = database_now(session)
            query = select(Run).where(Run.status.in_(ACTIVE_RUNS))
            if run_id is not None:
                query = query.where(Run.id == run_id)
            candidates = session.scalars(
                query
                .order_by(Run.created_at)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            for run in candidates:
                if run.status == "waiting" and not self._active(session, run.id):
                    continue
                if self._has_unknown_call(session, run.id):
                    # An unconfirmed outcome is never retried automatically, and no pass
                    # can advance this Run. It waits for a human decision instead of
                    # monopolising the single scheduler for every other Run.
                    continue
                if run.status == "queued":
                    run.started_at = now
                    run.status = "running"
                    run.phase = "collecting"
                    run.version += 1
                    self._new_task(session, run, "collector")
                    self._event(session, run, "run_started", {"version": run.version})
                task = session.scalar(
                    select(ResearchTask)
                    .where(
                        ResearchTask.run_id == run.id,
                        ResearchTask.status.in_(
                            ["queued", "running", "recovering", "pausing", "cancelling"]
                        ),
                    )
                    .order_by(ResearchTask.id)
                    .with_for_update(skip_locked=True)
                )
                if task is None:
                    self._converge(session, run)
                    continue
                if (
                    task.lease_owner != self.owner
                    and task.lease_expires_at
                    and task.lease_expires_at > now
                ):
                    continue
                if task.lease_owner != self.owner:
                    task.lease_generation += 1
                    task.lease_owner = self.owner
                    if task.status != "queued":
                        self._event(
                            session,
                            run,
                            "recovery_started",
                            {"task_id": str(task.id), "lease_generation": task.lease_generation},
                        )
                task.lease_expires_at = now + timedelta(seconds=15)
                task.status = (
                    "recovering"
                    if run.status == "waiting"
                    else ("running" if run.status == "running" else run.status)
                )
                agent = session.get(AgentSession, stable(run.id, "session:" + task.role))
                if agent is not None:
                    agent.status = task.status
                return {
                    "run_id": str(run.id),
                    "task_id": str(task.id),
                    "session_id": str(stable(run.id, "session:" + task.role)),
                    "lease_generation": task.lease_generation,
                    "step": task.step,
                }
        return None

    def plan(self, run_id: UUID, task_id: UUID | str, generation: int) -> dict[str, Any] | None:
        task_id = UUID(str(task_id))
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            task = session.get(ResearchTask, task_id, with_for_update=True)
            now = database_now(session)
            if task is None or task.run_id != run.id:
                raise ServiceError("task_not_found", 404)
            if (
                task.lease_generation != generation
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
            ):
                raise ServiceError("lease_stale", 409)
            if run.status != "running":
                return None
            decision_id = stable(run.id, f"decision:{task.role}:{task.step}")
            existing = session.get(Decision, decision_id)
            if existing is not None:
                return {"id": str(existing.id), **existing.content}
            # Reconcile an existing accepted call before any fresh decision.
            active = session.scalar(
                select(ToolCall).where(
                    ToolCall.run_id == run.id, ~ToolCall.status.in_(TERMINAL_CALLS)
                )
            )
            if active is not None:
                return None
            scope = ScopeSnapshot.model_validate(run.scope_snapshot)
            try:
                RunService._check_window(scope, now)
            except ServiceError as error:
                run.status = "waiting"
                run.version += 1
                self._interrupt(
                    session,
                    run,
                    error.reason_code,
                    "A new authorized Run is required after expiry.",
                )
                return None
            agent = session.get(AgentSession, stable(run.id, "session:" + task.role))
            assert agent is not None
            decision = self.model.decide(task.role, task.step, agent.context)
            session.add(
                Decision(
                    id=decision_id,
                    run_id=run.id,
                    session_id=agent.id,
                    step=task.step,
                    content=decision,
                )
            )
            session.flush()
            self._event(
                session,
                run,
                "decision",
                {"decision_id": str(decision_id), "session_id": str(agent.id), **decision},
            )
            if decision["action"] == "finish_research":
                task.status = "completed"
                task.version += 1
                agent.status = "completed"
                self._event(
                    session, run, "task_completed", {"task_id": str(task.id), "role": task.role}
                )
                index = ROLES.index(task.role)
                if index < 2:
                    self._new_task(session, run, ROLES[index + 1])
                    run.phase = "researching" if index == 0 else "reviewing"
                    run.version += 1
                else:
                    run.status = "waiting"
                    run.phase = "awaiting_human"
                    run.version += 1
                    self._event(
                        session,
                        run,
                        "automatic_stage_finished",
                        {
                            "demonstration": True,
                            "version": run.version,
                            "summary": "Fixed fake execution; no real vulnerability conclusion.",
                        },
                    )
                return {"id": str(decision_id), **decision}
            used = (
                session.scalar(
                    select(func.count())
                    .select_from(BudgetReservation)
                    .where(BudgetReservation.run_id == run.id)
                )
                or 0
            )
            output = (
                session.scalar(
                    select(func.coalesce(func.sum(BudgetReservation.output_bytes), 0)).where(
                        BudgetReservation.run_id == run.id
                    )
                )
                or 0
            )
            if (
                used >= scope.budget.max_tool_calls
                or output >= scope.budget.max_output_bytes
                or (
                    run.started_at
                    and now >= run.started_at + timedelta(seconds=scope.budget.max_wall_seconds)
                )
            ):
                run.status = "waiting"
                run.version += 1
                task.status = "blocked"
                agent.status = "blocked"
                self._interrupt(
                    session,
                    run,
                    "budget_exhausted",
                    "Create a new Run with a sufficient budget; this reservation is not reset.",
                )
                return {"id": str(decision_id), **decision}
            call_id = stable(decision_id, "call")
            reservation_id = stable(call_id, "reservation")
            parameters = FakeParameters(
                scenario=SCENARIOS[run.demonstration_scenario],
                duration_ms=run.demonstration_duration_ms,
            )
            deadline = min(
                scope.expires_at,
                now + timedelta(seconds=45),
                run.started_at + timedelta(seconds=scope.budget.max_wall_seconds)
                if run.started_at
                else scope.expires_at,
            )
            ticket = ExecutionRequest(
                call_id=call_id,
                run_id=run.id,
                session_id=agent.id,
                decision_id=decision_id,
                scope_id=run.scope_id,
                budget_reservation_id=reservation_id,
                action_id=decision["action"],
                parameters=parameters,
                parameters_hash=parameters_hash(parameters),
                scope_version=run.scope_version,
                policy_version=1,
                lease_generation=generation,
                lease_expires_at=task.lease_expires_at,
                deadline_at=deadline,
                authorized_until=scope.expires_at,
                target_ip=ip_address(scope.targets[0]),
                target_port=scope.ports[0],
            )
            session.add(
                ToolCall(
                    id=call_id,
                    run_id=run.id,
                    session_id=agent.id,
                    decision_id=decision_id,
                    status="planned",
                    ticket=ticket.model_dump(mode="json"),
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                BudgetReservation(
                    id=reservation_id, run_id=run.id, call_id=call_id, settled=False, output_bytes=0
                )
            )
            session.add(Outbox(call_id=call_id, acknowledged=False))
            self._event(
                session,
                run,
                "tool_planned",
                {
                    "call_id": str(call_id),
                    "action": decision["action"],
                    "parameters": parameters.model_dump(),
                    "demonstration": True,
                },
            )
            return {"id": str(decision_id), **decision}

    def pending(self, run_id: UUID) -> list[dict[str, Any]]:
        with Session(self.engine()) as session:
            return [
                {"id": str(x.id), "status": x.status, "ticket": x.ticket}
                for x in session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id, ~ToolCall.status.in_(TERMINAL_CALLS))
                    .order_by(ToolCall.created_at)
                )
            ]

    def accept(self, record: ExecutionRecord) -> None:
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, record.request.run_id)
            call = session.get(ToolCall, record.request.call_id, with_for_update=True)
            if (
                call is None
                or call.ticket["parameters_hash"] != record.request.parameters_hash
                or str(call.decision_id) != str(record.request.decision_id)
            ):
                raise ServiceError("execution_record_mismatch", 409)
            if session.get(ToolResult, call.id) is not None:
                return
            outbox = session.get(Outbox, call.id)
            assert outbox is not None
            outbox.acknowledged = True
            for event in record.events:
                self._event(
                    session,
                    run,
                    event.type,
                    {"call_id": str(call.id), **event.payload},
                    event.source_event_id,
                )
            if record.status == "unknown":
                call.status = "unknown"
                # Reconcile idempotently: repeated passes must not keep bumping the
                # version a human control request is checked against.
                if run.status not in {"cancelling", "pausing", "waiting"}:
                    run.status = "waiting"
                    run.version += 1
                self._interrupt(
                    session,
                    run,
                    "execution_unknown",
                    "Reconcile Runner ledger and execution identity; never blindly resubmit.",
                )
                return
            if record.status in {"accepted", "running"}:
                call.status = "dispatched" if record.status == "accepted" else "running"
                return
            call.status = {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}[
                record.status
            ]
            result = (
                record.result.model_dump(mode="json")
                if record.result
                else {"output": "", "exit_code": None, "evidence": []}
            )
            result["reason_code"] = record.reason_code
            session.add(
                ToolResult(call_id=call.id, content=result, created_at=database_now(session))
            )
            reservation = session.get(BudgetReservation, record.request.budget_reservation_id)
            assert reservation is not None
            reservation.settled = True
            reservation.output_bytes = sum(x["size_bytes"] for x in result["evidence"])
            for metadata in result["evidence"]:
                if session.get(Evidence, UUID(metadata["id"])) is None:
                    session.add(
                        Evidence(
                            id=UUID(metadata["id"]),
                            run_id=run.id,
                            call_id=call.id,
                            metadata_json=metadata,
                        )
                    )
            agent = session.get(AgentSession, call.session_id)
            assert agent is not None
            task = session.get(ResearchTask, agent.task_id)
            assert task is not None
            task.step += 1
            task.version += 1
            if run.status == "waiting" and run.reason_code == "execution_unknown":
                run.status = "running"
                run.reason_code = None
                run.version += 1
                task.status = "running"
                agent.status = "running"
                self._event(
                    session,
                    run,
                    "recovery_completed",
                    {"call_id": str(call.id), "version": run.version},
                )
            agent.context = {
                **agent.context,
                "last_output": result["output"],
                "last_status": call.status,
                "evidence_ids": [x["id"] for x in result["evidence"]],
            }
            self._event(
                session,
                run,
                "tool_completed",
                {
                    "call_id": str(call.id),
                    "status": call.status,
                    "result": result,
                    "demonstration": True,
                },
            )
            if record.status == "failed" and run.status not in {"pausing", "cancelling"}:
                run.status = "waiting"
                run.version += 1
                task.status = "blocked"
                agent.status = "blocked"
                self._interrupt(
                    session,
                    run,
                    record.reason_code or "dependency_failed",
                    "Inspect fixed failure; resume continues without repeating its effect.",
                )
            if record.status == "cancelled" and run.status not in {"pausing", "cancelling"}:
                run.status = "waiting"
                run.version += 1
                task.status = "blocked"
                agent.status = "blocked"
                self._interrupt(
                    session,
                    run,
                    record.reason_code or "control_lease_expired",
                    "Previous execution stopped; resume continues without replaying its effect.",
                )
            if any(not x["available"] for x in result["evidence"]):
                if run.status not in {"pausing", "cancelling"}:
                    run.status = "waiting"
                    run.version += 1
                    task.status = "blocked"
                    agent.status = "blocked"
                self._interrupt(
                    session,
                    run,
                    "evidence_incomplete",
                    "Restore the execution archive; incomplete evidence cannot support a finding.",
                )
            self._converge(session, run)

    def unknown(self, run_id: UUID, call_id: UUID, reason: str = "execution_unknown") -> None:
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            call = session.get(ToolCall, call_id)
            if call is None or call.status in TERMINAL_CALLS:
                return
            call.status = "unknown"
            # Reconcile idempotently: repeated passes must not keep bumping the version a
            # human control request is checked against.
            if run.status not in {"pausing", "cancelling", "waiting"}:
                run.status = "waiting"
                run.version += 1
            self._interrupt(
                session,
                run,
                reason,
                "Restore Runner connectivity and reconcile the original call_id.",
            )

    def unreachable(self, run_id: UUID, call_id: UUID) -> None:
        """Record one bounded dependency failure per call without guessing its outcome."""
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            self._event(
                session,
                run,
                "execution_unreachable",
                {
                    "call_id": str(call_id),
                    "dependency": "runner",
                    "reason_code": "execution_unreachable",
                    "recovery_condition": "Runner answered again; reconcile this call_id.",
                },
                str(call_id) + ":unreachable",
            )

    @staticmethod
    def _has_unknown_call(session: Session, run_id: UUID) -> bool:
        """True while the Run holds a call whose outcome no durable ledger confirmed."""
        return (
            session.scalar(
                select(ToolCall.id).where(
                    ToolCall.run_id == run_id,
                    ~ToolCall.status.in_(TERMINAL_CALLS),
                    ToolCall.status == "unknown",
                )
            )
            is not None
        )

    def _converge(self, session: Session, run: Run) -> None:
        active = session.scalar(
            select(ToolCall.id).where(
                ToolCall.run_id == run.id, ~ToolCall.status.in_(TERMINAL_CALLS)
            )
        )
        if active is not None:
            return
        if run.status in {"pausing", "cancelling"}:
            cancelling = run.status == "cancelling"
            run.status = "cancelled" if cancelling else "paused"
            run.version += 1
            for task in session.scalars(
                select(ResearchTask).where(
                    ResearchTask.run_id == run.id, ResearchTask.status != "completed"
                )
            ):
                task.status = "cancelled" if cancelling else "paused"
                task.version += 1
                agent = session.get(AgentSession, stable(run.id, "session:" + task.role))
                if agent is not None:
                    agent.status = task.status
            self._event(
                session,
                run,
                "run_cancelled" if cancelling else "run_paused",
                {"version": run.version, "cleanup_confirmed": True},
            )

    def control(self, run_id: UUID, action: str, version: int) -> dict[str, Any]:
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            if run.version != version:
                raise ServiceError("version_conflict", 409)
            if action == "close":
                if (
                    run.status != "waiting"
                    or run.phase != "awaiting_human"
                    or self._active(session, run.id)
                ):
                    raise ServiceError("invalid_run_state", 409)
                run.status = "closed"
                run.version += 1
                self._event(
                    session,
                    run,
                    "human_decision",
                    {"decision": "end_demonstration", "version": run.version},
                )
            elif action == "pause":
                if run.status not in {"queued", "running", "recovering"}:
                    raise ServiceError("invalid_run_state", 409)
                run.status = "pausing"
                run.version += 1
                self._event(session, run, "pause_requested", {"version": run.version})
                self._converge(session, run)
            elif action == "cancel":
                if run.status in {"closed", "cancelled", "failed", "draft"}:
                    raise ServiceError("invalid_run_state", 409)
                run.status = "cancelling"
                run.version += 1
                self._event(session, run, "cancel_requested", {"version": run.version})
                self._converge(session, run)
            elif action == "resume":
                if run.status not in {"paused", "waiting"} or run.phase == "awaiting_human":
                    raise ServiceError("invalid_run_state", 409)
                if self._active(session, run.id):
                    raise ServiceError("execution_reconciliation_required", 409)
                scope = ScopeSnapshot.model_validate(run.scope_snapshot)
                RunService._check_window(scope, database_now(session))
                if run.reason_code in {"budget_exhausted", "evidence_incomplete"}:
                    raise ServiceError(run.reason_code, 409)
                run.status = "running"
                run.reason_code = None
                run.version += 1
                for task in session.scalars(
                    select(ResearchTask).where(
                        ResearchTask.run_id == run.id,
                        ResearchTask.status.in_(["paused", "blocked", "recovering"]),
                    )
                ):
                    task.status = "queued"
                    task.lease_expires_at = None
                    task.lease_owner = None
                    task.version += 1
                if not session.scalar(select(ResearchTask.id).where(ResearchTask.run_id == run.id)):
                    self._new_task(session, run, "collector")
                    run.phase = "collecting"
                    run.started_at = database_now(session)
                self._event(session, run, "resume_requested", {"version": run.version})
            else:
                raise ServiceError("invalid_control", 422)
            session.flush()
            return RunService._run(run).model_dump(mode="json")

    @staticmethod
    def _active(session: Session, run_id: UUID) -> bool:
        return (
            session.scalar(
                select(ToolCall.id).where(
                    ToolCall.run_id == run_id, ~ToolCall.status.in_(TERMINAL_CALLS)
                )
            )
            is not None
        )

    def preview(self, run_id: UUID) -> dict[str, Any]:
        snapshot = self.snapshot(run_id)
        run = snapshot["run"]
        scope = ScopeSnapshot.model_validate(run["scope_snapshot"])
        valid = scope.starts_at <= datetime.now(UTC) < scope.expires_at
        pending = self.pending(run_id)
        reason = "execution_reconciliation_required" if pending else run.get("reason_code")
        return {
            "version": run["version"],
            "last_completed_step": max((x["step"] for x in snapshot["tasks"]), default=0),
            "pending_calls": [{"id": x["id"], "status": x["status"]} for x in pending],
            "remaining_tool_calls": max(
                0, scope.budget.max_tool_calls - snapshot["budget"]["reserved_tool_calls"]
            ),
            "authorization_valid": valid,
            "can_resume": valid
            and not pending
            and run["status"] in {"paused", "waiting"}
            and run["phase"] != "awaiting_human"
            and reason not in {"budget_exhausted", "evidence_incomplete"},
            "expected_actions": [
                "Continue existing graph from durable records; do not repeat completed effects."
            ]
            if not pending
            else ["Reconcile original call IDs with Runner."],
            "reason_code": reason if valid else "authorization_expired",
        }

    def snapshot(self, run_id: UUID) -> dict[str, Any]:
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            calls = list(
                session.scalars(
                    select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at)
                )
            )
            reservations = list(
                session.scalars(select(BudgetReservation).where(BudgetReservation.run_id == run.id))
            )
            cursor = session.get(EventCursor, run.id)
            heartbeat = session.scalar(
                text("SELECT heartbeat_at FROM huntweave.runtime_processes WHERE name='agentd'")
            )
            return {
                "run": RunService._run(run).model_dump(mode="json"),
                "tasks": [
                    {
                        "id": str(x.id),
                        "role": x.role,
                        "status": x.status,
                        "step": x.step,
                        "version": x.version,
                        "lease_generation": x.lease_generation,
                    }
                    for x in session.scalars(
                        select(ResearchTask).where(ResearchTask.run_id == run.id)
                    )
                ],
                "sessions": [
                    {"id": str(x.id), "role": x.role, "status": x.status, "context": x.context}
                    for x in session.scalars(
                        select(AgentSession).where(AgentSession.run_id == run.id)
                    )
                ],
                "decisions": [
                    {"id": str(x.id), "session_id": str(x.session_id), "step": x.step, **x.content}
                    for x in session.scalars(select(Decision).where(Decision.run_id == run.id))
                ],
                "calls": [
                    {
                        "id": str(x.id),
                        "session_id": str(x.session_id),
                        "decision_id": str(x.decision_id),
                        "status": x.status,
                        "action": x.ticket["action_id"],
                        "parameters": x.ticket["parameters"],
                        "result": result.content
                        if (result := session.get(ToolResult, x.id))
                        else None,
                        "evidence_ids": [
                            str(e)
                            for e in session.scalars(
                                select(Evidence.id).where(Evidence.call_id == x.id)
                            )
                        ],
                        "created_at": x.created_at.isoformat(),
                    }
                    for x in calls
                ],
                "budget": {
                    "reserved_tool_calls": len(reservations),
                    "settled_tool_calls": sum(x.settled for x in reservations),
                    "max_tool_calls": run.scope_snapshot["budget"]["max_tool_calls"],
                    "output_bytes": sum(x.output_bytes for x in reservations),
                },
                "interruptions": [
                    {
                        "reason_code": x.reason_code,
                        "recovery_condition": x.recovery_condition,
                        "created_at": x.created_at.isoformat(),
                    }
                    for x in session.scalars(
                        select(InterruptionRecord)
                        .where(InterruptionRecord.run_id == run.id)
                        .order_by(InterruptionRecord.created_at)
                    )
                ],
                "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
                "cursor": cursor.cursor if cursor else 0,
            }

    def history(self, run_id: UUID, after: int = 0, limit: int = 100) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= 500:
            raise ServiceError("invalid_event_cursor", 422)
        with Session(self.engine()) as session:
            if session.get(Run, run_id) is None:
                raise ServiceError("run_not_found", 404)
            cursor = session.get(EventCursor, run_id)
            current = cursor.cursor if cursor else 0
            if after > current:
                raise ServiceError("event_cursor_ahead", 409)
            events = list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.run_id == run_id, AuditEvent.cursor > after)
                    .order_by(AuditEvent.cursor)
                    .limit(limit)
                )
            )
            return {
                "events": [
                    {
                        "cursor": x.cursor,
                        "type": x.type,
                        "payload": x.payload,
                        "created_at": x.created_at.isoformat(),
                    }
                    for x in events
                ],
                "next_cursor": events[-1].cursor if events else after,
                "gap": False,
            }

    def evidence(self, evidence_id: UUID, offset: int = 0, limit: int = 65536) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 65536:
            raise ServiceError("invalid_evidence_range", 422)
        with Session(self.engine()) as session:
            record = session.get(Evidence, evidence_id)
            if record is None:
                raise ServiceError("evidence_not_found", 404)
            metadata = dict(record.metadata_json)
            metadata.update(
                {
                    "run_id": str(record.run_id),
                    "call_id": str(record.call_id),
                    "demonstration": True,
                    "offset": offset,
                }
            )
        path = (self.evidence_root / metadata["relative_path"]).resolve()
        root = self.evidence_root.resolve()
        if not path.is_relative_to(root) or not metadata["available"]:
            return {
                **metadata,
                "available": False,
                "content": "",
                "missing_reason": metadata.get("missing_reason") or "archive_unavailable",
            }
        try:
            # Hash before displaying, using bounded buffers even for large evidence.
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(65536):
                    digest.update(block)
                size = stream.tell()
                if digest.hexdigest() != metadata["sha256"] or size != metadata["size_bytes"]:
                    return {
                        **metadata,
                        "available": False,
                        "content": "",
                        "missing_reason": "archive_hash_mismatch",
                    }
                stream.seek(offset)
                content = stream.read(limit)
            return {
                **metadata,
                "content": content.decode("utf-8", errors="replace"),
                "next_offset": offset + len(content),
                "eof": offset + len(content) >= size,
            }
        except OSError:
            return {
                **metadata,
                "available": False,
                "content": "",
                "missing_reason": "archive_missing",
            }
