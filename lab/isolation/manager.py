"""Trusted, lab-only Runner manager: owns Docker; probes never receive its socket.

All resources use one fresh run label. Cleanup checks the label and prefix before removal.
No external target, arbitrary mount or Shell payload is accepted from callers.
"""

import hashlib
import json
import os
import re
import time
import traceback
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from uuid import uuid4

import docker
from huntweave.execution.network_policy import Endpoint, NetworkPolicy, ScopeDenied

LABEL = "com.huntweave.isolation_run"
PROFILES = {
    "windows": "windows11-wsl2-docker-desktop-gateway-v1",
    "linux": "linux-docker-engine-gateway-v1",
}


class Lab:
    def __init__(self):
        self.client = docker.from_env(timeout=15)
        self.run_id = uuid4().hex
        self.prefix = f"huntweave-lab-{self.run_id[:12]}"
        self.labels = {LABEL: self.run_id}
        self.report = {
            "profile": PROFILES[os.environ["HUNTWEAVE_HOST_PLATFORM"]],
            "run_id": self.run_id,
            "passed": False,
            "started_at": datetime.now(UTC).isoformat(),
            "events": [],
            "checks": [],
            "source_revision": os.environ.get("HUNTWEAVE_SOURCE_REVISION", "unknown"),
            "source_tree_dirty": os.environ["HUNTWEAVE_SOURCE_TREE_DIRTY"] == "true",
        }
        self.probe_image = self.client.images.get("huntweave-isolation-probe:p0").id
        self.report["probe_image_id"] = self.probe_image
        self.report["manager_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def event(self, action, **values):
        record = {"at": datetime.now(UTC).isoformat(), "action": action, **values}
        self.report["events"].append(record)
        print(json.dumps(record), flush=True)

    def check(self, name, success, **values):
        self.report["checks"].append({"name": name, "passed": bool(success), **values})
        self.event("check", name=name, passed=bool(success), **values)
        if not success:
            raise AssertionError(name)

    def network(self, role, *, internal=True):
        self.event("network_create_intent", role=role)
        network = self.client.networks.create(
            f"{self.prefix}-{role}",
            driver="bridge",
            internal=internal,
            labels=self.labels,
            options={
                "com.docker.network.bridge.gateway_mode_ipv4": "isolated" if internal else "nat"
            },
            enable_ipv6=False,
        )
        network.reload()
        self.event("network_created", role=role, id=network.id)
        self.check(
            f"{role}_isolated",
            network.attrs["Internal"] == internal
            and network.attrs["Options"].get("com.docker.network.bridge.gateway_mode_ipv4")
            == ("isolated" if internal else "nat"),
        )
        return network

    def container(
        self,
        role,
        command,
        *,
        network=None,
        network_mode=None,
        pid_mode=None,
        administrator=False,
        extra_hosts=None,
    ):
        self.event("container_create_intent", role=role, command=command)
        options = {
            "image": self.probe_image,
            "name": f"{self.prefix}-{role}",
            "command": command,
            "labels": self.labels,
            "detach": True,
            "user": "0:0" if administrator else "10001:10001",
            "init": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 64,
            "mem_limit": "96m",
            "nano_cpus": 500_000_000,
            "tmpfs": {"/tmp": "size=8m,mode=1777"},
        }
        if network:
            options["network"] = network.name
            options["dns"] = ["127.0.0.1"]
        else:
            options["network_mode"] = network_mode or "none"
        if pid_mode:
            options["pid_mode"] = pid_mode
        if extra_hosts:
            options["extra_hosts"] = extra_hosts
        if administrator:
            options["cap_add"] = ["NET_ADMIN"]
        if not network_mode:
            options["sysctls"] = {
                "net.ipv6.conf.all.disable_ipv6": "1",
                "net.ipv6.conf.default.disable_ipv6": "1",
            }
            if role == "gateway":
                options["sysctls"]["net.ipv4.ip_forward"] = "0"
        container = self.client.containers.create(**options)
        container.start()
        self.event("container_started", role=role, id=container.id)
        return container

    def execute(self, container, command, *, user="10001:10001", parse=False):
        self.event("exec_intent", container=container.name, command=command, user=user)
        result = container.exec_run(command, user=user)
        output = result.output.decode("utf-8", errors="replace")
        self.event(
            "exec_completed",
            container=container.name,
            exit_code=result.exit_code,
            output=output,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Fixed lab command failed in {container.name}")
        return json.loads(output) if parse else output

    def fixture(self, container, *args):
        return self.execute(container, ["python", "/lab/fixture.py", *map(str, args)], parse=True)

    def wait_file(self, container, filename):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = container.exec_run(["test", "-f", filename])
            if result.exit_code == 0:
                return
            time.sleep(0.1)
        raise TimeoutError(f"Fixed fixture readiness timed out: {filename}")

    def apply_policy(self, gateway, policy):
        values = {
            "permissions": [
                {"address": str(item.address), "port": item.port} for item in policy.permissions
            ],
            "protected": [str(item) for item in policy.protected],
        }
        self.execute(
            gateway,
            ["python", "/lab/network.py", "policy", json.dumps(values)],
            user="0:0",
        )

    def dropped_output(self, gateway):
        output = self.execute(gateway, ["iptables-save", "-c"], user="0:0")
        direct = int(re.search(r":OUTPUT DROP \[(\d+):", output)[1])
        explicit = sum(
            int(value) for value in re.findall(r"\[(\d+):\d+\] -A OUTPUT .* -j DROP", output)
        )
        return direct + explicit

    def run(self):
        info = self.client.info()
        version = self.client.version()
        self.report["environment"] = {
            "host_os": os.environ.get("HUNTWEAVE_HOST_OS"),
            "networking_mode": os.environ.get("HUNTWEAVE_WSL_NETWORKING"),
            "wsl_version": os.environ.get("HUNTWEAVE_WSL_VERSION"),
            "docker_platform": version.get("Platform"),
            "engine": info["ServerVersion"],
            "kernel": info["KernelVersion"],
            "cgroup_version": info.get("CgroupVersion"),
        }
        host = os.environ["HUNTWEAVE_HOST_PLATFORM"]
        if host == "windows":
            self.check(
                "windows_wsl2_nat_profile",
                os.environ["HUNTWEAVE_WSL_NETWORKING"] == "nat"
                and "microsoft-standard-WSL2" in info["KernelVersion"]
                and "Docker Desktop" in info["OperatingSystem"],
            )
        else:
            self.check(
                "linux_engine_profile",
                info["OSType"] == "linux" and "Docker Desktop" not in info["OperatingSystem"],
            )
        policy_path = Path("/opt/huntweave/src/huntweave/execution/network_policy.py")
        self.report["policy_sha256"] = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        session = self.network("session")
        targets = self.network("targets", internal=False)
        control = self.network("control")
        target_a = self.container(
            "authorized", ["python", "/lab/fixture.py", "target"], network=targets
        )
        target_b = self.container(
            "unauthorized", ["python", "/lab/fixture.py", "target"], network=targets
        )
        control_fixture = self.container(
            "control", ["python", "/lab/fixture.py", "target"], network=control
        )
        gateway = self.container(
            "gateway",
            ["python", "/lab/network.py", "gateway"],
            network=session,
            administrator=True,
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        targets.connect(gateway)
        self.event("network_connected", network=targets.name, container=gateway.name)
        self.wait_file(gateway, "/tmp/gateway-ready")
        routes = self.execute(gateway, ["ip", "-4", "route", "show"], user="0:0")
        self.check("egress_default_route_present", "default via " in routes)
        worker = self.container(
            "worker",
            ["python", "/lab/fixture.py", "hold"],
            network_mode=f"container:{gateway.id}",
        )
        worker.reload()
        self.check(
            "worker_network_boundary",
            worker.attrs["HostConfig"]["NetworkMode"] == f"container:{gateway.id}",
        )
        host_ip = self.fixture(worker, "host-ip")["host_ip"]
        protected = [
            IPv4Network(control.attrs["IPAM"]["Config"][0]["Subnet"]),
            IPv4Network(session.attrs["IPAM"]["Config"][0]["Subnet"]),
            IPv4Network(host_ip + "/32"),
            IPv4Network(self.ip(gateway, targets) + "/32"),
            IPv4Network(targets.attrs["IPAM"]["Config"][0]["Gateway"] + "/32"),
        ]
        host_addresses = [
            IPv4Address(value) for value in os.environ["HUNTWEAVE_HOST_ADDRESSES"].split(",")
        ]
        protected.extend(IPv4Network(str(value) + "/32") for value in host_addresses)
        # Also protect the actual platform's networks without connecting the lab to them.
        platform_targets = []
        for network in self.client.networks.list(
            filters={"label": "com.docker.compose.project=huntweave"}
        ):
            network.reload()
            for entry in network.attrs["IPAM"]["Config"]:
                if entry.get("Subnet") and ":" not in entry["Subnet"]:
                    protected.append(IPv4Network(entry["Subnet"]))
            for identifier, record in network.attrs.get("Containers", {}).items():
                if record.get("IPv4Address"):
                    platform_container = self.client.containers.get(identifier)
                    role = platform_container.labels.get("com.docker.compose.service")
                    port = {"app": 8000, "runner": 8001, "postgres": 5432}.get(role)
                    if port:
                        platform_targets.append((record["IPv4Address"].split("/")[0], port))
        a_ip, b_ip, control_ip = (
            self.ip(target_a, targets),
            self.ip(target_b, targets),
            self.ip(control_fixture, control),
        )
        for target in (target_a, target_b, control_fixture):
            for port in (7000, 7001):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if self.fixture(target, "probe", "127.0.0.1", port)["connected"]:
                        break
                else:
                    raise AssertionError("Target fixture is not listening")
        deny = NetworkPolicy(protected=tuple(protected))
        self.apply_policy(gateway, deny)
        for address in (a_ip, b_ip, control_ip):
            self.check(
                "default_deny_" + address,
                not self.fixture(worker, "probe", address, 7000)["connected"],
            )
        allow = NetworkPolicy((Endpoint(IPv4Address(a_ip), 7000),), tuple(protected))
        self.apply_policy(gateway, allow)
        connected = self.fixture(worker, "probe", a_ip, 7000)["connected"]
        if not connected:
            self.execute(gateway, ["ip", "-4", "route", "show"], user="0:0")
            self.execute(gateway, ["cat", "/proc/sys/net/ipv4/ip_forward"], user="0:0")
            self.execute(gateway, ["iptables-save", "-c"], user="0:0")
            self.event("target_logs", output=target_a.logs().decode())
        self.check("authorized_exact_tcp_endpoint", connected)
        self.check(
            "unauthorized_target_blocked",
            not self.fixture(worker, "probe", b_ip, 7000)["connected"],
        )
        self.check(
            "wrong_port_blocked",
            not self.fixture(worker, "probe", a_ip, 7001)["connected"],
        )
        before = self.dropped_output(gateway)
        self.fixture(worker, "udp", a_ip)
        self.check("udp_blocked_at_gateway", self.dropped_output(gateway) > before)
        before = self.dropped_output(gateway)
        self.fixture(worker, "dns")
        after = self.dropped_output(gateway)
        self.check(
            "embedded_dns_cannot_bypass_gateway",
            after > before,
        )
        for address, port in {
            (control_ip, 7000),
            (host_ip, 7000),
            ("169.254.169.254", 7000),
            ("203.0.113.1", 7000),
            *platform_targets,
            *((str(value), 7000) for value in host_addresses),
        }:
            before = self.dropped_output(gateway)
            connected = self.fixture(worker, "tcp-open", address, port)["connected"]
            dropped = self.dropped_output(gateway) > before
            self.check(
                f"protected_destination_{address}_{port}",
                not connected and dropped,
                filter_drop_observed=dropped,
            )
        try:
            NetworkPolicy(
                (Endpoint(IPv4Address("169.254.169.254"), 7000),),
                tuple(protected),
            )
        except ScopeDenied:
            self.check("metadata_cannot_be_added_to_scope", True)
        else:
            self.check("metadata_cannot_be_added_to_scope", False)
        privileges = self.fixture(worker, "privileges", self.ip(gateway, session))
        self.check(
            "unprivileged_tool_cannot_change_boundary",
            privileges["uid"] == 10001 and all(privileges["denied"].values()),
        )
        ipv6 = self.fixture(worker, "ipv6")
        self.check(
            "ipv6_disabled",
            ipv6["addresses"] == "" and ipv6["disabled"] == "1" and not ipv6["connected"],
        )
        self.fixture(worker, "spawn-stream", a_ip)
        self.wait_file(worker, "/tmp/stream-ready")
        time.sleep(0.2)
        started = time.monotonic()
        self.apply_policy(gateway, deny)
        self.wait_file(worker, "/tmp/stream-ended")
        self.check(
            "established_connection_revoked",
            time.monotonic() - started < 2,
            max_observed_seconds=round(time.monotonic() - started, 3),
        )
        self.check(
            "new_connection_after_revocation_blocked",
            not self.fixture(worker, "probe", a_ip, 7000)["connected"],
        )
        self.apply_policy(gateway, allow)
        self.fixture(worker, "double-fork", a_ip)
        self.wait_file(worker, "/tmp/orphan-ready")
        orphan = self.fixture(worker, "orphan-info")
        self.check(
            "orphan_reparented_to_container_init", orphan["ppid"] == 1 and orphan["uid"] == 10001
        )
        observer = self.container(
            "pid-observer",
            ["python", "/lab/fixture.py", "hold"],
            pid_mode=f"container:{worker.id}",
        )
        self.check("orphan_children_exist_before_cancel", len(worker.top()["Processes"]) >= 3)
        self.apply_policy(gateway, deny)
        self.event("stop_intent", container=worker.name, timeout_seconds=2)
        worker.stop(timeout=2)
        self.event("stop_completed", container=worker.name)
        worker.reload()
        observer.wait(timeout=5)
        observer.reload()
        targets.disconnect(gateway, force=True)
        self.event("network_disconnected", network=targets.name, container=gateway.name)
        targets.reload()
        self.check(
            "cancel_reaps_pid_namespace_and_endpoint",
            not worker.attrs["State"]["Running"]
            and not observer.attrs["State"]["Running"]
            and gateway.id not in targets.attrs.get("Containers", {}),
        )
        self.report["gateway_packages"] = self.execute(
            gateway, ["dpkg-query", "-W", "iptables", "iproute2"], user="0:0"
        )
        self.report["firewall_backend"] = self.execute(
            gateway, ["iptables", "--version"], user="0:0"
        ).strip()
        self.report["passed"] = True

    @staticmethod
    def ip(container, network):
        container.reload()
        return container.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]

    def cleanup(self):
        failed = []
        for container in self.client.containers.list(
            all=True, filters={"label": f"{LABEL}={self.run_id}"}
        ):
            if container.labels.get(LABEL) != self.run_id or not container.name.startswith(
                self.prefix + "-"
            ):
                failed.append("ownership_mismatch")
                continue
            try:
                container.remove(force=True)
                self.event("container_removed", id=container.id, name=container.name)
            except Exception as error:
                failed.append(type(error).__name__)
        for network in self.client.networks.list(filters={"label": f"{LABEL}={self.run_id}"}):
            network.reload()
            if network.attrs.get("Labels", {}).get(
                LABEL
            ) != self.run_id or not network.name.startswith(self.prefix + "-"):
                failed.append("ownership_mismatch")
                continue
            try:
                network.remove()
                self.event("network_removed", id=network.id, name=network.name)
            except Exception as error:
                failed.append(type(error).__name__)
        self.report["cleanup_errors"] = failed
        if failed:
            self.report["passed"] = False
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        Path("/results/report.json").write_text(json.dumps(self.report, indent=2))


if __name__ == "__main__":
    lab = Lab()
    try:
        lab.run()
    except Exception as error:
        lab.report["reason_code"] = (
            "environment_unsupported"
            if isinstance(error, docker.errors.APIError)
            else "isolation_probe_failed"
        )
        lab.report["error_type"] = type(error).__name__
        lab.report["error"] = str(error)
        traceback.print_exc()
    finally:
        lab.cleanup()
    raise SystemExit(0 if lab.report["passed"] else 1)
