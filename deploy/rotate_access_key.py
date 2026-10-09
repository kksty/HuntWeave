"""Rotate the mounted key in place so Docker file binds observe the new content."""

import os
import secrets
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent


def rotate(repository: Path = REPOSITORY) -> None:
    filename = repository / "runtime" / "secrets" / "access_key"
    if not filename.is_file():
        raise ValueError("Initialize deployment secrets before rotating the access key")
    if os.name == "posix":
        filename.chmod(0o600)
    try:
        # Keep the inode: replacing the file would leave an existing bind on the old key.
        with filename.open("w", encoding="utf-8") as output:
            output.write(secrets.token_hex(32))
            output.flush()
            os.fsync(output.fileno())
    finally:
        if os.name == "posix":
            filename.chmod(0o444)
    print("Access key rotated; old sessions are rejected on their next request.")


if __name__ == "__main__":
    rotate()
