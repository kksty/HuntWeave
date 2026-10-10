import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID

import httpx
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
from huntweave.api.facts import create_facts_router
from huntweave.api.readiness import CapabilityProbe
from huntweave.config import CONSOLE_BUILD, AppSettings
from huntweave.contracts.capabilities import Capabilities, SandboxManagement
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.execution import ExecutionObservation, RunRuntimeView
from huntweave.contracts.orchestration import (
    EventPage,
    EvidenceView,
    ReconciliationResult,
    ReconciliationVerdict,
    ResumePreview,
    RunSnapshot,
)
from huntweave.contracts.retention import (
    RetentionReport,
    RetentionRequest,
    RetentionView,
)
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
from huntweave.execution.client import (
    RetentionAction,
    RetentionTargetKind,
    RunnerClient,
    get_capabilities,
)
from huntweave.runs.dispatch import read_observation
from huntweave.runs.inputs import expand_ports, preview_targets
from huntweave.runs.orchestration import OrchestrationService
from huntweave.runs.retention import RetentionService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine

PUBLIC = Path(__file__).resolve().parents[1] / "access" / "public"
FRONTEND = CONSOLE_BUILD
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


def unobserved_runtime(run_id: UUID, reason_code: str) -> RunRuntimeView:
    """An execution-side read the platform could not take, stated as exactly that.

    `available=false` with a reason is the honest answer. The console renders it as an observation
    gap, so an operator never reads "we could not ask" as "there is nothing out there".
    """
    return RunRuntimeView(
        available=False,
        reason_code=reason_code,
        sandbox_management="unavailable",
        observed_at=datetime.now(UTC),
        run_id=run_id,
    )


def unobserved_retention(
    reason_code: str, management: SandboxManagement = "unavailable"
) -> RetentionView:
    """A retention read the platform could not take, with the same honesty as `unobserved_runtime`.

    The limits are still stated — they are the policy this deployment runs under, not the execution
    side's answer — while every list stays empty, so a reader sees "no answer" rather than "an empty
    cache".
    """
    return RetentionView.unavailable(reason_code, management=management)


def runner_reason(error: httpx.HTTPStatusError) -> tuple[str, int]:
    """The execution side's own refusal, kept as its own reason code and status.

    A refusal is not rewritten into a control-plane reason: 409 is a decision the execution side
    made about this request, and anything else means it could not answer now. A Runner that does not
    serve the retention routes at all says exactly that, instead of borrowing the sentence for a
    missing *read* interface.
    """
    reason = "runner_unavailable"
    try:
        body = error.response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("reason_code"), str):
        reason = str(body["reason_code"])
    elif error.response.status_code in {404, 501}:
        reason = "retention_unsupported"
    return reason, 409 if error.response.status_code == 409 else 503


