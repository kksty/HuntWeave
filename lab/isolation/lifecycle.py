"""Fixed sandbox lifecycle check: real containers, no target network, no target traffic.

This probe drives the product's own trusted manager (``huntweave.execution.sandbox``) against the
local Docker daemon and checks the facts P1 slice #16 has to demonstrate: one fixed profile, a
normal-user tool container inside the gateway's network namespace, unique resource labels, a
bounded log read, an archived evidence file, a confirmed stop, and a reclaim that leaves nothing
behind. The lifecycle is exercised through the manager; the *facts* are read back with the Docker
SDK, so the component under test is never the only witness of its own result.

The probe never creates a target bridge and never opens a connection to any address: the only
network it builds is the internal session network, and the checks assert that the tool container's
sole non-loopback address lies inside it.
"""

import json
import os
import traceback
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import docker

from huntweave.config import SandboxSettings
from huntweave.execution.dockerruntime import open_sandbox_manager
from huntweave.execution.sandbox import (
    LABEL_NAMESPACE,
    PROJECT_NAME,
    InstanceRecord,
    SandboxInstanceRequest,
    SandboxManager,
    SandboxSessionRequest,
)

RESULTS = Path("/results")


class LifecycleCheck:
    def __init__(self) -> None:
        self.client = docker.from_env(timeout=15)
        self.manager: SandboxManager | None = None
        # The probe's own run identity, so its cleanup can never touch another Run's resources.
        self.run_id = uuid4()
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

    # -- the lifecycle ------------------------------------------------------------------------

    def run(self) -> None:
        settings = SandboxSettings.from_env()
        self.check(
            "management_is_enabled_by_deployment_only",
            settings.enabled and settings.profile_id == "sandbox-lifecycle-v1",
            profile_id=settings.profile_id,
        )
        manager = open_sandbox_manager(settings)
        self.manager = manager
        profile = manager.profile
        self.report["profile"] = {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "status": profile.status,
            "network_internal": profile.network_internal,
            "tool_user": profile.tool.user,
            "gateway_capabilities": list(profile.gateway.capabilities_add),
            "tool_capabilities": list(profile.tool.capabilities_add),
            "images": {"gateway": profile.gateway.image, "tool": profile.tool.image},
        }
        self.check(
            "profile_keeps_the_tool_unprivileged",
            profile.network_internal
            and profile.tool.capabilities_add == ()
            and profile.tool.user.split(":")[0] != "0"
            and profile.limits.read_only_rootfs
            and profile.limits.capabilities_drop == ("ALL",),
        )
        observation = manager.observe()
        self.check("runtime_is_reachable", observation.available, reason_code=observation.reason_code)

        run_id, agent_session_id = self.run_id, uuid4()
        session = manager.open_session(
            SandboxSessionRequest(
                run_id=run_id,
                agent_session_id=agent_session_id,
                scope_id=uuid4(),
                scope_version=1,
                policy_version=1,
            )
        )
        repeated = manager.open_session(
            SandboxSessionRequest(
                run_id=run_id,
                agent_session_id=agent_session_id,
                scope_id=session.scope_id,
                scope_version=session.scope_version,
                policy_version=session.policy_version,
            )
        )
        self.check("session_identity_is_stable", session.session_id == repeated.session_id)

        instance = manager.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
        self.check("instance_is_ready", instance.state == "ready")
        self.check(
            "environment_manifest_records_what_ran",
            set(instance.environment.image_digests) == {"gateway", "tool"}
            and all(value for value in instance.environment.image_digests.values())
            and instance.environment.profile_version == profile.profile_version
            and bool(instance.environment.engine)
            and bool(instance.environment.architecture),
            engine=instance.environment.engine,
            architecture=instance.environment.architecture,
            image_digests=instance.environment.image_digests,
        )

        managed = manager.resources(session_id=session.session_id)
        self.check(
            "round_owns_a_volume_a_network_and_two_containers",
            sorted(item.kind for item in managed) == ["container", "container", "network", "volume"],
            resources=[{"kind": item.kind, "role": item.role, "name": item.name} for item in managed],
        )
        self.check(
            "every_resource_carries_the_project_labels",
            all(
                item.labels.get(f"{LABEL_NAMESPACE}.project") == PROJECT_NAME
                and item.labels.get(f"{LABEL_NAMESPACE}.run_id") == str(run_id)
                and item.labels.get(f"{LABEL_NAMESPACE}.session_id") == str(session.session_id)
                and item.labels.get(f"{LABEL_NAMESPACE}.instance_id") == str(instance.instance_id)
                and item.labels.get(f"{LABEL_NAMESPACE}.profile") == profile.profile_id
                for item in managed
            ),
        )
        self.check(
            "resource_names_are_derived_from_the_identities",
            all(
                item.name.startswith(
                    f"huntweave-{str(run_id)[:8]}-{str(session.session_id)[:8]}-"
                    f"{str(instance.instance_id)[:8]}"
                )
                for item in managed
            ),
            names=sorted(item.name for item in managed),
        )

        tool_resource = self.resource(instance, "tool")
        gateway_resource = self.resource(instance, "gateway")
        volume_resource = self.resource(instance, "workspace")
        network_resource = self.resource(instance, "session")
        tool = self.client.containers.get(tool_resource.id)
        gateway = self.client.containers.get(gateway_resource.id)
        network = self.client.networks.get(network_resource.id)
        network.reload()
        tool_host = tool.attrs["HostConfig"]
        gateway_host = gateway.attrs["HostConfig"]

        self.check(
            "tool_runs_as_a_normal_user_in_a_read_only_filesystem",
            tool.attrs["Config"]["User"] == "10001:10001"
            and tool_host["CapDrop"] == ["ALL"]
            and "no-new-privileges:true" in gateway_host["SecurityOpt"]
            and "no-new-privileges:true" in tool_host["SecurityOpt"]
            and tool_host["ReadonlyRootfs"] is True,
            user=tool.attrs["Config"]["User"],
        )
        self.check(
            "tool_shares_the_gateway_network_namespace",
            tool_host["NetworkMode"] == f"container:{gateway.id}" and not tool_host.get("PidMode"),
            network_mode=tool_host["NetworkMode"],
        )
        self.check(
            "tool_mounts_only_its_private_workspace",
            [mount["Name"] for mount in tool.attrs["Mounts"]] == [volume_resource.id]
            and all(mount["Type"] == "volume" for mount in tool.attrs["Mounts"]),
            mounts=[mount["Destination"] for mount in tool.attrs["Mounts"]],
        )
        self.check(
            "no_container_sees_a_management_socket",
            all(
                "docker.sock" not in str(mount.get("Source", ""))
                for container in (tool, gateway)
                for mount in container.attrs["Mounts"]
            ),
        )
        self.check(
            "gateway_is_the_only_privileged_part",
            gateway_host.get("CapAdd") == ["NET_ADMIN"]
            and gateway_host["CapDrop"] == ["ALL"]
            and gateway_host["ReadonlyRootfs"] is True,
        )
        self.check(
            "session_network_is_internal_and_isolated",
            network.attrs["Internal"] is True
            and network.attrs["Options"].get("com.docker.network.bridge.gateway_mode_ipv4")
            == "isolated",
        )
        self.check(
            "only_one_network_exists_for_this_round",
            [item.name for item in manager.resources(run_id=run_id) if item.kind == "network"]
            == [network_resource.name],
        )

        addresses = self.addresses(tool)
        subnet = IPv4Network(network.attrs["IPAM"]["Config"][0]["Subnet"])
        off_session = {
            name: values for name, values in addresses.items() if name != "lo"
        }
        self.check(
            "the_tool_has_no_address_outside_the_session_network",
            "lo" in addresses
            and bool(off_session)
            and all(
                IPv4Address(address) in subnet
                for values in off_session.values()
                for address in values
            ),
            addresses={name: list(values) for name, values in addresses.items()},
            session_subnet=str(subnet),
        )

        rules = gateway.exec_run(["iptables-save", "-c"], user="0:0").output.decode(
            "utf-8", errors="replace"
        )
        self.check(
            "gateway_installs_default_deny_rules",
            ":INPUT DROP" in rules
            and ":OUTPUT DROP" in rules
            and ":FORWARD DROP" in rules
            and " -j ACCEPT" not in rules,
            forwarded_lines=len(rules.splitlines()),
            note="#16 installs no allow rules; rule-before-process ordering is #18's acceptance",
        )
        self.check(
            "tool_processes_are_visible_through_the_manager",
            len(manager.processes(instance.instance_id)) >= 1,
            processes=manager.processes(instance.instance_id),
        )
        logs = manager.logs(instance.instance_id, "tool")
        self.check(
            "log_read_is_bounded",
            len(logs.text.encode()) <= profile.limits.log_tail_bytes,
            observed_bytes=len(logs.text.encode()),
            bound_bytes=profile.limits.log_tail_bytes,
            note="these fixed images print little; the cut branch is checked in the unit checks",
        )
        evidence = manager.archive_evidence(
            run_id=run_id,
            instance_id=instance.instance_id,
            name="lifecycle.json",
            payload=json.dumps({"run_id": str(run_id), "instance": str(instance.instance_id)}).encode(),
        )
        archived = settings.evidence_dir / evidence.relative_path
        self.check(
            "evidence_is_archived_for_this_instance",
            archived.exists() and archived.stat().st_size == evidence.size_bytes,
            relative_path=evidence.relative_path,
            size_bytes=evidence.size_bytes,
            sha256=evidence.sha256,
        )

        stopped = manager.stop_instance(instance.instance_id)
        self.check(
            "stop_is_confirmed_by_the_runtime",
            stopped.state == "stopped" and stopped.stop_confirmed_at is not None,
            stop_confirmed_at=str(stopped.stop_confirmed_at),
        )
        tool.reload()
        gateway.reload()
        self.check(
            "no_container_is_running_after_the_stop",
            tool.attrs["State"]["Running"] is False and gateway.attrs["State"]["Running"] is False,
        )

        reclamation = manager.reclaim_instance(instance.instance_id)
        self.check(
            "reclaim_removes_every_owned_resource",
            reclamation.complete
            and sorted(item.kind for item in reclamation.removed)
            == ["container", "container", "network", "volume"],
            removed=sorted(item.kind for item in reclamation.removed),
            failed=reclamation.failed,
        )
        self.check(
            "the_round_leaves_nothing_behind",
            manager.resources(run_id=run_id) == []
            and self.labeled_resources(run_id) == []
            and manager.audit().unaccounted == [],
        )

        # ADR-0014: the ledger outlives the process, the instance keeps its identity, and a
        # rebuild is a new instance rather than a reuse of the old one.
        reopened = open_sandbox_manager(settings)
        self.manager = reopened
        kept = reopened.instances[str(instance.instance_id)]
        self.check(
            "ledger_survives_a_new_manager",
            kept.state == "reclaimed"
            and kept.environment == instance.environment
            and reopened.sessions[str(session.session_id)].run_id == run_id,
            state=kept.state,
        )
        rebuilt = reopened.launch_instance(SandboxInstanceRequest(session_id=session.session_id))
        self.check(
            "a_rebuild_is_a_new_instance_identity",
            rebuilt.instance_id != instance.instance_id and rebuilt.state == "ready",
            previous=str(instance.instance_id),
            rebuilt=str(rebuilt.instance_id),
        )
        self.check(
            "the_old_instance_is_still_recorded",
            str(instance.instance_id) in reopened.instances
            and reopened.instances[str(instance.instance_id)].reclaimed_at is not None,
        )
        self.check(
            "the_second_round_reclaims_completely",
            reopened.reclaim_session(session.session_id).complete
            and reopened.resources(run_id=run_id) == [],
        )
        self.report["passed"] = True

    # -- helpers ------------------------------------------------------------------------------

    def resource(self, instance: InstanceRecord, role: str):
        for item in instance.resources:
            if item.role == role:
                return item
        raise AssertionError(f"the instance has no {role} resource")

    def addresses(self, container) -> dict[str, list[str]]:
        output = container.exec_run(["ip", "-4", "-o", "addr", "show"], user="10001:10001")
        found: dict[str, list[str]] = {}
        for line in output.output.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2] != "inet":
                continue
            found.setdefault(parts[1], []).append(parts[3].split("/")[0])
        return found

    def labeled_resources(self, run_id: UUID) -> list[str]:
        selector = {
            "label": [
                f"{LABEL_NAMESPACE}.project={PROJECT_NAME}",
                f"{LABEL_NAMESPACE}.run_id={run_id}",
            ]
        }
        names = [f"container:{item.name}" for item in self.client.containers.list(all=True, filters=selector)]
        names += [f"network:{item.name}" for item in self.client.networks.list(filters=selector)]
        names += [f"volume:{item.name}" for item in self.client.volumes.list(filters=selector)]
        return names

    def cleanup(self) -> None:
        """Last resort: this probe's resources must not outlive it, whatever failed.

        Scoped to this probe's own Run identity, so a concurrent Run's — or another probe's —
        resources are neither removed nor mistaken for this check's leftovers.
        """
        selector = {
            "label": [
                f"{LABEL_NAMESPACE}.project={PROJECT_NAME}",
                f"{LABEL_NAMESPACE}.run_id={self.run_id}",
            ]
        }
        prefix = f"huntweave-{str(self.run_id)[:8]}-"
        leftovers: list[str] = []
        failures: list[str] = []
        for container in self.client.containers.list(all=True, filters=selector):
            leftovers.append(f"container:{container.name}")
            if not container.name.startswith(prefix):
                failures.append("ownership_mismatch")
                continue
            try:
                container.remove(force=True)
            except Exception as error:
                failures.append(type(error).__name__)
        for network in self.client.networks.list(filters=selector):
            leftovers.append(f"network:{network.name}")
            if not network.name.startswith(prefix):
                failures.append("ownership_mismatch")
                continue
            try:
                network.remove()
            except Exception as error:
                failures.append(type(error).__name__)
        for volume in self.client.volumes.list(filters=selector):
            leftovers.append(f"volume:{volume.name}")
            if not volume.name.startswith(prefix):
                failures.append("ownership_mismatch")
                continue
            try:
                volume.remove(force=True)
            except Exception as error:
                failures.append(type(error).__name__)
        self.report["leftovers"] = leftovers
        self.report["cleanup_failures"] = failures
        if leftovers or failures:
            self.report["passed"] = False
        if self.manager is not None:
            self.manager.close()
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "report.json").write_text(json.dumps(self.report, indent=2, default=str))


if __name__ == "__main__":
    check = LifecycleCheck()
    try:
        check.run()
    except Exception as error:
        check.report["reason_code"] = "sandbox_lifecycle_failed"
        check.report["error_type"] = type(error).__name__
        check.report["error"] = str(error)
        traceback.print_exc()
    finally:
        check.cleanup()
    raise SystemExit(0 if check.report["passed"] else 1)
