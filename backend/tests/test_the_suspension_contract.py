"""The suspension contract: what "this Run stops here" consists of, and what it does not.

Six reason paths stop a Run's progress and state the condition under which it may move again. Five
of them also block the research task and its agent session, because that work cannot continue; the
authorization-window path does not, because the Run is waiting for a new authorization rather than
for that task. The distinction is load-bearing — `OrchestrationService.claim` selects tasks in
`blocked` — so it is a property of this module, checked here rather than left to a caller.

`run.version` moves with `run.status` in the same place, because the two are one state change: the
version is what a human control request is compared against. These checks hold that both halves
happen, and that the interruption is recorded exactly when the Run does not already carry it.

The database clock and the event append are stubbed: this is about the decisions `suspend` makes,
not about the transaction it participates in, which the integration checks cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from huntweave.runs import suspension


@dataclass
class _Run:
    id: UUID
    status: str = "running"
    version: int = 7
    reason_code: str | None = None


@dataclass
class _Task:
    id: UUID
    run_id: UUID
    status: str = "running"


@dataclass
class _Agent:
    id: UUID
    task_id: UUID
    status: str = "running"


class _Session:
    """Records what would be added to the transaction."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _fixture() -> tuple[_Session, _Run, _Task, _Agent]:
    session = _Session()
    run = _Run(id=uuid4())
    task = _Task(id=uuid4(), run_id=run.id)
    agent = _Agent(id=uuid4(), task_id=task.id)
    return session, run, task, agent


def _stub(monkeypatch: Any) -> list[tuple[UUID, str, dict[str, Any]]]:
    recorded: list[tuple[UUID, str, dict[str, Any]]] = []

    def append_event(
        session: Any, run_id: UUID, kind: str, payload: dict[str, Any], source: str | None = None
    ) -> None:
        recorded.append((run_id, kind, payload))

    monkeypatch.setattr(suspension, "append_event", append_event)
    monkeypatch.setattr(suspension, "database_now", lambda session: datetime.now(UTC))
    return recorded


def test_a_suspension_moves_the_version_and_blocks_the_work(monkeypatch: Any) -> None:
    events = _stub(monkeypatch)
    session, run, task, agent = _fixture()

    suspension.suspend(
        session,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        suspension.Suspension(
            reason_code="budget_exhausted", recovery_condition="Create a new Run."
        ),
        task=task,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
    )

    assert run.status == "waiting"
    # The version is the fence a human control request is compared against: a state change that
    # did not move it would let a stale request land on a Run that already changed.
    assert run.version == 8
    assert task.status == "blocked"
    assert agent.status == "blocked"
    assert run.reason_code == "budget_exhausted"
    assert len(session.added) == 1
    assert session.added[0].reason_code == "budget_exhausted"
    assert session.added[0].recovery_condition == "Create a new Run."
    assert events == [
        (
            run.id,
            "interrupted",
            {"reason_code": "budget_exhausted", "recovery_condition": "Create a new Run."},
        )
    ]


def test_a_suspension_without_a_task_leaves_the_work_alone(monkeypatch: Any) -> None:
    """The authorization-window path: the Run waits, the task does not become blocked.

    Blocking it would claim the task cannot continue, which the absence of an authorization does
    not say — the same task may run once a new Run is authorized.
    """
    _stub(monkeypatch)
    session, run, task, agent = _fixture()

    suspension.suspend(
        session,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        suspension.Suspension(
            reason_code="authorization_expired", recovery_condition="Authorize a new Run."
        ),
    )

    assert run.status == "waiting"
    assert run.version == 8
    assert task.status == "running", "this path never blocked the task"
    assert agent.status == "running"
    assert run.reason_code == "authorization_expired"


def test_blocking_the_task_without_its_session_is_refused(monkeypatch: Any) -> None:
    """The two travel together: half of that state change is not a state change."""
    _stub(monkeypatch)
    session, run, task, _ = _fixture()

    try:
        suspension.suspend(
            session,  # type: ignore[arg-type]
            run,  # type: ignore[arg-type]
            suspension.Suspension(reason_code="budget_exhausted", recovery_condition="Create one."),
            task=task,  # type: ignore[arg-type]
        )
    except ValueError:
        return
    raise AssertionError("a suspension blocking a task without its session must be refused")


def test_a_repeat_of_the_same_reason_records_one_interruption(monkeypatch: Any) -> None:
    events = _stub(monkeypatch)
    session, run, task, agent = _fixture()
    run.reason_code = "budget_exhausted"

    suspension.suspend(
        session,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        suspension.Suspension(
            reason_code="budget_exhausted", recovery_condition="Create a new Run."
        ),
        task=task,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
    )

    # The Run already stated this reason, so nothing is recorded twice — but the state did change,
    # so the version still moves and the work is still blocked.
    assert session.added == []
    assert events == []
    assert run.version == 8
    assert run.status == "waiting"


def test_a_different_reason_is_recorded_beside_the_one_already_stated(monkeypatch: Any) -> None:
    events = _stub(monkeypatch)
    session, run, task, agent = _fixture()
    run.reason_code = "execution_unknown"

    suspension.suspend(
        session,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        suspension.Suspension(
            reason_code="budget_exhausted", recovery_condition="Create a new Run."
        ),
        task=task,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
    )

    assert len(session.added) == 1
    assert len(events) == 1
    assert run.reason_code == "budget_exhausted"


def test_recording_an_interruption_does_not_suspend_the_run(monkeypatch: Any) -> None:
    """The record-only paths state why the Run cannot go on without changing its state."""
    events = _stub(monkeypatch)
    session, run, _, _ = _fixture()

    suspension.record_interruption(  # type: ignore[arg-type]
        session, run, "execution_unknown", "Reconcile the call."
    )

    assert run.status == "running", "recording a reason is not suspending the Run"
    assert run.version == 7
    assert run.reason_code == "execution_unknown"
    assert len(session.added) == 1
    assert len(events) == 1
