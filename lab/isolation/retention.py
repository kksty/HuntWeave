"""Selective-retention check: what a finished call keeps, and who may take it away.

This probe builds the *product's* Runner (`create_runner`) with sandbox management enabled and
drives it only through its HTTP surface — tickets in through `POST /v1/calls`, retention reads and
writes through `/v1/retention*` — exactly the way the real-action probe does. Every fact is then
read back with the Docker SDK and from the evidence directory, so the component under test is never
the only witness.

What it demonstrates (P1 slice #20, PROJECT.md section 10.5):

* a version's candidacy counts *independent Runs* inside the window, so a fourth successful call
  inside an existing Run raises the call count and never the Run count;
* a finished real call does not destroy its private workspace: the volume survives reclamation,
  named by the retention ledger, while that call's containers and session network are gone;
* a pin protects material from *automatic* reclamation under real capacity pressure — the sweep
  reclaims what it may and leaves the pinned artifact and its volume alone — and pressure that
  cannot be met is reported as a block with a shortfall, not paid for with protected material;
* an explicit delete removes the environment and keeps the evidence and the environment manifest;
* nothing is published: a private workspace never becomes a shared tool version.

Two Runners are started here, and that is deliberate. Checks 1 and 2 run under the deployment's own
defaults, which is the policy a normal deployment has. Everything after that needs *real* capacity
pressure to mean anything: a pin only demonstrates something where the policy would otherwise
reclaim the material, and an unpressured sweep selects nothing at all. So a second Runner is built
on the same state directory with `HUNTWEAVE_RETENTION_CAPACITY_BYTES=1`. The ledger is the
deployment's — artifacts, pins and uses are read back from `retention.json` — and only the policy
differs, which is exactly what that variable is for. It is the same construction the product's own
retention checks use (a store configured with a tiny capacity), minus the HTTP surface: the policy
is set in this process's environment before the second Runner's store is first opened, which is
when `RetentionLimits.from_env` reads it.
"""

import hashlib
import json
import os
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import docker
from fastapi.testclient import TestClient
from huntweave.config import SandboxSettings
from huntweave.contracts.execution import ExecutionRequest, parameters_hash
from huntweave.execution.dockerruntime import open_sandbox_manager
from huntweave.execution.sandbox import SandboxManager
from huntweave.execution.server import create_runner

RESULTS = Path("/results")
PROBE_IMAGE = "huntweave-isolation-probe:p0"
PROFILE = "sandbox-egress-v1"
TARGET_NETWORK = "huntweave-lab-egress-targets"
LABEL = "com.huntweave.retention_probe"
NAMESPACE = "com.huntweave"
PROJECT_LABEL = f"{NAMESPACE}.project"
PROJECT_NAME = "huntweave"
TOKEN = "retention-probe-token-" + "0" * 42
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ACTOR = "retention-probe"

# What each call writes into its own workspace before it ends. Capacity pressure has to be measured
# against something: an engine reports no size for a volume nobody wrote to, and an unmeasured
# artifact is never counted as bytes (contracts/retention.py). 256 KiB of random bytes is far above
# the tiny capacity the later checks configure and far below any profile quota, and the size is
# printed as the call's stdout so the workspace and the evidence agree.
MATERIAL_BYTES = 262144
SHELL_COMMAND = (
    f"head -c {MATERIAL_BYTES} /dev/urandom > /workspace/reproduction-material.bin; "
    "wc -c < /workspace/reproduction-material.bin; echo workspace-material-written"
)
TINY_CAPACITY_BYTES = 1


