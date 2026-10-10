import hmac
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from huntweave.config import SandboxSettings, runner_token
from huntweave.contracts.capabilities import Capabilities, SandboxManagement
from huntweave.contracts.execution import (
    CallCancellation,
    ExecutionRecord,
    ExecutionRequest,
    LeaseRenewal,
)
from huntweave.execution.capabilities import evaluate
from huntweave.execution.fake import FakeRunner, RunnerRejected
from huntweave.execution.sandbox import RuntimeUnavailable, SandboxManager, SandboxRejected


def _open_sandbox_manager(settings: SandboxSettings) -> SandboxManager:
    """Import the Docker adapter only when management was explicitly enabled.

    A deployment that keeps sandbox management disabled never builds a client and never imports
    the Docker SDK, so a default deployment has no container management at all (ADR-0010).
    """
    from huntweave.execution.dockerruntime import open_sandbox_manager

    return open_sandbox_manager(settings)


def create_runner(
    token: str | None = None,
    *,
    state_dir: Path | None = None,
    evidence_dir: Path | None = None,
    sandbox_settings: SandboxSettings | None = None,
    sandbox_factory: Callable[[SandboxSettings], SandboxManager] | None = None,
) -> FastAPI:
    token_bytes = (token or runner_token()).encode("utf-8")
    ledger: FakeRunner | None = None
    creation_lock = threading.Lock()
    settings = sandbox_settings or SandboxSettings.from_env()
    if state_dir is not None or evidence_dir is not None:
        # The directories this process uses for its ledgers are the same ones a sandbox ledger
        # and its evidence live in, so moving one moves the other instead of splitting them.
        settings = replace(
            settings,
            state_dir=state_dir or settings.state_dir,
            evidence_dir=evidence_dir or settings.evidence_dir,
        )
    sandbox: SandboxManager | None = None
    sandbox_failure: str | None = None
    sandbox_lock = threading.Lock()

    def execution() -> FakeRunner:
        nonlocal ledger
        with creation_lock:
            if ledger is None:
                ledger = FakeRunner(
                    settings.state_dir,
                    settings.evidence_dir,
                )
            return ledger

    def sandbox_state() -> tuple[SandboxManagement, str | None]:
        """Open the trusted manager on first use and report what it can really do.

        Enabled is not the same as usable: a Runner whose socket is missing, whose profile is not
        in the image or whose state directory cannot be written reports `unavailable` with the
        reason instead of presenting management as ready.
        """
        nonlocal sandbox, sandbox_failure
        if not settings.enabled:
            return "disabled", None
        with sandbox_lock:
            if sandbox is None and sandbox_failure is None:
                opener = sandbox_factory or _open_sandbox_manager
                try:
                    sandbox = opener(settings)
                except SandboxRejected as error:
                    sandbox_failure = error.reason_code
                except RuntimeUnavailable:
                    # The trusted manager could not open or reach the container runtime: an
                    # operator gets the reason instead of a failed request.
                    sandbox_failure = "sandbox_runtime_unreachable"
                except (OSError, ValueError):
                    sandbox_failure = "sandbox_state_unwritable"
            if sandbox is None:
                return "unavailable", sandbox_failure
        observation = sandbox.observe()
        if observation.available:
            return "ready", None
        return "unavailable", observation.reason_code

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if ledger is not None:
            ledger.close()
        if sandbox is not None:
            sandbox.close()

    app = FastAPI(
        title="HuntWeave Runner", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.exception_handler(RunnerRejected)
    async def rejected(request: Request, exc: RunnerRejected) -> JSONResponse:
        return JSONResponse({"reason_code": exc.reason_code}, status_code=409)

    @app.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response: Response
        if request.url.path == "/health/live" and request.method in {"GET", "HEAD"}:
            response = JSONResponse({"status": "alive"})
        elif not hmac.compare_digest(
            request.headers.get("authorization", "").encode("utf-8"), b"Bearer " + token_bytes
        ):
            response = JSONResponse(
                {"reason_code": "runner_authentication_required"}, status_code=401
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/capabilities")
    def capabilities() -> Capabilities:
        # Readiness is observed here, not declared: the endpoint still answers when the ledger
        # cannot be opened, and says which chain is unusable instead of reporting healthy.
        try:
            execution()
            fake_ready = True
        except (OSError, RunnerRejected):
            fake_ready = False
        management, reason = sandbox_state()
        return evaluate(
            fake_execution_ready=fake_ready,
            sandbox_management=management,
            sandbox_reason_code=reason,
            now=datetime.now(UTC),
        )

    @app.post("/v1/calls")
    def submit(request: ExecutionRequest) -> ExecutionRecord:
        return execution().submit(request)

    @app.get("/v1/calls/{call_id}", response_model=None)
    def query(call_id: UUID) -> ExecutionRecord | JSONResponse:
        record = execution().query(call_id)
        return (
            record
            if record is not None
            else JSONResponse({"reason_code": "call_not_found"}, status_code=404)
        )

    @app.post("/v1/calls/{call_id}/renew")
    def renew(call_id: UUID, request: LeaseRenewal) -> ExecutionRecord:
        return execution().renew(call_id, request.lease_generation, request.lease_expires_at)

    @app.post("/v1/calls/{call_id}/cancel")
    def cancel(call_id: UUID, request: CallCancellation) -> ExecutionRecord:
        return execution().cancel(call_id, request.lease_generation)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def unsupported(path: str) -> JSONResponse:
        return JSONResponse({"reason_code": "execution_not_implemented"}, status_code=501)

    return app
