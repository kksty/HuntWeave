"""Service facts, observations and cross-Run lineage over HTTP.

The router is built by a factory so the application assembles it with one line and this module owns
nothing of `api/app.py` (that file belongs to the integration owner):

    app.include_router(create_facts_router(database))

Authentication is not re-implemented here. Every route below is a non-public path, so the existing
`access_boundary` middleware has already authenticated the session and put it on
`request.state.browser` before the handler runs. What this module adds is the part a middleware
cannot express: each write takes the actor from that session -- never from the request body -- so a
caller cannot name somebody else as the operator.

Version discipline uses the platform's own refusals: a stale Run version is
``version_conflict``
and a key naming nothing this caller may see is ``scope_denied``. No new reason code is introduced.
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from huntweave.access.service import BrowserSession
from huntweave.contracts.errors import ServiceError
from huntweave.contracts.facts import (
    ClueWrite,
    CoverageWrite,
    LineageWrite,
    NavigationWrite,
    ObservationWrite,
    ServiceAttachmentWrite,
    SettlementWrite,
    clue_is_outside_scope,
)
from huntweave.contracts.runs import ScopeSnapshot
from huntweave.runs.facts import ServiceFactsService
from huntweave.storage.models import Run, Service, WebEndpoint


def create_facts_router(engine: Callable[[], Engine]) -> APIRouter:
    facts = ServiceFactsService(engine)
    router = APIRouter()

    # ------------------------------------------------------------------- reads

    @router.get("/api/v1/projects/{project_id}/facts")
    def project_facts(project_id: UUID) -> dict[str, Any]:
        """Every host, service and web entry a project has computed, with the Run version read."""
        return facts.page(project_id)

    @router.get("/api/v1/projects/{project_id}/observations")
    def project_observations(
        project_id: UUID,
        service_key: str | None = Query(default=None),
        entry_key: str | None = Query(default=None),
        host_key: str | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        """The whole history of one subject, including what was corrected, retracted or disputed."""
        return facts.observations(
            project_id, service_key=service_key, entry_key=entry_key, host_key=host_key
        )

    @router.get("/api/v1/runs/{run_id}/facts/anchor")
    def run_anchor(run_id: UUID) -> dict[str, Any] | None:
        """The navigation anchor read from its own versioned records, or null when none exists."""
        return facts.anchor(run_id)

    @router.get("/api/v1/runs/{run_id}/clues")
    def run_clues(run_id: UUID) -> list[dict[str, Any]]:
        return facts.clues(run_id)

    @router.get("/api/v1/runs/{run_id}/lineage")
    def run_lineage(run_id: UUID) -> list[dict[str, Any]]:
        """The frozen references this Run holds: read-only, and fixed at the version recorded."""
        return facts.lineage(run_id)

    # ------------------------------------------------------------------ writes

    @router.post("/api/v1/observations", status_code=201)
    def record_observation(
        request: Request, payload: Annotated[ObservationWrite, Body()]
    ) -> dict[str, Any]:
        """Append one observation. The subject's identity is computed from the connection facts."""
        _actor(request)
        return facts.observe(payload)

    @router.post("/api/v1/service-attachments", status_code=201)
    def record_attachment(
        request: Request, payload: Annotated[ServiceAttachmentWrite, Body()]
    ) -> dict[str, Any]:
        """ADR-0015 record 1: which service one call was actually bound to."""
        _actor(request)
        return facts.bind(payload)

    @router.post("/api/v1/runs/{run_id}/coverage", status_code=201)
    def record_coverage(
        request: Request, run_id: UUID, payload: Annotated[CoverageWrite, Body()]
    ) -> dict[str, Any]:
        """ADR-0015 record 2: one measurement of what a Run covered on one service."""
        _actor(request)
        service = _service_of_run(engine, run_id, payload.service_key_value)
        if payload.entry_key is not None:
            # A measurement may name the entry it was taken on, and that entry is checked like the
            # service is: one belonging to another project is refused, and one belonging to another
            # service of the same project is refused too, because storing it would record a page
            # under a service that never served it.
            entry = _entry_of_run(engine, run_id, payload.entry_key)
            if entry.service_key != service.key:
                raise ServiceError("invalid_request", 422)
        return facts.cover(payload.model_copy(update={"run_id": run_id}), service)

    @router.post("/api/v1/runs/{run_id}/facts/anchor", status_code=201)
    def set_anchor(
        request: Request, run_id: UUID, payload: Annotated[NavigationWrite, Body()]
    ) -> dict[str, Any]:
        """ADR-0015 record 3: move the navigation anchor. It writes no coverage and no cost."""
        browser = _actor(request)
        entry = _entry_of_run(engine, run_id, payload.entry_key)
        if entry.service_key != payload.service_key_value:
            # An anchor is a choice of an *entry*, and every entry belongs to exactly one service.
            # A payload naming a different service is refused rather than silently resolved in the
            # entry's favour: the alternative records a choice the caller did not make, and leaves
            # the stated service with no effect at all.
            raise ServiceError("invalid_request", 422)
        return facts.navigate(
            payload.model_copy(update={"run_id": run_id}),
            entry,
            operator_session_id=browser.id,
        )

    @router.post("/api/v1/runs/{run_id}/settlements", status_code=201)
    def record_settlement(
        request: Request, run_id: UUID, payload: Annotated[SettlementWrite, Body()]
    ) -> dict[str, Any]:
        """ADR-0015 record 4: charge one call once, however many services it touched."""
        _actor(request)
        # Checked before the write, so a key naming nothing this project may see produces no
        # settlement row at all. The service layer checks every key again inside its transaction.
        _service_of_run(engine, run_id, payload.primary_service_key)
        for extra in payload.extra_service_keys:
            _service_of_run(engine, run_id, extra)
        return facts.settle(
            payload.model_copy(update={"run_id": run_id}),
            extra_service_keys=payload.extra_service_keys,
        )

    @router.post("/api/v1/runs/{run_id}/lineage", status_code=201)
    def reference_history(
        request: Request, run_id: UUID, payload: Annotated[LineageWrite, Body()]
    ) -> dict[str, Any]:
        """Freeze one explicit reference, after checking ownership, retention and Run version."""
        browser = _actor(request)
        return facts.reference(
            payload.model_copy(update={"target_run_id": run_id}),
            operator_session_id=browser.id,
        )

    @router.post("/api/v1/runs/{run_id}/clues", status_code=201)
    def record_clue(
        request: Request, run_id: UUID, payload: Annotated[ClueWrite, Body()]
    ) -> dict[str, Any]:
        """Register a discovered address as a lead, and as nothing else.

        The decision about what is inside the authorization is made from the Run's own snapshot, and
        the response states which authorization set the address was measured against. A clue that
        turned out to be inside the authorized set is recorded as such rather than promoted, and the
        API has no route that could add it to a scope.
        """
        _actor(request)
        scope = _scope_of_run(engine, run_id)
        body = payload.model_copy(update={"run_id": run_id})
        decision = clue_is_outside_scope(body, scope.targets, scope.ports)
        return {
            **facts.record_clue(body, scope=scope, anchor=decision),
            # Stated in the response so the boundary is visible to whoever recorded the lead.
            "authorized_targets": len(scope.targets),
            "authorized_ports": len(scope.ports),
            "joins_the_scope": False,
        }

    return router


