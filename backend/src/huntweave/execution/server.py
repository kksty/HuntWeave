import hmac
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from huntweave.config import runner_token
from huntweave.contracts.capabilities import Capabilities
from huntweave.contracts.execution import (
    CallCancellation,
    ExecutionRecord,
    ExecutionRequest,
    LeaseRenewal,
)
from huntweave.execution.capabilities import evaluate
from huntweave.execution.fake import FakeRunner, RunnerRejected


def create_runner(
    token: str | None = None, *, state_dir: Path | None = None, evidence_dir: Path | None = None
) -> FastAPI:
    token_bytes = (token or runner_token()).encode("utf-8")
    ledger: FakeRunner | None = None
    creation_lock = threading.Lock()

    def execution() -> FakeRunner:
        nonlocal ledger
        with creation_lock:
            if ledger is None:
                ledger = FakeRunner(
                    state_dir
                    or Path(os.environ.get("HUNTWEAVE_RUNNER_STATE_DIR", "/runner-state")),
                    evidence_dir or Path(os.environ.get("HUNTWEAVE_EVIDENCE_DIR", "/evidence")),
                )
            return ledger

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if ledger is not None:
            ledger.close()

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
        return evaluate(fake_execution_ready=fake_ready, now=datetime.now(UTC))

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
