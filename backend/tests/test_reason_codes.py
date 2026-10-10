"""The console's reason vocabulary has to keep up with what the backend can actually say.

`PROJECT.md` section 12.2 lists the interruption reasons an operator must be able to read, and issue
#19 asks for the console's mapping to match the reason codes the backend really emits: entries that
can never be reached are dead weight, and a code with no entry leaves an operator staring at an
identifier. Neither is a mistake a reviewer can catch by reading a diff of 140 lines, so this check
reads the backend's own text for candidate codes and compares the two sets mechanically.

The scan is deliberately literal: it collects string literals that *look* like reason codes (lower
snake case) from the modules that produce them, and requires each one to be either mapped in the
frontend or explicitly listed here as something that is not a reason code. The allow-list is the
interesting part of the check — adding a new code means either writing a sentence for it or
recording why it is not one.
"""

import re
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SOURCE = REPOSITORY / "backend" / "src" / "huntweave"
CONSOLE_STATE = REPOSITORY / "frontend" / "src" / "workspace.ts"
CONSOLE_BUILD = REPOSITORY / "frontend" / "dist"

# The checks container is built from the control image, which ships the console's build artifact and
# not its sources. Where the source is absent, the comparison runs against the bundle instead: that
# still answers "does the console this deployment serves carry a sentence for every reason?", which
# is the property that matters. The reverse direction — a mapped code nothing can reach — is only
# checkable against the map itself, so it is skipped there and runs in the working tree.
reason_source_missing = pytest.mark.skipif(
    not CONSOLE_STATE.is_file(),
    reason="Console sources are not part of the deployed image; the bundle check covers the gap.",
)

# String literals that look like a reason code but are not one: event types, SSE event names, status
# values, contract literals used for something else, and labels that describe what was hidden rather
# than why something was refused. Each has to be justified here, which is what stops the check from
# being silenced by a blanket pattern.
NOT_REASON_CODES = {
    # Call and instance lifecycle values. A status is not a reason; the console renders them with
    # its own status maps, and a refusal carries its reason separately.
    "accepted",
    "running",
    "completed",
    "failed",
    "cancelled",
    "unknown",
    "planned",
    "dispatched",
    "succeeded",
    "denied",
    "incomplete",
    "creating",
    "ready",
    "stopped",
    "reclaimed",
    "interrupted",
    "queued",
    "waiting",
    "pausing",
    "paused",
    "recovering",
    "cancelling",
    "closed",
    "draft",
    "collecting",
    "researching",
    "reviewing",
    "awaiting_human",
    "collector",
    "worker",
    "reviewer",
    "gateway",
    "tool",
    "workspace",
    "session",
    "container",
    "network",
    "volume",
    "disabled",
    "unavailable",
    "never_started",
    "confirmed",
    "unconfirmed",
    "none",
    "internal",
    "bridge",
    "lifecycle_check_only",
    "verifying",
    "healthy",
    # State and mode names that are not refusals at all: the LangGraph node names, the container
    # open modes, and the status values the manager records beside a reason.
    "blocked",
    "complete",
    "decide",
    "wait",
    "ro",
    "rw",
    # Contract literals and identifiers that are not refusals.
    "fake-p0-v1",
    "real-lab-v1",
    "common-tcp-v1",
    "custom-tcp-v1",
    "all-tcp-v1",
    "p0-b-v1",
    "real",
    "demonstration",
    "positive",
    "negative",
    "failure",
    "success",
    "needs_evidence",
    "not_executed",
    "executed",
    "undetermined",
    "all_of",
    "any_of",
    "baseline",
    "proof",
    "verification",
    "supporting",
    "counterevidence",
    "fresh_replay",
    "existing_evidence",
    "not_replayed",
    "http",
    "https",
    "get",
    "head",
    "tcp",
    "udp",
    "docker",
    "text",
    # Event types and SSE event names. These travel in a different field than `reason_code`, and the
    # console shows them as event names rather than as explanations.
    "execution_started",
    "execution_output",
    "execution_heartbeat",
    "execution_stopped",
    "execution_instance",
    "execution_command_completed",
    "execution_stop_unconfirmed",
    "execution_output_truncated",
    "execution_output_redacted",
    "execution_evidence_refused",
    "session_expired",
    "tool_completed",
    "recovery_completed",
    "audit",
    "heartbeat",
    "gap",
    # Redaction pattern labels: they say *what* was hidden, not why something was refused.
    "private_key_block",
    "authorization_header",
    "aws_access_key_id",
    "openai_style_key",
    "github_token",
    "jwt",
    "password_assignment",
    # Field names and internal keys that happen to be lower snake case.
    "reason_code",
    "missing_reason",
    "halt_reason",
    "revocation_reason",
    "parameters_hash",
    "scope_version",
    "policy_version",
    "lease_generation",
    "execution_profile",
    "profile_id",
    "profile_version",
    "authorized_until",
    "target_ip",
    "target_port",
    "call_id",
    "run_id",
    "session_id",
    "decision_id",
    "scope_id",
    "instance_id",
    "tool_id",
    "action_id",
    "budget_reservation_id",
    "source_event_id",
    "relative_path",
    "execution_result_or_human_control",
}

