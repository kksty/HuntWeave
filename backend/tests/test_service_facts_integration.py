"""Service facts, observations, the four records and cross-Run lineage against real PostgreSQL.

These checks need a database because what they ask about is what the database *keeps*: that an
observation cannot be rewritten, that a correction, a retraction and a contradiction all remain
readable at once, that a cross-service call is charged once, and that a frozen reference
stays frozen
after its source moves. None of that is a property of a pure function, so none of it is checked in
`test_service_facts.py`.

The fixture is deliberately hostile to over-merging: one address carries two virtual hosts and two
schemes, one of those entries is observed at two different times by two different Runs, and one call
is bound to two services. The checks then assert that the platform keeps them apart —and that it
still joins what really is one thing, so the "kept apart" half cannot be satisfied by a program that
never merges anything.

Requires `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1`, like the other integration fixtures: it
deletes rows
from the tables it uses and must never be pointed at a database somebody cares about.
"""

import hashlib
import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from huntweave.api.app import create_app
from huntweave.api.facts import create_facts_router
from huntweave.config import AppSettings
from huntweave.contracts.facts import (
    AccessConditions,
    ConnectionFact,
    CoverageWrite,
    LineageWrite,
    NavigationWrite,
    ObservationWrite,
    ServiceAttachmentWrite,
    SettlementWrite,
)
from huntweave.contracts.runs import Budget, ProjectCreate, RunCreate, ScopeCreate
from huntweave.execution.capabilities import evaluate
from huntweave.runs.facts import ServiceFactsService
from huntweave.runs.service import RunService
from huntweave.storage.database import connect_engine
from huntweave.storage.models import (
    AccessKeyState,
    AddressClue,
    AgentSession,
    AuditEvent,
    AuthorizationScope,
    BudgetReservation,
    Decision,
    EventCursor,
    Evidence,
    Host,
    InterruptionRecord,
    Observation,
    Outbox,
    Project,
    ReconciliationDecision,
    ResearchLineageReference,
    ResearchTask,
    RetentionDecisionRecord,
    Run,
    Service,
    ServiceBindingRecord,
    ServiceCostShareRecord,
    ServiceCoverageRecord,
    ServiceNavigationRecord,
    ServiceSettlementRecord,
    ToolCall,
    ToolResult,
    WebEndpoint,
    WebSession,
)

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"
KEY = "service-facts-test-key-" + "c" * 64

