from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import ConfigDict

from huntweave.api.readiness import CapabilityProbe
from huntweave.contracts.capabilities import Capabilities
from huntweave.execution import capabilities as execution_capabilities


@pytest.fixture(autouse=True)
def _console_gate_cache() -> Iterator[None]:
    # The console gate memoizes the bundle it read; every check here starts from a clean read.
    execution_capabilities._console_consumes_capabilities.cache_clear()
    yield
    execution_capabilities._console_consumes_capabilities.cache_clear()


def test_contract_can_express_a_not_ready_state() -> None:
    assert execution_capabilities._contract_is_expressible() is True


def test_gate_two_fails_when_the_contract_pins_the_state() -> None:
    class Pinned(Capabilities):
        model_config = ConfigDict(extra="forbid", frozen=True)

        real_execution_ready: Literal[False] = False
        reason_code: Literal["environment_unsupported"] = "environment_unsupported"

    assert execution_capabilities._contract_is_expressible(Pinned) is False


def test_gate_three_reads_the_bundle_the_console_is_served_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(execution_capabilities, "CONSOLE_BUILD", tmp_path)
    assert execution_capabilities._console_consumes_capabilities() is False
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text(
        f'fetch("{execution_capabilities.CONSOLE_CAPABILITY_ENDPOINT}")', encoding="utf-8"
    )
    # The gate memoizes the build it read: a bundle written after that read needs a new read.
    execution_capabilities._console_consumes_capabilities.cache_clear()
    assert execution_capabilities._console_consumes_capabilities() is True


def test_unmet_gates_are_named_and_the_platform_stays_in_demonstration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shipped console build is not this check's subject; the stack check matches the live
    # image instead, so the gate is fixed here to keep this result independent of a local build.
    monkeypatch.setattr(execution_capabilities, "_console_consumes_capabilities", lambda: True)
    state = execution_capabilities.evaluate(fake_execution_ready=True, now=datetime.now(UTC))
    assert state.real_execution_ready is False
    assert state.mode == "demonstration"
    assert state.reason_code == "environment_unsupported"
    assert state.observed_at is not None
    unmet = [gate for gate in state.gates if not gate.ready]
    assert [gate.gate for gate in unmet] == ["profile_revalidation", "deployment_revert"]
    assert all(gate.reason_code for gate in unmet)
    assert all(gate.reason_code is None for gate in state.gates if gate.ready)


def test_real_execution_opens_only_when_every_gate_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_capabilities, "PROFILE_REVALIDATED", True)
    monkeypatch.setattr(execution_capabilities, "REVERT_ENTRY_AVAILABLE", True)
    monkeypatch.setattr(execution_capabilities, "_console_consumes_capabilities", lambda: True)
    state = execution_capabilities.evaluate(fake_execution_ready=True, now=datetime.now(UTC))
    assert state.real_execution_ready is True
    assert state.reason_code is None
    assert state.mode == "real"
    assert [gate for gate in state.gates if not gate.ready] == []


def test_enabling_real_execution_without_a_revert_path_keeps_the_gate_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_capabilities, "_console_consumes_capabilities", lambda: True)
    state = execution_capabilities.evaluate(fake_execution_ready=True, now=datetime.now(UTC))
    revert = [gate for gate in state.gates if gate.gate == "deployment_revert"][0]
    assert revert.ready is False and revert.reason_code == "revert_path_missing"
    assert state.real_execution_ready is False


def test_an_unusable_execution_chain_keeps_real_execution_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real execution runs through the same execution side, so every gate holding is not enough
    # while the chain itself cannot be opened: readiness must not outrun the chain it needs.
    monkeypatch.setattr(execution_capabilities, "PROFILE_REVALIDATED", True)
    monkeypatch.setattr(execution_capabilities, "REVERT_ENTRY_AVAILABLE", True)
    monkeypatch.setattr(execution_capabilities, "_console_consumes_capabilities", lambda: True)
    state = execution_capabilities.evaluate(fake_execution_ready=False, now=datetime.now(UTC))
    assert [gate for gate in state.gates if not gate.ready] == []
    assert state.real_execution_ready is False
    assert state.mode == "demonstration"
    assert state.reason_code == "runner_state_unavailable"


def test_the_probe_reports_an_observation_gap_instead_of_readiness() -> None:
    def unavailable() -> Capabilities:
        raise httpx.ConnectError("no route to the execution side")

    probe = CapabilityProbe(unavailable)
    assert probe.current() == Capabilities.unobserved("runner_unavailable")
    assert probe.current().observed_at is None
    assert probe.current().fake_execution_ready is False


def test_the_probe_does_not_ask_the_execution_side_on_every_render() -> None:
    reads = 0

    def read() -> Capabilities:
        nonlocal reads
        reads += 1
        return execution_capabilities.evaluate(fake_execution_ready=True, now=datetime.now(UTC))

    probe = CapabilityProbe(read, ttl_seconds=60)
    assert [probe.current().fake_execution_ready for _ in range(3)] == [True, True, True]
    assert reads == 1
