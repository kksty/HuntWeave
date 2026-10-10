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
from huntweave.execution.fake import FakeRunner
from huntweave.execution.ledger import CallLedger, RunnerRejected, RunnerUnavailable
from huntweave.execution.real import RealRunner
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
    ledgers: dict[str, CallLedger] = {}

    def execution() -> FakeRunner:
        fake = ledger_for("fake-p0-v1")
        assert isinstance(fake, FakeRunner)
        return fake

    def ledger_for(profile: str) -> CallLedger:
        """The executor that owns this profile's calls.

        Both executors share the call ledger's rules but not its file, and a ticket chooses its
        side by naming an execution profile. A real profile with no management enabled is refused
        rather than served by the demonstration side: a deployment that cannot act for real does
        not get to pretend it did (issue #17, criterion 6).

        The trusted manager is opened *outside* this lock: the manager's own construction takes
        that lock, and needing it twice in one thread is a deadlock, not a cache.
        """
        with sandbox_lock:
            known = ledgers.get(profile)
        if known is not None:
            return known
        manager = sandbox_manager() if profile == "real-lab-v1" else None
        with sandbox_lock:
            existing = ledgers.get(profile)
            if existing is not None:
                return existing
            if profile == "fake-p0-v1":
                ledgers[profile] = FakeRunner(settings.state_dir, settings.evidence_dir)
            elif profile == "real-lab-v1":
                if manager is None:
                    raise RunnerRejected("real_execution_disabled")
                ledgers[profile] = RealRunner(
                    settings.state_dir,
                    settings.evidence_dir,
                    manager=manager,
                    profile=manager.profile,
                )
            else:
                raise RunnerRejected("execution_profile_unknown")
            return ledgers[profile]

    def known_ledgers() -> list[CallLedger]:
        """Every executor this deployment can route a query to, real side first."""
        found: list[CallLedger] = []
        for profile in ("real-lab-v1", "fake-p0-v1"):
            try:
                found.append(ledger_for(profile))
            except (RunnerRejected, RunnerUnavailable, SandboxRejected, RuntimeUnavailable):
                continue
        return found

    def sandbox_manager() -> SandboxManager | None:
        """Open the trusted manager on first use, or explain why this deployment has none."""
        nonlocal sandbox, sandbox_failure
        if not settings.enabled:
            return None
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
        return sandbox

    def sandbox_state() -> tuple[SandboxManagement, str | None]:
        """Report whether this deployment can really manage sandboxes right now.

        Enabled is not the same as usable: a Runner whose socket is missing, whose profile is not
        in the image or whose state directory cannot be written reports `unavailable` with the
        reason instead of presenting management as ready.
        """
        manager = sandbox_manager()
        if manager is None:
            if sandbox_failure is None:
                return "disabled", None
            return "unavailable", sandbox_failure
        observation = manager.observe()
        if observation.available:
            return "ready", None
        return "unavailable", observation.reason_code

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        for ledger in ledgers.values():
            ledger.close()
        if sandbox is not None:
            sandbox.close()

    app = FastAPI(
        title="HuntWeave Runner", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.exception_handler(RunnerRejected)
    async def rejected(request: Request, exc: RunnerRejected) -> JSONResponse:
        return JSONResponse({"reason_code": exc.reason_code}, status_code=409)

    @app.exception_handler(RunnerUnavailable)
    async def unavailable(request: Request, exc: RunnerUnavailable) -> JSONResponse:
        # 503 keeps the intent with the control plane: this side cannot take work *now*, and a
        # call must not be settled as denied because the runtime blipped.
        return JSONResponse({"reason_code": exc.reason_code}, status_code=503)

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
        # The ticket names the side that serves it, and only that side accepts it.
        return ledger_for(request.execution_profile).submit(request)

    @app.get("/v1/calls/{call_id}", response_model=None)
    def query(call_id: UUID) -> ExecutionRecord | JSONResponse:
        for candidate in known_ledgers():
            record = candidate.query(call_id)
            if record is not None:
                return record
        return JSONResponse({"reason_code": "call_not_found"}, status_code=404)

    @app.post("/v1/calls/{call_id}/renew")
    def renew(call_id: UUID, request: LeaseRenewal) -> ExecutionRecord:
        return route(
            call_id,
            lambda ledger: ledger.renew(
                call_id, request.lease_generation, request.lease_expires_at
            ),
        )

    @app.post("/v1/calls/{call_id}/cancel")
    def cancel(call_id: UUID, request: CallCancellation) -> ExecutionRecord:
        return route(call_id, lambda ledger: ledger.cancel(call_id, request.lease_generation))

    def route(call_id: UUID, call: Callable[[CallLedger], ExecutionRecord]) -> ExecutionRecord:
        """Ask each executor that could own this call; the one that knows it answers.

        Only "no such call" moves on to the next executor: any other refusal is that executor's
        verdict on a call it does own, and is reported as itself.
        """
        for candidate in known_ledgers():
            try:
                return call(candidate)
            except RunnerRejected as refusal:
                if refusal.reason_code != "call_not_found":
                    raise
        raise RunnerRejected("call_not_found")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def unsupported(path: str) -> JSONResponse:
        return JSONResponse({"reason_code": "execution_not_implemented"}, status_code=501)

    return app
