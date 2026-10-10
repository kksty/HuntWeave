import hmac
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from huntweave.config import SandboxSettings, retention_sweep_seconds, runner_token
from huntweave.contracts.capabilities import Capabilities, SandboxManagement
from huntweave.contracts.execution import (
    CallCancellation,
    ExecutionRecord,
    ExecutionRequest,
    LeaseRenewal,
    RunRuntimeView,
)
from huntweave.contracts.retention import (
    RetentionReport,
    RetentionRequest,
    RetentionView,
)
from huntweave.execution.capabilities import evaluate
from huntweave.execution.fake import FakeRunner
from huntweave.execution.ledger import CallLedger, RunnerRejected, RunnerUnavailable
from huntweave.execution.operator import project_run_state, unavailable_run_state
from huntweave.execution.real import RealRunner
from huntweave.execution.retention import (
    REASON_OPERATOR,
    RetentionStore,
    remove_artifact,
)
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
    # The retention ledger lives beside the sandbox ledger and outlives any one call: pins and the
    # cache are deployment state, not a Run's (issue #20). It is opened on first use, so a
    # deployment whose state directory is unwritable still starts and reports the gap.
    retention_state: dict[str, RetentionStore] = {}
    retention_failure: list[str] = []
    retention_watchdog: dict[str, Any] = {}

    def retention_store() -> RetentionStore | None:
        if retention_state:
            return retention_state["store"]
        if retention_failure:
            return None
        try:
            retention_state["store"] = RetentionStore(settings.state_dir)
        except OSError:
            retention_failure.append("sandbox_state_unwritable")
            return None
        # Opening the ledger is what makes the policy live: from here on the TTL and the capacity
        # bound the cache whether or not anybody is watching the console.
        start_retention_watchdog()
        return retention_state["store"]

    def opened_retention() -> RetentionStore:
        store = retention_store()
        if store is None:
            raise RunnerRejected(retention_failure[0])
        return store

    def sweep_retention_now(
        store: RetentionStore, manager: SandboxManager
    ) -> RetentionReport | None:
        """Apply the retention policy once, measuring sizes only if something is actually due.

        The cheap decision is taken from the sizes the ledger already recorded; a fresh reading of
        the runtime is worth taking only when that decision selects something. Nothing at all is
        written when nothing is due — a policy that records a decision every tick would push the
        operator's own decisions out of the trace.
        """
        if not store.preview().to_delete:
            return None
        sizes = manager.volume_usage([record.resource_id for record in store.retained()])
        return store.sweep(
            actor=None,
            note="",
            remove=remove_artifact(manager),
            sizes=sizes,
            in_use=manager.in_use_volumes(),
        )

    def start_retention_watchdog() -> None:
        """Let the policy bound the cache on its own, unless the deployment turned that off.

        The TTL and the capacity are what keep a deployment's disk from filling with one-off
        environments (PROJECT.md section 10.5); a policy that only runs when somebody opens the
        console is a suggestion. Pinned material and material an instance still holds are protected
        by the same preview the operator reads, so an unattended sweep cannot take either.
        """
        interval = retention_sweep_seconds()
        if interval <= 0 or retention_watchdog:
            return
        stopping = threading.Event()

        def watch() -> None:
            while not stopping.wait(interval):
                try:
                    manager, store = sandbox_manager(), retention_store()
                    if manager is None or store is None:
                        continue
                    sweep_retention_now(store, manager)
                except Exception:
                    # A tick that could not run is not a reason to stop bounding the cache: the
                    # next one reads the ledger again.
                    continue

        thread = threading.Thread(target=watch, name="retention-watchdog", daemon=True)
        thread.start()
        retention_watchdog.update({"stop": stopping, "thread": thread})

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
                    retention=retention_store(),
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
        stopping = retention_watchdog.get("stop")
        if stopping is not None:
            stopping.set()
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

    @app.get("/v1/runs/{run_id}/sandbox-state")
    def sandbox_state_of_run(run_id: UUID) -> RunRuntimeView:
        """What this deployment can currently say about one Run's containers and gateways.

        A deployment without management answers with `available=false` and the reason, which is not
        the same as "nothing is running" — the console renders it as an observation gap. The read is
        a projection of the manager's records plus its runtime audit; it grants no capability and
        starts, stops or reclaims nothing.
        """
        management, reason = sandbox_state()
        manager = sandbox_manager()
        if manager is None:
            return unavailable_run_state(
                run_id, management=management, reason_code=reason, profile_id=None
            )
        return project_run_state(
            manager.run_state(run_id), profile_id=manager.profile.profile_id, reason_code=reason
        )

    @app.post("/v1/calls")
    def submit(request: ExecutionRequest) -> ExecutionRecord:
        # The ticket names the side that serves it, and only that side accepts it.
        return ledger_for(request.execution_profile).submit(request)

    # -- selective retention (issue #20) ------------------------------------------------------

    def retention_targets() -> tuple[
        SandboxManager, RetentionStore, dict[str, int | None], frozenset[str]
    ]:
        """The trusted manager a retention action needs, plus the facts a preview is computed from.

        Without management there is nothing this deployment owns to reclaim, so a retention write is
        refused rather than answered with an empty success.
        """
        manager = sandbox_manager()
        if manager is None:
            raise RunnerRejected("sandbox_management_disabled")
        store = opened_retention()
        sizes = manager.volume_usage([record.resource_id for record in store.retained()])
        return manager, store, sizes, manager.in_use_volumes()

    def retention_read() -> RetentionView:
        """The current retention answer: candidates, held artifacts, preview and trace."""
        management, reason = sandbox_state()
        manager = sandbox_manager()
        store = retention_store()
        if manager is None or store is None:
            return RetentionView.unavailable(
                reason or (retention_failure[0] if retention_failure else None)
                or "sandbox_management_disabled",
                management=management,
            )
        sizes = manager.volume_usage([record.resource_id for record in store.retained()])
        return store.view(
            sizes=sizes,
            in_use=manager.in_use_volumes(),
            available=True,
            sandbox_management=management,
        )

    @app.get("/v1/retention")
    def retention_view() -> RetentionView:
        return retention_read()

    @app.post("/v1/retention/versions/{environment_key}/pin")
    def pin_version(environment_key: str, request: RetentionRequest) -> RetentionView:
        _, store, sizes, in_use = retention_targets()
        return store.pin(
            target_kind="version",
            target=environment_key,
            actor=request.actor,
            note=request.note,
            sizes=sizes,
            in_use=in_use,
        )

    @app.post("/v1/retention/versions/{environment_key}/unpin")
    def unpin_version(environment_key: str, request: RetentionRequest) -> RetentionView:
        _, store, sizes, in_use = retention_targets()
        return store.unpin(
            target_kind="version",
            target=environment_key,
            actor=request.actor,
            note=request.note,
            sizes=sizes,
            in_use=in_use,
        )

    @app.post("/v1/retention/versions/{environment_key}/delete")
    def delete_version(environment_key: str, request: RetentionRequest) -> RetentionReport:
        manager, store, sizes, in_use = retention_targets()
        return store.delete_artifacts(
            store.by_version(environment_key),
            action="delete",
            target_kind="version",
            target=environment_key,
            actor=request.actor,
            note=request.note,
            remove=remove_artifact(manager),
            reason=REASON_OPERATOR,
            sizes=sizes,
            in_use=in_use,
        )

    @app.post("/v1/retention/artifacts/{artifact_id}/pin")
    def pin_artifact(artifact_id: UUID, request: RetentionRequest) -> RetentionView:
        _, store, sizes, in_use = retention_targets()
        return store.pin(
            target_kind="artifact",
            target=str(artifact_id),
            actor=request.actor,
            note=request.note,
            sizes=sizes,
            in_use=in_use,
        )

    @app.post("/v1/retention/artifacts/{artifact_id}/unpin")
    def unpin_artifact(artifact_id: UUID, request: RetentionRequest) -> RetentionView:
        _, store, sizes, in_use = retention_targets()
        return store.unpin(
            target_kind="artifact",
            target=str(artifact_id),
            actor=request.actor,
            note=request.note,
            sizes=sizes,
            in_use=in_use,
        )

    @app.post("/v1/retention/artifacts/{artifact_id}/delete")
    def delete_artifact(artifact_id: UUID, request: RetentionRequest) -> RetentionReport:
        manager, store, sizes, in_use = retention_targets()
        return store.delete_artifacts(
            [store.artifact(artifact_id)],
            action="delete",
            target_kind="artifact",
            target=str(artifact_id),
            actor=request.actor,
            note=request.note,
            remove=remove_artifact(manager),
            reason=REASON_OPERATOR,
            sizes=sizes,
            in_use=in_use,
        )

    @app.post("/v1/retention/sweep")
    def sweep_retention(request: RetentionRequest) -> RetentionReport:
        manager, store, sizes, in_use = retention_targets()
        return store.sweep(
            actor=request.actor,
            note=request.note,
            remove=remove_artifact(manager),
            sizes=sizes,
            in_use=in_use,
        )

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
