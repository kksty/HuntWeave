"""Build and execute the isolated Docker lab on Windows or Linux."""

import json
import os
import platform
import re
import socket
import subprocess
import uuid
from pathlib import Path

import psutil

REPOSITORY = Path(__file__).resolve().parent.parent


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=REPOSITORY, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def host_facts() -> dict[str, str]:
    host = platform.system().lower()
    addresses = sorted(
        {
            address.address
            for values in psutil.net_if_addrs().values()
            for address in values
            if address.family == socket.AF_INET
        }
    )
    values = {
        "HUNTWEAVE_HOST_PLATFORM": host,
        "HUNTWEAVE_HOST_OS": platform.platform(),
        "HUNTWEAVE_HOST_ADDRESSES": ",".join(addresses),
        "HUNTWEAVE_WSL_NETWORKING": "not_applicable",
        "HUNTWEAVE_WSL_VERSION": "not_applicable",
    }
    if host == "windows":
        configuration = Path.home() / ".wslconfig"
        mode = "nat"
        if configuration.exists():
            match = re.search(
                r"(?mi)^\s*networkingMode\s*=\s*([^\s#;]+)", configuration.read_text()
            )
            if match:
                mode = match.group(1).lower()
        result = subprocess.run(["wsl.exe", "--version"], capture_output=True, check=True)
        version = re.search(r"\d+\.\d+\.\d+\.\d+", result.stdout.decode("utf-16-le"))
        values["HUNTWEAVE_WSL_NETWORKING"] = mode
        values["HUNTWEAVE_WSL_VERSION"] = version.group(0)
    return values


def socket_source(host: str) -> str:
    if host == "windows":
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
        raise ValueError("The local lab requires a local Unix Docker socket")
    return endpoint.removeprefix("unix://")


def main() -> None:
    facts = host_facts()
    run_id = uuid.uuid4().hex
    output = REPOSITORY / "runtime" / "isolation" / run_id
    output.mkdir(parents=True)
    command("docker", "version", "--format", "{{.Server.Version}}")
    for target in ("probe", "manager"):
        command(
            "docker",
            "build",
            "-f",
            "lab/isolation/Dockerfile",
            "--target",
            target,
            "-t",
            f"huntweave-isolation-{target}:p0",
            ".",
        )
    facts["HUNTWEAVE_SOURCE_REVISION"] = command("git", "rev-parse", "HEAD", capture=True)
    facts["HUNTWEAVE_SOURCE_TREE_DIRTY"] = (
        "true" if command("git", "status", "--porcelain", capture=True) else "false"
    )
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"huntweave-isolation-manager-{run_id}",
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
        f"type=bind,source={socket_source(facts['HUNTWEAVE_HOST_PLATFORM'])},target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={output},target=/results",
    ]
    for key, value in facts.items():
        args.extend(["--env", f"{key}={value}"])
    try:
        command(*args, "huntweave-isolation-manager:p0")
    finally:
        print(f"Validation report: {output / 'report.json'}")


if __name__ == "__main__":
    main()
