"""Build and run the egress-control check on Windows or Linux.

The check drives the product's trusted manager from inside a lab-only container that owns the
Docker socket, exactly like the lifecycle probe. Unlike that one, it needs a network of its own:
the manager discovers the platform's networks from the container it runs in, and the control
bridge this container sits on is what the tool must be unable to reach.
"""

import platform
import uuid

from lab_docker import REPOSITORY, build, command, socket_source, source_facts

PROBE_IMAGE = "huntweave-isolation-probe:p0"
EGRESS_IMAGE = "huntweave-sandbox-egress:p1"
PROFILE = "sandbox-egress-v1"
CONTROL_NETWORK = "huntweave-lab-egress-control"


def main() -> None:
    run_id = uuid.uuid4().hex
    output = REPOSITORY / "runtime" / "sandbox" / run_id
    output.mkdir(parents=True)
    command("docker", "version", "--format", "{{.Server.Version}}")
    build("probe", PROBE_IMAGE)
    build("egress", EGRESS_IMAGE)
    # A bridge the probe itself sits on: it is the control network the sandbox must keep the tool
    # out of, and the manager discovers it from its own container instead of being told about it.
    command("docker", "network", "create", "--driver", "bridge", CONTROL_NETWORK)
    environment = {
        "HUNTWEAVE_HOST_PLATFORM": platform.system().lower(),
        "HUNTWEAVE_HOST_OS": platform.platform(),
        # Management is enabled here and only here: this is a lab check, not the product's default
        # deployment, and the product's own readiness gates are untouched by it.
        "HUNTWEAVE_SANDBOX_MANAGEMENT": "enabled",
        "HUNTWEAVE_SANDBOX_PROFILE": PROFILE,
        "HUNTWEAVE_SANDBOX_PROFILE_DIR": "/opt/huntweave/profiles",
        "HUNTWEAVE_RUNNER_STATE_DIR": "/results/state",
        "HUNTWEAVE_EVIDENCE_DIR": "/results/evidence",
        **source_facts(),
    }
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"huntweave-sandbox-egress-{run_id}",
        "--network",
        CONTROL_NETWORK,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "128",
        "--memory",
        "256m",
        "--tmpfs",
        "/tmp:size=16m,mode=1777",
        "--mount",
        f"type=bind,source={socket_source()},target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={output},target=/results",
    ]
    for key, value in environment.items():
        args.extend(["--env", f"{key}={value}"])
    try:
        command(*args, EGRESS_IMAGE)
    finally:
        command("docker", "network", "rm", CONTROL_NETWORK)
        print(f"Validation report: {output / 'report.json'}")


if __name__ == "__main__":
    main()
