"""One planning step, performed exactly the way the scheduler performs it.

The three-segment protocol is `runs.begin_planning` -> the adapter -> `runs.commit_planning`, and
`harness.graph.ResearchHarness.plan_step` is what puts them in that order. A test that
re-implemented the order would be checking its own copy, so every check that needs a plan goes
through the harness instead.
"""

from typing import Any
from uuid import UUID

from huntweave.harness.graph import ResearchHarness
from huntweave.harness.model import DeterministicModel, ModelAdapter
from huntweave.runs.orchestration import OrchestrationService


def plan_step(
    service: OrchestrationService,
    run_id: UUID,
    task_id: str,
    generation: int,
    adapter: ModelAdapter | None = None,
) -> dict[str, Any] | None:
    """Prepare, judge outside every transaction, and commit — in that order."""
    return ResearchHarness(service, adapter or DeterministicModel()).plan_step(
        run_id, task_id, generation
    )
