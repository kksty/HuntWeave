"""Business Interface for durable execution and human controls.

All mutations serialize on the Run row. No Runner or graph call occurs inside a
business transaction; stable IDs and the transactional outbox survive replay.
"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal, NamedTuple
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import Engine, func, or_, select, text
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import (
    ExecutionObservation,
    ExecutionProfile,
    ExecutionRecord,
    ExecutionRequest,
    FakeParameters,
    parameters_hash,
)
from huntweave.contracts.orchestration import ReconciliationVerdict
from huntweave.contracts.runs import ScopeSnapshot
from huntweave.harness.model import DeterministicModel
from huntweave.runs.events import append_event
from huntweave.runs.service import RunService
from huntweave.runs.suspension import Suspension, suspend
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
    ReconciliationDecision,
    ResearchTask,
    Run,
    ToolCall,
    ToolResult,
)

# A call whose outcome no durable ledger confirmed stays terminal only through a verdict:
# `incomplete` records that the action happened while its result never arrived.
TERMINAL_CALLS = {"succeeded", "failed", "cancelled", "denied", "incomplete"}
ACTIVE_RUNS = {"queued", "running", "recovering", "pausing", "cancelling", "waiting"}
# Conditions a call can still put on its Run, in the order an operator has to clear them.
OUTCOME_UNSETTLED = "outcome_unsettled"
STOP_UNCONFIRMED = "stop_unconfirmed"
CALL_PENDING = "call_pending"
UNRECONCILED_REASONS = {"execution_unknown", "execution_stop_unconfirmed"}
ROLES = ["collector", "worker", "reviewer"]
# The demonstration scenario is a fixed mapping; no user text reaches the fake adapter.
SCENARIOS: dict[str, Literal["success", "failure", "needs_evidence"]] = {
    "positive": "success",
    "negative": "needs_evidence",
    "failure": "failure",
}


def stable(run_id: UUID, kind: str) -> UUID:
    return uuid5(run_id, kind)


def resolve_target(decision: dict[str, Any], scope: ScopeSnapshot, ordinal: int) -> tuple[str, int]:
    """The endpoint one ticket acts on, always from inside the authorized snapshot.

    A plan that names its own target is checked against the snapshot and refused with
    ``scope_denied`` when it names one outside it. Retargeting it silently would attribute
    the action's evidence to an endpoint nobody authorized, and the ticket is the record
    that decides which target a result belongs to. A plan that names no target takes the
    Run's next authorized endpoint, so the successive actions of one Run are not all
    recorded against the first entry of the authorization list.
    """
    named_ip, named_port = decision.get("target_ip"), decision.get("target_port")
    if named_ip is None and named_port is None:
        endpoints = [(ip, port) for ip in scope.targets for port in scope.ports]
        if not endpoints:
            raise ServiceError("scope_denied", 409)
        return endpoints[ordinal % len(endpoints)]
    if named_ip is None or named_port is None:
        raise ServiceError("scope_denied", 409)
    try:
        address = str(ip_address(str(named_ip)))
    except ValueError:
        raise ServiceError("scope_denied", 409) from None
    if type(named_port) is not int or address not in scope.targets or named_port not in scope.ports:
        raise ServiceError("scope_denied", 409)
    return address, named_port


class Redispatch(NamedTuple):
    """The ids of one authorised re-dispatch of an original decision."""

    decision_id: UUID
    call_id: UUID
    replaced_call_id: UUID


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
    def _view(session: Session, run_id: UUID) -> Run:
        """Read a Run for display or reconciliation without taking the queue lock."""
        record = session.get(Run, run_id)
        if record is None:
            raise ServiceError("run_not_found", 404)
        return record

    @staticmethod
    def _event(
        session: Session, run: Run, kind: str, payload: dict[str, Any], source: str | None = None
    ) -> None:
        append_event(session, run.id, kind, payload, source)

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

    def hold_for_readiness(self, run_id: UUID, call_id: UUID, reason: str) -> None:
        """Record why a real call is not being offered to the execution side yet.

        The call keeps its place and its reservation: nothing ran, nothing is lost, and the Run
        states what it is waiting for. Switching the call to the demonstration side would answer
        with fixed output a question that was asked about a real target, which is the one thing
        this must never do.
        """
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            call = session.get(ToolCall, call_id, with_for_update=True)
            if call is None or call.status not in {"planned", "dispatched"}:
                return
            self._interrupt(
                session,
                run,
                reason,
                "Wait for the execution side to report every readiness gate again.",
            )
            self._event(session, run, "tool_held", {"call_id": str(call_id), "reason_code": reason})

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
            real = run.execution_profile != "fake-p0-v1"
            context = {
                "evidence_ids": evidence_ids,
                "demonstration": not real,
                "execution_profile": run.execution_profile,
            }
            if role != "reviewer":
                if real:
                    # What a real plan may look at comes from this Run's own authorized snapshot.
                    context["candidate_ports"] = list(
                        ScopeSnapshot.model_validate(run.scope_snapshot).ports
                    )[:8]
                else:
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
                {"task_id": str(task.id), "role": role, "demonstration": not real},
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
                if self._blockers(session, run.id, OUTCOME_UNSETTLED, STOP_UNCONFIRMED):
                    # An unconfirmed outcome is never retried automatically, and an unconfirmed
                    # stop never gains new actions. The Run waits for a human decision instead
                    # of monopolising the single scheduler for every other Run.
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
            replacing = (
                self._redispatch_target(session, existing) if existing is not None else None
            )
            if existing is not None and replacing is None:
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
            agent = session.get(AgentSession, stable(run.id, "session:" + task.role))
            assert agent is not None
            try:
                RunService._check_window(scope, now)
            except ServiceError as error:
                suspend(
                    session,
                    run,
                    task,
                    agent,
                    Suspension(
                        reason_code=error.reason_code,
                        recovery_condition="A new authorized Run is required after expiry.",
                    ),
                )
                return None
            if replacing is None:
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
            else:
                # The operator's verdict proved the original call never ran, so the same
                # decision is dispatched again: a new call id, related to the call it
                # replaces, and never a second action invented for the same step.
                decision_id = replacing.decision_id
                decision = existing.content  # type: ignore[union-attr]
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
                    {
                        "decision_id": str(decision_id),
                        "session_id": str(agent.id),
                        "redispatch_of": str(replacing.replaced_call_id),
                        **decision,
                    },
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
                            "demonstration": run.execution_profile == "fake-p0-v1",
                            "execution_profile": run.execution_profile,
                            "version": run.version,
                            "summary": (
                                "Fixed fake execution; no real vulnerability conclusion."
                                if run.execution_profile == "fake-p0-v1"
                                else "Real execution against the real authorized targets; "
                                "conclusions still need review."
                            ),
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
                suspend(
                    session,
                    run,
                    task,
                    agent,
                    Suspension(
                        reason_code="budget_exhausted",
                        recovery_condition="Create a new Run with a sufficient budget; "
                        "this reservation is not reset.",
                    ),
                )
                return {"id": str(decision_id), **decision}
            replaces: UUID | None = None
            if replacing is None:
                call_id = stable(decision_id, "call")
                planned_calls = (
                    session.scalar(
                        select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id)
                    )
                    or 0
                )
                try:
                    bound_ip, bound_port = resolve_target(decision, scope, planned_calls)
                except ServiceError as error:
                    # No ticket, no reservation and no outbox entry: a plan aimed outside the
                    # authorization is refused before it can hold budget or reach the Runner.
                    suspend(
                        session,
                        run,
                        task,
                        agent,
                        Suspension(
                            reason_code=error.reason_code,
                            recovery_condition="Plan an authorized target, or widen the "
                            "authorization snapshot.",
                        ),
                    )
                    self._event(
                        session,
                        run,
                        "scope_denied",
                        {
                            "decision_id": str(decision_id),
                            "action": decision["action"],
                            "planned_target_ip": decision.get("target_ip"),
                            "planned_target_port": decision.get("target_port"),
                            "authorized_targets": len(scope.targets),
                            "authorized_ports": len(scope.ports),
                        },
                    )
                    return None
            else:
                # A re-dispatch replays one original action, so it keeps that action's
                # binding: the same plan must not act on a second endpoint the first one
                # was never authorized to move to.
                replaced = session.get(ToolCall, replacing.replaced_call_id)
                assert replaced is not None
                call_id = replacing.call_id
                replaces = replacing.replaced_call_id
                bound_ip, bound_port = (
                    str(replaced.ticket["target_ip"]),
                    int(replaced.ticket["target_port"]),
                )
            reservation_id = stable(call_id, "reservation")
            # The Run's own mode, narrowed to the profiles this build serves: a Run reads its
            # profile once and every ticket it produces belongs to it.
            profile: ExecutionProfile = (
                "real-lab-v1" if run.execution_profile == "real-lab-v1" else "fake-p0-v1"
            )
            if profile == "fake-p0-v1":
                fake = FakeParameters(
                    scenario=SCENARIOS[run.demonstration_scenario],
                    duration_ms=run.demonstration_duration_ms,
                )
                parameters = fake.model_dump()
            else:
                # A real plan carries the parameters of the action it named, and the ticket's own
                # contract validates them against that action's schema. A plan that cannot be
                # expressed is refused rather than passed on as something else.
                parameters = dict(decision.get("parameters") or {})
            deadline = min(
                scope.expires_at,
                now + timedelta(seconds=45),
                run.started_at + timedelta(seconds=scope.budget.max_wall_seconds)
                if run.started_at
                else scope.expires_at,
            )
            try:
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
                    policy_version=scope.policy_version,
                    lease_generation=generation,
                    lease_expires_at=task.lease_expires_at,
                    deadline_at=deadline,
                    authorized_until=scope.expires_at,
                    execution_profile=profile,
                    target_ip=ip_address(bound_ip),
                    target_port=bound_port,
                )
            except ValidationError:
                # The plan named an action this Run's profile does not serve, or parameters its
                # schema does not accept: that call never runs, and the Run says why.
                raise ServiceError("action_not_in_profile", 409) from None
            session.add(
                ToolCall(
                    id=call_id,
                    run_id=run.id,
                    session_id=agent.id,
                    decision_id=decision_id,
                    status="planned",
                    ticket=ticket.model_dump(mode="json"),
                    created_at=now,
                    replaces_call_id=replaces,
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
                    "parameters": parameters,
                    "execution_profile": run.execution_profile,
                    "demonstration": run.execution_profile == "fake-p0-v1",
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

    def reconcilable(self, run_id: UUID) -> list[dict[str, Any]]:
        """Calls the dispatcher must still reconcile, oldest first.

        Broader than :meth:`pending`: a call a verdict already settled still has to be asked
        about until the execution side confirms what it left running, so the Run can converge
        instead of waiting for a confirmation nobody ever requests.
        """
        with Session(self.engine()) as session:
            rows = session.execute(
                select(ToolCall, ReconciliationDecision)
                .outerjoin(ReconciliationDecision, ReconciliationDecision.call_id == ToolCall.id)
                .where(ToolCall.run_id == run_id)
                .order_by(ToolCall.created_at)
            ).all()
            return [
                {"id": str(call.id), "status": call.status, "ticket": call.ticket}
                for call, verdict in rows
                if call.status not in TERMINAL_CALLS or verdict is not None
            ]

    def reconcilable_runs(self, limit: int = 20, offset: int = 0) -> list[UUID]:
        """Live Runs with at least one call left to reconcile, oldest first, for a sweep."""
        with Session(self.engine()) as session:
            verdicts = select(ReconciliationDecision.call_id)
            return list(
                session.scalars(
                    select(ToolCall.run_id)
                    .join(Run, Run.id == ToolCall.run_id)
                    .where(Run.status.not_in({"closed", "cancelled", "failed"}))
                    .where(
                        or_(
                            ~ToolCall.status.in_(TERMINAL_CALLS),
                            ToolCall.id.in_(verdicts),
                        )
                    )
                    .group_by(ToolCall.run_id)
                    .order_by(func.min(ToolCall.created_at))
                    .limit(limit)
                    .offset(offset)
                )
            )

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
            # The execution side's latest word on this call's process and connection is kept even
            # for a call that is already settled: a stop confirmation can arrive after the outcome
            # did, and every reader of the call — reconciliation, capacity and the console — has to
            # see that fact rather than the one that was true when the outcome landed.
            if record.observation is not None:
                call.observation = record.observation.model_dump(mode="json")
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
                verdict = session.get(ReconciliationDecision, call.id)
                if record.observation is not None:
                    call.observation = record.observation.model_dump(mode="json")
                self._keep_runtime(call, record)
                if verdict is None:
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
                else:
                    # The verdict settled what this Run may do with the call; the ledger's
                    # own facts still arrive and still decide whether the Run can converge.
                    self._refresh_interruption(session, run)
                self._converge(session, run)
                return
            if record.status in {"accepted", "running"}:
                call.status = "dispatched" if record.status == "accepted" else "running"
                if record.observation is not None:
                    call.observation = record.observation.model_dump(mode="json")
                self._keep_runtime(call, record)
                return
            call.status = {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}[
                record.status
            ]
            self._keep_runtime(call, record)
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
                # What the tool really reported is what the next decision reads. An action that
                # declares no summary leaves this empty rather than inventing facts.
                "last_summary": result.get("summary") or {},
                # Where it reported it from: a plan that wants to look at what it just found
                # names this endpoint, and the authorization decides whether that is allowed.
                "last_target": {
                    "ip": str(record.request.target_ip),
                    "port": record.request.target_port,
                },
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
                    "execution_profile": run.execution_profile,
                    "demonstration": run.execution_profile == "fake-p0-v1",
                },
            )
            if record.status == "failed" and run.status not in {"pausing", "cancelling"}:
                suspend(
                    session,
                    run,
                    task,
                    agent,
                    Suspension(
                        reason_code=record.reason_code or "dependency_failed",
                        recovery_condition="Inspect fixed failure; resume continues without "
                        "repeating its effect.",
                    ),
                )
            if record.status == "cancelled" and run.status not in {"pausing", "cancelling"}:
                suspend(
                    session,
                    run,
                    task,
                    agent,
                    Suspension(
                        reason_code=record.reason_code or "control_lease_expired",
                        recovery_condition="Previous execution stopped; resume continues "
                        "without replaying its effect.",
                    ),
                )
            if any(not x["available"] for x in result["evidence"]):
                if run.status not in {"pausing", "cancelling"}:
                    suspend(
                        session,
                        run,
                        task,
                        agent,
                        Suspension(
                            reason_code="evidence_incomplete",
                            recovery_condition="Restore the execution archive; incomplete "
                            "evidence cannot support a finding.",
                        ),
                    )
                else:
                    # A Run already ending does not change state here, but it still has to
                    # state the reason its evidence cannot support a finding.
                    self._interrupt(
                        session,
                        run,
                        "evidence_incomplete",
                        "Restore the execution archive; incomplete evidence cannot "
                        "support a finding.",
                    )
            self._converge(session, run)

    def _keep_runtime(self, call: ToolCall, record: ExecutionRecord) -> None:
        """Keep the execution side's statement about where this call acted, and how it went.

        Stored on the call row rather than derived at read time: the runtime view is the execution
        side's own account of one call (argv, cwd, user, instance, image digests, gateways), and a
        profile that changed since then must not silently rewrite what an old call ran. The progress
        view is stored with it for the same reason — how long a call ran and when it last spoke are
        facts about that call, not properties of the deployment answering the request now.
        """
        if record.runtime is not None:
            call.runtime = record.runtime.model_dump(mode="json")
        if record.progress is not None:
            call.progress = record.progress.as_view().model_dump(mode="json")

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

    def reconcile(
        self,
        run_id: UUID,
        call_id: UUID,
        verdict: ReconciliationVerdict,
        operator_session_id: UUID,
        observation: ExecutionObservation | None,
    ) -> dict[str, Any]:
        """Record an operator's evidence-bound verdict on a call with an unknown outcome.

        The verdict is business-side only: it never rewrites the execution ledger, never
        supplies a stop the execution side has not confirmed, and never turns a missing
        result into a ToolResult. ``observation`` is what the trusted ledger says right now.
        """
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            call = session.get(ToolCall, call_id, with_for_update=True)
            if call is None or call.run_id != run.id:
                raise ServiceError("call_not_found", 404)
            recorded = session.get(ReconciliationDecision, call.id)
            if recorded is not None:
                # Repeating a verdict is idempotent, so it is answered before the version is
                # fenced: a lost response is not a second decision on a changed Run. Changing
                # the answer, though, would rewrite an execution-bound record in place.
                if recorded.outcome != verdict.outcome:
                    raise ServiceError("reconciliation_conflict", 409)
                return self._verdict_result(session, run, call, recorded)
            if run.version != verdict.version:
                raise ServiceError("version_conflict", 409)
            if call.status != "unknown":
                raise ServiceError("invalid_call_state", 409)
            self._check_citations(session, run, verdict.evidence_ids)
            if verdict.outcome == "not_executed":
                redispatch = self._authorize_redispatch(observation)
            elif verdict.outcome == "executed":
                self._require_executed_basis(observation)
                redispatch = False
            else:
                redispatch = False
            latest = observation or self._stored_observation(call)
            decision = ReconciliationDecision(
                call_id=call.id,
                run_id=run.id,
                outcome=verdict.outcome,
                operator_session_id=operator_session_id,
                scope_version=run.scope_version,
                evidence_ids=[str(x) for x in verdict.evidence_ids],
                note=verdict.note,
                observation=latest.model_dump(mode="json") if latest else None,
                redispatch_authorized=redispatch,
                created_at=database_now(session),
            )
            session.add(decision)
            run.version += 1
            if latest is not None:
                call.observation = latest.model_dump(mode="json")
            if verdict.outcome == "not_executed":
                call.status = "cancelled"
            elif verdict.outcome == "executed":
                call.status = "incomplete"
                self._advance_past_unreported_step(session, call)
            self._event(
                session,
                run,
                "reconciliation_recorded",
                {
                    "call_id": str(call.id),
                    "outcome": verdict.outcome,
                    "operator_session_id": str(operator_session_id),
                    "scope_version": run.scope_version,
                    "evidence_ids": [str(x) for x in verdict.evidence_ids],
                    "note": verdict.note,
                    "call_status": call.status,
                    "stop_confirmed": self._stop_confirmed(call.observation),
                    "redispatch_authorized": redispatch,
                },
                str(call.id) + ":reconciliation",
            )
            self._refresh_interruption(session, run)
            self._converge(session, run)
            session.flush()
            return self._verdict_result(session, run, call, decision)

    @staticmethod
    def _check_citations(session: Session, run: Run, evidence_ids: list[UUID]) -> None:
        """A verdict's basis must exist inside this Run; a stray id is no basis."""
        if not evidence_ids:
            return
        found = set(
            session.scalars(
                select(Evidence.id).where(Evidence.run_id == run.id, Evidence.id.in_(evidence_ids))
            )
        )
        if found != set(evidence_ids):
            raise ServiceError("reconciliation_evidence_missing", 409)

    def _authorize_redispatch(self, observation: ExecutionObservation | None) -> bool:
        """Whether the trusted record lets the same decision run again on a new call id.

        The operator clicks; the execution side proves. Without a record that the action
        never started, or with a process, connection or lease it cannot account for, no
        verdict re-dispatches: the old call might still be the one holding the target.
        """
        if observation is None:
            raise ServiceError("reconciliation_evidence_missing", 409)
        if observation.started:
            raise ServiceError("reconciliation_evidence_contradicted", 409)
        if observation.lease_active is not False:
            raise ServiceError("reconciliation_lease_active", 409)
        if not self._stop_confirmed(observation.model_dump(mode="json")):
            raise ServiceError("execution_stop_unconfirmed", 409)
        return True

    @staticmethod
    def _require_executed_basis(observation: ExecutionObservation | None) -> None:
        """Confirming execution is settled from what exists, and only what exists."""
        if observation is None:
            raise ServiceError("reconciliation_evidence_missing", 409)
        if not observation.started:
            raise ServiceError("reconciliation_evidence_contradicted", 409)

    @staticmethod
    def _advance_past_unreported_step(session: Session, call: ToolCall) -> None:
        """Move the role past a step whose result nobody can report.

        No output is invented for the agent, so the missing result stays visible in the
        call history instead of being papered over as a success.
        """
        agent = session.get(AgentSession, call.session_id)
        if agent is None:
            return
        task = session.get(ResearchTask, agent.task_id)
        if task is not None:
            task.step += 1
            task.version += 1
        agent.context = {
            **agent.context,
            "last_status": "incomplete",
            "unreported_call_id": str(call.id),
        }

    @staticmethod
    def _stored_observation(call: ToolCall) -> ExecutionObservation | None:
        if not call.observation:
            return None
        return ExecutionObservation.model_validate(call.observation)

    @staticmethod
    def _redispatch_target(session: Session, original: Decision | None) -> Redispatch | None:
        """The ids of one authorised re-dispatch of an original decision, if any.

        A verdict that proved the original action never ran is the only thing that lets the
        same decision be dispatched again; the replacement takes a new call id and names the
        call it replaces. A graph replay finds the replacement already there and dispatches
        nothing.
        """
        if original is None:
            return None
        call = session.scalar(select(ToolCall).where(ToolCall.decision_id == original.id))
        if call is None:
            return None
        recorded = session.get(ReconciliationDecision, call.id)
        if recorded is None or not recorded.redispatch_authorized:
            return None
        call_id = stable(call.id, "redispatch")
        if session.get(ToolCall, call_id) is not None:
            return None
        return Redispatch(stable(call.id, "redispatch-decision"), call_id, call.id)

    @staticmethod
    def _stop_confirmed(observation: dict[str, Any] | None) -> bool:
        """A stop is proven by the execution side, never by an operator's verdict.

        The process, the connection and the old control lease are one answer: a ledger that
        reports ``None`` for any of them is saying it cannot confirm, which is not a stop. A
        shell may have left descendants behind, and a live lease can still authorise the call
        the Run is trying to leave behind.
        """
        return bool(
            observation
            and observation.get("process_active") is False
            and observation.get("connection_open") is False
            and observation.get("lease_active") is False
        )

    def _call_conditions(
        self, call: ToolCall, verdict: ReconciliationDecision | None
    ) -> list[str]:
        """What one call still asks of its Run, in the order an operator clears it."""
        conditions = []
        if call.status in {"planned", "dispatched", "running"}:
            # An ordinary call in flight: it holds the Run, but it needs no decision.
            conditions.append(CALL_PENDING)
        if call.status == "unknown" or verdict is not None:
            unsettled = verdict is None or verdict.outcome == "undetermined"
            if call.status == "unknown" and unsettled:
                conditions.append(OUTCOME_UNSETTLED)
            if not self._stop_confirmed(call.observation):
                conditions.append(STOP_UNCONFIRMED)
        return conditions

    def _outstanding(self, session: Session, run_id: UUID) -> list[tuple[ToolCall, list[str]]]:
        """Every call that still puts a condition on its Run, oldest first.

        Outcomes and resources are separate conditions: an unsettled outcome stops the Run
        from building on a result nobody has, while an unconfirmed stop stops it from adding
        anything to a session that may still be running.
        """
        rows = session.execute(
            select(ToolCall, ReconciliationDecision)
            .outerjoin(ReconciliationDecision, ReconciliationDecision.call_id == ToolCall.id)
            .where(ToolCall.run_id == run_id)
            .order_by(ToolCall.created_at)
        ).all()
        outstanding = []
        for call, verdict in rows:
            conditions = self._call_conditions(call, verdict)
            if conditions:
                outstanding.append((call, conditions))
        return outstanding

    def _blockers(self, session: Session, run_id: UUID, *wanted: str) -> list[ToolCall]:
        return [
            call
            for call, conditions in self._outstanding(session, run_id)
            if set(conditions) & set(wanted)
        ]

    def _refresh_interruption(self, session: Session, run: Run) -> None:
        """Keep the Run's stated reason equal to what its calls actually still need."""
        conditions = {x for _, items in self._outstanding(session, run.id) for x in items}
        if STOP_UNCONFIRMED in conditions:
            self._interrupt(
                session,
                run,
                "execution_stop_unconfirmed",
                "Confirm the call's process and connection stopped; no new action runs "
                "against a session that may still be occupied.",
            )
        elif OUTCOME_UNSETTLED in conditions:
            self._interrupt(
                session,
                run,
                "execution_unknown",
                "Reconcile the original call_id from evidence; never blindly resubmit.",
            )
        elif run.reason_code in UNRECONCILED_REASONS:
            previous = run.reason_code
            run.reason_code = None
            self._event(
                session,
                run,
                "interruption_resolved",
                {"previous_reason_code": previous, "version": run.version},
            )

    @staticmethod
    def _unreported_calls(session: Session, run_id: UUID) -> list[str]:
        """Calls the Run ends with no confirmed result for, oldest first.

        These are what make an end limited: the action either never got a confirmed outcome
        or was settled as executed without its result ever arriving. A call a verdict proved
        never ran is not one of them, because its outcome is known.
        """
        return [
            str(call_id)
            for call_id in session.scalars(
                select(ToolCall.id)
                .where(
                    ToolCall.run_id == run_id,
                    ToolCall.status.in_(["unknown", "incomplete"]),
                )
                .order_by(ToolCall.created_at)
            )
        ]

    def _converge(self, session: Session, run: Run) -> None:
        # An unsettled outcome does not keep a Run alive: with every stop confirmed the Run
        # ends on the facts it has, marked as limited instead of pretending to be normal.
        if self._blockers(session, run.id, CALL_PENDING, STOP_UNCONFIRMED):
            return
        if run.status in {"pausing", "cancelling"}:
            cancelling = run.status == "cancelling"
            incomplete = self._unreported_calls(session, run.id)
            run.status = "cancelled" if cancelling else "paused"
            run.version += 1
            if incomplete:
                # Ended on the facts it has: the Run says so, instead of looking like a
                # normal end while a call's result is still missing.
                run.reason_code = "result_incomplete"
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
                {
                    "version": run.version,
                    "cleanup_confirmed": True,
                    "limited": bool(incomplete),
                    "incomplete_calls": incomplete,
                },
            )

    def control(self, run_id: UUID, action: str, version: int) -> dict[str, Any]:
        with Session(self.engine()) as session, session.begin():
            run = self._run(session, run_id)
            if run.version != version:
                raise ServiceError("version_conflict", 409)
            if action == "close":
                if run.status != "waiting" or run.phase != "awaiting_human":
                    raise ServiceError("invalid_run_state", 409)
                if self._blockers(session, run.id, CALL_PENDING):
                    raise ServiceError("invalid_run_state", 409)
                # A verdict never stands in for a stop: closing on unknown resources would
                # hide work the operator still has to confirm.
                if self._blockers(session, run.id, STOP_UNCONFIRMED):
                    raise ServiceError("execution_stop_unconfirmed", 409)
                incomplete = self._unreported_calls(session, run.id)
                run.status = "closed"
                run.version += 1
                if incomplete:
                    # A human end is still a limited end while a result nobody confirmed.
                    run.reason_code = "result_incomplete"
                self._event(
                    session,
                    run,
                    "human_decision",
                    {
                        "decision": "end_demonstration",
                        "version": run.version,
                        "limited": bool(incomplete),
                        "incomplete_calls": incomplete,
                    },
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
                # Resuming starts new actions: they must not run against a session whose
                # earlier process or connection is still unaccounted for.
                if self._blockers(session, run.id, STOP_UNCONFIRMED):
                    raise ServiceError("execution_stop_unconfirmed", 409)
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
            # The API revalidates this payload and derives its presentation fields itself.
            return RunService._run(run).model_dump(mode="json", exclude={"demonstration"})

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
        with Session(self.engine()) as session:
            self._view(session, run_id)
            outstanding = self._outstanding(session, run_id)
        conditions = {item for _, items in outstanding for item in items}
        reason = run.get("reason_code")
        if OUTCOME_UNSETTLED in conditions:
            reason = "execution_reconciliation_required"
        elif STOP_UNCONFIRMED in conditions:
            reason = "execution_stop_unconfirmed"
        expected = []
        if OUTCOME_UNSETTLED in conditions:
            expected.append("Reconcile the unknown call from evidence before continuing.")
        if STOP_UNCONFIRMED in conditions:
            expected.append("Confirm the call's process and connection stopped.")
        if not expected:
            expected.append(
                "Continue existing graph from durable records; do not repeat completed effects."
            )
        return {
            "version": run["version"],
            "last_completed_step": max((x["step"] for x in snapshot["tasks"]), default=0),
            "pending_calls": [
                {"id": str(call.id), "status": call.status, "conditions": items}
                for call, items in outstanding
            ],
            "remaining_tool_calls": max(
                0, scope.budget.max_tool_calls - snapshot["budget"]["reserved_tool_calls"]
            ),
            "authorization_valid": valid,
            "can_resume": valid
            and not conditions
            and run["status"] in {"paused", "waiting"}
            and run["phase"] != "awaiting_human"
            and run.get("reason_code") not in {"budget_exhausted", "evidence_incomplete"},
            "expected_actions": expected,
            "reason_code": reason if valid else "authorization_expired",
        }

    def _call_view(
        self,
        session: Session,
        call: ToolCall,
        recorded: ReconciliationDecision | None = None,
    ) -> dict[str, Any]:
        """One call as the console sees it: execution facts, and any verdict kept apart.

        Everything a reader needs in order to separate "what the plan said" from "what really ran"
        is here: the ticket's parameters, the hash that identifies them, and the execution side's
        own statement of the argv, working directory, user, instance and environment it used. When
        no such statement was recorded — calls made before the execution side started writing one —
        the field is `None` rather than a value reconstructed from today's profile.
        """
        verdict = recorded or session.get(ReconciliationDecision, call.id)
        result = session.get(ToolResult, call.id)
        return {
            "id": str(call.id),
            "session_id": str(call.session_id),
            "decision_id": str(call.decision_id),
            "status": call.status,
            "action": call.ticket["action_id"],
            "parameters": call.ticket["parameters"],
            "parameters_hash": call.ticket["parameters_hash"],
            "runtime": call.runtime,
            "progress": self._progress_view(call, result),
            "result": result.content if result else None,
            "evidence_ids": [
                str(e)
                for e in session.scalars(select(Evidence.id).where(Evidence.call_id == call.id))
            ],
            "created_at": call.created_at.isoformat(),
            "replaces_call_id": str(call.replaces_call_id) if call.replaces_call_id else None,
            "observation": self._observation_view(call.observation),
            "reconciliation": self._reconciliation_view(verdict) if verdict else None,
            "conditions": self._call_conditions(call, verdict),
        }

    def _progress_view(
        self, call: ToolCall, result: ToolResult | None
    ) -> dict[str, Any] | None:
        """When this call last spoke, and how long it has been going.

        The stored view is the execution side's own account. When there is none — calls made before
        this side started writing one — the platform adds only what it can prove from the call row:
        that the call has produced nothing at all. Everything else stays empty, because the console
        answers "is this stuck or just quiet" from facts and never from a guess.
        """
        stored = dict(call.progress) if call.progress else {}
        if "last_output_at" not in stored:
            stored["last_output_at"] = None
        if "no_output_yet" not in stored:
            stored["no_output_yet"] = result is None or not (result.content or {}).get("output")
        return stored or None

    def _observation_view(self, observation: dict[str, Any] | None) -> dict[str, Any] | None:
        if not observation:
            return None
        return {**observation, "stop_confirmed": self._stop_confirmed(observation)}

    def _reconciliation_view(self, verdict: ReconciliationDecision) -> dict[str, Any]:
        return {
            "call_id": str(verdict.call_id),
            "outcome": verdict.outcome,
            "operator_session_id": str(verdict.operator_session_id),
            "scope_version": verdict.scope_version,
            "evidence_ids": verdict.evidence_ids,
            "note": verdict.note,
            "observation": self._observation_view(verdict.observation),
            "redispatch_authorized": verdict.redispatch_authorized,
            "recorded_at": verdict.created_at.isoformat(),
        }

    def _verdict_result(
        self, session: Session, run: Run, call: ToolCall, verdict: ReconciliationDecision
    ) -> dict[str, Any]:
        return {
            # `demonstration` is computed on the way out rather than stored, so it is left out here:
            # the caller's contract derives it again from `execution_profile`, and a field handed
            # over as if it were a column is exactly what a strict contract has to refuse.
            "run": RunService._run(run).model_dump(mode="json", exclude={"demonstration"}),
            "call": self._call_view(session, call, verdict),
        }

    def snapshot(self, run_id: UUID) -> dict[str, Any]:
        with Session(self.engine()) as session, session.begin():
            run = self._view(session, run_id)
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
                # `demonstration` is derived from the execution profile on the way out, so it is
                # left out of the payload here: the view that carries this Run to the console
                # derives it again, and a derived field travelling as if it were a column is
                # exactly what a strict contract must refuse.
                "run": RunService._run(run).model_dump(mode="json", exclude={"demonstration"}),
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
                "calls": [self._call_view(session, x) for x in calls],
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
            run = session.get(Run, record.run_id)
            metadata = dict(record.metadata_json)
            metadata.update(
                {
                    "run_id": str(record.run_id),
                    "call_id": str(record.call_id),
                    # Evidence says which side produced it, so a report never presents a fixed
                    # fixture as a real observation or the reverse.
                    "execution_profile": run.execution_profile if run is not None else "fake-p0-v1",
                    "demonstration": run is None or run.execution_profile == "fake-p0-v1",
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
