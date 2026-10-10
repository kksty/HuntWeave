"""Real-action check: tickets in through the Runner's own surface, containers on the lab range.

This probe builds the *product's* Runner (`create_runner`) with sandbox management enabled, submits
real tickets to its HTTP surface, and lets the real executor drive the trusted manager: the call
prepares an instance for its own authorized endpoint, runs the action inside the tool container,
archives what it produced, and releases the instance. The facts are then read back with the Docker
SDK and from the evidence directory, so the components under test are never the only witness.

Everything it talks to is a lab fixture on a lab bridge — a fixed echo target on 7000/7001, a plain
HTTP server on 8080, and a fixture on the probe's own control network that must stay unreachable.
No external address is contacted.

What it demonstrates (P1 slice #17):

* three real actions run through the existing ticket, lease, timeout, cancellation and evidence
  contracts, and produce real evidence;
* the target binding is the ticket's own endpoint, and an unauthorized target stays out of reach;
* a real failure and a real non-HTTP answer drive a different outcome from a successful one;
* the same surface still serves the demonstration profile, and a real ticket on a deployment
  without management is refused instead of being served by the demonstration side;
* every call ends with no instance and no permit left behind.
"""

import hashlib
import json
import os
import time
import traceback
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import docker
from fastapi.testclient import TestClient

from huntweave.config import SandboxSettings
from huntweave.contracts.execution import ExecutionRequest, parameters_hash
from huntweave.execution.dockerruntime import open_sandbox_manager
from huntweave.execution.sandbox import SandboxManager
from huntweave.harness.model import DeterministicModel
from huntweave.execution.server import create_runner