CLEARED = (
    AuditEvent,
    EventCursor,
    ServiceCostShareRecord,
    ServiceSettlementRecord,
    ServiceNavigationRecord,
    ServiceCoverageRecord,
    ServiceBindingRecord,
    ResearchLineageReference,
    AddressClue,
    Observation,
    WebEndpoint,
    Service,
    Host,
    ToolResult,
    Outbox,
    BudgetReservation,
    Evidence,
    InterruptionRecord,
    ReconciliationDecision,
    RetentionDecisionRecord,
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


class Fixture:
    """Two projects, several Runs, one address carrying several applications.

    Built through the real services (`RunService`) rather than by inserting rows, so the identities
    the checks read are the ones the platform really produces.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.facts = ServiceFactsService(lambda: engine)
        runs = RunService(lambda: engine, readiness=lambda: True)
        now = datetime.now(UTC)
        self.alpha = runs.create_project(ProjectCreate(name="alpha"))
        self.beta = runs.create_project(ProjectCreate(name="beta"))

        def scope_of(project: Any, targets: str) -> Any:
            return runs.create_scope(
                ScopeCreate(
                    project_id=project.id,
                    targets_text=targets,
                    starts_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(hours=4),
                    authorization="Fixed fake actions only",
                    budget=Budget(max_tool_calls=20),
                    execution_profile="real-lab-v1",
                    mode="real",
                )
            )

        alpha_scope = scope_of(self.alpha, "192.0.2.10\n192.0.2.11")
        beta_scope = scope_of(self.beta, "198.51.100.5")
        self.alpha_scope = alpha_scope
        self.beta_scope = beta_scope
        self.run_a = runs.create_run(
            RunCreate(scope_id=alpha_scope.id, scope_version=1, execution_profile="real-lab-v1"),
            secrets.token_hex(8),
        )
        self.run_b = runs.create_run(
            RunCreate(scope_id=alpha_scope.id, scope_version=1, execution_profile="real-lab-v1"),
            secrets.token_hex(8),
        )
        self.other_project_run = runs.create_run(
            RunCreate(scope_id=beta_scope.id, scope_version=1, execution_profile="real-lab-v1"),
            secrets.token_hex(8),
        )
        # A call row each Run can bind observations and settlements to: the evidence a service
        # binding cites has to be real, so the calls are.
        self.call_a = self._call(self.run_a)
        self.call_b = self._call(self.run_b)
        self.foreign_call = self._call(self.other_project_run)

    def _call(self, run: Any) -> UUID:
        now = datetime.now(UTC)
        with Session(self.engine) as session, session.begin():
            task = ResearchTask(
                id=uuid4(),
                run_id=run.id,
                role="worker",
                status="running",
                step=0,
                version=1,
                lease_generation=1,
            )
            session.add(task)
            # The task has to reach the database before the session that names it: the dependency
            # order is the schema's, and flushing is how this fixture respects it.
            session.flush()
            agent = AgentSession(
                id=uuid4(),
                run_id=run.id,
                task_id=task.id,
                role="worker",
                status="running",
            )
            session.add(agent)
            session.flush()
            decision = Decision(
                id=uuid4(),
                run_id=run.id,
                session_id=agent.id,
                step=0,
                content={"action": "probe_http"},
            )
            session.add(decision)
            session.flush()
            call = ToolCall(
                id=uuid4(),
                run_id=run.id,
                session_id=agent.id,
                decision_id=decision.id,
                status="succeeded",
                ticket={"target_ip": "192.0.2.10", "target_port": 443},
                created_at=now,
            )
            session.add(call)
            session.flush()
            return call.id

    def entry(self, **changes: Any) -> ConnectionFact:
        base: dict[str, Any] = {
            "address": "192.0.2.10",
            "transport": "tcp",
            "port": 443,
            "scheme": "https",
            "host": "app.example",
            "path": "/login",
        }
        base.update(changes)
        return ConnectionFact.model_validate(base)

    def observe(
        self,
        run: Any,
        *,
        call_id: UUID | None = None,
        kind: str = "observation",
        content: str = "the login page answered with a session cookie",
        previous_id: UUID | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        return self.facts.observe(
            ObservationWrite(
                run_id=run.id,
                call_id=call_id,
                connection=self.entry(**changes),
                access=AccessConditions(
                    method="http_request",
                    bound_ip="192.0.2.10",
                    bound_port=443,
                    scope_version=run.scope_version,
                ),
                kind=kind,  # type: ignore[arg-type]
                content=content,
                previous_id=previous_id,
            )
        )


@pytest.fixture
def engine() -> Iterator[Engine]:
    if os.environ.get("HUNTWEAVE_DISPOSABLE_TEST_DATABASE") != "1":
        pytest.skip("Service-fact checks require an explicitly disposable Compose database")
    result = connect_engine()
    with Session(result) as session, session.begin():
        for model in CLEARED:
            session.execute(delete(model))
    yield result
    result.dispose()


@pytest.fixture
def fixture(engine: Engine) -> Fixture:
    return Fixture(engine)


@pytest.fixture
def client(engine: Engine) -> TestClient:
    """The application with the facts router mounted, as the integration owner mounts it."""
    app = create_app(
        AppSettings(access_key=KEY),
        engine,
        capability_reader=lambda: evaluate(fake_execution_ready=True, now=datetime.now(UTC)),
    )
    app.include_router(create_facts_router(lambda: engine))
    return TestClient(app, base_url=ORIGIN)


def login(client: TestClient) -> dict[str, str]:
    response = client.post("/auth/login", json={"access_key": KEY}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


# ------------------------------------------------------------------ criterion 1


def test_one_address_carrying_two_applications_produces_two_entries(
    fixture: Fixture,
) -> None:
    """The over-merge this slice exists to prevent, asked of the database rather than of
    a function."""
    first = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="app.example")
    second = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="admin.example")
    assert first["service_key_value"] == second["service_key_value"]
    assert first["entry_key"] != second["entry_key"]

    page = fixture.facts.page(fixture.alpha.id)
    assert len(page["services"]) == 1
    assert {item["host"] for item in page["entries"]} == {"app.example", "admin.example"}


def test_the_same_application_seen_twice_stays_one_entry(fixture: Fixture) -> None:
    """The other direction: normalization has to join spellings, or the split above is
    meaningless."""
    first = fixture.observe(
        fixture.run_a, call_id=fixture.call_a, host="app.example", path="/login"
    )
    second = fixture.observe(
        fixture.run_a, call_id=fixture.call_a, host="APP.example.", path="/login/"
    )
    assert first["entry_key"] == second["entry_key"]
    page = fixture.facts.page(fixture.alpha.id)
    assert len(page["entries"]) == 1


def test_every_connection_fact_is_kept_on_the_record(fixture: Fixture) -> None:
    """Issue #44 criterion 1: the address, transport, port, scheme, Host/SNI and path survive."""
    recorded = fixture.observe(
        fixture.run_a, call_id=fixture.call_a, sni="app.example", host="app.example"
    )
    assert recorded["connection"] == {
        "address": "192.0.2.10",
        "transport": "tcp",
        "port": 443,
        "scheme": "https",
        "host": "app.example",
        "path": "/login",
        "sni": "app.example",
    }
    assert recorded["resolver_version"] >= 1
    assert recorded["identity_version"] >= 1
    assert recorded["facts_hash"] == hashlib.sha256(
        __import__("json")
        .dumps(
            {
                "connection": recorded["connection"],
                "kind": "observation",
                "content": recorded["content"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        .encode()
    ).hexdigest()


def test_a_tcp_observation_creates_a_service_and_no_web_entry(fixture: Fixture) -> None:
    probe = ConnectionFact(address="192.0.2.11", port=8080)
    fixture.facts.observe(
        ObservationWrite(
            run_id=fixture.run_a.id,
            call_id=fixture.call_a,
            connection=probe,
            access=AccessConditions(method="tcp_connect", bound_ip="192.0.2.11", bound_port=8080),
            content="port 8080 accepted a connection",
        )
    )
    page = fixture.facts.page(fixture.alpha.id)
    assert len(page["services"]) == 1
    assert page["entries"] == []
    assert page["hosts"][0]["address"] == "192.0.2.11"


# ------------------------------------------------------------------ criterion 2


def test_an_observation_cannot_be_rewritten_even_by_direct_sql(
    engine: Engine, fixture: Fixture
) -> None:
    """The promise is enforced by the database, not by the service layer's good manners."""
    recorded = fixture.observe(fixture.run_a, call_id=fixture.call_a)
    with Session(engine) as session:
        with pytest.raises(DBAPIError) as refusal:
            session.execute(
                text("UPDATE huntweave.observations SET content = :content WHERE id = :id"),
                {"content": "something else entirely", "id": recorded["id"]},
            )
            session.flush()
        session.rollback()
    assert "cannot be rewritten" in str(refusal.value)


def test_a_correction_a_retraction_and_a_contradiction_all_remain_readable(
    fixture: Fixture,
) -> None:
    """Issue #44 criterion 2, the part that is easy to fake: all three coexist, and
    all three read."""
    original = fixture.observe(fixture.run_a, call_id=fixture.call_a, content="the banner said 8.9")
    corrected = fixture.observe(
        fixture.run_a,
        call_id=fixture.call_b,
        kind="correction",
        previous_id=UUID(original["id"]),
        content="the banner said 9.3",
    )
    contradiction = fixture.observe(
        fixture.run_b,
        call_id=fixture.call_b,
        kind="contradiction",
        previous_id=UUID(original["id"]),
        content="the banner said 8.9 on one connection and 9.3 on the next",
    )
    retracted = fixture.observe(
        fixture.run_b,
        call_id=fixture.call_b,
        kind="retraction",
        previous_id=UUID(corrected["id"]),
        content="the 9.3 reading was a cached response and is withdrawn",
    )

    history = fixture.facts.observations(fixture.alpha.id, entry_key=original["entry_key"])
    by_id = {item["id"]: item for item in history}
    assert set(by_id) == {original["id"], corrected["id"], contradiction["id"], retracted["id"]}
    assert by_id[original["id"]]["content"] == "the banner said 8.9"
    assert by_id[original["id"]]["state"] == "superseded"
    assert by_id[corrected["id"]]["state"] == "retracted"
    assert by_id[contradiction["id"]]["state"] == "current"
    assert by_id[retracted["id"]]["state"] == "current"
    # Two Runs sourced them, and each record says which: a second Run's observation of the same
    # address is a second record rather than an overwrite of the first.
    assert {by_id[original["id"]]["run_id"], by_id[contradiction["id"]]["run_id"]} == {
        str(fixture.run_a.id),
        str(fixture.run_b.id),
    }


def test_the_same_reading_taken_twice_by_one_call_is_one_record(fixture: Fixture) -> None:
    """Idempotence by content and source, so a replayed step does not double the history."""
    first = fixture.observe(fixture.run_a, call_id=fixture.call_a, content="one reading")
    again = fixture.observe(fixture.run_a, call_id=fixture.call_a, content="one reading")
    assert first["id"] == again["id"]
    assert len(fixture.facts.observations(fixture.alpha.id)) == 1


def test_the_same_reading_by_a_second_run_is_a_second_record(fixture: Fixture) -> None:
    """`PROJECT.md` section 4.2: observations from different runs do not overwrite each other."""
    first = fixture.observe(fixture.run_a, call_id=fixture.call_a, content="one reading")
    second = fixture.observe(fixture.run_b, call_id=fixture.call_b, content="one reading")
    assert first["id"] != second["id"]
    history = fixture.facts.observations(fixture.alpha.id)
    assert {item["run_id"] for item in history} == {str(fixture.run_a.id), str(fixture.run_b.id)}


def test_a_correction_of_another_subject_is_refused(fixture: Fixture) -> None:
    """A correction belongs to the subject it corrects; a different socket is a different subject.

    Two names on one address and port are the *same* service — that is the whole
    point of the service
    key — so moving a correction between them would be legal and is not what this asserts. A
    correction that names another socket is the case that has to be refused, because accepting it
    would move a fact between services.
    """
    original = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="app.example")
    different_service = fixture.observe(
        fixture.run_a,
        call_id=fixture.call_b,
        address="192.0.2.11",
        port=8080,
        scheme=None,
        path=None,
    )
    assert different_service["service_key_value"] != original["service_key_value"]
    with pytest.raises(Exception) as refusal:
        fixture.observe(
            fixture.run_a,
            call_id=fixture.call_b,
            kind="correction",
            previous_id=UUID(original["id"]),
            address="192.0.2.11",
            port=8080,
            scheme=None,
            path=None,
        )
    assert getattr(refusal.value, "reason_code", None) == "invalid_request"


def test_a_correction_cannot_reach_another_projects_observation(fixture: Fixture) -> None:
    """An id from outside the project is a refusal, not a cross-project write."""
    foreign = fixture.observe(
        fixture.other_project_run, call_id=fixture.foreign_call, address="198.51.100.5"
    )
    with pytest.raises(Exception) as refusal:
        fixture.observe(
            fixture.run_a,
            call_id=fixture.call_a,
            kind="correction",
            previous_id=UUID(foreign["id"]),
        )
    assert getattr(refusal.value, "reason_code", None) == "scope_denied"


# ------------------------------------------------------------------ criterion 3


def test_one_call_bound_to_two_services_is_charged_once_and_reported_twice(
    fixture: Fixture,
) -> None:
    """ADR-0015's fourth record: "不按主锚点重复结算" as a property of the stored data.

    The call really acts on two services —one it verifies directly and one it passes through —so it
    gets two attachments and two derived shares, and exactly one settlement.
    """
    direct = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="app.example")
    via = fixture.observe(
        fixture.run_a, call_id=fixture.call_a, address="192.0.2.11",
        host="relay.example",
    )
    for recorded, basis in ((direct, "observed"), (via, "observed")):
        fixture.facts.bind(
            ServiceAttachmentWrite(
                run_id=fixture.run_a.id,
                call_id=fixture.call_a,
                observation_id=UUID(recorded["id"]),
                entry_key=recorded["entry_key"],
                basis=basis,
            )
        )
    assert len(fixture.facts.page(fixture.alpha.id)["services"]) == 2

    settlement = fixture.facts.settle(
        SettlementWrite(
            call_id=fixture.call_a,
            run_id=fixture.run_a.id,
            cost_units=7,
            output_bytes=2048,
            primary_service_key=direct["service_key_value"],
        ),
        extra_service_keys=[via["service_key_value"]],
    )
    assert settlement["cost_units"] == 7
    assert {share["service_key"]: share["attribution"] for share in settlement["shares"]} == {
        direct["service_key_value"]: "anchor",
        via["service_key_value"]: "through",
    }

    # Charging again —a re-dispatch, a retry, a replayed graph step —returns the first record.
    again = fixture.facts.settle(
        SettlementWrite(
            call_id=fixture.call_a,
            run_id=fixture.run_a.id,
            cost_units=7,
            output_bytes=2048,
            primary_service_key=direct["service_key_value"],
        )
    )
    assert again["settled_at"] == settlement["settled_at"]
    with Session(fixture.engine) as session:
        rows = list(session.scalars(select(ServiceSettlementRecord)))
        shares = list(session.scalars(select(ServiceCostShareRecord)))
    assert len(rows) == 1
    assert len(shares) == 2
    assert sum(share.share_units for share in shares) == 14  # the same 7, attributed twice


def test_a_binding_is_recorded_once_per_call_and_service(fixture: Fixture) -> None:
    recorded = fixture.observe(fixture.run_a, call_id=fixture.call_a)
    body = ServiceAttachmentWrite(
        run_id=fixture.run_a.id,
        call_id=fixture.call_a,
        observation_id=UUID(recorded["id"]),
        entry_key=recorded["entry_key"],
        basis="observed",
    )
    first = fixture.facts.bind(body)
    again = fixture.facts.bind(body)
    assert first["id"] == again["id"]
    with Session(fixture.engine) as session:
        assert len(list(session.scalars(select(ServiceBindingRecord)))) == 1


def test_coverage_distinguishes_what_was_verified_from_what_was_passed_through(
    fixture: Fixture,
) -> None:
    """「用 A 的条件验证 B 只覆盖 B」: the flag is what makes the difference readable."""
    direct = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="app.example")
    via = fixture.observe(
        fixture.run_a, call_id=fixture.call_a, address="192.0.2.11",
        host="relay.example",
    )
    with Session(fixture.engine) as session:
        direct_service = session.get(Service, direct["service_key_value"])
        via_service = session.get(Service, via["service_key_value"])
    assert direct_service is not None and via_service is not None
    fixture.facts.cover(
        CoverageWrite(
            run_id=fixture.run_a.id,
            service_key_value=direct["service_key_value"],
            call_id=fixture.call_a,
            entry_key=direct["entry_key"],
            directly_verified=True,
        ),
        direct_service,
    )
    fixture.facts.cover(
        CoverageWrite(
            run_id=fixture.run_a.id,
            service_key_value=via["service_key_value"],
            call_id=fixture.call_a,
            entry_key=via["entry_key"],
            directly_verified=False,
        ),
        via_service,
    )
    with Session(fixture.engine) as session:
        rows = {row.service_key: row for row in session.scalars(select(ServiceCoverageRecord))}
    assert rows[direct["service_key_value"]].directly_verified is True
    assert rows[via["service_key_value"]].directly_verified is False


def test_moving_the_anchor_writes_no_coverage_and_no_cost(fixture: Fixture) -> None:
    """ADR-0015: a navigation change is versioned, and it does not rewrite what was measured."""
    recorded = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="app.example")
    other = fixture.observe(fixture.run_a, call_id=fixture.call_a, host="admin.example")
    with Session(fixture.engine) as session:
        entry = session.get(WebEndpoint, other["entry_key"])
    assert entry is not None
    before = _run_version(fixture.engine, fixture.run_a.id)
    chosen = fixture.facts.navigate(
        NavigationWrite(
            run_id=fixture.run_a.id,
            entry_key=other["entry_key"],
            service_key_value=other["service_key_value"],
            reason="the second application is the one being researched",
            run_version=before,
        ),
        entry,
        operator_session_id=uuid4(),
    )
    assert fixture.facts.anchor(fixture.run_a.id) == {
        "run_id": fixture.run_a.id,
        "entry_key": other["entry_key"],
        "service_key": other["service_key_value"],
        "version": chosen["version"],
        "reason": "the second application is the one being researched",
        "decided_at": chosen["decided_at"],
    }
    assert fixture.facts.anchor(fixture.run_a.id)["entry_key"] != recorded["entry_key"]
    with Session(fixture.engine) as session:
        assert list(session.scalars(select(ServiceCoverageRecord))) == []
        assert list(session.scalars(select(ServiceSettlementRecord))) == []


