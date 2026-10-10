"""Egress control and cancellation check: real containers, real targets, no external address.

The probe drives the product's own trusted manager and reads the result back with the Docker SDK,
so the component under test is never the only witness of its own behaviour. Everything it talks
to is a lab fixture on a lab bridge: the authorized target, an unauthorized target and a fixture
on the probe's own control network. No external address is contacted; the metadata address is
probed only because the gateway must drop it locally.

What it demonstrates (P1 slice #18):

* the gateway holds default-deny rules before the tool container exists at all;
* only the authorized IPv4/TCP endpoints are reachable, and the control network, the metadata
  address and IPv6 are not;
* a revoke ends an established connection within the profile's revocation bound, and the moment
  it happened is recorded;
* a narrower scope stops out-of-scope use while leaving the remaining endpoint reachable;
* a halt reclaims the processes — including a descendant that escaped its process group — and the
  connections, and confirms the stop;
* a lapsed control lease is closed by the manager itself, without the control plane;
* the revert sequence stops new executions, revokes, reclaims, audits and only then allows the
  deployment to withdraw management.
"""

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

from huntweave.config import SandboxSettings
from huntweave.execution.dockerruntime import open_sandbox_manager
from huntweave.execution.sandbox import (
    LABEL_NAMESPACE,
    PROJECT_NAME,
    AuthorizedEndpoint,
    EgressUpdate,
    HaltRequest,
    LeaseRenewal,
    SandboxInstanceRequest,
    SandboxManager,
    SandboxRejected,
    SandboxSessionRequest,
)

RESULTS = Path("/results")
PROBE_IMAGE = "huntweave-isolation-probe:p0"
CONTROL_NETWORK = "huntweave-lab-egress-control"
LABEL = "com.huntweave.egress_probe"


