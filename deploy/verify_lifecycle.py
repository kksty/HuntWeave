"""Build and run the fixed sandbox lifecycle check on Windows or Linux.

The check runs inside a lab-only container that owns the Docker socket, exactly like the isolation
probe, because the product's trusted manager is what has to talk to the daemon. This entry point
only prepares the two lab images and passes the fixed profile and the sandbox directories in; the
check itself lives in ``lab/isolation/lifecycle.py``.
"""

import platform
import uuid

from lab_docker import REPOSITORY, build, command, socket_source, source_facts

PROBE_IMAGE = "huntweave-isolation-probe:p0"
LIFECYCLE_IMAGE = "huntweave-sandbox-lifecycle:p1"
PROFILE = "sandbox-lifecycle-v1"


def main() -> None:
    run_id = uuid.uuid4().hex
    output = REPOSITORY / "runtime" / "sandbox" / run_id
    output.mkdir(parents=True)
    command("docker", "version", "--format", "{{.Server.Version}}")
    build("probe", PROBE_IMAGE)
    build("sandbox", LIFECYCLE_IMAGE)
    environment = {
        "HUNTWEAVE_HOST_PLATFORM": platform.system().lower(),
        "HUNTWEAVE_HOST_OS": platform.platform(),
        # Management is enabled here and only here: this is a lab check, not the product's
        # default deployment, and the product's own readiness gates are untouched by it.
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
        f"huntweave-sandbox-lifecycle-{run_id}",
        "--network",
        "none",
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
        command(*args, LIFECYCLE_IMAGE)
    finally:
        print(f"Validation report: {output / 'report.json'}")


if __name__ == "__main__":
    main()
