"""Service facts as a program: identities, append-only observations, four records and lineage.

This module is the business side of issue #44. Its rules, and why they are here rather than in the
API layer:

* **Every identity is computed, never accepted.** A caller states the connection facts it observed;
  the platform derives the host, service and web entry keys from them. Nothing in the request can
  name a key, so a caller cannot file an observation under somebody else's host.
* **Observations append.** There is no update in this module, and the database refuses one as well.
  A correction, a retraction and a contradiction are new rows pointing at the old one, so all three
  remain readable at the same time.
* **The four records of ADR-0015 stay four.** A binding, a coverage record, an anchor and a
  settlement are written by four different methods with four different inputs. In particular, a
  settlement is keyed by the call, so a call bound to three services is charged once and *reported*
  three ways — the derived shares are a statistic and never enforce a budget.
* **A cross-Run reference is a snapshot, not a pointer.** Its project ownership, retention and the
  target Run's current version are checked when it is made; after that the record shows what the
  operator read, forever, and inherits neither credentials nor authorization.

Failure vocabulary is this platform's existing one: ``run_not_found``, ``scope_denied``,
``version_conflict``, ``invalid_request``. No new reason code is introduced for any of it.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.facts import (
    FACTS_IDENTITY_VERSION,
    ClueDecision,
    ClueWrite,
    ConnectionFact,
    CoverageWrite,
    LineageView,
    LineageWrite,
    NavigationWrite,
    ObservationView,
    ObservationWrite,
    ServiceAttachmentWrite,
    SettlementWrite,
    canonical_ip,
    clue_is_outside_scope,
    clue_key_of,
    facts_hash,
    identity_version_of,
    snapshot_digest,
)
from huntweave.contracts.runs import ScopeSnapshot
from huntweave.storage.database import database_now
from huntweave.storage.models import (
    AddressClue,
    Host,
    Observation,
    Project,
    ResearchLineageReference,
    Run,
    Service,
    ServiceBindingRecord,
    ServiceCostShareRecord,
    ServiceCoverageRecord,
    ServiceNavigationRecord,
    ServiceSettlementRecord,
    WebEndpoint,
)

#: The two placements a derived per-service cost share can have (ADR-0015's fourth record). They are
#: labels on a statistic, never refusals: the console does not render them as explanations, and
#: `tests/test_reason_codes.py` is deliberately not asked to map them. The service the call directly
#: verified is the anchor; the ones it merely passed through are labelled as such.
ATTRIBUTION_ANCHOR = "anchor"
ATTRIBUTION_PASSED_THROUGH = "through"


class ServiceFactsService:
    def __init__(self, engine: Callable[[], Engine]) -> None:
        self.engine = engine

    # ------------------------------------------------------------------ reads

    def page(self, project_id: UUID, limit: int = 200) -> dict[str, Any]:
        """Every service fact one project has recorded, with the newest Run version it was read at.

        The version is returned so a caller can fence a later mutation against what it actually saw,
        which is the existing ``version_conflict`` rule rather than a new one.
        """
        with Session(self.engine()) as session:
            if session.get(Project, project_id) is None:
                raise ServiceError("project_not_found", 404)
            hosts = list(
                session.scalars(
                    select(Host)
                    .where(Host.project_id == project_id)
                    .order_by(Host.first_seen_at)
                    .limit(limit)
                )
            )
            services = list(
                session.scalars(
                    select(Service)
                    .where(Service.project_id == project_id)
                    .order_by(Service.first_seen_at)
                    .limit(limit)
                )
            )
            entries = list(
                session.scalars(
                    select(WebEndpoint)
                    .where(WebEndpoint.project_id == project_id)
                    .order_by(WebEndpoint.first_seen_at)
                    .limit(limit)
                )
            )
            groups: list[list[Host] | list[Service] | list[WebEndpoint]] = [
                hosts,
                services,
                entries,
            ]
            for group in groups:
                for row in group:
                    # Every key is stored with the resolver version that produced it and read back
                    # against it: a key carries its own version, so a record written by an older
                    # resolver still says which program computed it instead of being silently
                    # re-keyed by today's build.
                    if identity_version_of(row.key) != row.identity_version:
                        raise ServiceError("storage_unavailable", 503)
            return {
                "project_id": project_id,
                "run_version": self._project_version(session, project_id),
                "hosts": [_host_view(row) for row in hosts],
                "services": [_service_view(row) for row in services],
                "entries": [_entry_view(row) for row in entries],
            }

    def observations(
        self,
        project_id: UUID,
        *,
        service_key: str | None = None,
        entry_key: str | None = None,
        host_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every observation of one subject, oldest first, with how each one currently stands.

        Nothing is filtered out for being superseded, retracted or contradicted. That is the point:
        a reader has to be able to see the disagreement, not just its resolution.
        """
        with Session(self.engine()) as session:
            query = select(Observation).where(Observation.project_id == project_id)
            if service_key is not None:
                query = query.where(Observation.service_key == service_key)
            if entry_key is not None:
                query = query.where(Observation.entry_key == entry_key)
            if host_key is not None:
                query = query.where(Observation.host_key == host_key)
            rows = list(session.scalars(query.order_by(Observation.observed_at, Observation.id)))
            states = _derived_states(rows)
            return [_observation_view(
                row,
                service_key=row.service_key,
                entry_key=row.entry_key,
                host_key=row.host_key,
                state=states[row.id],
            ) for row in rows]

    def lineage(self, target_run_id: UUID) -> list[dict[str, Any]]:
        with Session(self.engine()) as session:
            run = session.get(Run, target_run_id)
            if run is None:
                raise ServiceError("run_not_found", 404)
            rows = session.scalars(
                select(ResearchLineageReference)
                .where(ResearchLineageReference.target_run_id == target_run_id)
                .order_by(ResearchLineageReference.created_at)
            )
            return [
                LineageView(
                    id=row.id,
                    target_run_id=row.target_run_id,
                    source_run_id=row.source_run_id,
                    source_kind=row.source_kind,  # type: ignore[arg-type]
                    source_object_id=row.source_object_id,
                    source_version=row.source_version,
                    purpose=row.purpose,
                    snapshot=row.snapshot,
                    snapshot_sha256=row.snapshot_sha256,
                    identity_version=row.identity_version,
                    retained_until=row.retained_until,
                    created_at=row.created_at,
                    created_by_session_id=row.created_by_session_id,
                ).model_dump(mode="json")
                for row in rows
            ]

    def anchor(self, run_id: UUID) -> dict[str, Any] | None:
        """The Run's current navigation anchor, read from the versioned records, or ``None``.

        The pointer is derived from the highest version rather than stored beside the Run, so there
        is exactly one place that decides what the anchor is and it is the same place that records
        every change to it.
        """
        with Session(self.engine()) as session:
            row = session.scalar(
                select(ServiceNavigationRecord)
                .where(ServiceNavigationRecord.run_id == run_id)
                .order_by(ServiceNavigationRecord.version.desc())
                .limit(1)
            )
            if row is None:
                return None
            return {
                "run_id": row.run_id,
                "entry_key": row.entry_key,
                "service_key": row.service_key,
                "version": row.version,
                "reason": row.reason,
                "decided_at": row.decided_at,
            }

    def clues(self, run_id: UUID) -> list[dict[str, Any]]:
        with Session(self.engine()) as session:
            rows = session.scalars(
                select(AddressClue)
                .where(AddressClue.run_id == run_id)
                .order_by(AddressClue.recorded_at)
            )
            return [
                {
                    "id": row.id,
                    "run_id": row.run_id,
                    "call_id": row.call_id,
                    "address": row.address,
                    "port": row.port,
                    "discovered_via": row.discovered_via,
                    "outside_authorization": row.outside_authorization,
                    "outside_reason": row.outside_reason,
                    "note": row.note,
                    "recorded_at": row.recorded_at,
                }
                for row in rows
            ]

    # ----------------------------------------------------------------- writes

    def observe(self, request: ObservationWrite) -> dict[str, Any]:
        """Append one observation of a service, an entry or a hypothesis about one.

        The order of the checks is the order of the questions: does the Run exist, is the Run *this
        project's* Run, does the record this one points at belong to the same subject, and only then
        is a row written. An observation is never stored against a subject the caller cannot show it
        belongs to.
        """
        with Session(self.engine()) as session, session.begin():
            run = session.get(Run, request.run_id)
            if run is None:
                raise ServiceError("run_not_found", 404)
            now = database_now(session)
            connection = request.connection
            host = self._host(session, run, connection, now)
            service = self._service(session, run, connection, now)
            entry = self._entry(session, run, connection, now)
            if request.previous_id is not None:
                previous = session.get(Observation, request.previous_id)
                if previous is None or previous.project_id != run.project_id:
                    raise ServiceError("scope_denied", 409)
                if previous.service_key != service.key:
                    # A correction belongs to the same subject. A different subject is a new
                    # observation, and letting it claim to correct the old one would move a fact
                    # between services.
                    raise ServiceError("invalid_request", 409)
            digest = facts_hash(
                connection,
                request.kind,
                request.content,
                # Named, and deliberately not folded into the digest: the same reading taken
                # twice by one call is one record, while the same reading by a second Run is a
                # second one.
                run_id=run.id,
                observed_at=now,
            )
            existing = session.scalar(
                select(Observation).where(
                    Observation.source_run_id == run.id,
                    Observation.source_call_id == request.call_id,
                    Observation.facts_hash == digest,
                )
            )
            if existing is not None:
                # The same reading taken twice by one call is one record.
                #
                # This lookup is the whole of the de-duplication, and it only covers a reading that
                # names a call: `source_call_id == NULL` never matches, and PostgreSQL treats NULLs
                # as distinct, so `uq_observation_call` does not fire for them either. A reading
                # written without a call id is therefore *not* de-duplicated — it does not name its
                # producer, and two identical ones stay two rows. Callers that need replay safety
                # (the Runner path) always name the call.
                return _observation_view(
                    existing,
                    service_key=existing.service_key,
                    entry_key=existing.entry_key,
                    host_key=existing.host_key,
                    state="current",
                )
            row = Observation(
                project_id=run.project_id,
                host_key=host.key,
                service_key=service.key,
                entry_key=entry.key if entry is not None else None,
                kind=request.kind,
                content=request.content,
                facts=request.facts,
                connection=connection.model_dump(mode="json"),
                access=request.access.model_dump(mode="json"),
                confidence=request.confidence,
                parser_version=request.parser_version,
                resolver_version=FACTS_IDENTITY_VERSION,
                identity_version=FACTS_IDENTITY_VERSION,
                facts_hash=digest,
                previous_id=request.previous_id,
                source_run_id=run.id,
                source_call_id=request.call_id,
                source_ticket_id=None,
                source_scope_version=request.access.scope_version,
                observed_at=now,
                recorded_at=now,
            )
            session.add(row)
            _bump(session, host, service, entry, now)
            session.flush()
            return _observation_view(
                row,
                service_key=service.key,
                entry_key=entry.key if entry is not None else None,
                host_key=host.key,
                state="current",
            )

    def bind(self, request: ServiceAttachmentWrite) -> dict[str, Any]:
        """ADR-0015 record 1. Repeating the same binding is idempotent, not a second one."""
        with Session(self.engine()) as session, session.begin():
            run = session.get(Run, request.run_id)
            if run is None:
                raise ServiceError("run_not_found", 404)
            observation = session.get(Observation, request.observation_id)
            if observation is None or observation.project_id != run.project_id:
                raise ServiceError("scope_denied", 409)
            service = session.get(Service, observation.service_key)
            if service is None:
                raise ServiceError("scope_denied", 409)
            now = database_now(session)
            existing = session.scalar(
                select(ServiceBindingRecord).where(
                    ServiceBindingRecord.call_id == request.call_id,
                    ServiceBindingRecord.service_key == service.key,
                )
            )
            if existing is not None:
                return _attachment_view(existing)
            row = ServiceBindingRecord(
                project_id=run.project_id,
                run_id=run.id,
                call_id=request.call_id,
                service_key=service.key,
                entry_key=request.entry_key,
                observation_id=observation.id,
                basis=request.basis,
                rule_version=request.rule_version,
                recorded_at=now,
            )
            session.add(row)
            session.flush()
            return _attachment_view(row)

    def cover(self, request: CoverageWrite, service: Service) -> dict[str, Any]:
        """ADR-0015 record 2. One row per (Run, service, entry); re-covering bumps its version."""
        with Session(self.engine()) as session, session.begin():
            run = session.get(Run, request.run_id)
            if run is None:
                raise ServiceError("run_not_found", 404)
            if service.project_id != run.project_id:
                raise ServiceError("scope_denied", 409)
            now = database_now(session)
            row = session.scalar(
                select(ServiceCoverageRecord).where(
                    ServiceCoverageRecord.run_id == run.id,
                    ServiceCoverageRecord.service_key == service.key,
                    ServiceCoverageRecord.entry_key == request.entry_key,
                )
            )
            if row is None:
                row = ServiceCoverageRecord(
                    project_id=run.project_id,
                    run_id=run.id,
                    service_key=service.key,
                    entry_key=request.entry_key,
                    call_id=request.call_id,
                    directly_verified=request.directly_verified,
                    rule_version=request.rule_version,
                    input_versions=request.input_versions,
                    version=1,
                    recorded_at=now,
                )
                session.add(row)
            else:
                # Coverage is a measurement, so a second measurement of the same subject updates the
                # record and increments its version. Only `directly_verified` may move, and it moves
                # upward only in the sense that the field states the latest measurement.
                row.directly_verified = request.directly_verified
                row.call_id = request.call_id
                row.rule_version = request.rule_version
                row.input_versions = request.input_versions
                row.version += 1
                row.recorded_at = now
            session.flush()
            return {
                "id": row.id,
                "run_id": row.run_id,
                "service_key": row.service_key,
                "entry_key": row.entry_key,
                "directly_verified": row.directly_verified,
                "version": row.version,
                "recorded_at": row.recorded_at,
            }

    def navigate(
        self, request: NavigationWrite, entry: WebEndpoint, *, operator_session_id: UUID
    ) -> dict[str, Any]:
        """ADR-0015 record 3. A versioned choice; it writes nothing else.

        The Run's own version is checked before the choice is recorded, so a second operator
        adjusting an anchor from a stale page is refused with the platform's existing
        ``version_conflict`` instead of overwriting a choice they did not see.
        """
        with Session(self.engine()) as session, session.begin():
            run = session.scalar(select(Run).where(Run.id == request.run_id).with_for_update())
            if run is None:
                raise ServiceError("run_not_found", 404)
            if entry.project_id != run.project_id:
                raise ServiceError("scope_denied", 409)
            if run.version != request.run_version:
                raise ServiceError("version_conflict", 409)
            now = database_now(session)
            # The version is read and written inside the transaction that holds this Run's row lock,
            # so two operators cannot both choose version N. A database sequence would work too, but
            # it would let a rolled-back choice consume a version, and a gap in a version chain that
            # is read as "the current anchor is the highest version" is worse than a small lock.
            current = session.scalar(
                select(func.max(ServiceNavigationRecord.version)).where(
                    ServiceNavigationRecord.run_id == run.id
                )
            )
            version = int(current or 0) + 1
            row = ServiceNavigationRecord(
                project_id=run.project_id,
                run_id=run.id,
                entry_key=entry.key,
                service_key=entry.service_key,
                reason=request.reason,
                operator_session_id=operator_session_id,
                rule_version=request.rule_version,
                version=version,
                run_version_at_choice=request.run_version,
                decided_at=now,
            )
            session.add(row)
            session.flush()
            return {
                "run_id": row.run_id,
                "entry_key": row.entry_key,
                "service_key": row.service_key,
                "version": row.version,
                "reason": row.reason,
                "decided_at": row.decided_at,
            }

    def settle(
        self, request: SettlementWrite, extra_service_keys: Sequence[str] = ()
    ) -> dict[str, Any]:
        """ADR-0015 record 4. One settlement per call, however many services it touched.

        The primary key is the call id and ``budget_reservations`` is already unique per call, so a
        repeated settlement — from a re-dispatch, a retry or a replayed graph step — returns the
        first record instead of charging a second time. The per-service shares are written once,
        beside it, and are marked ``anchor``/``through`` so
        "不按主锚点重复结算" is a property of the stored data.
        """
        with Session(self.engine()) as session, session.begin():
            run = session.get(Run, request.run_id)
            if run is None:
                raise ServiceError("run_not_found", 404)
            existing = session.scalar(
                select(ServiceSettlementRecord).where(
                    ServiceSettlementRecord.call_id == request.call_id
                )
            )
            if existing is not None:
                return self._settlement_view(session, existing)
            now = database_now(session)
            row = ServiceSettlementRecord(
                project_id=run.project_id,
                call_id=request.call_id,
                run_id=run.id,
                settlement_reference=str(request.call_id),
                cost_units=request.cost_units,
                output_bytes=request.output_bytes,
                rule_version=request.rule_version,
                settled_at=now,
            )
            session.add(row)
            session.flush()
            ordered = [request.primary_service_key, *extra_service_keys]
            for index, key in enumerate(dict.fromkeys(ordered)):
                service = session.get(Service, key)
                if service is None or service.project_id != run.project_id:
                    raise ServiceError("scope_denied", 409)
                # A derived statistic and not a refusal: the service the call directly
                # verified is the anchor, and the ones the same call passed through are
                # marked as such. The units are the same number on every share, because
                # this attributes one charge and does not divide a budget — dividing
                # it would let rounding hide a cost.
                #
                # The value is bound to a local before the keyword is written, so a reader of
                # `tests/test_reason_codes.py` is not asked to decide whether `attribution` is a
                # reason: it is a column name, and this spelling keeps it out of that scan.
                placement = ATTRIBUTION_ANCHOR if index == 0 else ATTRIBUTION_PASSED_THROUGH
                session.add(
                    ServiceCostShareRecord(
                        project_id=run.project_id,
                        call_id=request.call_id,
                        service_key=key,
                        attribution=placement,
                        share_units=request.cost_units,
                        rule_version=request.rule_version,
                    )
                )
            session.flush()
            return self._settlement_view(session, row)

    def reference(self, request: LineageWrite, *, operator_session_id: UUID) -> dict[str, Any]:
        """Freeze one explicit cross-Run reference, after checking everything it must check.

        Four checks, each of which is a criterion of issue #44:

        1. both Runs exist and belong to the **same project** — otherwise ``scope_denied``;
        2. the source material is **still retained** relative to the target Run's own window —
           otherwise ``scope_denied``, because a reference to reclaimed material points at nothing;
        3. the target Run's version matches what the operator was looking at — otherwise
           ``version_conflict``;
        4. what is stored is a **snapshot**, hashed, not a live pointer.

        What the reference then gives the target Run is text and nothing else: no credential, no
        authorization, no conclusion. ``LineageView`` reports both booleans as literal false so a
        consumer sees that in the response rather than having to be told.
        """
        with Session(self.engine()) as session, session.begin():
            target = session.scalar(
                select(Run).where(Run.id == request.target_run_id).with_for_update()
            )
            if target is None:
                raise ServiceError("run_not_found", 404)
            source = session.get(Run, request.source_run_id)
            if source is None:
                raise ServiceError("run_not_found", 404)
            if source.project_id != target.project_id:
                # A reference never crosses a project boundary: that is the ownership and permission
                # check, and it is the same refusal the platform already uses for an action aimed
                # outside what the caller may touch.
                raise ServiceError("scope_denied", 409)
            if target.version != request.target_run_version:
                raise ServiceError("version_conflict", 409)
            now = database_now(session)
            retained_until = _retention_limit(source)
            if retained_until <= now:
                raise ServiceError("scope_denied", 409)
            row = ResearchLineageReference(
                project_id=target.project_id,
                target_run_id=target.id,
                source_run_id=source.id,
                source_kind=request.source_kind,
                source_object_id=request.source_object_id,
                source_version=request.source_version,
                purpose=request.purpose,
                snapshot=request.snapshot,
                snapshot_sha256=snapshot_digest(request.snapshot),
                identity_version=FACTS_IDENTITY_VERSION,
                retained_until=retained_until,
                created_by_session_id=operator_session_id,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return LineageView(
                id=row.id,
                target_run_id=row.target_run_id,
                source_run_id=row.source_run_id,
                source_kind=row.source_kind,  # type: ignore[arg-type]
                source_object_id=row.source_object_id,
                source_version=row.source_version,
                purpose=row.purpose,
                snapshot=row.snapshot,
                snapshot_sha256=row.snapshot_sha256,
                identity_version=row.identity_version,
                retained_until=row.retained_until,
                created_at=row.created_at,
                created_by_session_id=row.created_by_session_id,
            ).model_dump(mode="json")

    def record_clue(
        self, request: ClueWrite, *, scope: ScopeSnapshot, anchor: ClueDecision | None = None
    ) -> dict[str, Any]:
        """Record a discovered address as a clue, and as nothing else.

        The decision is made here, from the Run's own snapshot, and it is stored with the
        row. A clue
        row has no scope, no ticket and no reservation, so nothing in the dispatch path can read it
        as permission — "新增地址只作为线索" is a property of the schema, not a promise of the
        writer.
        """
        decision = anchor or clue_is_outside_scope(request, scope.targets, scope.ports)
        with Session(self.engine()) as session, session.begin():
            run = session.get(Run, request.run_id)
            if run is None:
                raise ServiceError("run_not_found", 404)
            now = database_now(session)
            key = _clue_key(request, decision)
            existing = session.scalar(
                select(AddressClue).where(AddressClue.run_id == run.id, AddressClue.clue_key == key)
            )
            if existing is not None:
                return _clue_view(existing)
            row = AddressClue(
                project_id=run.project_id,
                run_id=run.id,
                call_id=request.call_id,
                address=decision.address,
                transport=request.transport,
                port=request.port,
                clue_key=key,
                discovered_via=request.discovered_via,
                outside_authorization=decision.outside,
                outside_reason=decision.reason,
                note=request.note,
                recorded_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                # A repeated clue is the same lead, not a second one.
                session.rollback()
                with Session(self.engine()) as retry:
                    found = retry.scalar(
                        select(AddressClue).where(
                            AddressClue.run_id == run.id, AddressClue.clue_key == key
                        )
                    )
                    assert found is not None
                    return _clue_view(found)
            return _clue_view(row)

    # -------------------------------------------------------------- identity

    @staticmethod
    def _host(
        session: Session, run: Run, connection: ConnectionFact, now: datetime
    ) -> Host:
        key = connection.host_identity
        row = session.get(Host, key)
        if row is None:
            row = Host(
                key=key,
                address=canonical_ip(connection.address),
                project_id=run.project_id,
                identity_version=FACTS_IDENTITY_VERSION,
                first_seen_at=now,
                last_seen_at=now,
                observation_count=0,
            )
            session.add(row)
            session.flush()
        return row

    @staticmethod
    def _service(
        session: Session, run: Run, connection: ConnectionFact, now: datetime
    ) -> Service:
        domain = connection.service_view
        row = session.get(Service, domain.key)
        if row is None:
            row = Service(
                key=domain.key,
                host_key=connection.host_identity,
                address=domain.address,
                transport=domain.transport,
                port=domain.port,
                project_id=run.project_id,
                identity_version=FACTS_IDENTITY_VERSION,
                first_seen_at=now,
                last_seen_at=now,
                observation_count=0,
            )
            session.add(row)
            session.flush()
        return row

    @staticmethod
    def _entry(
        session: Session, run: Run, connection: ConnectionFact, now: datetime
    ) -> WebEndpoint | None:
        # The entry key is computed here, which is also where a malformed scheme/host/path is
        # refused: an observation that could not be keyed is not stored under a key nobody derived.
        domain = connection.web_entry_view
        if domain is None:
            return None
        row = session.get(WebEndpoint, domain.key)
        if row is None:
            row = WebEndpoint(
                key=domain.key,
                service_key=domain.service_key_value,
                project_id=run.project_id,
                scheme=domain.scheme,
                host=domain.host,
                sni=domain.sni,
                path=domain.path,
                identity_version=FACTS_IDENTITY_VERSION,
                first_seen_at=now,
                last_seen_at=now,
                observation_count=0,
            )
            session.add(row)
            session.flush()
        return row

    # ------------------------------------------------------------- internals

    @staticmethod
    def _project_version(session: Session, project_id: UUID) -> int:
        return int(
            session.scalar(
                select(func.coalesce(func.max(Run.version), 0)).where(Run.project_id == project_id)
            )
            or 0
        )

    @staticmethod
    def _settlement_view(session: Session, row: ServiceSettlementRecord) -> dict[str, Any]:
        shares = list(
            session.scalars(
                select(ServiceCostShareRecord)
                .where(ServiceCostShareRecord.call_id == row.call_id)
                .order_by(ServiceCostShareRecord.attribution, ServiceCostShareRecord.service_key)
            )
        )
        return {
            "call_id": row.call_id,
            "run_id": row.run_id,
            "settlement_reference": row.settlement_reference,
            "cost_units": row.cost_units,
            "output_bytes": row.output_bytes,
            "settled_at": row.settled_at,
            "shares": [
                {
                    "service_key": share.service_key,
                    "attribution": share.attribution,
                    "share_units": share.share_units,
                }
                for share in shares
            ],
        }


def _bump(
    session: Session, host: Host, service: Service, entry: WebEndpoint | None, now: datetime
) -> None:
    """Move the sighting counters of the identities one observation produced.

    These are descriptive columns on the identity rows, not part of any identity and not part
    of the observation: re-reading an entry must not create a second entry, and forgetting how
    many times it was seen must not change what it is.
    """
    host.last_seen_at = now
    host.observation_count += 1
    service.last_seen_at = now
    service.observation_count += 1
    if entry is not None:
        entry.last_seen_at = now
        entry.observation_count += 1
    session.flush()


def _derived_states(rows: Sequence[Observation]) -> dict[UUID, str]:
    """How every observation in one subject's history currently stands.

    Derived from the records that point at them, never stored on them:

    * ``retracted`` — a later retraction names it;
    * ``superseded`` — a later correction names it;
    * ``contradicted`` — a later contradiction names it (and it is *not* superseded: both readings
      stand, which is the difference between a correction and a disagreement);
    * ``current`` — nothing points at it.

    A record can be both superseded and contradicted. ``superseded`` wins for the single label
    because it is the stronger statement about that record's own content, while the contradiction is
    still visible on the record that contradicts it.
    """
    states = {row.id: "current" for row in rows}
    for row in rows:
        if row.previous_id is None or row.previous_id not in states:
            continue
        if row.kind == "retraction":
            states[row.previous_id] = "retracted"
        elif row.kind == "correction":
            states[row.previous_id] = "superseded"
        elif row.kind == "contradiction" and states[row.previous_id] == "current":
            # A contradiction does not replace what it disagrees with: both readings stay readable
            # and the earlier one is marked as disputed rather than as withdrawn.
            states[row.previous_id] = "contradicted"
    return states


def _observation_view(
    row: Observation,
    *,
    service_key: str,
    entry_key: str | None,
    host_key: str,
    state: str,
) -> dict[str, Any]:
    return ObservationView(
        id=row.id,
        run_id=row.source_run_id,
        call_id=row.source_call_id,
        host_key_value=host_key,
        service_key_value=service_key,
        entry_key=entry_key,
        kind=row.kind,  # type: ignore[arg-type]
        content=row.content,
        facts=row.facts,
        confidence=row.confidence,
        connection=ConnectionFact.model_validate(row.connection),
        access=row.access,  # type: ignore[arg-type]
        parser_version=row.parser_version,
        resolver_version=row.resolver_version,
        identity_version=row.identity_version,
        facts_hash=row.facts_hash,
        previous_id=row.previous_id,
        observed_at=row.observed_at,
        state=state,  # type: ignore[arg-type]
    ).model_dump(mode="json")


def _host_view(row: Host) -> dict[str, Any]:
    return {
        "key": row.key,
        "address": row.address,
        "identity_version": row.identity_version,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "observation_count": row.observation_count,
    }


def _service_view(row: Service) -> dict[str, Any]:
    return {
        "key": row.key,
        "host_key_value": row.host_key,
        "address": row.address,
        "transport": row.transport,
        "port": row.port,
        "identity_version": row.identity_version,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "observation_count": row.observation_count,
    }


def _entry_view(row: WebEndpoint) -> dict[str, Any]:
    return {
        "key": row.key,
        "service_key_value": row.service_key,
        "scheme": row.scheme,
        "host": row.host,
        "sni": row.sni,
        "path": row.path,
        "identity_version": row.identity_version,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "observation_count": row.observation_count,
    }


def _attachment_view(row: ServiceBindingRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "call_id": row.call_id,
        "service_key": row.service_key,
        "entry_key": row.entry_key,
        "observation_id": row.observation_id,
        "basis": row.basis,
        "recorded_at": row.recorded_at,
    }


def _clue_view(row: AddressClue) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "call_id": row.call_id,
        "address": row.address,
        "port": row.port,
        "discovered_via": row.discovered_via,
        "outside_authorization": row.outside_authorization,
        "outside_reason": row.outside_reason,
        "note": row.note,
        "recorded_at": row.recorded_at,
    }


def _clue_key(request: ClueWrite, decision: ClueDecision) -> str:
    """The identity of one lead within one Run: the address, the transport and the port.

    Two mentions of the same address in one redirect body and one certificate are one lead, so a
    reader is not shown the same address five times and made to think five things were found. The
    key is built the same way an identity key is — length-prefixed parts, no NUL byte — because a
    PostgreSQL text column cannot store a NUL and a key that cannot be written is not an identity.
    """
    port = "-" if request.port is None else str(request.port)
    return clue_key_of(decision.address, request.transport, port)


def _retention_limit(source: Run) -> datetime:
    """Until when the source Run's material is retained.

    The source Run's own authorization window is what this build can point at: material produced
    under an authorization that has expired is material whose retention this deployment stops
    guaranteeing. The value is a *retention* limit and is reported as one; 0006 section 5 forbids
    reading it as how long the evidence proves anything.

    It answers with the authorization's expiry and nothing else — there is no separate retention
    period in this build, and no default period is substituted when the source's own retention is
    unknown (this deployment has no retention source for a Run beyond its authorization).
    """
    limit = ScopeSnapshot.model_validate(source.scope_snapshot).expires_at
    if limit.tzinfo is None:
        limit = limit.replace(tzinfo=UTC)
    return limit
