"""Shared entry-point helpers for the container-based lab probes.

Both probes build a lab-only image and run it with the Docker socket, because the component under
check is the one that must talk to the daemon. The socket resolution differs between Windows and
Linux, so it is decided once here instead of in each probe.
"""

import json
import os
import platform
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
DOCKERFILE = "lab/isolation/Dockerfile"


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=REPOSITORY, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def socket_source() -> str:
    """The local Docker socket, as a container on this host sees it."""
    if platform.system().lower() == "windows":
        return "/var/run/docker.sock"
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        endpoint = json.loads(
            command(
                "docker",
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
                capture=True,
            )
        )
    if not endpoint.startswith("unix://"):
        raise ValueError("This lab probe requires a local Unix Docker socket")
    return endpoint.removeprefix("unix://")


def build(target: str, image: str) -> None:
    command("docker", "build", "-f", DOCKERFILE, "--target", target, "-t", image, ".")


def run_probe(
    *,
    image: str,
    run_id: str,
    output: Path,
    environment: dict[str, str],
    network: str | None = None,
) -> None:
    """Run one lab probe: socket, results mount, and nothing else.

    Every probe needs the same shape — a read-only container with the Docker socket, a bounded
    filesystem and its results directory — and the same report path, so the shape lives here and
    each entry point only says which image, which network and which environment.
    """
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"huntweave-{run_id}",
        "--network",
        network or "none",
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
        command(*args, image)
    finally:
        print(f"Validation report: {output / 'report.json'}")


def source_facts() -> dict[str, str]:
    """Which revision the probe is really running against, and whether it was modified."""
    return {
        "HUNTWEAVE_SOURCE_REVISION": command("git", "rev-parse", "HEAD", capture=True),
        "HUNTWEAVE_SOURCE_TREE_DIRTY": (
            "true" if command("git", "status", "--porcelain", capture=True) else "false"
        ),
    }