class RetentionCheck:
    def __init__(self) -> None:
        self.client = docker.from_env(timeout=15)
        self.manager: SandboxManager | None = None
        self.probe_id = str(uuid4())
        self.run_ids = [uuid4() for _ in range(3)]
        self.authorizations: dict[str, dict[str, Any]] = {}
        self.prefix = f"huntweave-retention-{self.probe_id[:12]}"
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
            "observations": [],
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

    def observe(self, name: str, **values: object) -> None:
        """A fact worth reading back later that is not itself a pass/fail rule."""
        self.report["observations"].append(
            {"at": datetime.now(UTC).isoformat(), "observation": name, **values}
        )

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
        fixture = self.fixture("target", network, ["python", "/lab/fixture.py", "target"])
        address = self.address_of(fixture, network)
        self.report["fixture"] = {"network": network, "address": address}
        self.await_listening(fixture, address, 7000)
        self.check("the_lab_target_is_really_listening", bool(address), address=address)

        app = create_runner(
            token=TOKEN,
            state_dir=settings.state_dir,
            evidence_dir=settings.evidence_dir,
            sandbox_settings=settings,
            sandbox_factory=lambda _: manager,
        )
        call_client = TestClient(app)

        # 1. Three Runs, one successfully finished call each: candidacy counts Runs, not calls.
        for run_id in self.run_ids:
            self.call(call_client, run_id, target=address, port=7000)
        view = self.view(call_client)
        versions = view["versions"]
        self.check(
            "candidate_counting_is_per_independent_run",
            len(versions) == 1
            and versions[0]["successful_runs"] == 3
            and versions[0]["successful_calls"] == 3
            and versions[0]["threshold_runs"] == 3
            and versions[0]["window_days"] == 30
            and versions[0]["candidate"] is True,
            versions=len(versions),
            successful_runs=None if not versions else versions[0]["successful_runs"],
            successful_calls=None if not versions else versions[0]["successful_calls"],
            threshold_runs=None if not versions else versions[0]["threshold_runs"],
            window_days=None if not versions else versions[0]["window_days"],
            candidate=None if not versions else versions[0]["candidate"],
        )

        # A fourth successful call inside a Run that already used the version is a retry: it raises
        # the call count and must not raise the number of Runs.
        self.call(call_client, self.run_ids[0], target=address, port=7000)
        view = self.view(call_client)
        versions = view["versions"]
        self.check(
            "a_retry_does_not_look_like_a_new_run",
            len(versions) == 1
            and versions[0]["successful_runs"] == 3
            and versions[0]["successful_calls"] == 4
            and versions[0]["candidate"] is True,
            successful_runs=None if not versions else versions[0]["successful_runs"],
            successful_calls=None if not versions else versions[0]["successful_calls"],
        )

        # 2. Every finished call kept its private workspace, and the retention ledger names it. The
        #    Docker SDK is the witness for the volume; the ledger is the witness for the artifact
        #    and for the identities that tie the two together.
        artifacts = view["artifacts"]
        problems: list[str] = []
        files_kept: list[dict[str, Any]] = []
        for entry in self.report["calls"]:
            instance_id = entry["instance_id"]
            run_id = entry["run_id"]
            session_id = entry["session_id"]
            artifact = next(
                (
                    item
                    for item in artifacts
                    if item["instance_id"] == instance_id and item["run_id"] == run_id
                ),
                None,
            )
            if artifact is None:
                problems.append(f"no_artifact:{instance_id}")
                continue
            volume = self.volume_of(instance_id)
            if volume is None:
                problems.append(f"no_project_volume:{instance_id}")
                continue
            containers = self.containers_of(instance_id)
            networks = self.networks_of(instance_id)
            expected = (
                f"huntweave-{run_id[:8]}-{session_id[:8]}-{instance_id[:8]}-workspace"
            )
            if artifact["state"] != "retained":
                problems.append(f"artifact_not_retained:{artifact['artifact_id']}")
            if artifact["resource_name"] != volume.name:
                problems.append(f"ledger_names_another_volume:{artifact['resource_name']}")
            if volume.name != expected:
                problems.append(f"volume_name_not_derived_from_identities:{volume.name}")
            if containers:
                problems.append(f"containers_survived:{instance_id}")
            if networks:
                problems.append(f"session_network_survived:{instance_id}")
            if artifact["size_bytes"] is not None and artifact["size_bytes"] < 0:
                problems.append(f"negative_size:{artifact['artifact_id']}")
            # The later checks act on this call's artifact; the ledger's own answer is what names it.
            entry["artifact_id"] = artifact["artifact_id"]
            entry["environment_key"] = artifact["environment_key"]
            files_kept.append(
                {
                    "call_id": entry["call_id"],
                    "run_id": run_id,
                    "instance_id": instance_id,
                    "artifact_id": artifact["artifact_id"],
                    "resource_name": artifact["resource_name"],
                    "state": artifact["state"],
                    # An unmeasured artifact is reported as null, never as a zero-byte one.
                    "size_bytes": artifact["size_bytes"],
                    "containers": len(containers),
                    "session_networks": len(networks),
                }
            )
        self.observe(
            "private_workspaces_kept",
            artifacts=files_kept,
            capacity_bytes=view["limits"]["cache_capacity_bytes"],
        )
        self.check(
            "a_finished_call_keeps_its_private_workspace_named_by_the_ledger",
            not problems and len(files_kept) == len(self.report["calls"]),
            calls=len(self.report["calls"]),
            artifacts=files_kept,
            problems=problems,
        )

        # Everything from here on runs under the deployment's own ledger and a tiny capacity: the
        # policy a pin has to survive. No further call is submitted through the first Runner, so
        # the two stores never write the same ledger from different in-memory states.
        os.environ["HUNTWEAVE_RETENTION_CAPACITY_BYTES"] = str(TINY_CAPACITY_BYTES)
        constrained = create_runner(
            token=TOKEN,
            state_dir=settings.state_dir,
            evidence_dir=settings.evidence_dir,
            sandbox_settings=settings,
            sandbox_factory=lambda _: manager,
        )
        # The first Runner keeps the call ledger — that ledger has a single owner, and the second
        # Runner is here for the retention policy, not to run calls. So `call_client` stays the
        # witness for what a call recorded, and `client` is the deployment under the tiny cache.
        client = TestClient(constrained)
        self.event(
            "constrained_runner_started",
            capacity_bytes=TINY_CAPACITY_BYTES,
            note="the deployment's ledger, a tiny cache: the policy a pin must survive",
        )

        # 3. A pin protects material from automatic reclamation. The retry call's artifact is the
        #    one held back, so the policy really has other artifacts to take.
        retry = self.report["calls"][-1]
        artifact_id = retry["artifact_id"]
        view = self.view(client)
        capacity = view["limits"]["cache_capacity_bytes"]
        pinned_view = self.post(
            client,
            f"/v1/retention/artifacts/{artifact_id}/pin",
            {"note": "probe: keep this reproduction material", "actor": ACTOR},
        )
        pinned = next(
            (item for item in pinned_view["artifacts"] if item["artifact_id"] == artifact_id), None
        )
        protected = [
            item
            for item in pinned_view["preview"]["protected"]
            if item["artifact_id"] == artifact_id
        ]
        selected = [item["artifact_id"] for item in pinned_view["preview"]["to_delete"]]
        swept = self.post(client, "/v1/retention/sweep", {"note": "probe: sweep", "actor": ACTOR})
        surviving = next(
            (
                item
                for item in swept["view"]["artifacts"]
                if item["artifact_id"] == artifact_id
            ),
            None,
        )
        self.observe(
            "pin_under_pressure",
            artifact_id=artifact_id,
            capacity_bytes=capacity,
            size_bytes=None if pinned is None else pinned["size_bytes"],
            selected_for_reclaim=selected,
            protected_reasons=[item["reason_code"] for item in protected],
            swept=swept["deleted"],
            blocked=pinned_view["preview"]["blocked"],
            needed_bytes=pinned_view["preview"]["needed_bytes"],
        )
        self.check(
            "a_pin_protects_material_from_automatic_reclamation",
            capacity == TINY_CAPACITY_BYTES
            and pinned is not None
            and pinned["pinned"] is True
            and len(protected) == 1
            and protected[0]["reason_code"] == "retention_artifact_pinned"
            and artifact_id not in selected
            and bool(selected)
            and set(swept["deleted"]) == set(selected)
            and artifact_id not in swept["deleted"]
            and surviving is not None
            and surviving["state"] == "retained"
            and self.volume_of(retry["instance_id"]) is not None,
            capacity_bytes=capacity,
            size_bytes=None if pinned is None else pinned["size_bytes"],
            pinned=None if pinned is None else pinned["pinned"],
            protected_reasons=[item["reason_code"] for item in protected],
            selected_for_reclaim=selected,
            swept=swept["deleted"],
            failed=swept["failed"],
            volume_after_sweep=self.volume_of(retry["instance_id"]) is not None,
        )

        # 3b. The `pinned` flag on a *version* names a version-level pin, not an artifact pin: the
        #     product ties it to the `version|<key>` pin (execution/retention.py `_versions`), which
        #     is a different granularity and a different act. The rule "a version pin covers its
        #     artifacts" is checked here, and the state the later checks need — exactly the artifact
        #     pin of check 3 and nothing else — is restored before returning.
        key = retry["environment_key"]
        self.post(
            client,
            f"/v1/retention/artifacts/{artifact_id}/unpin",
            {"note": "probe: exercising the version-level pin", "actor": ACTOR},
        )
        version_pinned = self.post(
            client,
            f"/v1/retention/versions/{key}/pin",
            {"note": "probe: pin the whole version", "actor": ACTOR},
        )
        covered = [
            item
            for item in version_pinned["preview"]["protected"]
            if item["artifact_id"] == artifact_id
        ]
        version_flag = next(
            (
                item["pinned"]
                for item in version_pinned["versions"]
                if item["environment_key"] == key
            ),
            None,
        )
        self.post(
            client,
            f"/v1/retention/versions/{key}/unpin",
            {"note": "probe: version pin checked", "actor": ACTOR},
        )
        restored = self.post(
            client,
            f"/v1/retention/artifacts/{artifact_id}/pin",
            {"note": "probe: keep this reproduction material", "actor": ACTOR},
        )
        restored_artifact = next(
            (item for item in restored["artifacts"] if item["artifact_id"] == artifact_id), None
        )
        self.check(
            "a_version_pin_protects_every_artifact_of_the_version",
            version_flag is True
            and len(covered) == 1
            and covered[0]["reason_code"] == "retention_version_pinned"
            and restored_artifact is not None
            and restored_artifact["pinned"] is True,
            environment_key=key,
            version_pinned=version_flag,
            protected_reasons=[item["reason_code"] for item in covered],
            artifact_pinned_again=None
            if restored_artifact is None
            else restored_artifact["pinned"],
        )

        # 5. Capacity pressure is a block, not a deletion of protected material. Nothing is
        #    reclaimable any more and the cache is still over its capacity: the answer is the
        #    shortfall, and a sweep asked again must not answer it by taking the pinned artifact.
        #
        #    The probe's own volume is created first, under the probe's label and with none of this
        #    project's: whatever a reclaim does about this deployment's unreferenced artifacts, a
        #    volume it does not own is not its to remove.
        foreign = self.client.volumes.create(
            name=f"{self.prefix}-foreign", labels={LABEL: self.probe_id}
        )
        view = self.view(client)
        preview = view["preview"]
        swept_again = self.post(
            client, "/v1/retention/sweep", {"note": "probe: sweep under pressure", "actor": ACTOR}
        )
        self.observe(
            "capacity_pressure",
            capacity_bytes=preview["capacity_bytes"],
            retained_bytes=preview["retained_bytes"],
            unmeasured_artifacts=preview["unmeasured_artifacts"],
            blocked=preview["blocked"],
            needed_bytes=preview["needed_bytes"],
            foreign_volume=foreign.name,
        )
        self.check(
            "capacity_pressure_is_a_block_not_a_deletion_of_protected_material",
            preview["capacity_bytes"] == TINY_CAPACITY_BYTES
            and preview["blocked"] is True
            and preview["reason_code"] == "retention_capacity_insufficient"
            and preview["needed_bytes"] > 0
            and preview["retained_bytes"] > preview["capacity_bytes"]
            and artifact_id not in swept_again["deleted"]
            and self.volume_of(retry["instance_id"]) is not None
            and self.foreign_volume_exists(foreign.name),
            capacity_bytes=preview["capacity_bytes"],
            retained_bytes=preview["retained_bytes"],
            unmeasured_artifacts=preview["unmeasured_artifacts"],
            blocked=preview["blocked"],
            reason_code=preview["reason_code"],
            needed_bytes=preview["needed_bytes"],
            swept=swept_again["deleted"],
            artifact_volume_still_present=self.volume_of(retry["instance_id"]) is not None,
            foreign_volume_still_present=self.foreign_volume_exists(foreign.name),
        )

        # 4. An explicit delete removes the environment and keeps the evidence and the manifest:
        #    the one thing a pin was holding is exactly what an operator may still remove.
        self.post(
            client,
            f"/v1/retention/artifacts/{artifact_id}/unpin",
            {"note": "probe: release the pin", "actor": ACTOR},
        )
        deleted = self.post(
            client,
            f"/v1/retention/artifacts/{artifact_id}/delete",
            {"note": "probe: reclaim after reproduction", "actor": ACTOR},
        )
        record = self.query(call_client, retry["call_id"])
        evidence = (record.get("result") or {}).get("evidence") or []
        paths = {item["relative_path"] for item in evidence}
        transcript = f"{retry['run_id']}/{retry['call_id']}.txt"
        hashes_match = bool(evidence) and all(self.evidence_matches(item) for item in evidence)
        state = self.get(client, f"/v1/runs/{retry['run_id']}/sandbox-state")
        instance = next(
            (
                item
                for session in state["sessions"]
                for item in session["instances"]
                if item["instance_id"] == retry["instance_id"]
            ),
            None,
        )
        manifest = (instance or {}).get("environment") or {}
        self.observe(
            "explicit_delete",
            artifact_id=artifact_id,
            deleted=deleted["deleted"],
            evidence=sorted(paths),
            manifest_images=sorted(manifest.get("image_digests", {})),
        )
        self.check(
            "an_explicit_delete_removes_the_environment_and_keeps_the_evidence_and_the_manifest",
            artifact_id in deleted["deleted"]
            and deleted["failed"] == []
            and self.volume_of(retry["instance_id"]) is None
            and all(item["artifact_id"] != artifact_id for item in deleted["view"]["artifacts"])
            and hashes_match
            and transcript in paths
            and any(path.endswith("/stdout.txt") for path in paths)
            and any(path.endswith("/stderr.txt") for path in paths)
            and instance is not None
            and instance["state"] == "reclaimed"
            and manifest.get("profile_id") == PROFILE
            and set(manifest.get("image_digests", {})) == {"gateway", "tool"},
            deleted=deleted["deleted"],
            failed=deleted["failed"],
            volume_after_delete=self.volume_of(retry["instance_id"]) is not None,
            evidence=sorted(paths),
            evidence_hashes_match=hashes_match,
            instance_state=None if instance is None else instance["state"],
            manifest_profile=manifest.get("profile_id"),
            manifest_images=sorted(manifest.get("image_digests", {})),
        )

        # 6. Nothing is published: a private workspace is never promoted into a shared tool version,
        #    and the surface that would do it does not exist.
        view = self.view(client)
        publish = client.post(
            "/v1/retention/publish",
            headers=HEADERS,
            json={"note": "probe: try to promote a private workspace", "actor": ACTOR},
        )
        self.check(
            "nothing_is_published_as_a_shared_tool_version",
            bool(view["versions"])
            and all(item["shared_tool_version"] is False for item in view["versions"])
            and publish.status_code == 501
            and publish.json().get("reason_code") == "execution_not_implemented",
            versions=len(view["versions"]),
            shared_tool_version=sorted(
                {str(item["shared_tool_version"]) for item in view["versions"]}
            ),
            publish_status=publish.status_code,
            publish_reason=publish.json().get("reason_code"),
        )
        self.report["passed"] = True

    # -- driving real tickets ----------------------------------------------------------------

    def call(
        self, client: TestClient, run_id: UUID, *, target: str, port: int
    ) -> dict[str, Any]:
        """Submit one `shell.exec` ticket for a Run and wait for it to settle."""
        ticket = self.ticket(run_id, target, port)
        response = client.post("/v1/calls", headers=HEADERS, json=ticket.model_dump(mode="json"))
        if response.status_code != 200:
            raise AssertionError(f"submit refused: {response.status_code} {response.text}")
        record = self.settle(client, ticket.call_id)
        runtime = record.get("runtime") or {}
        instance_id = runtime.get("instance_id")
        if not instance_id:
            # A real call that never announced an instance is not something to be guessed about.
            raise AssertionError(f"call {ticket.call_id} announced no instance")
        self.report["calls"].append(
            {
                "call_id": str(ticket.call_id),
                "run_id": str(run_id),
                # The execution session the manager opened for this call, not the model session on
                # the ticket: the workspace volume is named after the instance's own identities.
                "session_id": str(runtime.get("session_id")),
                "agent_session_id": str(ticket.session_id),
                "instance_id": str(instance_id),
                "action": "shell.exec",
                "status": record["status"],
                "reason_code": record.get("reason_code"),
                "result": record.get("result"),
            }
        )
        self.event(
            "call_settled",
            action="shell.exec",
            run_id=str(run_id),
            status=record["status"],
            reason_code=record.get("reason_code"),
        )
        return record

    def settle(self, client: TestClient, call_id: UUID, timeout: float = 120) -> dict[str, Any]:
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

    def ticket(self, run_id: UUID, target: str, port: int) -> ExecutionRequest:
        """One ticket of this probe's Run.

        The authorization identity is fixed per Run — one Run acts under one authorization snapshot —
        while the call, session, decision and deadlines are per call. Each of the probe's Runs is its
        own Run, so each carries its own snapshot: three Runs sharing one scope would be three names
        for one authorization.
        """
        now = datetime.now(UTC)
        key = str(run_id)
        if key not in self.authorizations:
            self.authorizations[key] = {
                "scope_id": str(uuid4()),
                "scope_version": 1,
                "policy_version": 1,
                "authorized_until": (now + timedelta(hours=1)).isoformat(),
            }
        authorization = self.authorizations[key]
        parameters = {"command": SHELL_COMMAND, "timeout_seconds": 60}
        return ExecutionRequest.model_validate(
            {
                "call_id": str(uuid4()),
                "run_id": key,
                "session_id": str(uuid4()),
                "decision_id": str(uuid4()),
                "scope_id": authorization["scope_id"],
                "budget_reservation_id": str(uuid4()),
                "action_id": "shell.exec",
                "parameters": parameters,
                "parameters_hash": parameters_hash(parameters),
                "scope_version": authorization["scope_version"],
                "policy_version": authorization["policy_version"],
                "lease_generation": 1,
                "lease_expires_at": (now + timedelta(seconds=15)).isoformat(),
                "deadline_at": (now + timedelta(seconds=90)).isoformat(),
                "authorized_until": authorization["authorized_until"],
                "execution_profile": "real-lab-v1",
                "target_ip": target,
                "target_port": port,
            }
        )

    # -- the Runner's retention surface ------------------------------------------------------

    def get(self, client: TestClient, path: str) -> dict[str, Any]:
        response = client.get(path, headers=HEADERS)
        if response.status_code != 200:
            raise AssertionError(f"{path} refused: {response.status_code} {response.text}")
        return response.json()

    def post(self, client: TestClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = client.post(path, headers=HEADERS, json=payload)
        if response.status_code != 200:
            raise AssertionError(f"{path} refused: {response.status_code} {response.text}")
        return response.json()

    def view(self, client: TestClient) -> dict[str, Any]:
        return self.get(client, "/v1/retention")

    def query(self, client: TestClient, call_id: str) -> dict[str, Any]:
        return self.get(client, f"/v1/calls/{call_id}")

    def evidence_matches(self, entry: dict[str, Any]) -> bool:
        try:
            payload = (RESULTS / "evidence" / entry["relative_path"]).read_bytes()
        except OSError:
            return False
        return hashlib.sha256(payload).hexdigest() == entry["sha256"]

    # -- Docker SDK facts --------------------------------------------------------------------

    def volume_of(self, instance_id: str) -> Any | None:
        """The workspace volume of one instance, found only by this project's own labels."""
        found = self.client.volumes.list(
            filters={
                "label": [
                    f"{PROJECT_LABEL}={PROJECT_NAME}",
                    f"{NAMESPACE}.instance_id={instance_id}",
                ]
            }
        )
        return found[0] if found else None

    def containers_of(self, instance_id: str) -> list[Any]:
        return self.client.containers.list(
            all=True, filters={"label": f"{NAMESPACE}.instance_id={instance_id}"}
        )

    def networks_of(self, instance_id: str) -> list[Any]:
        return self.client.networks.list(
            filters={"label": f"{NAMESPACE}.instance_id={instance_id}"}
        )

    def foreign_volume_exists(self, name: str) -> bool:
        return bool(self.client.volumes.list(filters={"name": f"^{name}$"}))

    # -- lab plumbing -------------------------------------------------------------------------

    def target_network(self) -> str:
        """The bridge the profile names as its target network, created here if it is missing.

        The profile attaches every instance's gateway to this network, so nothing can launch without
        it. A network that was already there belongs to whoever made it: only one this probe created
        carries this probe's label, and only a labelled one is removed with the rest of its fixtures.
        """
        if self.client.networks.list(filters={"name": f"^{TARGET_NETWORK}$"}):
            return TARGET_NETWORK
        self.client.networks.create(
            TARGET_NETWORK, driver="bridge", internal=False, labels={LABEL: self.probe_id}
        )
        self.event("target_network_created", name=TARGET_NETWORK)
        return TARGET_NETWORK

    def fixture(self, role: str, network: str, command: list[str]) -> Any:
        container = self.client.containers.create(
            image=PROBE_IMAGE,
            name=f"{self.prefix}-{role}",
            command=command,
            labels={LABEL: self.probe_id},
            detach=True,
            user="10001:10001",
            init=True,
            network=network,
        )
        container.start()
        return container

    def address_of(self, container: Any, network: str) -> str:
        container.reload()
        return str(container.attrs["NetworkSettings"]["Networks"][network]["IPAddress"])

    def await_listening(self, container: Any, address: str, port: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.listening(container, address, port):
                return
            time.sleep(0.2)
        raise AssertionError(f"{container.name} never listened on {port}")

    def listening(self, container: Any, address: str, port: int) -> bool:
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

    # -- cleanup ------------------------------------------------------------------------------

    def cleanup(self) -> None:
        failures: list[str] = []
        if self.manager is not None:
            try:
                # A revert stops and reclaims every instance this manager still records as running
                # or unfinished — the case where a call failed before it could release its own
                # instance. It deliberately skips an instance that was already reclaimed, because a
                # *retained* volume outlives its reclaimed instance by design; what returns those is
                # the labelled removal below, which selects exactly the Runs this probe drove.
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
        removed: list[str] = []
        for container in self.client.containers.list(
            all=True, filters={"label": f"{LABEL}={self.probe_id}"}
        ):
            removed.append(f"container:{container.name}")
            container.remove(force=True)
        for network in self.client.networks.list(filters={"label": f"{LABEL}={self.probe_id}"}):
            removed.append(f"network:{network.name}")
            network.remove()
        for volume in self.client.volumes.list(filters={"label": f"{LABEL}={self.probe_id}"}):
            removed.append(f"volume:{volume.name}")
            volume.remove(force=True)
        # The probe's own Runs are this project's too: anything still labelled with one of them is
        # this probe's leak, whoever's ledger wrote it.
        for selector in self.project_selectors():
            for container in self.client.containers.list(all=True, filters=selector):
                removed.append(f"container:{container.name}")
                container.remove(force=True)
            for network in self.client.networks.list(filters=selector):
                removed.append(f"network:{network.name}")
                network.remove()
            for volume in self.client.volumes.list(filters=selector):
                removed.append(f"volume:{volume.name}")
                volume.remove(force=True)
        return removed

    def list_labelled(self) -> list[str]:
        names = [
            f"container:{item.name}"
            for item in self.client.containers.list(
                all=True, filters={"label": f"{LABEL}={self.probe_id}"}
            )
        ]
        names += [
            f"network:{item.name}"
            for item in self.client.networks.list(filters={"label": f"{LABEL}={self.probe_id}"})
        ]
        names += [
            f"volume:{item.name}"
            for item in self.client.volumes.list(filters={"label": f"{LABEL}={self.probe_id}"})
        ]
        for selector in self.project_selectors():
            names += [
                f"container:{item.name}"
                for item in self.client.containers.list(all=True, filters=selector)
            ]
            names += [
                f"network:{item.name}" for item in self.client.networks.list(filters=selector)
            ]
            names += [f"volume:{item.name}" for item in self.client.volumes.list(filters=selector)]
        return names

    def project_selectors(self) -> list[dict[str, Any]]:
        """This project's resources, one selector per Run this probe drove."""
        return [
            {
                "label": [
                    f"{PROJECT_LABEL}={PROJECT_NAME}",
                    f"{NAMESPACE}.run_id={run_id}",
                ]
            }
            for run_id in self.run_ids
        ]


if __name__ == "__main__":
    check = RetentionCheck()
    try:
        check.run()
    except Exception as error:
        check.report["reason_code"] = "selective_retention_failed"
        check.report["error_type"] = type(error).__name__
        check.report["error"] = str(error)
        traceback.print_exc()
    finally:
        check.cleanup()
    results = check.report["checks"]
    print(
        f"checks: {sum(1 for item in results if item['passed'])}/{len(results)} passed; "
        f"leftovers: {len(check.report['leftovers'])}; "
        f"cleanup_failures: {len(check.report['cleanup_failures'])}",
        flush=True,
    )
    raise SystemExit(0 if check.report["passed"] else 1)
