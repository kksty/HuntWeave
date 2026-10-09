import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from threading import Lock
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.staticfiles import StaticFiles

from huntweave.access.body_limit import BodyLimitMiddleware
from huntweave.access.service import AccessService, BrowserSession
from huntweave.config import AppSettings
from huntweave.contracts.capabilities import Capabilities
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.runs import (
    LoginRequest,
    PortInput,
    ProjectCreate,
    ProjectView,
    RunCreate,
    RunView,
    ScopeCreate,
    ScopeView,
    TargetInput,
    TargetPreview,
    VersionRequest,
)
from huntweave.execution.client import get_capabilities
from huntweave.runs.inputs import expand_ports, preview_targets
from huntweave.runs.orchestration import OrchestrationService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine

PUBLIC = Path(__file__).resolve().parents[1] / "access" / "public"
FRONTEND = Path(__file__).resolve().parents[4] / "frontend" / "dist"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'",
}


def error_response(error: ServiceError) -> JSONResponse:
    headers = dict(SECURITY_HEADERS)
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(
        {"reason_code": error.reason_code}, status_code=error.status_code, headers=headers
    )


def create_app(settings: AppSettings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or AppSettings.from_env()
    app = FastAPI(title="HuntWeave", docs_url=None, redoc_url=None, openapi_url=None)
    engine_lock = Lock()

    def database() -> Engine:
        nonlocal engine
        with engine_lock:
            if engine is None:
                engine = connect_engine()
        return engine

    access = AccessService(database, settings)
    runs = RunService(database)
    orchestration = OrchestrationService(
        database, Path(os.environ.get("HUNTWEAVE_EVIDENCE_ROOT", "/evidence"))
    )

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError) -> Response:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> Response:
        # Pydantic normally echoes input. Never echo secrets or authorization contents.
        return JSONResponse(
            {
                "reason_code": "invalid_request",
                "errors": [
                    {"field": list(item["loc"]), "type": item["type"]} for item in error.errors()
                ],
            },
            status_code=422,
            headers=SECURITY_HEADERS,
        )

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request: Request, error: SQLAlchemyError) -> Response:
        return error_response(ServiceError("storage_unavailable", 503))

    @app.middleware("http")
    async def access_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path, method = request.url.path, request.method
        public = path in {"/login", "/login.js", "/login.css", "/auth/login"}
        try:
            if path == "/health/live" and method in {"GET", "HEAD"}:
                response: Response = JSONResponse({"status": "alive"})
            elif settings.current_access_key() is None:
                response = error_response(ServiceError("access_key_missing", 503))
            else:
                if not public:
                    csrf = None
                    if method not in {"GET", "HEAD", "OPTIONS"}:
                        csrf = request.headers.get("x-csrf-token", "")
                    browser = await run_in_threadpool(
                        access.authenticate, request.cookies.get(settings.cookie_name), csrf
                    )
                    request.state.browser = browser
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    if request.headers.get("origin") != settings.public_origin:
                        raise ServiceError("origin_invalid", 403)
                response = await call_next(request)
        except ServiceError as error:
            if (
                error.status_code == 401
                and method == "GET"
                and (path == "/" or path.startswith("/runs/"))
                and "text/html" in request.headers.get("accept", "")
            ):
                response = RedirectResponse("/login", status_code=303)
            else:
                response = error_response(error)
        except SQLAlchemyError:
            response = error_response(ServiceError("storage_unavailable", 503))
        response.headers.update(SECURITY_HEADERS)
        return response

    app.add_middleware(BodyLimitMiddleware)

    @app.get("/login", include_in_schema=False)
    def login_page() -> Response:
        return FileResponse(PUBLIC / "login.html", media_type="text/html")

    @app.get("/login.js", include_in_schema=False)
    def login_script() -> Response:
        return FileResponse(PUBLIC / "login.js", media_type="application/javascript")

    @app.get("/login.css", include_in_schema=False)
    def login_style() -> Response:
        return FileResponse(PUBLIC / "login.css", media_type="text/css")

    @app.post("/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response) -> BrowserSession:
        token, browser = access.login(
            payload.access_key.get_secret_value(),
            request.client.host if request.client else "unknown",
            request.cookies.get(settings.cookie_name),
        )
        response.set_cookie(
            settings.cookie_name,
            token,
            max_age=settings.absolute_seconds,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return browser

    @app.get("/auth/session")
    def current_session(request: Request) -> BrowserSession:
        return request.state.browser  # type: ignore[no-any-return]

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        access.logout(request.state.browser.id)
        response = Response(status_code=204)
        response.delete_cookie(
            settings.cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/v1/projects")
    def projects() -> list[ProjectView]:
        return runs.projects()

    @app.post("/api/v1/projects", status_code=201)
    def create_project(payload: ProjectCreate) -> ProjectView:
        return runs.create_project(payload)

    @app.post("/api/v1/targets/preview")
    def targets_preview(payload: TargetInput) -> TargetPreview:
        return preview_targets(payload.text)

    @app.post("/api/v1/ports/preview")
    def ports_preview(payload: PortInput) -> dict[str, object]:
        return {"profile": payload.profile, "transport": "tcp", "ports": expand_ports(payload)}

    @app.post("/api/v1/scopes", status_code=201)
    def create_scope(payload: ScopeCreate) -> ScopeView:
        return runs.create_scope(payload)

    @app.get("/api/v1/scopes/{scope_id}")
    def scope(scope_id: UUID) -> ScopeView:
        return runs.scope(scope_id)

    @app.get("/api/v1/runs")
    def list_runs() -> list[RunView]:
        return runs.runs()

    @app.post("/api/v1/runs", status_code=201)
    def create_run(
        payload: RunCreate, idempotency_key: str = Header(alias="Idempotency-Key")
    ) -> RunView:
        return runs.create_run(payload, idempotency_key)

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: UUID) -> RunView:
        return runs.run(run_id)

    @app.post("/api/v1/runs/{run_id}/start")
    def start_run(run_id: UUID, payload: VersionRequest) -> RunView:
        return runs.start(run_id, payload.version)

    @app.get("/api/v1/runs/{run_id}/snapshot")
    def run_snapshot(run_id: UUID) -> dict[str, object]:
        return orchestration.snapshot(run_id)

    @app.get("/api/v1/runs/{run_id}/event-history")
    def event_history(
        run_id: UUID,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        return orchestration.history(run_id, after, limit)

    @app.get("/api/v1/runs/{run_id}/resume-preview")
    def resume_preview(run_id: UUID) -> dict[str, object]:
        return orchestration.preview(run_id)

    @app.post("/api/v1/runs/{run_id}/pause")
    def pause_run(run_id: UUID, payload: VersionRequest) -> dict[str, object]:
        return orchestration.control(run_id, "pause", payload.version)

    @app.post("/api/v1/runs/{run_id}/resume")
    def resume_run(run_id: UUID, payload: VersionRequest) -> dict[str, object]:
        return orchestration.control(run_id, "resume", payload.version)

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: UUID, payload: VersionRequest) -> dict[str, object]:
        return orchestration.control(run_id, "cancel", payload.version)

    @app.post("/api/v1/runs/{run_id}/close")
    def close_run(run_id: UUID, payload: VersionRequest) -> dict[str, object]:
        return orchestration.control(run_id, "close", payload.version)

    @app.get("/api/v1/evidence/{evidence_id}")
    def evidence(
        evidence_id: UUID,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=65536, ge=1, le=65536),
    ) -> dict[str, object]:
        return orchestration.evidence(evidence_id, offset, limit)

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(
        run_id: UUID, request: Request, after: int = Query(default=0, ge=0)
    ) -> Response:
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor is not None:
            try:
                after = int(raw_cursor)
            except ValueError:
                raise ServiceError("invalid_event_cursor", 422) from None
        # Validate before response headers are sent, so invalid/ahead cursors get a clear 409.
        await run_in_threadpool(orchestration.history, run_id, after, 1)
        token = request.cookies.get(settings.cookie_name)

        async def stream() -> AsyncIterator[str]:
            cursor = after
            next_auth = 0.0
            next_heartbeat = 0.0
            while not await request.is_disconnected():
                try:
                    if time.monotonic() >= next_auth:
                        await run_in_threadpool(access.authenticate, token)
                        next_auth = time.monotonic() + 15
                    page = await run_in_threadpool(orchestration.history, run_id, cursor, 100)
                    for event in page["events"]:
                        cursor = event["cursor"]
                        data = json.dumps(event, ensure_ascii=False)
                        yield f"id: {cursor}\nevent: audit\ndata: {data}\n\n"
                    if time.monotonic() >= next_heartbeat:
                        yield 'event: heartbeat\ndata: {"demonstration":true}\n\n'
                        next_heartbeat = time.monotonic() + 5
                except ServiceError:
                    yield "event: session_expired\ndata: {}\n\n"
                    return
                except SQLAlchemyError:
                    yield "event: storage_unavailable\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/v1/system/capabilities")
    def capabilities() -> Capabilities:
        return get_capabilities()

    @app.get("/openapi.json", include_in_schema=False)
    def schema() -> dict[str, object]:
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    def docs() -> Response:
        return RedirectResponse("/openapi.json", status_code=303)

    @app.get("/", include_in_schema=False)
    @app.get("/runs/{run_id}", include_in_schema=False)
    def business_page() -> Response:
        if not (FRONTEND / "index.html").is_file():
            return error_response(ServiceError("frontend_unavailable", 503))
        return FileResponse(FRONTEND / "index.html", media_type="text/html")

    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets", check_dir=False), name="assets")
    return app