class EgressCheck:
    def __init__(self) -> None:
        self.client = docker.from_env(timeout=15)
        self.manager: SandboxManager | None = None
        self.run_id = uuid4()
        self.prefix = f"huntweave-egress-{str(self.run_id)[:12]}"
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
            "leftovers": [],
        }

    # -- reporting ----------------------------------------------------------------------------

    def event(self, action: str, **values: object) -> None:
        record = {"at": datetime.now(UTC).isoformat(), "action": action, **values}
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
            "egress_profile_selected",
            settings.enabled and settings.profile_id == "sandbox-egress-v1",
            profile_id=settings.profile_id,
        )
        manager = open_sandbox_manager(settings)
        self.manager = manager
        profile = manager.profile
        self.report["profile"] = {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "status": profile.status,
            "target_network": profile.target_network,
            "network_internal": profile.network_internal,
            "revocation_seconds": profile.limits.revocation_seconds,
            "gateway_healthcheck": list(profile.gateway.healthcheck),
        }
        self.check(
            "the_profile_has_a_target_network_and_a_readiness_check",
            profile.target_network is not None and bool(profile.gateway.healthcheck),
        )
        network = self.target_network(profile.target_network or "")
        authorized = self.fixture("authorized", network)
        unauthorized = self.fixture("unauthorized", network)
        control = self.fixture("control", CONTROL_NETWORK)
        fixtures = {"authorized": authorized, "unauthorized": unauthorized, "control": control}
        addresses = {
            "authorized": self.address_of(authorized, network),
            "unauthorized": self.address_of(unauthorized, network),
            "control": self.address_of(control, CONTROL_NETWORK),
        }
        self.report["fixtures"] = addresses
        for role, fixture in fixtures.items():
            self.await_listening(fixture, addresses[role])
        self.check(
            "lab_fixtures_are_listening",
            bool(addresses["authorized"] and addresses["unauthorized"] and addresses["control"]),
            addresses=addresses,
        )

        session = manager.open_session(
            SandboxSessionRequest(
                run_id=self.run_id,
                agent_session_id=uuid4(),
                scope_id=uuid4(),
                scope_version=1,
                policy_version=1,
            )
        )
        instance = manager.launch_instance(
            SandboxInstanceRequest(
                session_id=session.session_id,
                authorized=[
                    AuthorizedEndpoint(address=addresses["authorized"], port=7000),
                    AuthorizedEndpoint(address=addresses["authorized"], port=7001),
                ],
            )
        )
        self.check("instance_is_ready", instance.state == "ready")
        tool, gateway = self.containers(instance, "tool", "gateway")

        # 1. The rules existed before any tool *process* could: the daemon's own timestamps are
        #    compared with the moment the manager recorded the policy application.
        applied_at = instance.egress_changes[0].at
        tool_started = _state_of(tool, "StartedAt")
        gateway_started = _state_of(gateway, "StartedAt")
        tool_created = _created_at(tool)
        self.check(
            "the_policy_was_applied_before_the_tool_container_existed",
            _created_at(gateway) <= applied_at <= tool_created,
            gateway_created=str(_created_at(gateway)),
            policy_applied_at=str(applied_at),
            tool_created=str(tool_created),
        )
        self.check(
            "the_policy_was_applied_before_the_tool_process_started",
            gateway_started <= applied_at <= tool_started,
            gateway_started=str(gateway_started),
            policy_applied_at=str(applied_at),
            tool_started=str(tool_started),
        )
        facts = self.client.containers.get(gateway.id).attrs["State"].get("Health") or {}
        self.check(
            "the_gateway_reported_ready_before_the_tool_started",
            facts.get("Status") == "healthy",
            health=facts.get("Status"),
        )
        rules = manager.runtime.read_gateway_policy(gateway.id)
        grants = [line for line in rules.splitlines() if line.endswith("-j ACCEPT")]
        drops = [line for line in rules.splitlines() if line.endswith("-j DROP")]
        self.check(
            "the_gateway_holds_default_deny_plus_exactly_the_authorized_endpoints",
            ":OUTPUT DROP" in rules
            and ":INPUT DROP" in rules
            and ":FORWARD DROP" in rules
            and len(grants) == 4  # two directions for each of the two authorized endpoints
            and any(f"-d {addresses['authorized']}/32" in line for line in grants)
            and all(
                f"-d {address}/32" not in line
                for address in (addresses["unauthorized"], addresses["control"])
                for line in grants
            ),
            accept_rules=len(grants),
            drop_rules=len(drops),
        )
        self.check(
            "the_protected_ranges_are_dropped_explicitly",
            any("169.254.0.0/16" in line and line.endswith("-j DROP") for line in drops)
            and any("127.0.0.0/8" in line and line.endswith("-j DROP") for line in drops),
        )

        # 2. What the authorized scope allows and what it must refuse.
        self.check(
            "the_authorized_endpoint_is_reachable",
            self.reachable(tool, addresses["authorized"], 7000),
        )
        self.check(
            "an_unauthorized_target_is_not_reachable",
            not self.reachable(tool, addresses["unauthorized"], 7000),
        )
        self.check(
            "a_port_outside_the_authorization_is_not_reachable",
            not self.reachable(tool, addresses["authorized"], 7002),
        )
        self.check(
            "the_control_network_is_not_reachable",
            not self.reachable(tool, addresses["control"], 7000),
        )
        self.check(
            "the_metadata_address_is_not_reachable",
            not self.reachable(tool, "169.254.169.254", 80),
        )
        # The host's own address on each bridge the session uses is a management interface, not a
        # target. An isolated bridge's host side is not always published in IPAM, so the address
        # Docker gives it — the first host address of the subnet — is probed either way.
        session_resource = next(item for item in instance.resources if item.role == "session")
        session_network = self.client.networks.get(session_resource.id)
        session_network.reload()
        target_config = network.attrs["IPAM"]["Config"][0]
        session_config = session_network.attrs["IPAM"]["Config"][0]
        bridge_gateways = {
            "target_bridge": str(
                target_config.get("Gateway") or IPv4Network(target_config["Subnet"])[1]
            ),
            "session_bridge": str(
                session_config.get("Gateway") or IPv4Network(session_config["Subnet"])[1]
            ),
        }
        self.report["bridge_gateways"] = bridge_gateways
        reachable = {
            name: self.reachable(tool, address, 7000)
            for name, address in bridge_gateways.items()
        }
        self.check(
            "the_host_side_of_each_bridge_is_not_reachable",
            not any(reachable.values()),
            gateways=bridge_gateways,
            reachable=reachable,
        )
        ipv6 = self.fixture_call(tool, "ipv6")
        self.check(
            "ipv6_is_disabled_in_the_session_namespace",
            ipv6["disabled"] == "1" and ipv6["addresses"] == "" and not ipv6["connected"],
            observed=ipv6,
        )
        privileges = self.fixture_call(tool, "privileges", addresses["authorized"])
        self.check(
            "the_tool_cannot_widen_its_own_boundary",
            privileges["uid"] == 10001 and all(privileges["denied"].values()),
            observed=privileges,
        )

        # 3. Revocation: an established connection ends inside the profile's bound, and the moment
        #    is recorded rather than asserted.
        self.fixture_call(tool, "spawn-stream", addresses["authorized"])
        self.wait_file(tool, "/tmp/stream-ready")
        started = time.monotonic()
        revoked = manager.revoke_egress(instance.instance_id, "scope_revoked")
        self.wait_file(tool, "/tmp/stream-ended")
        elapsed = time.monotonic() - started
        self.check(
            "an_established_connection_is_revoked_within_the_bound",
            elapsed < profile.limits.revocation_seconds,
            elapsed_seconds=round(elapsed, 3),
            bound_seconds=profile.limits.revocation_seconds,
            revoked_at=str(revoked.egress.revoked_at),
            revocation_reason=revoked.egress.revocation_reason,
        )
        self.check(
            "no_new_connection_is_possible_after_the_revocation",
            not self.reachable(tool, addresses["authorized"], 7000),
        )

        # 4. A narrower scope: the endpoint it drops stops working, the one it keeps keeps working.
        shrunk = manager.authorize_egress(
            EgressUpdate(
                instance_id=instance.instance_id,
                authorized=[AuthorizedEndpoint(address=addresses["authorized"], port=7000)],
                reason="scope_shrunk",
            )
        )
        self.check(
            "a_narrower_scope_leaves_only_the_remaining_endpoint",
            shrunk.egress.revoked_at is not None
            and not self.reachable(tool, addresses["authorized"], 7001)
            and self.reachable(tool, addresses["authorized"], 7000),
            changes=[change.reason for change in shrunk.egress_changes],
        )

        # 5. Cancel: descendants that escaped their process group and open connections are gone,
        #    and the stop is confirmed by the execution side.
        observer = self.pid_observer(tool.id)
        self.fixture_call(tool, "double-fork", addresses["authorized"])
        self.wait_file(tool, "/tmp/orphan-ready")
        self.fixture_call(tool, "spawn-stream", addresses["authorized"])
        self.wait_file(tool, "/tmp/stream-ready")
        self.check(
            "descendants_exist_before_the_cancel",
            len(tool.top()["Processes"]) >= 3,
            processes=len(tool.top()["Processes"]),
        )
        halted = manager.halt_instance(
            HaltRequest(instance_id=instance.instance_id, reason="operator_cancelled")
        )
        self.check(
            "the_cancel_confirms_the_stop",
            halted.state == "stopped"
            and halted.stop_confirmed_at is not None
            and halted.halt_reason == "operator_cancelled",
        )
        observer.wait(timeout=10)
        observer.reload()
        tool.reload()
        gateway.reload()
        self.check(
            "cancelling_reclaims_the_pid_namespace_and_the_connections",
            not tool.attrs["State"]["Running"]
            and not gateway.attrs["State"]["Running"]
            and not observer.attrs["State"]["Running"],
            observer_running=observer.attrs["State"]["Running"],
        )

        # 6. Lease expiry: nobody renews, and the manager itself closes the execution.
        second = manager.launch_instance(
            SandboxInstanceRequest(
                session_id=session.session_id,
                authorized=[AuthorizedEndpoint(address=addresses["authorized"], port=7000)],
            )
        )
        second_tool, second_gateway = self.containers(second, "tool", "gateway")
        self.check("the_rebuild_reaches_its_target", self.reachable(second_tool, addresses["authorized"], 7000))
        manager.renew_instance_lease(
            LeaseRenewal(
                instance_id=second.instance_id,
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=2),
            )
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if manager.instances[str(second.instance_id)].state == "stopped":
                break
            time.sleep(0.5)
        expired = manager.instances[str(second.instance_id)]
        self.check(
            "a_lapsed_control_lease_is_closed_by_the_manager",
            expired.state == "stopped" and expired.halt_reason == "control_lease_expired",
            state=expired.state,
            halt_reason=expired.halt_reason,
        )
        second_tool.reload()
        second_gateway.reload()
        self.check(
            "the_expired_execution_is_stopped_and_its_egress_revoked",
            not second_tool.attrs["State"]["Running"]
            and not second_gateway.attrs["State"]["Running"]
            and expired.egress.revoked_at is not None,
        )

        # 7. Revert: stop new executions, revoke, stop, reclaim, audit — and only then is the
        #    deployment allowed to withdraw management.
        reverted = manager.begin_revert()
        self.check(
            "the_revert_sequence_completes_and_allows_withdrawal",
            reverted.state == "complete" and reverted.withdrew and reverted.outstanding == [],
            halted=len(reverted.halted),
            reclaimed=len(reverted.reclaimed),
        )
        self.check(
            "the_revert_archives_what_it_reconciled",
            len(reverted.archived) == 1
            and (Path("/results/evidence") / reverted.archived[0]).exists(),
            archived=reverted.archived,
        )
        self.check(
            "a_revert_leaves_nothing_behind",
            manager.resources(run_id=self.run_id) == []
            and self.labeled_resources() == []
            and manager.audit().unaccounted == [],
        )
        with self.assert_rejected("sandbox_reverting"):
            manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
        repeated = manager.begin_revert()
        self.check(
            "repeating_the_revert_is_a_no_op",
            repeated.state == "complete" and repeated.halted == [],
        )
        self.report["passed"] = True

    # -- lab plumbing -------------------------------------------------------------------------

    class assert_rejected:
        def __init__(self, reason_code: str) -> None:
            self.reason_code = reason_code

        def __enter__(self) -> None:
            return None

        def __exit__(self, kind: object, value: BaseException | None, trace: object) -> bool:
            if value is None:
                raise AssertionError(f"expected a refusal with {self.reason_code}")
            if not isinstance(value, SandboxRejected) or value.reason_code != self.reason_code:
                raise AssertionError(f"expected {self.reason_code}, got {value!r}")
            return True

    def target_network(self, name: str):
        existing = [item for item in self.client.networks.list(filters={"name": f"^{name}$"})]
        if existing:
            raise AssertionError(
                f"the target network {name} already exists: another run is using it"
            )
        network = self.client.networks.create(
            name,
            driver="bridge",
            internal=False,
            labels={LABEL: str(self.run_id)},
            enable_ipv6=False,
        )
        self.event("target_network_created", name=name, id=network.id)
        return network

    def fixture(self, role: str, network: Any):
        container = self.client.containers.create(
            image=PROBE_IMAGE,
            name=f"{self.prefix}-{role}",
            command=["python", "/lab/fixture.py", "target"],
            labels={LABEL: str(self.run_id)},
            detach=True,
            user="10001:10001",
            init=True,
            network=network.name if hasattr(network, "name") else network,
        )
        container.start()
        self.event("fixture_started", role=role, id=container.id)
        return container

    def pid_observer(self, tool_id: str):
        # Created by the probe, not by the product: it is an independent witness that the pid
        # namespace really is gone once the manager stops the tool.
        container = self.client.containers.create(
            image=PROBE_IMAGE,
            name=f"{self.prefix}-pid-observer",
            command=["python", "/lab/fixture.py", "hold"],
            labels={LABEL: str(self.run_id)},
            detach=True,
            user="10001:10001",
            init=True,
            pid_mode=f"container:{tool_id}",
            network_mode="none",
        )
        container.start()
        return container

    def address_of(self, container: Any, network: Any) -> str:
        container.reload()
        name = network.name if hasattr(network, "name") else network
        return str(container.attrs["NetworkSettings"]["Networks"][name]["IPAddress"])

    def await_listening(self, container: Any, address: str) -> None:
        if not address:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.fixture_call(container, "probe", address, 7000)["connected"]:
                return
            time.sleep(0.2)
        raise AssertionError(f"the {container.name} fixture never listened")

    def containers(self, instance: Any, *roles: str) -> list[Any]:
        found = []
        for role in roles:
            resource = next(item for item in instance.resources if item.role == role)
            found.append(self.client.containers.get(resource.id))
        return found

    def session_gateway(self, instance: Any) -> str:
        """The host's address on the session bridge, read from the network the manager created."""
        resource = next(item for item in instance.resources if item.role == "session")
        network = self.client.networks.get(resource.id)
        network.reload()
        config = network.attrs["IPAM"]["Config"]
        return str(config[0].get("Gateway") or "") if config else ""

    def session_gateway(self, instance: Any) -> str:
        """The host's address on the session bridge, read from the network the manager created."""
        resource = next(item for item in instance.resources if item.role == "session")
        network = self.client.networks.get(resource.id)
        network.reload()
        config = network.attrs["IPAM"]["Config"]
        return str(config[0].get("Gateway") or "") if config else ""

    def reachable(self, container: Any, address: str, port: int) -> bool:
        return bool(self.fixture_call(container, "probe", address, port)["connected"])

    def fixture_call(self, container: Any, *args: object) -> dict[str, Any]:
        container.reload()
        result = container.exec_run(
            ["python", "/lab/fixture.py", *[str(item) for item in args]], user="10001:10001"
        )
        output = result.output.decode("utf-8", errors="replace").strip().splitlines()
        if not output:
            return {}
        value: dict[str, Any] = json.loads(output[-1])
        return value

    def wait_file(self, container: Any, filename: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if container.exec_run(["test", "-f", filename]).exit_code == 0:
                return
            time.sleep(0.1)
        raise AssertionError(f"{filename} was never written in {container.name}")

    def labeled_resources(self) -> list[str]:
        selector = {"label": [f"{LABEL_NAMESPACE}.project={PROJECT_NAME}"]}
        names = [f"container:{item.name}" for item in self.client.containers.list(all=True, filters=selector)]
        names += [f"network:{item.name}" for item in self.client.networks.list(filters=selector)]
        names += [f"volume:{item.name}" for item in self.client.volumes.list(filters=selector)]
        return names

    def cleanup(self) -> None:
        """Remove this probe's own lab resources, then report anything that survived.

        `cleaned` is what this probe took away (its fixtures and their bridge); `leftovers` is
        what was still there afterwards, which is a failure rather than bookkeeping.
        """
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
        names = [f"container:{item.name}" for item in self.client.containers.list(all=True, filters=selector)]
        names += [f"network:{item.name}" for item in self.client.networks.list(filters=selector)]
        return names


def _created_at(container: Any) -> datetime:
    container.reload()
    return datetime.fromisoformat(str(container.attrs["Created"]).replace("Z", "+00:00"))


def _state_of(container: Any, name: str) -> datetime:
    container.reload()
    return datetime.fromisoformat(str(container.attrs["State"][name]).replace("Z", "+00:00"))


if __name__ == "__main__":
    check = EgressCheck()
    try:
        check.run()
    except Exception as error:
        check.report["reason_code"] = "sandbox_egress_failed"
        check.report["error_type"] = type(error).__name__
        check.report["error"] = str(error)
        traceback.print_exc()
    finally:
        check.cleanup()
    raise SystemExit(0 if check.report["passed"] else 1)
