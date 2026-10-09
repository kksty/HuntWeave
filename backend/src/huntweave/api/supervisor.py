"""Start migrations before API/agentd; any child failure stops the whole service."""

import json
import os
import signal
import subprocess
import sys
import threading
from types import FrameType

from huntweave.storage.migrate import migrate


def main() -> int:
    if os.name != "posix":
        print('{"reason_code":"environment_unsupported"}')
        return 1
    stopping = threading.Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children: list[subprocess.Popen[bytes]] = []
    failed = False
    try:
        migrate()
        if stopping.is_set():
            return 0
        env = os.environ.copy()
        env.pop("HUNTWEAVE_MIGRATOR_DB_PASSWORD_FILE", None)
        agent_env = env.copy()
        agent_env.pop("HUNTWEAVE_ACCESS_KEY_FILE", None)
        api_env = env.copy()
        api_env.pop("HUNTWEAVE_CHECKPOINT_DB_PASSWORD_FILE", None)
        commands = [
            (
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "huntweave.api.app:create_app",
                    "--factory",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--no-access-log",
                    "--no-proxy-headers",
                ],
                api_env,
            ),
            ([sys.executable, "-m", "huntweave.api.agentd"], agent_env),
        ]
        for command, child_env in commands:
            children.append(subprocess.Popen(command, env=child_env, start_new_session=True))
        print(json.dumps({"event": "startup_complete", "children": len(children)}), flush=True)
        while not stopping.wait(0.25):
            if any(child.poll() is not None for child in children):
                print('{"reason_code":"required_process_exited"}', flush=True)
                failed = True
                break
    except Exception as error:
        print(json.dumps({"reason_code": "startup_failed", "error_type": type(error).__name__}))
        failed = True
    finally:
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # Reap descendants even if the original process exited during shutdown.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