# Codes the backend can state that the console deliberately does not carry its own sentence for,
# with the reason. These are refusals the console never renders as an explanation: a database that
# is down or a missing console build is transport, not an execution outcome, and the pages that hit
# them show their own wording. Each entry is a decision someone made, with its justification.
UNDISPLAYED: dict[str, str] = {}


def frontend_keys() -> set[str]:
    """The keys of the console's `messages` map, read from the source that renders them.

    Several entries share a line, so the pattern is not anchored to the end of one: it looks for a
    lower-snake-case key at the start of an entry — after a newline or after the previous entry's
    comma — which is what the map's shape guarantees.
    """
    text = CONSOLE_STATE.read_text(encoding="utf-8")
    start = text.index("export const messages")
    end = text.index("\n};", start)
    body = text[start:end]
    return set(re.findall(r"[\n,]\s*([a-z][a-z0-9_]*):", body))


def backend_candidates() -> set[str]:
    """Every lower-snake-case literal the backend passes *into a reason-shaped position*.

    The scan is anchored on the constructs that carry a reason — a refusal being raised, a reason
    being returned or assigned, a gate naming why it is unmet, a payload key — rather than on every
    string in the file. A bare literal sweep also picks up field names, event types and contract
    values, and a check that fires on those gets silenced instead of fixed; what this does catch is
    the code somebody adds next, which is the whole point.
    """
    patterns = (
        # Every refusal type the execution and control sides raise or return, whatever precedes the
        # call: `raise RunnerRejected("...")`, `return error_response(ServiceError("...", 503))`.
        re.compile(
            r'(?:ServiceError|RunnerRejected|RunnerUnavailable|SandboxRejected|ArchiveRejected)'
            r'\(\s*"([a-z][a-z0-9_]*)"',
            re.S,
        ),
        # `record_interruption(session, run, "reason", "recovery condition")` — the third argument.
        # Both the suspension paths and the record-only paths record through this one function. The
        # name is deliberately not `interrupt`: LangGraph's own primitive has that name and
        # `harness/graph.py` calls it with a payload, so anchoring on the bare word also picked up a
        # dict key there (`pending_call_ids`) as if it were a reason.
        re.compile(r'\brecord_interruption\(\s*[^,]+,\s*[^,]+,\s*"([a-z][a-z0-9_]*)"'),
        # A payload that names the reason it is about, or a span that records one beside a status.
        re.compile(r'"reason_code":\s*"([a-z][a-z0-9_]*)"'),
        re.compile(r'"missing_reason":\s*"([a-z][a-z0-9_]*)"'),
        re.compile(r'"revocation_reason":\s*"([a-z][a-z0-9_]*)"'),
        # `reason = "..."`, `return "..."` from a method whose job is to name a refusal, and the
        # two-branch form a preview uses to choose between two reasons.
        re.compile(r'\breason\s*=\s*"([a-z][a-z0-9_]*)"'),
        re.compile(r'"([a-z][a-z0-9_]*)"\s+if\s+[^"\n]+\s+else\s+"([a-z][a-z0-9_]*)"'),
        re.compile(r'^\s+return\s+"([a-z][a-z0-9_]*)"\s*$', re.M),
        # `_gate("name", condition, "reason")` from the capability evaluator.
        re.compile(r'_gate\(\s*"[a-z_]+",\s*[^,]+,\s*"([a-z][a-z0-9_]*)"'),
        # A local that records *why* something failed or is being reported rather than raising it:
        # `failed.append("ownership_mismatch")`, `sandbox_failure = "sandbox_state_unwritable"`.
        re.compile(
            r'(?:failed|failure|missing_reason)\s*[.=\[]+\s*(?:append\()?"([a-z][a-z0-9_]*)"'
        ),
        # A local that records *why* something failed or is being reported rather than raising it:
        # `failed.append("ownership_mismatch")`, `sandbox_failure = "sandbox_state_unwritable"`.
        re.compile(
            r'(?:failed|failure|missing_reason)\s*[.=\[]+\s*(?:append\()?"([a-z][a-z0-9_]*)"'
        ),
        # A keyword that carries a reason into a call, or a fallback that supplies one. The `or`
        # forms are how the control plane names a reason it did not get from the execution side.
        re.compile(r'\breason(?:_code)?\s*=\s*"([a-z][a-z0-9_]*)"', re.S),
        re.compile(r'\bor\s+"([a-z][a-z0-9_]{6,})"'),
        re.compile(r'\bor\s+"([a-z][a-z0-9_]{6,})",'),
    )
    # Values a contract names as reasons that reach the record through a field other than
    # `reason_code`: a halted instance's `halt_reason`, an egress change's `reason`, and an
    # authorization verdict. They are mapped in the console so the isolation panel reads in the same
    # language, so they count as candidates for the "no dead entries" direction too.
    literals = ("execution/sandbox.py", "execution/real.py", "execution/retention.py")
    found: set[str] = set()
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                found.update(group for group in match.groups() if group)
    for relative in literals:
        text = (SOURCE / relative).read_text(encoding="utf-8")
        # `HaltReason = Literal["...", ...]` and `reason: Literal["...", ...]` inside a contract.
        for block in re.findall(r'=\s*Literal\[(.*?)\]|reason:\s*Literal\[(.*?)\]', text, re.S):
            for group in block:
                found.update(re.findall(r'"([a-z][a-z0-9_]*)"', group))
    return found


