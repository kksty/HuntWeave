"""Deterministic P0 model adapter: fixed fake evidence changes the next decision."""

from typing import Any


class DeterministicModel:
    def decide(self, role: str, step: int, context: dict[str, Any]) -> dict[str, Any]:
        if step > 0 or (role == "worker" and context.get("scenario") == "negative"):
            return {
                "action": "finish_research",
                "summary": "Fixed evidence excludes the demonstration hypothesis."
                if context.get("scenario") == "negative"
                else "Current role has completed its bounded demonstration.",
                "expected": "No new target action",
                "stop_condition": "Role complete",
                "evidence_ids": context.get("evidence_ids", []),
            }
        action = {"collector": "fake.collect", "worker": "fake.verify", "reviewer": "fake.review"}[
            role
        ]
        return {
            "action": action,
            "summary": {
                "collector": "Collect fixed service observations.",
                "worker": "Positive fixed observation warrants independent verification.",
                "reviewer": "Review execution evidence in a separate context.",
            }[role],
            "expected": "A labelled fixed fake result",
            "stop_condition": "One bounded action or execution failure",
            "evidence_ids": context.get("evidence_ids", []),
        }
