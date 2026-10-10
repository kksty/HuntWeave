"""Build and run the selective-retention check on Windows or Linux.

The check drives the product's Runner — its HTTP surface, its real executor, the trusted manager and
the retention ledger — from inside a lab-only container that owns the Docker socket. It needs a
bridge of its own because the manager discovers the platform's networks from the container it runs
in, and that control bridge is what the tool must be unable to reach.
"""

import platform
import uuid

from lab_docker import REPOSITORY, build, command, run_probe, source_facts

PROBE_IMAGE = "huntweave-isolation-probe:p0"
RETENTION_IMAGE = "huntweave-sandbox-retention:p1"
PROFILE = "sandbox-egress-v1"
CONTROL_NETWORK = "huntweave-lab-retention-control"


def main() -> None:
    run_id = uuid.uuid4().hex
    output = REPOSITORY / "runtime" / "sandbox" / run_id
    output.mkdir(parents=True)
    command("docker", "version", "--format", "{{.Server.Version}}")
    build("probe", PROBE_IMAGE)
    build("retention", RETENTION_IMAGE)
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
            image=RETENTION_IMAGE,
            run_id=f"sandbox-retention-{run_id}",
            output=output,
            environment=environment,
            network=CONTROL_NETWORK,
        )
    finally:
        command("docker", "network", "rm", CONTROL_NETWORK)


if __name__ == "__main__":
    main()
