"""Generate deployment secrets on Windows or Linux; never print their values."""

import os
import secrets
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
SECRET_NAMES = (
    "access_key",
    "runner_token",
    "postgres_password",
    "app_db_password",
    "checkpoint_db_password",
    "migrator_db_password",
)


def initialize(repository: Path = REPOSITORY) -> None:
    directory = repository / "runtime" / "secrets"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory.chmod(0o700)
    for name in SECRET_NAMES:
        filename = directory / name
        if filename.exists():
            if len(filename.read_text(encoding="utf-8").strip()) < 64:
                raise ValueError(f"Existing secret file is empty or too short: {name}")
        else:
            # Exclusive creation avoids overwriting an initialized deployment.
            with filename.open("x", encoding="utf-8") as output:
                output.write(secrets.token_hex(32))
        if os.name == "posix":
            # File binds must be readable by app UID 10001 and Postgres UID 999.
            # Host users cannot traverse the owner-only parent directory.
            filename.chmod(0o444)
    print("Deployment secrets are ready under runtime/secrets; no values were printed.")


if __name__ == "__main__":
    initialize()