def test_a_stale_run_version_is_refused_rather_than_overwriting_a_choice(fixture: Fixture) -> None:
    recorded = fixture.observe(fixture.run_a, call_id=fixture.call_a)
    with Session(fixture.engine) as session:
        entry = session.get(WebEndpoint, recorded["entry_key"])
        assert entry is not None
    # Somebody else moves the Run, and commits: the page the second operator is looking at now names
    # a version the Run has left.
    with Session(fixture.engine) as session, session.begin():
        run = session.scalar(select(Run).where(Run.id == fixture.run_a.id).with_for_update())
        assert run is not None
        stale_version = run.version
        run.version += 1
    with pytest.raises(Exception) as refusal:
        fixture.facts.navigate(
            NavigationWrite(
                run_id=fixture.run_a.id,
                entry_key=recorded["entry_key"],
                service_key_value=recorded["service_key_value"],
                reason="stale page",
                run_version=stale_version,
            ),
            entry,
            operator_session_id=uuid4(),
        )
    assert getattr(refusal.value, "reason_code", None) == "version_conflict"


# ------------------------------------------------------------------ criterion 4


def lineage_body(fixture: Fixture, **changes: Any) -> LineageWrite:
    text = "the ssh banner was OpenSSH_8.9 on 192.0.2.10:22"
    base: dict[str, Any] = {
        "target_run_id": fixture.run_b.id,
        "source_run_id": fixture.run_a.id,
        "source_kind": "observation",
        "source_object_id": uuid4(),
        "source_version": 1,
        "purpose": "compare this Run's banner against last month's",
        "snapshot": text,
        "target_run_version": _run_version(fixture.engine, fixture.run_b.id),
    }
    base.update(changes)
    return LineageWrite.model_validate(base)


