"""Durable role graphs; framework objects never cross the business Interface."""

from typing import Any, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from huntweave.harness.checkpoints import checkpoint_connection
from huntweave.runs.orchestration import OrchestrationService


class ResearchState(TypedDict, total=False):
    run_id: str
    task_id: str
    session_id: str
    lease_generation: int
    step: int
    decision: dict[str, Any] | None
    call_ids: list[str]
    result_ids: list[str]


class ResearchHarness:
    def __init__(self, business: OrchestrationService):
        self.business = business

    def advance(self, claim: dict[str, Any]) -> None:
        run_id = UUID(claim["run_id"])
        # The trusted claim is the only seed the graph accepts; it carries the lease
        # generation this pass must not silently lose across checkpoint replays.
        seed: ResearchState = {
            "run_id": str(claim["run_id"]),
            "task_id": str(claim["task_id"]),
            "session_id": str(claim["session_id"]),
            "lease_generation": int(claim["lease_generation"]),
            "step": int(claim["step"]),
        }

        def decide(state: ResearchState) -> ResearchState:
            # Fresh trusted claim supplies the lease after restart/Command(resume).
            return {
                "decision": self.business.plan(run_id, claim["task_id"], claim["lease_generation"])
            }

        def observe(state: ResearchState) -> ResearchState:
            snapshot = self.business.snapshot(run_id)
            calls = [x for x in snapshot["calls"] if x["session_id"] == claim["session_id"]]
            return {
                "call_ids": [x["id"] for x in calls],
                "result_ids": [x["id"] for x in calls if x["result"] is not None],
            }

        def route(state: ResearchState) -> str:
            snapshot = self.business.snapshot(run_id)
            task = next(x for x in snapshot["tasks"] if x["id"] == claim["task_id"])
            if task["status"] in {"completed", "cancelled", "failed"}:
                return END
            if snapshot["run"]["status"] != "running" or self.business.pending(run_id):
                return "wait"
            return "decide"

        def wait(state: ResearchState) -> ResearchState:
            snapshot = self.business.snapshot(run_id)
            interrupt(
                {
                    "reason": "execution_result_or_human_control",
                    "run_status": snapshot["run"]["status"],
                    "pending_call_ids": [x["id"] for x in self.business.pending(run_id)],
                }
            )
            return {}

        graph = StateGraph(ResearchState)
        graph.add_node("decide", decide)
        graph.add_node("observe", observe)
        graph.add_node("wait", wait)
        graph.add_edge(START, "decide")
        graph.add_edge("decide", "observe")
        graph.add_conditional_edges("observe", route)
        graph.add_edge("wait", "observe")
        with checkpoint_connection() as connection:
            compiled = graph.compile(checkpointer=PostgresSaver(connection))
            config: RunnableConfig = {"configurable": {"thread_id": seed["session_id"]}}
            previous = compiled.get_state(config)
            if previous.next:
                # Keep the same durable interrupt while its condition is unmet.
                if (
                    self.business.pending(run_id)
                    or self.business.snapshot(run_id)["run"]["status"] != "running"
                ):
                    return
                compiled.invoke(Command(resume=True), config=config)
            else:
                compiled.invoke(seed, config=config)
