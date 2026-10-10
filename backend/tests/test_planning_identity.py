"""Independent identities, and the two rules that keep a model request out of a transaction.

Two P0 assumptions stop working the moment a remote model is attached (issue #43,
`docs/specs/0003-agent-research.md` section 5.1), and both are mechanical enough to check without a
database:

* A task was identified by `run_id + role`, so two Workers of one role — or one role asked for a
  second round of evidence — shared a session, a decision and a call.
* The model was asked from inside `runs`' transaction, while the Run's row lock was held.

The checks here pin the identity scheme, the refusal that answers a late suggestion, the accounting
rule that keeps unknown usage out of the known total, and the shape of the code that calls a model:
`begin_planning` -> the adapter (no transaction) -> `commit_planning`.
"""

import inspect
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from huntweave.contracts.orchestration import PlanningAttemptView
from huntweave.harness.graph import ResearchHarness
from huntweave.harness.model import (
    PROMPT_VERSION,
    DeterministicModel,
    ModelAdapter,
    ModelRequest,
    ModelSuggestion,
    ModelUsage,
)
from huntweave.runs.orchestration import (
    ATTEMPT_APPLIED,
    USAGE_KNOWN,
    USAGE_UNKNOWN,
    OrchestrationService,
    application_refusal,
    attempt_identity,
    model_usage_summary,
    session_identity,
    stable,
    task_identity,
)
from huntweave.storage.models import Decision

REPOSITORY = Path(__file__).resolve().parents[2]
SOURCE = REPOSITORY / "backend" / "src" / "huntweave"

# Packages whose types must not reach the contract layer or the business rules: a model framework
# and a provider SDK are harness concerns, and a transport or database client is not a business
# vocabulary either.
FRAMEWORK_OR_VENDOR = (
    "langchain",
    "langgraph",
    "openai",
    "anthropic",
    "google",
    "transformers",
    "litellm",
    "ollama",
    "httpx",
    "docker",
    "psycopg",
    "sqlalchemy",
)


# -- identity ------------------------------------------------------------------------------------


def test_two_workers_of_one_role_cannot_share_a_task_session_decision_or_call() -> None:
    """The defect this replaces: `stable(run_id, "task:" + role)` named one task per role."""
    run_id = uuid4()
    first = task_identity(run_id, "worker", 0)
    second = task_identity(run_id, "worker", 1)
    assert first != second
    assert session_identity(first) != session_identity(second)
    first_decision = attempt_identity(first, 0, 0)
    second_decision = attempt_identity(second, 0, 0)
    assert first_decision != second_decision
    assert stable(first_decision, "call") != stable(second_decision, "call")


def test_a_second_round_of_evidence_asks_the_same_step_under_its_own_task() -> None:
    """Round two re-asks step 0, so only the task can keep the two questions apart."""
    run_id = uuid4()
    first, second = task_identity(run_id, "worker", 0), task_identity(run_id, "worker", 1)
    assert attempt_identity(first, 0, 0) != attempt_identity(second, 0, 0)
    assert attempt_identity(first, 1, 0) != attempt_identity(second, 1, 0)


def test_a_new_attempt_at_one_step_is_a_new_question_and_a_replay_is_not() -> None:
    task = task_identity(uuid4(), "collector", 0)
    assert attempt_identity(task, 0, 0) == attempt_identity(task, 0, 0)
    assert attempt_identity(task, 0, 0) != attempt_identity(task, 0, 1)
    assert attempt_identity(task, 1, 0) != attempt_identity(task, 0, 0)


def test_every_identity_is_reproducible_from_its_own_inputs() -> None:
    run_id = uuid4()
    task = task_identity(run_id, "reviewer", 2)
    assert task == task_identity(run_id, "reviewer", 2)
    assert session_identity(task) == session_identity(task)


# -- what may be applied -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_status",
    ["pausing", "paused", "cancelling", "cancelled", "closed", "waiting", "failed"],
)
def test_a_run_that_stopped_answers_a_late_suggestion_with_its_own_state(run_status: str) -> None:
    assert (
        application_refusal(
            run_status=run_status,
            window_refusal=None,
            lease_matches=True,
            versions_match=True,
        )
        == "invalid_run_state"
    )


def test_the_other_reasons_a_late_answer_is_refused_are_the_existing_ones() -> None:
    def refusal(**changes: Any) -> str | None:
        arguments: dict[str, Any] = {
            "run_status": "running",
            "window_refusal": None,
            "lease_matches": True,
            "versions_match": True,
        }
        arguments.update(changes)
        return application_refusal(**arguments)

    assert refusal() is None
    assert refusal(window_refusal="authorization_expired") == "authorization_expired"
    assert refusal(lease_matches=False) == "lease_stale"
    assert refusal(versions_match=False) == "version_conflict"


def test_the_stop_is_answered_before_any_question_about_staleness() -> None:
    """The operator's stop is what the record has to name, not a version drift behind it."""
    assert (
        application_refusal(
            run_status="cancelled",
            window_refusal="authorization_expired",
            lease_matches=False,
            versions_match=False,
        )
        == "invalid_run_state"
    )


# -- model accounting ----------------------------------------------------------------------------


def attempt(
    *,
    request_count: int = 1,
    usage_state: str = USAGE_UNKNOWN,
    usage: dict[str, Any] | None = None,
    identifier: UUID | None = None,
) -> Decision:
    return Decision(
        id=identifier or uuid4(),
        run_id=uuid4(),
        session_id=uuid4(),
        task_id=uuid4(),
        step=0,
        status=ATTEMPT_APPLIED,
        content={},
        request_count=request_count,
        usage_state=usage_state,
        usage=usage,
    )