def test_a_reference_is_frozen_and_does_not_follow_its_source(fixture: Fixture) -> None:
    """Issue #44 criterion 4: the reference holds what was read, at the version that was named."""
    reference = fixture.facts.reference(lineage_body(fixture), operator_session_id=uuid4())
    assert reference["snapshot"] == "the ssh banner was OpenSSH_8.9 on 192.0.2.10:22"
    assert reference["snapshot_sha256"] == hashlib.sha256(
        reference["snapshot"].encode()
    ).hexdigest()
    assert reference["source_version"] == 1
    # The source Run then moves: new observations, a new Run version, more evidence. The frozen text
    # and the version it names do not change, because they are a record of what was read.
    fixture.observe(fixture.run_a, call_id=fixture.call_a, content="the banner is now OpenSSH_9.3")
    with Session(fixture.engine) as session, session.begin():
        run = session.scalar(select(Run).where(Run.id == fixture.run_a.id).with_for_update())
        assert run is not None
        run.version += 5
    read_back = fixture.facts.lineage(fixture.run_b.id)
    assert read_back == [reference]


def test_a_reference_never_crosses_a_project_boundary(fixture: Fixture) -> None:
    """Ownership is checked at reference time and is the platform's own refusal."""
    with pytest.raises(Exception) as refusal:
        fixture.facts.reference(
            lineage_body(fixture, source_run_id=fixture.other_project_run.id),
            operator_session_id=uuid4(),
        )
    assert getattr(refusal.value, "reason_code", None) == "scope_denied"


