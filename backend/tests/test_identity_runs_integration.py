import os
import secrets
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from huntweave.access.service import digest
from huntweave.api.app import create_app
from huntweave.config import AppSettings
from huntweave.contracts.capabilities import Capabilities
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.runs import ProjectCreate, RunCreate, ScopeCreate
from huntweave.execution.capabilities import evaluate
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine
from huntweave.storage.models import (
    AccessKeyState,
    AgentSession,
    AuditEvent,
    AuthorizationScope,
    BudgetReservation,
    Decision,
    EventCursor,
    Evidence,
    InterruptionRecord,
    Outbox,
    Project,
    ReconciliationDecision,
    ResearchTask,
    Run,
    ToolCall,
    ToolResult,
    WebSession,
)

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"
KEY = "p0-b-test-key-" + "a" * 64
# Children first: execution records reference Runs, Projects and Agent sessions.
CLEARED = (
    AuditEvent,
    EventCursor,
    ToolResult,
    Outbox,
    BudgetReservation,
    Evidence,
    InterruptionRecord,
    ReconciliationDecision,
    ToolCall,
    Decision,
    AgentSession,
    ResearchTask,
    Run,
    AuthorizationScope,
    Project,
    WebSession,
    AccessKeyState,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    # These checks run in the disposable huntweave-p0b-checks Compose project, never the dev DB.
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Identity/Run fault tests require an explicitly disposable Compose database")
    result = connect_engine()
    for attempt in range(5):
        try:
            with Session(result) as session, session.begin():
                for model in CLEARED:
                    session.execute(delete(model))
            break
        except IntegrityError:
            # The live scheduler may attach a research task to a queued Run mid-cleanup.
            if attempt == 4:
                raise
            time.sleep(0.5)
    yield result
    result.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(
        create_app(AppSettings(access_key=KEY), engine, capability_reader=ready_capabilities),
        base_url=ORIGIN,
    ) as result:
        yield result


def ready_capabilities() -> Capabilities:
    """The execution side's answer while the fixed fake chain works and real execution stays shut.

    The capability source is injected because these checks run without a Runner process; the
    observation itself is covered by the Runner boundary and startup stack checks.
    """
    return evaluate(fake_execution_ready=True, now=datetime.now(UTC))


def login(client: TestClient) -> dict[str, str]:
    response = client.post("/auth/login", json={"access_key": KEY}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert KEY not in response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def scope_request(project_id: str) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "project_id": project_id,
        "targets_text": "192.0.2.2\n192.0.2.1\n192.0.2.1",
        "ports": {"profile": "custom-tcp-v1", "custom": "443,80,80"},
        "starts_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "authorization": "本地假执行验证",
    }


def test_all_business_resources_require_session_and_html_redirects(client: TestClient) -> None:
    for path in (
        "/",
        "/runs/x",
        "/assets/index.js",
        "/docs",
        "/openapi.json",
        "/api/v1/runs",
        "/api/v1/runs/x/events",
        "/api/v1/evidence/x",
    ):
        assert client.get(path).status_code == 401
    assert client.post("/api/v1/projects", json={"name": "denied"}).status_code == 401
    assert client.get("/api/v1/runs", headers={"Accept": "text/html"}).status_code == 401
    assert (
        client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).headers["location"]
        == "/login"
    )
    for path in ("/login", "/login.js", "/login.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "/assets/" not in response.text


def test_origin_csrf_validation_never_echoes_key_and_bounds_request(client: TestClient) -> None:
    assert client.post("/auth/login", json={"access_key": KEY}).status_code == 403
    response = client.post("/auth/login", json={"access_key": [KEY]}, headers={"Origin": ORIGIN})
    assert response.status_code == 422 and KEY not in response.text
    response = client.post("/auth/login", content=b"a" * 4097, headers={"Origin": ORIGIN})
    assert response.status_code == 413
    headers = login(client)
    assert (
        client.post("/api/v1/projects", json={"name": "a"}, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/projects",
            json={"name": "a"},
            headers={**headers, "Origin": "http://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/projects", json={"name": "a"}, headers={**headers, "X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert client.post("/api/v1/projects", json={"name": "a"}, headers=headers).status_code == 201


def test_session_hash_logout_relogin_and_app_restart(client: TestClient, engine: Engine) -> None:
    headers = login(client)
    token = client.cookies.get("huntweave_local_session")
    with Session(engine) as session:
        stored = session.scalar(select(WebSession))
        assert stored is not None and stored.token_hash == digest(str(token))
        assert stored.token_hash != token
    replacement = TestClient(create_app(AppSettings(access_key=KEY), engine), base_url=ORIGIN)
    replacement.cookies.update(client.cookies)
    assert replacement.get("/auth/session").status_code == 200
    login(client)
    assert replacement.get("/auth/session").status_code == 401
    headers = {**headers, "X-CSRF-Token": client.get("/auth/session").json()["csrf_token"]}
    assert client.post("/auth/logout", json={}, headers=headers).status_code == 204
    replacement.cookies.update(client.cookies)
    assert client.get("/api/v1/projects").status_code == 401


@pytest.mark.parametrize("column", ["idle_expires_at", "absolute_expires_at"])
def test_each_session_expiry_is_enforced(client: TestClient, engine: Engine, column: str) -> None:
    login(client)
    with Session(engine) as session, session.begin():
        session.execute(
            update(WebSession).values({column: datetime.now(UTC) - timedelta(seconds=1)})
        )
    assert client.get("/auth/session").status_code == 401


def test_rotation_file_revokes_old_sessions_on_next_request(engine: Engine, tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    settings = AppSettings(access_key_file=key_file)
    client = TestClient(create_app(settings, engine), base_url=ORIGIN)
    login(client)
    key_file.write_text("new-test-key-" + "b" * 64)
    assert client.get("/auth/session").status_code == 401
    with Session(engine) as session:
        assert session.get(AccessKeyState, 1).version == 2  # type: ignore[union-attr]
        assert all(item.revoked for item in session.scalars(select(WebSession)))
    assert (
        client.post("/auth/login", json={"access_key": KEY}, headers={"Origin": ORIGIN}).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login", json={"access_key": key_file.read_text()}, headers={"Origin": ORIGIN}
        ).status_code
        == 200
    )
    key_file.unlink()
    assert client.get("/auth/session").status_code == 503


def test_submitting_the_key_is_not_throttled(client: TestClient) -> None:
    """A wrong key is refused on its own merits, and a right one is accepted however often.

    The key is generated by the deployment rather than chosen by a person, so repeating the request
    is not an attack a per-address counter addresses; what such a counter did do was lock the
    operator out of their own platform. Unlimited wrong attempts are still answered with an
    authentication failure, one at a time.
    """
    for attempt in range(40):
        response = client.post(
            "/auth/login",
            json={"access_key": "wrong"},
            headers={"Origin": ORIGIN, "X-Forwarded-For": f"192.0.2.{attempt % 250}"},
        )
        assert response.status_code == 401
        assert response.json()["reason_code"] == "invalid_access_key"
    accepted = client.post(
        "/auth/login", json={"access_key": KEY}, headers={"Origin": ORIGIN}
    )
    assert accepted.status_code == 200
    assert client.get("/auth/session").status_code == 200


def test_https_session_uses_host_cookie_and_secure_flag(engine: Engine) -> None:
    origin = "https://huntweave.example"
    client = TestClient(
        create_app(AppSettings(access_key=KEY, public_origin=origin, cookie_secure=True), engine),
        base_url=origin,
    )
    response = client.post("/auth/login", json={"access_key": KEY}, headers={"Origin": origin})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "__Host-huntweave=" in cookie and "Secure" in cookie and "Path=/" in cookie
    assert "Domain=" not in cookie
    assert client.get("/auth/session").status_code == 200


def test_future_authorization_cannot_create_a_run(client: TestClient) -> None:
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "future"}, headers=headers).json()
    now = datetime.now(UTC)
    request = {
        **scope_request(project["id"]),
        "starts_at": (now + timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
    }
    response = client.post("/api/v1/scopes", json=request, headers=headers)
    assert response.status_code == 201
    response = client.post(
        "/api/v1/runs",
        json={"scope_id": response.json()["id"], "scope_version": 1},
        headers={**headers, "Idempotency-Key": "future-run"},
    )
    assert (
        response.status_code == 409
        and response.json()["reason_code"] == "authorization_not_started"
    )
    assert client.get("/api/v1/runs").json() == []


def test_create_scope_run_replay_conflicts_and_immutable_snapshot(
    client: TestClient, engine: Engine
) -> None:
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "演示"}, headers=headers).json()
    scope = client.post("/api/v1/scopes", json=scope_request(project["id"]), headers=headers).json()
    assert scope["snapshot"]["targets"] == ["192.0.2.1", "192.0.2.2"]
    assert scope["snapshot"]["ports"] == [80, 443]
    body = {"scope_id": scope["id"], "scope_version": 1}
    key_headers = {**headers, "Idempotency-Key": "same-request"}
    first = client.post("/api/v1/runs", json=body, headers=key_headers)
    assert first.status_code == 201
    run = first.json()
    assert run["scope_snapshot"] == scope["snapshot"] and run["status"] == "draft"
    # A draft still records whether the fixed fake execution path is available.
    assert run["demonstration"] and run["execution_ready"]
    assert client.post("/api/v1/runs", json=body, headers=key_headers).json()["id"] == run["id"]
    assert (
        client.post(
            "/api/v1/runs", json={**body, "scope_version": 2}, headers=key_headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/runs",
            json={**body, "scope_version": 2},
            headers={**headers, "Idempotency-Key": "different"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/runs/{run['id']}/start", json={"version": 99}, headers=headers
        ).status_code
        == 409
    )
    queued = client.post(
        f"/api/v1/runs/{run['id']}/start", json={"version": 1}, headers=headers
    ).json()
    assert queued["status"] == "queued" and queued["version"] == 2
    assert (
        client.post(
            f"/api/v1/runs/{run['id']}/start", json={"version": 1}, headers=headers
        ).status_code
        == 409
    )
    # P0-C schedules a queued Run immediately, so a later read may already show progress;
    # a second app instance must still serve the same durable Run and Scope records.
    restarted = TestClient(
        create_app(AppSettings(access_key=KEY), engine, capability_reader=ready_capabilities),
        base_url=ORIGIN,
    )
    restarted.cookies.update(client.cookies)
    reread = restarted.get(f"/api/v1/runs/{run['id']}").json()
    assert reread["id"] == run["id"] and reread["scope_snapshot"] == run["scope_snapshot"]
    assert reread["version"] >= queued["version"]
    assert reread["status"] in {
        "queued",
        "running",
        "recovering",
        "waiting",
        "pausing",
        "paused",
        "cancelling",
        "cancelled",
        "failed",
        "closed",
    }
    assert restarted.get(f"/api/v1/scopes/{scope['id']}").json() == scope


def test_control_response_preserves_derived_run_fields(
    client: TestClient, engine: Engine
) -> None:
    """A successful control must return its Run, including the public derived mode field.

    The resume route revalidates the control result before returning it. Passing a previously
    serialized computed field into that validation used to raise after the state was committed.
    """
    headers = login(client)
    project = client.post(
        "/api/v1/projects", json={"name": "control response"}, headers=headers
    ).json()
    scope = client.post(
        "/api/v1/scopes", json=scope_request(project["id"]), headers=headers
    ).json()
    run = client.post(
        "/api/v1/runs",
        json={"scope_id": scope["id"], "scope_version": 1},
        headers={**headers, "Idempotency-Key": "control-response-contract"},
    ).json()
    with Session(engine) as session, session.begin():
        session.execute(update(Run).where(Run.id == run["id"]).values(status="paused"))

    response = client.post(
        f"/api/v1/runs/{run['id']}/resume", json={"version": run["version"]}, headers=headers
    )
    assert response.status_code == 200
    resumed = response.json()
    assert resumed["id"] == run["id"]
    assert resumed["status"] == "running"
    assert resumed["version"] > run["version"]
    assert resumed["execution_profile"] == "fake-p0-v1"
    assert resumed["demonstration"] is True


def test_run_readiness_follows_the_execution_side(engine: Engine) -> None:
    with TestClient(
        create_app(AppSettings(access_key=KEY), engine, capability_reader=ready_capabilities),
        base_url=ORIGIN,
    ) as client:
        headers = login(client)
        project = client.post("/api/v1/projects", json={"name": "就绪"}, headers=headers).json()
        scope = client.post(
            "/api/v1/scopes", json=scope_request(project["id"]), headers=headers
        ).json()
        run = client.post(
            "/api/v1/runs",
            json={"scope_id": scope["id"], "scope_version": 1},
            headers={**headers, "Idempotency-Key": "readiness"},
        ).json()
        assert run["execution_ready"] is True
        # An execution side that cannot be reached is reported as not ready, never as assumed
        # ready, and the capability answer says which observation is missing.
        unavailable = TestClient(
            create_app(
                AppSettings(access_key=KEY),
                engine,
                capability_reader=lambda: Capabilities.unobserved("runner_unavailable"),
            ),
            base_url=ORIGIN,
        )
        unavailable.cookies.update(client.cookies)
        detail = unavailable.get(f"/api/v1/runs/{run['id']}").json()
        assert detail["execution_ready"] is False
        snapshot = unavailable.get(f"/api/v1/runs/{run['id']}/snapshot").json()
        assert snapshot["run"]["execution_ready"] is False
        listed = unavailable.get("/api/v1/runs").json()
        assert [item["execution_ready"] for item in listed] == [False]
        capability = unavailable.get("/api/v1/system/capabilities").json()
        assert capability["reason_code"] == "runner_unavailable"
        assert capability["observed_at"] is None
        assert capability["real_execution_ready"] is False
        assert capability["mode"] == "demonstration"


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"targets_text": "192.0.2.1\nexample.com"}, 422),
        ({"targets_text": "2001:db8::1"}, 422),
        ({"expires_at": "2020-01-01T00:00:00Z", "starts_at": "2019-01-01T00:00:00Z"}, 409),
        ({"expires_at": "2020-01-01T00:00:00"}, 422),
        ({"budget": {"max_tool_calls": 0}}, 422),
        ({"mode": "autonomous"}, 422),
        ({"execution_profile": "real"}, 422),
    ],
)
def test_invalid_authorizations_never_create_run(
    client: TestClient, change: dict[str, object], expected: int
) -> None:
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "validation"}, headers=headers).json()
    response = client.post(
        "/api/v1/scopes", json={**scope_request(project["id"]), **change}, headers=headers
    )
    assert response.status_code == expected
    assert client.get("/api/v1/runs").json() == []


def test_concurrent_creation_and_state_updates_are_serialized(engine: Engine) -> None:
    service = RunService(lambda: engine)
    project = service.create_project(ProjectCreate(name="concurrency"))
    scope = service.create_scope(ScopeCreate.model_validate(scope_request(str(project.id))))
    request = RunCreate(scope_id=scope.id, scope_version=1)
    key = secrets.token_hex(16)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.create_run(request, key), range(8)))
    assert len({run.id for run in results}) == 1
    with Session(engine) as session:
        assert len(list(session.scalars(select(Run)))) == 1

    def start(_: int) -> str:
        try:
            return service.start(results[0].id, 1).status
        except ServiceError as error:
            return error.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(start, range(2)))
    assert sorted(statuses) == ["queued", "version_conflict"]
