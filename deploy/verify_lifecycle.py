"""Build and run the fixed sandbox lifecycle check on Windows or Linux.

The check runs inside a lab-only container that owns the Docker socket, exactly like the isolation
probe, because the product's trusted manager is what has to talk to the daemon. This entry point
only prepares the two lab images and passes the fixed profile and the sandbox directories in; the
check itself lives in ``lab/isolation/lifecycle.py``.
"""

import platform
import uuid

from lab_docker import REPOSITORY, build, command, run_probe, source_facts

PROBE_IMAGE = "huntweave-isolation-probe:p0"
LIFECYCLE_IMAGE = "huntweave-sandbox-lifecycle:p1"
PROFILE = "sandbox-lifecycle-v1"
CONTROL_NETWORK = "huntweave-lab-lifecycle-control"


def main() -> None:
    run_id = uuid.uuid4().hex
    output = REPOSITORY / "runtime" / "sandbox" / run_id
    output.mkdir(parents=True)
    command("docker", "version", "--format", "{{.Server.Version}}")
    build("probe", PROBE_IMAGE)
    build("sandbox", LIFECYCLE_IMAGE)
    # The probe needs a network of its own: the manager discovers the platform's networks from the
    # container it runs in, and refuses a launch when it cannot identify them. This bridge is the
    # probe's control network, not a target network — the lifecycle profile carries no target.
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
    try:
        run_probe(
            image=LIFECYCLE_IMAGE,
            run_id=f"sandbox-lifecycle-{run_id}",
            output=output,
            environment=environment,
            network=CONTROL_NETWORK,
        )
    finally:
        command("docker", "network", "rm", CONTROL_NETWORK)


if __name__ == "__main__":
    main()