def _actor(request: Request) -> BrowserSession:
    """The operator, taken from the authenticated session and from nowhere else."""
    browser: BrowserSession | None = getattr(request.state, "browser", None)
    if browser is None:
        # Reachable only if a write route were mounted outside the access boundary. The refusal is
        # the platform's existing authentication reason rather than a silent success.
        raise ServiceError("authentication_required", 401)
    return browser


def _run_of(engine: Callable[[], Engine], run_id: UUID) -> tuple[Run, ScopeSnapshot]:
    with Session(engine()) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ServiceError("run_not_found", 404)
        return run, ScopeSnapshot.model_validate(run.scope_snapshot)


def _scope_of_run(engine: Callable[[], Engine], run_id: UUID) -> ScopeSnapshot:
    return _run_of(engine, run_id)[1]


def _service_of_run(engine: Callable[[], Engine], run_id: UUID, key: str) -> Service:
    """The stored service one key names, provided it belongs to the Run's own project.

    A key the database does not know and a key belonging to another project are refused the same
    way: from this caller's position both name something they may not touch.
    """
    run, _scope = _run_of(engine, run_id)
    with Session(engine()) as session:
        row = session.get(Service, key)
        if row is None or row.project_id != run.project_id:
            raise ServiceError("scope_denied", 409)
        session.expunge(row)
        return row


def _entry_of_run(engine: Callable[[], Engine], run_id: UUID, key: str) -> WebEndpoint:
    run, _scope = _run_of(engine, run_id)
    with Session(engine()) as session:
        row = session.get(WebEndpoint, key)
        if row is None or row.project_id != run.project_id:
            raise ServiceError("scope_denied", 409)
        session.expunge(row)
        return row
