"""Server-only settings. Secret values never participate in repr or logs."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import URL, make_url

# Where the control image ships the console build (deploy/Dockerfile). The app serves it, and the
# execution side reads the served bundle to decide the console-consumption readiness gate.
CONSOLE_BUILD = Path(__file__).resolve().parents[3] / "frontend" / "dist"

# Where the image ships the committed, versioned execution profiles (deploy/Dockerfile).
PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles"


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
    access_key_file: Path | None = field(default=None, repr=False)
    public_origin: str = "http://127.0.0.1:8000"
    cookie_secure: bool = False
    idle_seconds: int = 7200
    absolute_seconds: int = 86400

    def __post_init__(self) -> None:
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Public origin must be an HTTP(S) origin without a path")
        if not self.cookie_secure and (
            origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Insecure cookies are allowed only in explicit localhost HTTP mode")
        if self.cookie_secure and origin.scheme != "https":
            raise ValueError("Secure cookies require an HTTPS public origin")
        if min(self.idle_seconds, self.absolute_seconds) < 1:
            raise ValueError("Session lifetimes must be positive")

    @property
    def cookie_name(self) -> str:
        return "__Host-huntweave" if self.cookie_secure else "huntweave_local_session"

    def current_access_key(self) -> str | None:
        if self.access_key_file is None:
            value = self.access_key
        else:
            try:
                value = self.access_key_file.read_text(encoding="utf-8").strip()
            except OSError:
                return None
        return value if value and len(value.encode("utf-8")) >= 32 else None

    @classmethod
    def from_env(cls) -> "AppSettings":
        value = read_secret("HUNTWEAVE_ACCESS_KEY_FILE", required=False)
        if value is not None and len(value.encode("utf-8")) < 32:
            raise ValueError("Access key must contain at least 32 bytes")
        filename = os.environ.get("HUNTWEAVE_ACCESS_KEY_FILE")
        return cls(
            access_key_file=Path(filename) if filename else None,
            public_origin=os.environ.get("HUNTWEAVE_PUBLIC_ORIGIN", "http://127.0.0.1:8000"),
            cookie_secure=os.environ.get("HUNTWEAVE_COOKIE_SECURE", "false").lower() == "true",
        )


@dataclass(frozen=True)
class SandboxSettings:
    """Whether this Runner may manage sandbox containers, and with which fixed profile.

    Disabled unless the deployment says otherwise: `HUNTWEAVE_SANDBOX_MANAGEMENT` has to be the
    exact word `enabled`. Every other value — including a typo — leaves the Runner without any
    container management, so a default Compose deployment cannot open real execution by accident.
    """

    enabled: bool = False
    profile_id: str = "sandbox-lifecycle-v1"
    profile_dir: Path = PROFILE_DIR
    state_dir: Path = Path("/runner-state")
    evidence_dir: Path = Path("/evidence")

    @classmethod
    def from_env(cls) -> "SandboxSettings":
        return cls(
            enabled=os.environ.get("HUNTWEAVE_SANDBOX_MANAGEMENT", "").lower() == "enabled",
            profile_id=os.environ.get("HUNTWEAVE_SANDBOX_PROFILE", "sandbox-lifecycle-v1"),
            profile_dir=Path(os.environ.get("HUNTWEAVE_SANDBOX_PROFILE_DIR", str(PROFILE_DIR))),
            state_dir=Path(os.environ.get("HUNTWEAVE_RUNNER_STATE_DIR", "/runner-state")),
            evidence_dir=Path(os.environ.get("HUNTWEAVE_EVIDENCE_DIR", "/evidence")),
        )


def retention_sweep_seconds() -> int:
    """How often the Runner applies the retention policy on its own, in seconds.

    Zero means "only when an operator asks": the preview is then the authority and nothing is
    removed unattended. The default lets the TTL and the capacity actually bound the cache in a
    deployment nobody is watching, which is what the policy is for.
    """
    raw = os.environ.get("HUNTWEAVE_RETENTION_SWEEP_SECONDS", "300")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("HUNTWEAVE_RETENTION_SWEEP_SECONDS must be an integer") from None
    if value < 0:
        raise ValueError("HUNTWEAVE_RETENTION_SWEEP_SECONDS must not be negative")
    return value


def event_retention_keep() -> int:
    """How many of a Run's most recent events this deployment keeps.

    Zero disables pruning, which is the default: a timeline that is never pruned cannot report a
    gap, and a deployment that has not decided how long an operator needs to replay a Run should not
    have that decision made for it by a default. A positive value is the retention policy for the
    *timeline* only — never for the business record or the archived evidence, which have their own
    lifetimes (`PROJECT.md` section 12, `0002` section 3.8).
    """
    raw = os.environ.get("HUNTWEAVE_EVENT_RETENTION_KEEP", "0")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("HUNTWEAVE_EVENT_RETENTION_KEEP must be an integer") from None
    if value < 0:
        raise ValueError("HUNTWEAVE_EVENT_RETENTION_KEEP must not be negative")
    return value


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
