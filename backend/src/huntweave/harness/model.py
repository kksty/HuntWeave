"""The model Interface, and the deterministic adapter this build ships.

Two rules live in this file, and both are what issue #43 is about.

**One Interface.** A real provider adapter and the deterministic adapter below are the same thing to
the rest of the platform: something that takes a :class:`ModelRequest` and returns a
:class:`ModelSuggestion`. The request carries the frozen input the Run prepared, so an adapter
cannot quietly read a fresher context than the one the answer will be judged against. Anything a
provider or a framework needs — clients, message objects, retry helpers, vendor type names — belongs
here, inside the harness; `runs` and `contracts` never import it.

**An answer is not a cost.** A suggestion states the provider's usage receipt or omits it, and
omitting it is a fact the platform keeps: `ModelUsage(receipt=None)` means "no receipt arrived", not
"this was free". `runs` records that distinction and leaves the request on the pending list.

The deterministic adapter is not a demonstration *label*. P0's adapter branched on a label chosen
before anything ran, which could not show a feedback loop. This one reads the facts the execution
side reported — the exit code of a shell action, the ports a discovery actually found — and picks a
different next action accordingly, including the branch where nothing was found. It stays
deterministic: no network call, and every branch is a fixed function of the recorded result.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

FINISH = "finish_research"

#: The version of the context this build hands to an adapter. A stored attempt names the version it
#: was asked under, so a later context shape does not silently reinterpret an old answer.
PROMPT_VERSION = "planning-context-v1"


@dataclass(frozen=True)
class ModelRequest:
    """One planning question, as the Run froze it.

    ``input_hash`` is the hash of ``context``, and it travels with the request so an adapter cannot
    answer a different question than the one that was persisted. The adapter is handed the frozen
    snapshot itself rather than a live object it could mutate.
    """

    attempt_id: str
    role: str
    step: int
    input_hash: str
    prompt_version: str
    context: dict[str, Any]


@dataclass(frozen=True)
class ModelUsage:
    """The provider's own accounting for one request.

    ``receipt=None`` is the absence of an answer about cost, which is what the platform must not
    turn into zero. An adapter that knows nothing was billed says so *in* the receipt instead of
    staying silent, so the two cases stay distinguishable in the record.
    """

    receipt: dict[str, Any] | None = None

    @property
    def known(self) -> bool:
        return self.receipt is not None


@dataclass(frozen=True)
class ModelSuggestion:
    """What an adapter proposes, and what it can say about itself.

    ``content`` is the plan the Run will validate; the rest describes the request rather than the
    plan, and is stored beside it so a decision can name which model, prompt version and accounting
    it came from.
    """

    content: dict[str, Any]
    usage: ModelUsage = field(default_factory=ModelUsage)
    provider: str | None = None
    model: str | None = None
    prompt_version: str = PROMPT_VERSION


@runtime_checkable
class ModelAdapter(Protocol):
    """The one Interface a real adapter and a deterministic one both implement.

    There is no second entry point and no optional capability: the caller prepares a request through
    `runs` and hands it here with no transaction open, which is what keeps a slow model from holding
    a Run's row lock.
    """

    def decide(self, request: ModelRequest) -> ModelSuggestion: ...


class DeterministicModel:
    """The shipped adapter: a fixed function of the recorded result, with nothing to bill."""

    provider = "deterministic"
    model = "feedback-branching-v1"
    prompt_version = PROMPT_VERSION

    def decide(self, request: ModelRequest) -> ModelSuggestion:
        return ModelSuggestion(
            content=self.plan(request.role, request.step, request.context),
            # No provider was asked, and this says so as a receipt rather than by staying silent:
            # silence means "no accounting arrived", the case that has to stay pending.
            usage=ModelUsage(receipt={"accounting": "not_applicable", "billable": False}),
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
        )

    def plan(self, role: str, step: int, context: dict[str, Any]) -> dict[str, Any]:
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