def test_every_reason_the_backend_can_state_has_a_sentence_in_the_console() -> None:
    """A refusal an operator cannot read is not an explanation, it is an identifier."""
    if not CONSOLE_STATE.is_file():
        pytest.skip("Console sources are absent; the served-bundle check covers this instead.")
    mapped = frontend_keys()
    missing = sorted(backend_candidates() - mapped - NOT_REASON_CODES - set(UNDISPLAYED))
    assert missing == [], (
        "these look like reason codes the console has no sentence for: "
        f"{missing}. Either write one in frontend/src/workspace.ts, or add the literal to "
        "NOT_REASON_CODES with the justification that it is not a reason."
    )


def console_bundle_text() -> str:
    """Every JavaScript and CSS artifact the console is served from, concatenated."""
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(CONSOLE_BUILD.rglob("*"))
        if path.is_file()
    )


def test_the_console_this_deployment_serves_can_state_every_reason() -> None:
    """The check that holds where the sources are absent: the shipped bundle carries the map.

    A message the bundle cannot produce is a message an operator will never read, whatever the
    source says — so this is the version of the question an installed deployment can answer about
    itself, and it is the one that runs in the checks container.
    """
    bundle = console_bundle_text()
    if not bundle.strip():
        pytest.skip("No console build is present in this checkout.")
    missing = sorted(
        code
        for code in backend_candidates()
        if code not in NOT_REASON_CODES and code not in UNDISPLAYED and code not in bundle
    )
    assert missing == [], (
        f"the console bundle has no sentence for these reasons: {missing}. "
        "Rebuild the console (`npm run build` in frontend/) so the deployment serves the map."
    )


@reason_source_missing
def test_the_console_carries_no_sentence_the_backend_can_never_need() -> None:
    """An entry nothing can reach is a mapping that looks like coverage without being any."""
    candidates = backend_candidates()
    dead = sorted(frontend_keys() - candidates - set(UNDISPLAYED))
    assert dead == [], (
        f"the console maps codes the backend never states: {dead}. "
        "Remove them, or the map stops being a statement about this platform."
    )


def test_the_undisplayed_list_is_empty_or_explained() -> None:
    """Every exemption carries its reason, so an exemption is a decision rather than a silence."""
    unexplained = [code for code, why in UNDISPLAYED.items() if len(why.strip()) < 20]
    assert unexplained == []