RESULTS = Path("/results")
PROBE_IMAGE = "huntweave-isolation-probe:p0"
PROFILE = "sandbox-egress-v1"
CONTROL_NETWORK = "huntweave-lab-action-control"
TARGET_NETWORK = "huntweave-lab-egress-targets"
LABEL = "com.huntweave.action_probe"
TOKEN = "action-probe-token-" + "0" * 46
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class ActionCheck:
    def __init__(self) -> None:
        self.client = docker.from_env(timeout=15)
        self.manager: SandboxManager | None = None
        self.run_id = uuid4()
        self.authorization: dict[str, Any] | None = None
        self.prefix = f"huntweave-action-{str(self.run_id)[:12]}"
        self.report: dict[str, Any] = {
            "passed": False,
            "started_at": datetime.now(UTC).isoformat(),
            "host": {
                "platform": os.environ.get("HUNTWEAVE_HOST_PLATFORM", "unknown"),
                "os": os.environ.get("HUNTWEAVE_HOST_OS", "unknown"),
            },
            "source_revision": os.environ.get("HUNTWEAVE_SOURCE_REVISION", "unknown"),
            "source_tree_dirty": os.environ.get("HUNTWEAVE_SOURCE_TREE_DIRTY") == "true",
            "checks": [],
            "events": [],
            "calls": [],
            "leftovers": [],
        }

    # -- reporting ----------------------------------------------------------------------------

    def event(self, event_name: str, **values: object) -> None:
        record = {"at": datetime.now(UTC).isoformat(), "action": event_name, **values}
        self.report["events"].append(record)
        print(json.dumps(record, default=str), flush=True)

    def check(self, name: str, success: bool, **values: object) -> None:
        self.report["checks"].append({"name": name, "passed": bool(success), **values})
        self.event("check", name=name, passed=bool(success), **values)
        if not success:
            raise AssertionError(name)

    # -- the check ----------------------------------------------------------------------------

    def run(self) -> None:
        settings = SandboxSettings.from_env()
        self.check(
            "real_profile_selected",
            settings.enabled and settings.profile_id == PROFILE,
            profile_id=settings.profile_id,
        )
        manager = open_sandbox_manager(settings)
        self.manager = manager
        network = self.target_network()
        fixtures = {
            "tcp": self.fixture("tcp", network, ["python", "/lab/fixture.py", "target"]),
            "http": self.fixture("http", network, ["python", "-m", "http.server", "8080"]),
            "control": self.fixture("control", CONTROL_NETWORK, ["python", "/lab/fixture.py", "target"]),
        }
        addresses = {
            "tcp": self.address_of(fixtures["tcp"], network),
            "http": self.address_of(fixtures["http"], network),
            "control": self.address_of(fixtures["control"], CONTROL_NETWORK),
        }
        self.report["fixtures"] = addresses
        self.await_listening(fixtures["tcp"], addresses["tcp"], 7000)
        self.await_listening(fixtures["http"], addresses["http"], 8080)
        self.await_listening(fixtures["control"], addresses["control"], 7000)
        self.check("lab_fixtures_are_listening", all(addresses.values()), addresses=addresses)

        app = create_runner(
            token=TOKEN,
            state_dir=settings.state_dir,
            evidence_dir=settings.evidence_dir,
            sandbox_settings=settings,
            sandbox_factory=lambda _: manager,
        )
        client = TestClient(app)

        # 1. A real shell action: the command runs as the tool user, and its output is evidence.
        shell = self.call(
            client,
            "shell.exec",
            {"command": "id -u", "timeout_seconds": 30},
            target=addresses["tcp"],
            port=7000,
        )
        self.check(
            "shell_exec_runs_as_the_tool_user_and_completes",
            shell["status"] == "completed"
            and shell["result"]["output"].strip() == "10001"
            and shell["result"]["exit_code"] == 0,
            status=shell["status"],
            output=shell["result"]["output"].strip() if shell.get("result") else None,
        )
        first = self.instances()[0]
        self.check(
            "the_permit_was_the_tickets_own_endpoint_and_is_gone_afterwards",
            [(item.address, item.port) for item in first.egress_changes[0].authorized]
            == [(addresses["tcp"], 7000)]
            and first.egress.authorized == []
            and first.egress.revoked_at is not None,
            changes=[change.reason for change in first.egress_changes],
            instance_state=first.state,
        )
        self.check(
            "the_call_left_no_instance_and_no_permit_behind",
            manager.resources() == []
            and all(item.state == "reclaimed" for item in self.instances()),
        )

        # 2. Discovery: the open port and the closed one come from the target itself.
        discovery = self.call(
            client,
            "discover_tcp_services",
            {"ports": [7000, 7001], "timeout_seconds": 30},
            target=addresses["tcp"],
            port=7000,
        )
        summary = discovery["result"]["summary"] if discovery.get("result") else {}
        self.check(
            "discovery_reports_what_the_target_really_answers",
            discovery["status"] == "completed"
            and summary.get("open_ports") == [7000]
            and summary.get("closed_ports") == [7001],
            summary=summary,
        )

        # 3. HTTP: a real 200 from a real server, and a real non-HTTP answer as the counterexample.
        probe = self.call(
            client, "probe_http", {"path": "/", "method": "GET"}, target=addresses["http"], port=8080
        )
        http_summary = probe["result"]["summary"] if probe.get("result") else {}
        self.check(
            "an_http_probe_reports_the_status_the_target_answered",
            probe["status"] == "completed" and http_summary.get("status_code") == 200,
            summary=http_summary,
        )
        blocked = self.call(
            client,
            "probe_http",
            {"path": "/", "method": "GET"},
            target=addresses["tcp"],
            port=7000,
        )
        self.check(
            "a_non_http_answer_is_a_different_outcome_not_a_success",
            blocked["status"] == "failed" and blocked["reason_code"] in {"action_failed"},
            status=blocked["status"],
            reason_code=blocked["reason_code"],
        )

        # 4. A real failure keeps its stderr as evidence.
        failure = self.call(
            client,
            "shell.exec",
            {"command": "echo problem >&2; exit 3", "timeout_seconds": 30},
            target=addresses["tcp"],
            port=7000,
        )
        stderr = next(
            (
                entry
                for entry in (failure["result"]["evidence"] if failure.get("result") else [])
                if entry["relative_path"].endswith("stderr.txt")
            ),
            None,
        )
        self.check(
            "a_failing_action_is_reported_as_failed_with_its_stderr_archived",
            failure["status"] == "failed"
            and failure["reason_code"] == "action_failed"
            and stderr is not None
            and (RESULTS / "evidence" / stderr["relative_path"]).read_text(encoding="utf-8").strip()
            == "problem",
            status=failure["status"],
        )

        # 5. What the tool really reported decides the next action, and that action really runs.
        #    Two different observations lead to two different executed calls: one service answers
        #    HTTP, and the echo target does not.
        http_discovery = self.call(
            client,
            "discover_tcp_services",
            {"ports": [8080], "timeout_seconds": 30},
            target=addresses["http"],
            port=8080,
        )
        http_summary = http_discovery["result"]["summary"] if http_discovery.get("result") else {}
        model = DeterministicModel()
        planned = model.decide(
            "reviewer",
            1,
            {
                "execution_profile": "real-lab-v1",
                "last_summary": http_summary,
                "candidate_ports": [8080],
                "last_target": {"ip": addresses["http"], "port": 8080},
            },
        )
        self.check(
            "a_real_return_selects_the_next_action",
            planned["action"] == "probe_http"
            and planned.get("target_port") == 8080
            and planned.get("target_ip") == addresses["http"],
            discovery=http_summary,
            decision=planned,
        )
        followed = self.call(
            client,
            planned["action"],
            planned["parameters"],
            target=planned.get("target_ip") or addresses["http"],
            port=planned.get("target_port") or 8080,
        )
        followed_summary = followed["result"]["summary"] if followed.get("result") else {}
        self.check(
            "the_selected_action_really_runs_and_reports_the_target",
            followed["status"] == "completed" and followed_summary.get("status_code") == 200,
            summary=followed_summary,
        )
        empty = model.decide(
            "reviewer",
            1,
            {
                "execution_profile": "real-lab-v1",
                "last_summary": {"open_ports": [], "closed_ports": [7000, 7001]},
            },
        )
        self.check(
            "nothing_listening_selects_a_different_action_instead",
            empty["action"] == "finish_research",
            decision=empty,
        )

        # 6. Evidence is real bytes: every entry's hash matches the file that was written.
        mismatches = [
            entry["relative_path"]
            for call in self.report["calls"]
            for entry in (call.get("result") or {}).get("evidence") or []
            if not self.evidence_matches(entry)
        ]
        self.check("every_evidence_file_matches_its_recorded_hash", not mismatches, mismatches=mismatches)

        # 7. One surface, two executors: the demonstration profile still works, and a deployment
        #    without management refuses a real ticket instead of faking it.
        demonstration = client.post(
            "/v1/calls", headers=HEADERS, json=self.fake_ticket().model_dump(mode="json")
        )
        self.check("the_demonstration_profile_is_served_too", demonstration.status_code == 200)
        fake_record = self.settle(client, UUID(demonstration.json()["request"]["call_id"]))
        self.check(
            "the_demonstration_call_stays_a_demonstration_call",
            fake_record["status"] == "completed"
            and fake_record["request"]["execution_profile"] == "fake-p0-v1",
            status=fake_record["status"],
        )
        plain = create_runner(
            token=TOKEN,
            state_dir=RESULTS / "plain-state",
            evidence_dir=RESULTS / "plain-evidence",
            sandbox_settings=SandboxSettings(
                enabled=False,
                state_dir=RESULTS / "plain-state",
                evidence_dir=RESULTS / "plain-evidence",
            ),
        )
        refused = TestClient(plain).post(
            "/v1/calls",
            headers=HEADERS,
            json=self.ticket("shell.exec", {"command": "id"}, addresses["tcp"], 7000).model_dump(
                mode="json"
            ),
        )
        self.check(
            "a_real_ticket_without_management_is_refused_rather_than_faked",
            refused.status_code == 409
            and refused.json()["reason_code"] == "real_execution_disabled",
            status_code=refused.status_code,
        )

        # 8. Nothing is left behind by any of it.
        self.check(
            "no_labelled_resource_survives_the_calls",
            manager.resources(run_id=self.run_id) == []
            and [item.state for item in self.instances() if item.state != "reclaimed"] == [],
            instances=len(self.instances()),
        )
        self.report["passed"] = True

    # -- driving real tickets ----------------------------------------------------------------

    def call(
        self, client: TestClient, action: str, parameters: dict[str, Any], *, target: str, port: int
    ) -> dict[str, Any]:
        ticket = self.ticket(action, parameters, target, port)
        response = client.post("/v1/calls", headers=HEADERS, json=ticket.model_dump(mode="json"))
        if response.status_code != 200:
            raise AssertionError(f"submit refused: {response.status_code} {response.text}")
        record = self.settle(client, ticket.call_id)
        self.report["calls"].append(
            {
                "call_id": str(ticket.call_id),
                "action": action,
                "status": record["status"],
                "reason_code": record.get("reason_code"),
                "result": record.get("result"),
            }
        )
        self.event(
            "call_settled",
            action=action,
            status=record["status"],
            reason_code=record.get("reason_code"),
        )
        return record

    def settle(self, client: TestClient, call_id: UUID, timeout: float = 90) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = client.get(f"/v1/calls/{call_id}", headers=HEADERS)
            if response.status_code == 200:
                body = response.json()
                if body["status"] in {"completed", "failed", "cancelled"}:
                    return body
            time.sleep(0.2)
        raise AssertionError(f"call {call_id} never settled: {body.get('status')}")

    def ticket(self, action: str, parameters: dict[str, Any], target: str, port: int) -> ExecutionRequest:
        """One ticket of this probe's Run.

        The authorization identity is fixed for the whole run — one Run acts under one
        authorization snapshot — while the call, session, decision and deadlines are per call.
        """
        now = datetime.now(UTC)
        if self.authorization is None:
            self.authorization = {
                "scope_id": str(uuid4()),
                "scope_version": 1,
                "policy_version": 1,
                "authorized_until": (now + timedelta(hours=1)).isoformat(),
            }
        return ExecutionRequest.model_validate(
            {
                "call_id": str(uuid4()),
                "run_id": str(self.run_id),
                "session_id": str(uuid4()),
                "decision_id": str(uuid4()),
                "scope_id": self.authorization["scope_id"],
                "budget_reservation_id": str(uuid4()),
                "action_id": action,
                "parameters": parameters,
                "parameters_hash": parameters_hash(parameters),
                "scope_version": self.authorization["scope_version"],
                "policy_version": self.authorization["policy_version"],
                "lease_generation": 1,
                "lease_expires_at": (now + timedelta(seconds=15)).isoformat(),
                "deadline_at": (now + timedelta(seconds=90)).isoformat(),
                "authorized_until": self.authorization["authorized_until"],
                "execution_profile": "real-lab-v1",
                "target_ip": target,
                "target_port": port,
            }
        )

    def fake_ticket(self) -> ExecutionRequest:
        now = datetime.now(UTC)
        parameters = {"scenario": "success", "duration_ms": 200}
        return ExecutionRequest.model_validate(
            {
                "call_id": str(uuid4()),
                "run_id": str(uuid4()),
                "session_id": str(uuid4()),
                "decision_id": str(uuid4()),
                "scope_id": str(uuid4()),
                "budget_reservation_id": str(uuid4()),
                "action_id": "fake.collect",
                "parameters": parameters,
                "parameters_hash": parameters_hash(parameters),
                "scope_version": 1,
                "policy_version": 1,
                "lease_generation": 1,
                "lease_expires_at": (now + timedelta(seconds=15)).isoformat(),
                "deadline_at": (now + timedelta(seconds=60)).isoformat(),
                "authorized_until": (now + timedelta(hours=1)).isoformat(),
                "execution_profile": "fake-p0-v1",
                "target_ip": "192.0.2.10",
                "target_port": 80,
            }
        )

    def instances(self) -> list[Any]:
        assert self.manager is not None
        return list(self.manager.instances.values())

    def evidence_matches(self, entry: dict[str, Any]) -> bool:
        try:
            payload = (RESULTS / "evidence" / entry["relative_path"]).read_bytes()
        except OSError:
            return False
        return hashlib.sha256(payload).hexdigest() == entry["sha256"]

    # -- lab plumbing -------------------------------------------------------------------------

    def target_network(self) -> Any:
        existing = self.client.networks.list(filters={"name": f"^{TARGET_NETWORK}$"})
        if existing:
            raise AssertionError(f"{TARGET_NETWORK} already exists: another run is using it")
        network = self.client.networks.create(
            TARGET_NETWORK, driver="bridge", internal=False, labels={LABEL: str(self.run_id)}
        )
        self.event("target_network_created", name=TARGET_NETWORK)
        return network

    def fixture(self, role: str, network: Any, command: list[str]) -> Any:
        container = self.client.containers.create(
            image=PROBE_IMAGE,
            name=f"{self.prefix}-{role}",
            command=command,
            labels={LABEL: str(self.run_id)},
            detach=True,
            user="10001:10001",
            init=True,
            network=network.name if hasattr(network, "name") else network,
        )
        container.start()
        return container

    def address_of(self, container: Any, network: Any) -> str:
        container.reload()
        name = network.name if hasattr(network, "name") else network
        return str(container.attrs["NetworkSettings"]["Networks"][name]["IPAddress"])

    def await_listening(self, container: Any, address: str, port: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.probe(container, address, port):
                return
            time.sleep(0.2)
        raise AssertionError(f"{container.name} never listened on {port}")

    def probe(self, container: Any, address: str, port: int) -> bool:
        """Whether something is listening: a plain TCP connect, not the echo fixture's reply."""
        container.reload()
        if not address:
            return False
        result = container.exec_run(
            ["python", "/lab/fixture.py", "tcp-open", address, str(port)], user="10001:10001"
        )
        lines = result.output.decode("utf-8", errors="replace").strip().splitlines()
        if not lines:
            return False
        return bool(json.loads(lines[-1]).get("connected"))

    def cleanup(self) -> None:
        failures: list[str] = []
        if self.manager is not None:
            try:
                self.manager.begin_revert(reason="probe_cleanup")
            except Exception as error:
                failures.append(type(error).__name__)
            self.manager.close()
        cleaned = self.remove_labelled()
        surviving = self.list_labelled()
        self.report["cleaned"] = cleaned
        self.report["leftovers"] = surviving
        self.report["cleanup_failures"] = failures
        if surviving or failures:
            self.report["passed"] = False
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "report.json").write_text(json.dumps(self.report, indent=2, default=str))

    def remove_labelled(self) -> list[str]:
        selector = {"label": f"{LABEL}={self.run_id}"}
        removed: list[str] = []
        for container in self.client.containers.list(all=True, filters=selector):
            removed.append(f"container:{container.name}")
            container.remove(force=True)
        for network in self.client.networks.list(filters=selector):
            removed.append(f"network:{network.name}")
            network.remove()
        return removed

    def list_labelled(self) -> list[str]:
        selector = {"label": f"{LABEL}={self.run_id}"}
        names = [
            f"container:{item.name}"
            for item in self.client.containers.list(all=True, filters=selector)
        ]
        names += [f"network:{item.name}" for item in self.client.networks.list(filters=selector)]
        names += [f"volume:{item.name}" for item in self.client.volumes.list(filters=selector)]
        return names


if __name__ == "__main__":
    check = ActionCheck()
    try:
        check.run()
    except Exception as error:
        check.report["reason_code"] = "real_action_failed"
        check.report["error_type"] = type(error).__name__
        check.report["error"] = str(error)
        traceback.print_exc()
    finally:
        check.cleanup()
    raise SystemExit(0 if check.report["passed"] else 1)
