"""Durable browser sessions and key rotation behind the deployment's access key.

There is no account system here: the operator presents the deployment's access key and receives a
server-side session bound to the key's version. Submitting that key is deliberately **not** rate
limited. The key is a value this deployment generates (at least 32 bytes of cryptographic
randomness), so guessing it is not a threat that a per-address throttle addresses; what a throttle
would do is lock the operator out of their own platform for a minute after a few typos. A deployment
that wants request-level abuse protection for its public entry point puts it at the reverse proxy,
which is the layer that can see real client addresses and apply a policy to the whole surface rather
than to one endpoint.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, delete, or_, select, text, update
from sqlalchemy.orm import Session

from huntweave.config import AppSettings
from huntweave.contracts.errors import ServiceError
from huntweave.storage.database import database_now
from huntweave.storage.models import AccessKeyState, WebSession


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BrowserSession:
    id: UUID
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


class AccessService:
    def __init__(self, engine: Callable[[], Engine], settings: AppSettings):
        self.engine = engine
        self.settings = settings

    def _key_version(self, session: Session, key: str) -> int:
        # Serializes first initialization and rotations across processes/restarts.
        session.execute(text("SELECT pg_advisory_xact_lock(986013771)"))
        state = session.get(AccessKeyState, 1)
        fingerprint = digest(key)
        if state is None:
            state = AccessKeyState(id=1, fingerprint=fingerprint, version=1)
            session.add(state)
            session.flush()
        elif not hmac.compare_digest(state.fingerprint, fingerprint):
            state.fingerprint = fingerprint
            state.version += 1
            session.execute(update(WebSession).values(revoked=True))
        return state.version

    def login(self, supplied: str, previous_token: str | None) -> tuple[str, BrowserSession]:
        key = self.settings.current_access_key()
        if key is None:
            raise ServiceError("access_key_missing", 503)
        if not hmac.compare_digest(digest(supplied), digest(key)):
            raise ServiceError("invalid_access_key", 401)
        token = secrets.token_urlsafe(32)
        with Session(self.engine()) as session, session.begin():
            version = self._key_version(session, key)
            now = database_now(session)
            session.execute(
                delete(WebSession).where(
                    or_(
                        WebSession.revoked.is_(True),
                        WebSession.idle_expires_at <= now,
                        WebSession.absolute_expires_at <= now,
                    )
                )
            )
            if previous_token:
                session.execute(
                    update(WebSession)
                    .where(WebSession.token_hash == digest(previous_token))
                    .values(revoked=True)
                )
            record = WebSession(
                token_hash=digest(token),
                csrf_token=secrets.token_urlsafe(32),
                key_version=version,
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(seconds=self.settings.idle_seconds),
                absolute_expires_at=now + timedelta(seconds=self.settings.absolute_seconds),
                revoked=False,
            )
            session.add(record)
            session.flush()
            result = self._view(record)
        return token, result

    def authenticate(self, token: str | None, csrf: str | None = None) -> BrowserSession:
        if not token or not 32 <= len(token) <= 128:
            raise ServiceError("authentication_required", 401)
        key = self.settings.current_access_key()
        if key is None:
            raise ServiceError("access_key_missing", 503)
        with Session(self.engine()) as session, session.begin():
            version = self._key_version(session, key)
            now = database_now(session)
            record = session.scalar(
                select(WebSession).where(WebSession.token_hash == digest(token)).with_for_update()
            )
            valid = (
                record is not None
                and not record.revoked
                and record.key_version == version
                and now < record.idle_expires_at
                and now < record.absolute_expires_at
            )
            if not valid:
                result = None
            else:
                assert record is not None
                if csrf is not None and not hmac.compare_digest(
                    digest(record.csrf_token), digest(csrf)
                ):
                    raise ServiceError("csrf_invalid", 403)
                record.last_seen_at = now
                record.idle_expires_at = min(
                    now + timedelta(seconds=self.settings.idle_seconds), record.absolute_expires_at
                )
                result = self._view(record)
        # Commit key rotations even when the old session has just been invalidated.
        if result is None:
            raise ServiceError("authentication_required", 401)
        return result

    def logout(self, session_id: UUID) -> None:
        with Session(self.engine()) as session, session.begin():
            session.execute(
                update(WebSession).where(WebSession.id == session_id).values(revoked=True)
            )

    @staticmethod
    def _view(record: WebSession) -> BrowserSession:
        return BrowserSession(
            record.id, record.csrf_token, record.idle_expires_at, record.absolute_expires_at
        )
