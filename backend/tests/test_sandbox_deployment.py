"""The deployment boundary: no container management unless the deployer asks for it, by name.

These checks read the deployment artifacts themselves — a Compose file that quietly grew a socket
mount, or a profile that stopped travelling in the image, would otherwise only be visible to
someone reading YAML by hand.
"""

import tomllib
from pathlib import Path
from typing import Any

import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
DEPLOY = REPOSITORY / "deploy"
COMPOSE_FILES = ("compose.yaml", "compose.sandbox.yaml", "compose.verify.yaml", "compose.dev.yaml")


def compose(name: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((DEPLOY / name).read_text(encoding="utf-8"))
    return document


def socket_mounts(name: str) -> list[str]:
    """Every place any Compose file mounts a Docker socket into a service."""
    found: list[str] = []
    for service, body in compose(name)["services"].items():
        for mount in body.get("volumes", []):
            if "docker.sock" in str(mount):
                found.append(service)
    return found


def test_the_default_deployment_has_three_services_and_no_management_capability() -> None:
    base = compose("compose.yaml")
    assert set(base["services"]) == {"app", "postgres", "runner"}
    assert socket_mounts("compose.yaml") == []
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "HUNTWEAVE_SANDBOX_MANAGEMENT" not in text
    # Neither service guesses at a Docker endpoint: the app never gets one at all.
    assert "DOCKER_HOST" not in text


def test_management_is_switched_on_only_by_the_opt_in_override() -> None:
    override = compose("compose.sandbox.yaml")
    assert set(override["services"]) == {"runner"}
    runner = override["services"]["runner"]
    assert runner["environment"]["HUNTWEAVE_SANDBOX_MANAGEMENT"] == "enabled"
    assert socket_mounts("compose.sandbox.yaml") == ["runner"]
    assert "HUNTWEAVE_SANDBOX_PROFILE" in runner["environment"]


def test_only_the_runner_ever_sees_a_container_management_socket() -> None:
    for name in COMPOSE_FILES:
        assert socket_mounts(name) in ([], ["runner"]), f"{name} mounts a socket elsewhere"


def test_the_control_image_ships_the_committed_profiles_and_the_container_client() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY profiles /opt/huntweave/profiles" in dockerfile, (
        "the fixed profile has to travel in the image, or management cannot open at all"
    )
    project = tomllib.loads(
        (REPOSITORY / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    # A main dependency, so the Runner image carries the client it needs when management is
    # enabled instead of depending on the lab-only group.
    assert any(requirement.startswith("docker") for requirement in project["dependencies"])
