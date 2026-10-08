"""Server-only settings. Secret values never participate in repr or logs."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import URL, make_url


def read_secret(name: str, *, required: bool = True) -> str | None:
    filename = os.environ.get(name)
    value = None
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            pass
    if required and not value:
        raise ValueError(f"Required secret file is unavailable: {name}")
    return value


@dataclass(frozen=True)
class AppSettings:
    access_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "AppSettings":
        value = read_secret("HUNTWEAVE_ACCESS_KEY_FILE", required=False)
        if value is not None and len(value.encode("utf-8")) < 32:
            raise ValueError("Access key must contain at least 32 bytes")
        return cls(access_key=value)


def database_url(role: str = "app") -> URL:
    if role not in {"app", "checkpoint", "migrator"}:
        raise ValueError("Unsupported database role")
    password = read_secret(f"HUNTWEAVE_{role.upper()}_DB_PASSWORD_FILE")
    base = make_url(
        os.environ.get("HUNTWEAVE_DATABASE_URL", "postgresql+psycopg://postgres/huntweave")
    )
    return base.set(username=f"huntweave_{role}", password=password)


def runner_token() -> str:
    value = read_secret("HUNTWEAVE_RUNNER_TOKEN_FILE")
    if value is None or len(value.encode("utf-8")) < 32:
        raise ValueError("Runner token must contain at least 32 bytes")
    return value