def create_app(
    settings: AppSettings | None = None,
    engine: Engine | None = None,
    observation_reader: Callable[[UUID], ExecutionObservation | None] | None = None,
    capability_reader: Callable[[], Capabilities] | None = None,
    runtime_reader: Callable[[UUID], RunRuntimeView] | None = None,
    retention_reader: Callable[[], RetentionView] | None = None,
    retention_writer: Callable[
        [str, str, str, RetentionRequest], RetentionView | RetentionReport
    ]
    | None = None,
) -> FastAPI:
    settings = settings or AppSettings.from_env()
    app = FastAPI(title="HuntWeave", docs_url=None, redoc_url=None, openapi_url=None)
    engine_lock = Lock()
    capabilities_probe = CapabilityProbe(capability_reader or get_capabilities)

    def database() -> Engine:
        nonlocal engine
        with engine_lock:
            if engine is None:
                engine = connect_engine()
        return engine

    # The facts, observations and lineage routes ship as a router factory
    # (`docs/agents/p2-execution-batches.md` section 4): route assembly stays in this file, and this
    # is the one line that mounts them. Every path it registers is new — none of them shadows an
    # inline handler here.
    app.include_router(create_facts_router(database))

    def ledger_observation(call_id: UUID) -> ExecutionObservation | None:
        # Read-only access to the Runner's own ledger: the verdict needs the execution side's
        # facts, and the app never gains any container or execution capability from reading.
        return read_observation(RunnerClient(), call_id)

    observation_source = observation_reader or ledger_observation

    def execution_runtime(run_id: UUID) -> RunRuntimeView:
        """Ask the execution side what this Run currently has outside the platform.

        A refusal or an unreachable execution side is reported as an observation gap with the
        reason, never as "this Run has nothing running": those are different statements, and only
        one of them this process is entitled to make (issue #19 clause 1).
        """
        try:
            return RunnerClient().run_runtime(run_id)
        except httpx.HTTPStatusError as failure:
            # A Runner that does not serve this read is a deployment fact, not a Run fact.
            reason = (
                "sandbox_state_unsupported"
                if failure.response.status_code in {404, 501}
                else "runner_unavailable"
            )
            return unobserved_runtime(run_id, reason)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            return unobserved_runtime(run_id, "runner_unavailable")

    runtime_source = runtime_reader or execution_runtime

    def execution_retention() -> RetentionView:
        """Ask the execution side what the retention policy currently holds and would reclaim.

        As with the Run runtime read, a refusal or an unreachable side is an observation gap with
        the reason, never an empty cache: "we could not ask" and "nothing is retained" are different
        statements and only one of them this process is entitled to make.
        """
        try:
            return RunnerClient().retention()
        except httpx.HTTPStatusError as failure:
            reason = (
                "retention_unsupported"
                if failure.response.status_code in {404, 501}
                else "runner_unavailable"
            )
            return unobserved_retention(reason)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            return unobserved_retention("runner_unavailable")

    retention_source = retention_reader or execution_retention

    def execution_retention_action(
        kind: str,
        target: str,
        action: str,
        request: RetentionRequest,
    ) -> RetentionView | RetentionReport:
        """Send one retention action to the execution side and keep its own answer.

        A refusal travels back as the execution side's reason code and status: this process is not
        the one that decides whether a volume may be removed.
        """
        client = RunnerClient()
        try:
            if action == "sweep":
                return client.sweep_retention(request)
            # Both halves of the path are named explicitly: a kind or action this process does not
            # know is refused rather than quietly sent as something else.
            kinds: dict[str, RetentionTargetKind] = {
                "versions": "versions",
                "artifacts": "artifacts",
            }
            actions: dict[str, RetentionAction] = {
                "pin": "pin",
                "unpin": "unpin",
                "delete": "delete",
            }
            if kind not in kinds or action not in actions:
                raise ServiceError("invalid_request", 422)
            return client.retention_action(
                target_kind=kinds[kind],
                target=target,
                action=actions[action],
                request=request,
            )
        except httpx.HTTPStatusError as failure:
            reason, status = runner_reason(failure)
            raise ServiceError(reason, status) from None
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            raise ServiceError("runner_unavailable", 503) from None

    retention_action_source = retention_writer or execution_retention_action
    retention = RetentionService(database)

    def operator_request(payload: RetentionRequest, browser: BrowserSession) -> RetentionRequest:
        """The action as this process sends it: the actor is the session, not a caller's text."""
        return RetentionRequest(note=payload.note, actor=str(browser.id))

    def recorded(
        result: RetentionView | RetentionReport, operator_session_id: UUID, note: str
    ) -> RetentionView | RetentionReport:
        """Keep the business-side trace of a decision the execution side just reported.

        The decision is the execution side's own record — identifier, target, affected Runs and what
        was really deleted — so nothing here can claim a removal that did not happen. The note stays
        the operator's own words as this process received them.
        """
        decision = (
            result.decision
            if isinstance(result, RetentionReport)
            else (result.decisions[0] if result.decisions else None)
        )
        if decision is not None:
            retention.record(decision, operator_session_id, note)
        return result

    def observed(view: RunView) -> RunView:
        """Report the execution chain this Run would use as it is now, never as assumed.

        `execution_ready` describes the fixed fake execution path (P0 spec section 1); it follows
        the execution side's own answer, so an unreachable Runner turns it false instead of
        leaving the console showing a ready platform.
        """
        ready = capabilities_probe.current().fake_execution_ready
        return view.model_copy(update={"execution_ready": ready})

    access = AccessService(database, settings)
    # Opening a Run that acts for real asks the execution side *now*: ADR-0010's gates are a
    # precondition of that Run, not a value this process remembers from a previous answer.
    runs = RunService(
        database, readiness=lambda: capabilities_probe.current().real_execution_ready
    )
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
        # The key submission is not rate limited: the key is a value this deployment generates, so
        # guessing it is not what a per-address throttle would prevent, and a throttle here only
        # locks the operator out of their own platform (see access/service.py).
        token, browser = access.login(
            payload.access_key.get_secret_value(),
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
        return [observed(item) for item in runs.runs()]

    @app.post("/api/v1/runs", status_code=201)
    def create_run(
        payload: RunCreate, idempotency_key: str = Header(alias="Idempotency-Key")
    ) -> RunView:
        return observed(runs.create_run(payload, idempotency_key))

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: UUID) -> RunView:
        return observed(runs.run(run_id))

    @app.post("/api/v1/runs/{run_id}/start")
    def start_run(run_id: UUID, payload: VersionRequest) -> RunView:
        return observed(runs.start(run_id, payload.version))

    @app.get("/api/v1/runs/{run_id}/snapshot")
    def run_snapshot(run_id: UUID) -> RunSnapshot:
        snapshot = RunSnapshot.model_validate(orchestration.snapshot(run_id))
        return snapshot.model_copy(
            update={"run": observed(snapshot.run), "runtime": runtime_source(run_id)}
        )

    @app.get("/api/v1/runs/{run_id}/event-history")
    def event_history(
        run_id: UUID,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> EventPage:
        return EventPage.model_validate(orchestration.history(run_id, after, limit))

    @app.get("/api/v1/runs/{run_id}/resume-preview")
    def resume_preview(run_id: UUID) -> ResumePreview:
        return ResumePreview.model_validate(orchestration.preview(run_id))

    @app.post("/api/v1/runs/{run_id}/pause")
    def pause_run(run_id: UUID, payload: VersionRequest) -> RunView:
        return observed(
            RunView.model_validate(orchestration.control(run_id, "pause", payload.version))
        )

    @app.post("/api/v1/runs/{run_id}/resume")
    def resume_run(run_id: UUID, payload: VersionRequest) -> RunView:
        return observed(
            RunView.model_validate(orchestration.control(run_id, "resume", payload.version))
        )

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: UUID, payload: VersionRequest) -> RunView:
        return observed(
            RunView.model_validate(orchestration.control(run_id, "cancel", payload.version))
        )

    @app.post("/api/v1/runs/{run_id}/close")
    def close_run(run_id: UUID, payload: VersionRequest) -> RunView:
        return observed(
            RunView.model_validate(orchestration.control(run_id, "close", payload.version))
        )

    @app.post("/api/v1/runs/{run_id}/calls/{call_id}/reconciliation")
    def reconcile_call(
        run_id: UUID, call_id: UUID, payload: ReconciliationVerdict, request: Request
    ) -> ReconciliationResult:
        browser: BrowserSession = request.state.browser
        # The ledger is read before, and outside, the business transaction that records the
        # verdict: the operator's decision is never the evidence for its own outcome.
        result = ReconciliationResult.model_validate(
            orchestration.reconcile(
                run_id, call_id, payload, browser.id, observation_source(call_id)
            )
        )
        return result.model_copy(update={"run": observed(result.run)})

    @app.get("/api/v1/evidence/{evidence_id}")
    def evidence(
        evidence_id: UUID,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=65536, ge=1, le=65536),
    ) -> EvidenceView:
        return EvidenceView.model_validate(orchestration.evidence(evidence_id, offset, limit))

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
                        # The stream heartbeat carries the mode the platform is really in, not a
                        # fixed demonstration claim; the observation is read off the event loop.
                        mode = (await run_in_threadpool(capabilities_probe.current)).mode
                        yield f'event: heartbeat\ndata: {{"mode":"{mode}"}}\n\n'
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
        # The execution side's own answer, or an explicit observation gap: never a ready state
        # invented by the control side.
        return capabilities_probe.current()

    @app.get("/api/v1/retention")
    def retention_state() -> RetentionView:
        """What the retention policy holds, what counts as a candidate, and what it would reclaim.

        Read-only, and never the control plane's invention: a deployment without management, or a
        Runner that cannot be reached, answers with the gap instead of an empty cache.
        """
        return retention_source()

    @app.post("/api/v1/retention/versions/{environment_key}/pin")
    def pin_version(
        environment_key: str, payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        return recorded(
            retention_action_source(
                "versions",
                environment_key,
                "pin",
                operator_request(payload, request.state.browser),
            ),
            request.state.browser.id,
            payload.note,
        )

    @app.post("/api/v1/retention/versions/{environment_key}/unpin")
    def unpin_version(
        environment_key: str, payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        return recorded(
            retention_action_source(
                "versions",
                environment_key,
                "unpin",
                operator_request(payload, request.state.browser),
            ),
            request.state.browser.id,
            payload.note,
        )

    @app.post("/api/v1/retention/versions/{environment_key}/delete")
    def delete_version(
        environment_key: str, payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        return recorded(
            retention_action_source(
                "versions",
                environment_key,
                "delete",
                operator_request(payload, request.state.browser),
            ),
            request.state.browser.id,
            payload.note,
        )

    @app.post("/api/v1/retention/artifacts/{artifact_id}/pin")
    def pin_artifact(
        artifact_id: UUID, payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        return recorded(
            retention_action_source(
                "artifacts",
                str(artifact_id),
                "pin",
                operator_request(payload, request.state.browser),
            ),
            request.state.browser.id,
            payload.note,
        )

    @app.post("/api/v1/retention/artifacts/{artifact_id}/unpin")
    def unpin_artifact(
        artifact_id: UUID, payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        return recorded(
            retention_action_source(
                "artifacts",
                str(artifact_id),
                "unpin",
                operator_request(payload, request.state.browser),
            ),
            request.state.browser.id,
            payload.note,
        )

    @app.post("/api/v1/retention/artifacts/{artifact_id}/delete")
    def delete_artifact(
        artifact_id: UUID, payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        return recorded(
            retention_action_source(
                "artifacts",
                str(artifact_id),
                "delete",
                operator_request(payload, request.state.browser),
            ),
            request.state.browser.id,
            payload.note,
        )

    @app.post("/api/v1/retention/sweep")
    def sweep_retention(
        payload: RetentionRequest, request: Request
    ) -> RetentionView | RetentionReport:
        """Apply the reclaim preview the operator just read."""
        return recorded(
            retention_action_source(
                "deployment", "preview", "sweep", operator_request(payload, request.state.browser)
            ),
            request.state.browser.id,
            payload.note,
        )

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