def test_a_reference_to_material_past_its_retention_is_refused(fixture: Fixture) -> None:
    """A pointer to reclaimed material points at nothing, so it is not stored."""
    with Session(fixture.engine) as session, session.begin():
        source = session.scalar(select(Run).where(Run.id == fixture.run_a.id).with_for_update())
        assert source is not None
        snapshot = dict(source.scope_snapshot)
        snapshot["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        source.scope_snapshot = snapshot
    with pytest.raises(Exception) as refusal:
        fixture.facts.reference(lineage_body(fixture), operator_session_id=uuid4())
    assert getattr(refusal.value, "reason_code", None) == "scope_denied"


def test_a_reference_does_not_hand_over_a_credential_or_a_scope(fixture: Fixture) -> None:
    """The response says so in its own fields, so a consumer does not have to be told.

    `inherits_credentials` contains the word "credential" and is exactly the field that makes the
    guarantee checkable, so the shape of the name is not what is asserted here. What is asserted is
    that no field of the record could carry one: the record has no secret, token or scope payload.
    """
    reference = fixture.facts.reference(lineage_body(fixture), operator_session_id=uuid4())
    assert reference["inherits_credentials"] is False
    assert reference["inherits_authorization"] is False
    carried = set(reference) - {"inherits_credentials", "inherits_authorization"}
    assert not {name for name in carried if "secret" in name or "token" in name}
    assert not {name for name in carried if "scope" in name or "authorization" in name}
    # And the target Run's own authorization is exactly what it was: the reference added nothing.
    with Session(fixture.engine) as session:
        run = session.get(Run, fixture.run_b.id)
        assert run is not None
        assert run.scope_id == fixture.alpha_scope.id
        assert run.scope_snapshot["targets"] == fixture.alpha_scope.snapshot.targets


def test_a_frozen_snapshot_is_not_a_second_observation(fixture: Fixture) -> None:
    """A historical reference cannot become this Run's evidence: it is a different kind
    of record."""
    before = fixture.facts.observations(fixture.alpha.id)
    fixture.facts.reference(lineage_body(fixture), operator_session_id=uuid4())
    assert fixture.facts.observations(fixture.alpha.id) == before
    with Session(fixture.engine) as session:
        rows = session.scalars(select(Evidence).where(Evidence.run_id == fixture.run_b.id))
        assert list(rows) == []


# ------------------------------------------------------------------ criterion 5


def test_a_clue_is_recorded_and_the_authorization_does_not_move(fixture: Fixture) -> None:
    """Issue #44 criterion 5: a new address is a lead, and the authorization it was measured
    against is unchanged: the Run's snapshot still names exactly its authorized addresses."""
    from huntweave.contracts.facts import ClueWrite, clue_is_outside_scope

    clue = ClueWrite(
        run_id=fixture.run_a.id,
        call_id=fixture.call_a,
        address="203.0.113.9",
        port=443,
        discovered_via="redirect",
        note="the login page redirected here",
    )
    recorded = fixture.facts.record_clue(clue, scope=fixture.alpha_scope.snapshot)
    assert recorded["outside_authorization"] is True
    assert recorded["outside_reason"] == "address_not_in_authorization"

    with Session(fixture.engine) as session:
        run = session.get(Run, fixture.run_a.id)
        scope = session.get(AuthorizationScope, fixture.alpha_scope.id)
        calls = list(session.scalars(select(ToolCall).where(ToolCall.run_id == fixture.run_a.id)))
        reservations = list(
            session.scalars(
                select(BudgetReservation).where(BudgetReservation.run_id == fixture.run_a.id)
            )
        )
    assert "203.0.113.9" not in run.scope_snapshot["targets"]
    assert scope.snapshot["targets"] == fixture.alpha_scope.snapshot.targets
    # No ticket and no reservation was created for the lead, and the clue carries no
    # scope of its own.
    assert len(calls) == 1
    assert reservations == []
    decision = clue_is_outside_scope(
        clue, run.scope_snapshot["targets"], run.scope_snapshot["ports"]
    )
    assert decision.outside is True
    assert decision.joins_the_scope is False


def test_a_repeated_clue_is_one_lead(fixture: Fixture) -> None:
    from huntweave.contracts.facts import ClueWrite

    body = ClueWrite(
        run_id=fixture.run_a.id, address="203.0.113.9", port=443, discovered_via="redirect"
    )
    first = fixture.facts.record_clue(body, scope=fixture.alpha_scope.snapshot)
    second = fixture.facts.record_clue(
        body.model_copy(update={"discovered_via": "certificate"}),
        scope=fixture.alpha_scope.snapshot,
    )
    assert first["id"] == second["id"]
    assert len(fixture.facts.clues(fixture.run_a.id)) == 1


# --------------------------------------------------------------------- the API


def test_every_facts_route_refuses_an_unauthenticated_caller(client: TestClient) -> None:
    """The reads are behind the same access boundary as the rest of the platform."""
    project = uuid4()
    for method, path in (
        ("get", f"/api/v1/projects/{project}/facts"),
        ("get", f"/api/v1/projects/{project}/observations"),
        ("get", f"/api/v1/runs/{uuid4()}/facts/anchor"),
        ("get", f"/api/v1/runs/{uuid4()}/clues"),
        ("get", f"/api/v1/runs/{uuid4()}/lineage"),
        ("post", "/api/v1/service-attachments"),
        ("post", "/api/v1/observations"),
        ("post", f"/api/v1/runs/{uuid4()}/coverage"),
        ("post", f"/api/v1/runs/{uuid4()}/settlements"),
        ("post", f"/api/v1/runs/{uuid4()}/lineage"),
        ("post", f"/api/v1/runs/{uuid4()}/clues"),
    ):
        call = getattr(client, method)
        if method == "get":
            response = call(path, headers={"Origin": ORIGIN})
        else:
            response = call(path, json={}, headers={"Origin": ORIGIN})
        assert response.status_code == 401, (method, path)
        assert response.json() == {"reason_code": "authentication_required"}


def test_the_api_computes_the_identity_instead_of_accepting_one(
    client: TestClient, fixture: Fixture
) -> None:
    """A caller states connection facts; the keys are the platform's, and it reports them back."""
    headers = login(client)
    response = client.post(
        "/api/v1/observations",
        headers=headers,
        json={
            "run_id": str(fixture.run_a.id),
            "call_id": str(fixture.call_a),
            "connection": {
                "address": "192.0.2.10",
                "transport": "tcp",
                "port": 443,
                "scheme": "https",
                "host": "app.example",
                "path": "/login",
            },
            "access": {"method": "http_request", "authenticated": False},
            "content": "the login page answered",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["host_key_value"].startswith("host:v")
    assert body["service_key_value"].startswith("service:v")
    assert body["entry_key"].startswith("entry:v")
    # There is no request field that could have named a key.
    assert "key" not in set(
        client.get("/openapi.json").json()["components"]["schemas"]["ObservationWrite"]["properties"]
    )


def test_the_clue_route_reports_the_boundary_in_its_own_response(
    client: TestClient, fixture: Fixture
) -> None:
    headers = login(client)
    response = client.post(
        f"/api/v1/runs/{fixture.run_a.id}/clues",
        headers=headers,
        json={
            "run_id": str(fixture.run_a.id),
            "call_id": str(fixture.call_a),
            "address": "203.0.113.9",
            "port": 443,
            "discovered_via": "redirect",
            "note": "found in a Location header",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["joins_the_scope"] is False
    assert body["outside_authorization"] is True
    assert body["authorized_targets"] == len(fixture.alpha_scope.snapshot.targets)
    assert body["authorized_ports"] == len(fixture.alpha_scope.snapshot.ports)


def test_a_cover_for_a_key_from_another_project_is_refused(
    client: TestClient, fixture: Fixture
) -> None:
    """A key naming something the caller may not touch is refused before any row is written."""
    headers = login(client)
    recorded = fixture.observe(fixture.run_a, call_id=fixture.call_a)
    response = client.post(
        f"/api/v1/runs/{fixture.other_project_run.id}/coverage",
        headers=headers,
        json={
            "run_id": str(fixture.other_project_run.id),
            "service_key_value": recorded["service_key_value"],
            "directly_verified": True,
        },
    )
    assert response.status_code == 409
    assert response.json() == {"reason_code": "scope_denied"}
    with Session(fixture.engine) as session:
        assert list(session.scalars(select(ServiceCoverageRecord))) == []


def test_an_unknown_run_is_refused_with_the_platforms_own_reason(client: TestClient) -> None:
    headers = login(client)
    response = client.post(
        f"/api/v1/runs/{uuid4()}/lineage",
        headers=headers,
        json={
            "target_run_id": str(uuid4()),
            "source_run_id": str(uuid4()),
            "source_kind": "observation",
            "source_object_id": str(uuid4()),
            "source_version": 1,
            "purpose": "nothing",
            "snapshot": "nothing",
            "target_run_version": 1,
        },
    )
    assert response.status_code == 404
    assert response.json() == {"reason_code": "run_not_found"}


def _run_version(engine: Engine, run_id: UUID) -> int:
    with Session(engine) as session:
        row = session.scalar(select(Run.version).where(Run.id == run_id))
        assert row is not None
        return int(row)
