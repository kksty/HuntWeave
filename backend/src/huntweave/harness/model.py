"""Deterministic model adapter: what the tool returned decides the next action.

P0's adapter branched on a demonstration *label*; that could not show a feedback loop, because the
label was chosen before anything ran. This one reads the facts the execution side reported — the
exit code of a shell action, the ports a discovery actually found — and picks a different next
action accordingly, including the branch where nothing was found. It stays deterministic: no model
is called here, and every branch is a fixed function of the recorded result.
"""

from typing import Any

FINISH = "finish_research"


class DeterministicModel:
    def decide(self, role: str, step: int, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("execution_profile") == "real-lab-v1":
            return self._real(role, step, context)
        return self._demonstration(role, step, context)

    # -- real execution -----------------------------------------------------------------------

    @staticmethod
    def _real(role: str, step: int, context: dict[str, Any]) -> dict[str, Any]:
        """The real plan: establish who we are, find what is listening, then look at it.

        Each step reads what the previous one observed. A discovery that found nothing ends the
        research instead of probing a port that was never open — the counterexample that makes the
        branch real rather than a script.
        """
        summary = context.get("last_summary") or {}
        if role == "collector":
            if step == 0:
                return {
                    "action": "shell.exec",
                    "parameters": {"command": "id -u", "timeout_seconds": 30},
                    "summary": "Establish the execution identity inside the sandbox.",
                    "expected": "A numeric uid from the tool container",
                    "stop_condition": "One bounded action",
                }
            return _finish("The execution identity was already established.")
        if role == "worker":
            if not summary and step == 0:
                return {
                    "action": "shell.exec",
                    "parameters": {"command": "id -u", "timeout_seconds": 30},
                    "summary": "Establish the execution identity inside the sandbox.",
                    "expected": "A numeric uid from the tool container",
                    "stop_condition": "One bounded action",
                }
            if "open_ports" not in summary:
                return {
                    "action": "discover_tcp_services",
                    "parameters": {
                        "ports": [port for port in context.get("candidate_ports", [])][:8],
                        "timeout_seconds": 30,
                    },
                    "summary": "Check the authorized endpoints for a listening service.",
                    "expected": "Which of the authorized ports answer",
                    "stop_condition": "One bounded discovery",
                }
            return _finish(
                "Authorized endpoints were checked and are reported to the reviewer."
            )
        if role == "reviewer":
            if summary.get("open_ports"):
                last = context.get("last_target") or {}
                decision: dict[str, Any] = {
                    "action": "probe_http",
                    "parameters": {"path": "/", "method": "GET", "timeout_seconds": 20},
                    "summary": "A listening port was found; ask it for an HTTP response.",
                    "expected": "The status and headers the service really answered",
                    "stop_condition": "One bounded request",
                }
                if last.get("ip"):
                    # The endpoint is part of the ticket's binding, not of the action's
                    # parameters: a plan names which authorized target to look at, and the
                    # authorization decides whether that target is allowed at all.
                    decision["target_ip"] = last["ip"]
                    decision["target_port"] = summary["open_ports"][0]
                return decision
            return _finish(
                "No authorized endpoint answered, so there is nothing to request."
            )
        return _finish("Role has no further bounded action.")

    # -- demonstration ------------------------------------------------------------------------

    @staticmethod
    def _demonstration(role: str, step: int, context: dict[str, Any]) -> dict[str, Any]:
        if step > 0 or (role == "worker" and context.get("scenario") == "negative"):
            return {
                "action": FINISH,
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


def _finish(summary: str) -> dict[str, Any]:
    return {
        "action": FINISH,
        "summary": summary,
        "expected": "No new target action",
        "stop_condition": "Role complete",
    }
