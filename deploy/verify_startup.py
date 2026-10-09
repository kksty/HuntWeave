"""Run startup fault probes through Docker CLI on Windows or Linux."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", str(REPOSITORY / "deploy" / "compose.yaml")]
COMPOSE_ENV = os.environ.copy()


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE, *args], cwd=REPOSITORY, env=COMPOSE_ENV, text=True, check=check
    )


def inspect(container: str) -> dict:
    result = subprocess.run(
        ["docker", "inspect", container], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="Isolated Compose project; default preserves compose.yaml")
    parser.add_argument("--web-port", type=int, help="Loopback Web port for an isolated stack")
    options = parser.parse_args()
    if options.project:
        COMPOSE.extend(["--project-name", options.project])
    if options.web_port is not None:
        if not 1 <= options.web_port <= 65535:
            parser.error("--web-port must be between 1 and 65535")
        COMPOSE_ENV["HUNTWEAVE_WEB_PORT"] = str(options.web_port)
        COMPOSE_ENV["HUNTWEAVE_PUBLIC_ORIGIN"] = f"http://127.0.0.1:{options.web_port}"
    compose("up", "-d", "--wait", "--wait-timeout", "90")
    missing = """
from fastapi.testclient import TestClient
from huntweave.api.app import create_app
client = TestClient(create_app())
for path in ('/', '/api/v1/system/capabilities', '/docs', '/openapi.json', '/evidence/x'):
    response = client.get(path)
    assert response.status_code == 503
    assert response.json()['reason_code'] == 'access_key_missing'
assert client.get('/health/live').status_code == 200
print('PASS: missing-key image blocks business resources')
"""
    compose(
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "-e",
        "HUNTWEAVE_ACCESS_KEY_FILE=/tmp/absent-key",
        "app",
        "python",
        "-c",
        missing,
    )
    boundary = """
import os
from pathlib import Path
assert os.getuid() == 10001
assert not Path('/run/secrets/access_key').exists()
assert not Path('/var/run/docker.sock').exists()
assert not Path('/opt/huntweave/.agents').exists()
print('PASS: non-root Runner has no platform key, socket or development skills')
"""
    compose("exec", "-T", "runner", "python", "-c", boundary)
    readonly = """
from pathlib import Path
try:
    Path('/evidence/.startup-write-probe').write_text('probe')
except OSError as error:
    assert error.errno == 30
else:
    raise AssertionError('app evidence archive is writable')
print('PASS: app evidence archive is read-only')
"""
    compose("exec", "-T", "app", "python", "-c", readonly)
    try:
        compose("stop", "runner")
        result = compose(
            "exec", "-T", "app", "python", "-m", "huntweave.api.healthcheck", check=False
        )
        assert result.returncode != 0, "Readiness passed while Runner was stopped"
        print("PASS: readiness rejects an unavailable Runner")
    finally:
        compose("up", "-d", "--wait", "--wait-timeout", "90")
    result = subprocess.run(
        [*COMPOSE, "ps", "-q", "app"], cwd=REPOSITORY, env=COMPOSE_ENV,
        capture_output=True, text=True, check=True
    )
    app_id = result.stdout.strip()
    before = inspect(app_id)["RestartCount"]
    fault = """
import os, signal
from pathlib import Path
matches = []
for folder in Path('/proc').iterdir():
    if not folder.name.isdigit():
        continue
    try:
        parts = (folder / 'cmdline').read_bytes().split(b'\\0')
    except (FileNotFoundError, PermissionError):
        continue
    if b'huntweave.api.agentd' in parts:
        matches.append(int(folder.name))
assert len(matches) == 1
os.kill(matches[0], signal.SIGTERM)
print('Injected: agentd exit')
"""
    compose("exec", "-T", "app", "python", "-c", fault)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = inspect(app_id)
        if (
            state["RestartCount"] > before
            and state["State"].get("Health", {}).get("Status") == "healthy"
        ):
            print("PASS: supervisor restarts and restores app health")
            break
        time.sleep(1)
    else:
        raise RuntimeError("Supervisor did not recover after agentd exited")
    compose("ps")


if __name__ == "__main__":
    main()
