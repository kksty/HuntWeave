import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from huntweave.contracts.runs import Budget, ProjectCreate, RunCreate, ScopeCreate
from huntweave.runs.orchestration import OrchestrationService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine


@pytest.fixture
def business():
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Requires a disposable PostgreSQL database")
    engine = connect_engine()
    runs = RunService(lambda: engine)
    project = runs.create_project(ProjectCreate(name="p0 contract verification"))
    now = datetime.now(UTC)
    scope = runs.create_scope(
        ScopeCreate(
            project_id=project.id,
            targets_text="192.0.2.1",
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            authorization="Fixed fake actions only",
            budget=Budget(max_tool_calls=2),
        )
    )
    run = runs.create_run(RunCreate(scope_id=scope.id, scope_version=1), str(uuid4()))
    runs.start(run.id, run.version)
    yield runs, OrchestrationService(lambda: engine), run.id
    engine.dispose()


def test_replayed_decision_reserves_one_call_and_commits_one_event(business):
    runs, service, run_id = business
    claim = service.claim(run_id)
    assert claim is not None and claim["run_id"] == str(run_id)
    first = service.plan(run_id, claim["task_id"], claim["lease_generation"])
    replay = service.plan(run_id, claim["task_id"], claim["lease_generation"])
    assert replay == first
    snapshot = service.snapshot(run_id)
    assert len(snapshot["calls"]) == 1
    assert snapshot["budget"]["reserved_tool_calls"] == 1
    assert len([e for e in service.history(run_id)["events"] if e["type"] == "tool_planned"]) == 1