def test_unknown_model_usage_is_counted_apart_and_never_as_zero() -> None:
    pending = uuid4()
    summary = model_usage_summary(
        [
            attempt(identifier=pending),
            attempt(usage_state=USAGE_KNOWN, usage={"billable": False}),
            # A row that is not a model request at all (a re-dispatch) adds nothing to the count.
            attempt(request_count=0, usage_state=USAGE_KNOWN),
        ]
    )
    assert summary["requests"] == 2
    assert summary["known"] == 1
    assert summary["unknown"] == 1
    assert summary["pending_attempt_ids"] == [pending]


def test_an_attempt_view_cannot_state_a_cost_without_saying_whether_it_is_known() -> None:
    payload = {
        "id": str(uuid4()),
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "session_id": str(uuid4()),
        "role": "collector",
        "step": 0,
        "status": "applied",
        "usage": {"known": False, "receipt": None},
    }
    view = PlanningAttemptView.model_validate(payload)
    assert view.usage.known is False and view.usage.receipt is None
    with pytest.raises(ValidationError):
        PlanningAttemptView.model_validate(
            {key: value for key, value in payload.items() if key != "usage"}
        )


# -- where the model may be called from ----------------------------------------------------------


def test_the_runs_module_never_reaches_for_an_adapter() -> None:
    """`runs` prepares and commits; it does not decide, so it holds no adapter at all."""
    text = (SOURCE / "runs" / "orchestration.py").read_text(encoding="utf-8")
    assert ".decide(" not in text
    assert "DeterministicModel" not in text
    assert "ModelAdapter" not in text
    assert not hasattr(OrchestrationService, "plan")
    for name in ("begin_planning", "commit_planning"):
        parameters = inspect.signature(getattr(OrchestrationService, name)).parameters
        assert "adapter" not in parameters, f"{name} must not take an adapter"


def test_the_contract_layer_carries_no_framework_or_vendor_type() -> None:
    offenders: list[str] = []
    for path in sorted((SOURCE / "contracts").glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            module = stripped.split()[1].split(".")[0]
            if module in FRAMEWORK_OR_VENDOR:
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert offenders == [], f"framework or vendor types in the contract layer: {offenders}"


def test_one_interface_carries_both_a_deterministic_and_a_provider_adapter() -> None:
    assert isinstance(DeterministicModel(), ModelAdapter)
    assert getattr(ModelAdapter, "_is_protocol", False) is True
    assert inspect.getmodule(ModelAdapter).__name__ == "huntweave.harness.model"

    class NoReceiptAdapter:
        """A provider adapter states what it can; it does not have to be deterministic."""

        def decide(self, request: ModelRequest) -> ModelSuggestion:
            return ModelSuggestion(content={"action": "finish_research"})

    assert isinstance(NoReceiptAdapter(), ModelAdapter)
    assert NoReceiptAdapter().decide(
        ModelRequest(
            attempt_id="a",
            role="collector",
            step=0,
            input_hash="0" * 64,
            prompt_version=PROMPT_VERSION,
            context={},
        )
    ).usage.known is False


def test_the_adapter_is_asked_between_the_two_business_transactions() -> None:
    """The order is the whole point: a model call inside either transaction holds the Run's lock."""
    events: list[str] = []

    class StubBusiness:
        def begin_planning(self, run_id: UUID, task_id: str, generation: int) -> dict[str, Any]:
            events.append("begin")
            return {
                "kind": "request",
                "attempt_id": "attempt",
                "role": "collector",
                "step": 0,
                "input_hash": "0" * 64,
                "prompt_version": PROMPT_VERSION,
                "context": {},
            }

        def commit_planning(
            self, attempt_id: str, suggestion: ModelSuggestion
        ) -> dict[str, Any]:
            events.append("commit")
            return {"id": attempt_id}

    class Spy:
        def decide(self, request: ModelRequest) -> ModelSuggestion:
            events.append("model")
            return ModelSuggestion(content={"action": "finish_research"})

    result = ResearchHarness(StubBusiness(), Spy()).plan_step(uuid4(), "task", 0)  # type: ignore[arg-type]
    assert events == ["begin", "model", "commit"]
    assert result == {"id": "attempt"}


def test_a_replayed_step_never_wakes_the_adapter() -> None:
    calls: list[str] = []

    class StubBusiness:
        def begin_planning(self, run_id: UUID, task_id: str, generation: int) -> dict[str, Any]:
            calls.append("begin")
            return {"kind": "reuse", "decision": {"id": "decided"}}

        def commit_planning(
            self, attempt_id: str, suggestion: ModelSuggestion
        ) -> dict[str, Any]:  # pragma: no cover - a committed answer is not committed twice
            raise AssertionError("a committed answer must not be committed again")

    class Spy:
        def decide(self, request: ModelRequest) -> ModelSuggestion:  # pragma: no cover
            raise AssertionError("a committed answer must not be asked again")

    result = ResearchHarness(StubBusiness(), Spy()).plan_step(uuid4(), "task", 0)  # type: ignore[arg-type]
    assert result == {"id": "decided"}
    assert calls == ["begin"]


def test_the_deterministic_adapter_reports_usage_through_the_interface() -> None:
    suggestion = DeterministicModel().decide(
        ModelRequest(
            attempt_id="attempt",
            role="worker",
            step=0,
            input_hash="0" * 64,
            prompt_version=PROMPT_VERSION,
            context={"scenario": "positive"},
        )
    )
    assert suggestion.usage.known
    assert suggestion.usage.receipt == {"accounting": "not_applicable", "billable": False}
    assert suggestion.provider == "deterministic" and suggestion.model
    assert suggestion.prompt_version == PROMPT_VERSION
    assert ModelUsage(receipt=None).known is False
