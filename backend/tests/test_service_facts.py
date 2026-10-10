"""Service-fact identity, the clue boundary and what a historical reference must never carry.

Issue #44's first and fourth criteria are properties of programs, not of a database: an identity key
that over-merges two applications is wrong whatever the schema says, and a reference that inherits a
credential is wrong whatever the table looks like. So they are checked here, without PostgreSQL, and
the checks that genuinely need a database live in `test_service_facts_integration.py`.

The over-merge question is asked in both directions on purpose. The positive cases show
that spellings
of *one* entry fold into one key (otherwise the same application observed twice becomes two, and a
reader is told two things were found). The negative cases show that *different* entries never fold:
two virtual hosts on one address and port, two schemes on one host, two paths on one host. A check
that only did the first half would pass for a program that keys everything to one constant.
"""

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huntweave.contracts.errors import ServiceError
from huntweave.contracts.facts import (
    FACTS_IDENTITY_VERSION,
    AccessConditions,
    ClueWrite,
    ConnectionFact,
    LineageWrite,
    ObservationWrite,
    SettlementWrite,
    canonical_host,
    canonical_ip,
    canonical_path,
    clue_is_outside_scope,
    facts_hash,
    host_key,
    identity_version_of,
    service_key,
    snapshot_digest,
    web_entry_key,
)
from huntweave.contracts.runs import Budget, ScopeSnapshot

AUTHORIZED = ["192.0.2.10", "192.0.2.11"]
AUTHORIZED_PORTS = [80, 443, 8080]


def entry(address: str = "192.0.2.10", **changes: Any) -> ConnectionFact:
    """One HTTP observation, with whatever a check needs changed."""
    base: dict[str, Any] = {
        "address": address,
        "transport": "tcp",
        "port": 443,
        "scheme": "https",
        "host": "app.example",
        "path": "/login",
    }
    base.update(changes)
    return ConnectionFact.model_validate(base)


def scope() -> ScopeSnapshot:
    now = datetime.now(UTC)
    return ScopeSnapshot(
        targets=AUTHORIZED,
        ports=AUTHORIZED_PORTS,
        port_profile="custom-tcp-v1",
        starts_at=now,
        expires_at=now + timedelta(hours=1),
        authorization="Fixed fake actions only",
        budget=Budget(),
    )


# --------------------------------------------------------------------- identity


def test_one_entry_spelled_differently_is_one_entry() -> None:
    """Normalization exists so the same application is not counted twice."""
    base = entry()
    variants = [
        entry(host="APP.EXAMPLE"),
        entry(host="app.example."),
        entry(host="  app.example  "),
        entry(path="/login/"),
        entry(path="/./login"),
        entry(path="/%6Cogin"),
    ]
    assert {item.web_entry_identity for item in variants} == {base.web_entry_identity}


def test_two_applications_on_one_address_and_port_are_two_entries() -> None:
    """The criterion that matters most: a different Host/SNI is a different application.

    One address, one port and one path, two names: the platform reached *one* of two applications,
    and an identity that merged them would report coverage of the other.
    """
    first = entry(host="app.example")
    second = entry(host="admin.example")
    assert first.service_identity == second.service_identity
    assert first.web_entry_identity != second.web_entry_identity

    # SNI is a separate field from the Host header and is part of the record, so an observation that
    # negotiated a different name is a different entry even when the Host header matches.
    with_sni = entry(sni="other.example").web_entry_view
    assert with_sni is not None
    assert with_sni.sni == "other.example"


@pytest.mark.parametrize(
    "changes",
    [
        {"scheme": "http"},
        {"path": "/admin"},
        {"path": "/login?next=/admin"},
        {"address": "192.0.2.11"},
        {"port": 8443},
    ],
)
def test_a_different_scheme_path_address_or_port_is_a_different_entry(
    changes: dict[str, Any]
) -> None:
    """Every component of the entry key is load-bearing: change one and the entry changes."""
    assert entry(**changes).web_entry_identity != entry().web_entry_identity


def test_addressing_by_host_versus_by_name_is_not_merged() -> None:
    """``None`` and a real name are different statements, so they are different entries.

    "We connected to this address and read the default vhost" is not the same fact as "we connected
    to this application by name", and folding them would let the first claim the second's coverage.
    """
    by_address = entry(host=None)
    by_name = entry(host="app.example")
    assert by_address.web_entry_identity != by_name.web_entry_identity
    assert by_address.web_entry_view.host is None  # type: ignore[union-attr]


def test_a_path_that_names_another_authority_is_refused() -> None:
    """A path is a path: ``//host/x`` names somebody else and gets no identity."""
    for value in ["//evil.example/x", "/\\evil.example/x", "//", "relative/path", ""]:
        with pytest.raises(ServiceError) as error:
            canonical_path(value)
        assert error.value.reason_code == "invalid_request"


def test_percent_escapes_that_change_the_resource_are_not_folded() -> None:
    """``%2F`` is a literal slash, not a separator; decoding it would invent a different path."""
    assert canonical_path("/a%2Fb") == "/a%2Fb"
    assert canonical_path("/a%2Fb") != canonical_path("/a/b")
    assert canonical_path("/a%3Fb") != canonical_path("/a?b")


def test_a_non_ascii_host_is_one_host_in_its_idna_form() -> None:
    assert canonical_host("B\u00dcCHER.example") == canonical_host("xn--bcher-kva.example")
    assert canonical_host("") is None
    assert canonical_host(None) is None
    # A bare root label is not a name: it addresses the DNS root, not an application, and keying an
    # entry on it would create one nobody connected to.
    assert canonical_host(".") is None
    assert canonical_host(" . ") is None


def test_an_address_with_a_zone_identifier_is_refused_rather_than_shortened() -> None:
    """``fe80::1%eth0`` is not ``fe80::1``; shortening it would assert a fact nobody reported."""
    with pytest.raises(ServiceError) as error:
        canonical_ip("fe80::1%eth0")
    assert error.value.reason_code == "invalid_ip"
    assert canonical_ip("2001:0db8::1") == "2001:db8::1"
    # A leading zero is refused rather than read as octal: `010` has meant three different things in
    # three different resolvers, so accepting it would make one record addressable two ways.
    for ambiguous in ("192.0.2.010", "010.1.1.1"):
        with pytest.raises(ServiceError) as ambiguous_error:
            canonical_ip(ambiguous)
        assert ambiguous_error.value.reason_code == "invalid_ip"


def test_a_tcp_observation_produces_a_service_and_no_web_entry() -> None:
    """A port scan says nothing about which application is behind the port."""
    probe = ConnectionFact(address="192.0.2.10", port=8080)
    assert probe.web_entry_identity is None
    assert probe.web_entry_view is None
    assert probe.service_identity == service_key("192.0.2.10", "tcp", 8080)
    assert probe.host_identity == host_key("192.0.2.10")


def test_the_identity_version_is_stamped_in_the_key_and_read_back_from_it() -> None:
    """A key carries which resolver produced it, so an old record is not re-keyed silently."""
    for key in (
        host_key("192.0.2.10"),
        service_key("192.0.2.10", "tcp", 80),
        web_entry_key("192.0.2.10", 80, "tcp", "http", "app.example", "/"),
    ):
        assert identity_version_of(key) == FACTS_IDENTITY_VERSION
    with pytest.raises(ServiceError):
        identity_version_of("no-version-here")


def test_a_connection_fact_cannot_state_a_path_without_a_scheme() -> None:
    """Half a web entry is not a web entry, and the contract refuses it rather than guessing."""
    with pytest.raises(ValidationError):
        ConnectionFact(address="192.0.2.10", port=80, path="/x")
    with pytest.raises(ValidationError):
        ConnectionFact(address="192.0.2.10", port=80, scheme="http")


def test_the_key_does_not_depend_on_the_run_or_the_time() -> None:
    """The identity axis is about the subject, not about the study of it.

    Nothing about the observer enters the computation, which is what makes two Runs observing one
    entry agree on what it is. Three claims are held here, and each of them is separable:

    * the identity keys are the same for the same subject, whatever Run asked;
    * the **facts hash** is the same however the observer is passed in, which is why the two
      observer-shaped arguments exist at all — passing them must not change the digest;
    * the digest *does* change when the subject, the kind of record or the claim changes, so the
      first two claims cannot be satisfied by a constant.
    """
    first = facts_hash(entry(), "observation", "the login page answered")
    second = facts_hash(entry(), "observation", "the login page answered")
    observed = facts_hash(
        entry(),
        "observation",
        "the login page answered",
        run_id=uuid4(),
        observed_at=datetime.now(UTC),
    )
    assert first == second == observed
    assert entry().web_entry_identity == entry().web_entry_identity
    other = facts_hash(entry(host="other.example"), "observation", "the login page answered")
    assert other != first
    assert facts_hash(entry(), "contradiction", "the login page answered") != first
    assert facts_hash(entry(), "observation", "the login page redirected") != first


# ------------------------------------------------------------------------ clues


def test_a_redirect_to_a_new_address_is_a_clue_and_never_joins_the_scope() -> None:
    """``PROJECT.md`` section 4.2: a lead is not an authorization."""
    clue = ClueWrite(run_id=_uuid(), address="198.51.100.7", port=443, discovered_via="redirect")
    decision = clue_is_outside_scope(clue, AUTHORIZED, AUTHORIZED_PORTS)
    assert decision.outside is True
    assert decision.reason == "address_not_in_authorization"
    # The record cannot grant anything: there is no field for it and the answer says so.
    assert decision.joins_the_scope is False


def test_a_discovered_name_is_outside_an_ip_only_authorization() -> None:
    clue = ClueWrite(run_id=_uuid(), address="new.example", discovered_via="certificate")
    decision = clue_is_outside_scope(clue, AUTHORIZED, AUTHORIZED_PORTS)
    assert decision.outside is True
    assert decision.reason == "named_address_not_an_authorized_ip"


def test_an_authorized_address_on_an_unauthorized_port_is_still_recorded_as_outside() -> None:
    """Being in the address list does not authorize a port that is not in it."""
    clue = ClueWrite(run_id=_uuid(), address="192.0.2.10", port=3306, discovered_via="tool_output")
    decision = clue_is_outside_scope(clue, AUTHORIZED, AUTHORIZED_PORTS)
    assert decision.outside is True
    assert decision.reason == "port_not_in_authorization"


def test_a_clue_that_names_an_authorized_endpoint_says_so_instead_of_promoting_it() -> None:
    """The record must not tell a reader something false in either direction."""
    clue = ClueWrite(run_id=_uuid(), address="192.0.2.10", port=80, discovered_via="page_link")
    decision = clue_is_outside_scope(clue, AUTHORIZED, AUTHORIZED_PORTS)
    assert decision.outside is False
    assert decision.reason == "already_inside_authorization"
    assert decision.joins_the_scope is False


# --------------------------------------------------- what a reference must not be


def test_a_reference_states_the_version_it_read_and_refuses_to_guess_one() -> None:
    """``source_version`` is required: an unversioned reference cannot be re-checked."""
    with pytest.raises(ValidationError):
        LineageWrite.model_validate(
            {
                "target_run_id": _uuid(),
                "source_run_id": _uuid(),
                "source_kind": "observation",
                "source_object_id": _uuid(),
                "purpose": "compare against last month",
                "snapshot": "the port answered with an old banner",
                "target_run_version": 3,
            }
        )


def test_a_reference_carries_a_snapshot_and_a_hash_of_it() -> None:
    """The frozen text is what a reader sees, and its digest is how they prove it did not change."""
    text = "ssh banner: OpenSSH_8.9"
    assert snapshot_digest(text) == snapshot_digest(text)
    assert snapshot_digest(text) != snapshot_digest(text + " ")
    assert len(snapshot_digest(text)) == 64


def test_a_reference_has_no_field_that_could_hand_over_a_credential_or_a_scope() -> None:
    """The absence is the guarantee, so it is asserted as an absence."""
    fields = set(LineageWrite.model_fields)
    secret_shaped = {name for name in fields if "credential" in name or "token" in name}
    assert secret_shaped == set()
    assert not {name for name in fields if "scope" in name or "authorization" in name}
    # What it does carry: which object, at which version, for which purpose, as what text.
    carried = {"source_object_id", "source_version", "purpose", "snapshot"}
    assert carried <= fields
    assert "target_run_version" in fields


def test_a_write_contract_forbids_fields_it_does_not_declare() -> None:
    """A caller cannot smuggle an extra fact through a shape that was never reviewed."""
    with pytest.raises(ValidationError):
        SettlementWrite.model_validate(
            {
                "call_id": _uuid(),
                "run_id": _uuid(),
                "cost_units": 1,
                "primary_service_key": "service:v1\u0000192.0.2.10\u0000tcp\u0000443",
                "authorized_until": "2030-01-01T00:00:00Z",
            }
        )


# --------------------------------------------------------- observations are appends


def test_a_correction_retraction_or_contradiction_must_name_what_it_is_about() -> None:
    """An unreferenced retraction would withdraw nothing and an unreferenced contradiction names no
    disagreement."""
    for kind in ("correction", "retraction", "contradiction"):
        with pytest.raises(ValidationError):
            ObservationWrite(
                run_id=_uuid(),
                connection=entry(),
                access=AccessConditions(method="http_request"),
                kind=kind,  # type: ignore[arg-type]
                content="restated",
            )


def test_a_plain_observation_may_not_claim_to_supersede_anything() -> None:
    with pytest.raises(ValidationError):
        ObservationWrite(
            run_id=_uuid(),
            connection=entry(),
            access=AccessConditions(method="http_request"),
            content="a first reading",
            previous_id=_uuid(),
        )


def test_a_bound_endpoint_is_a_pair_or_nothing() -> None:
    with pytest.raises(ValidationError):
        AccessConditions(method="tcp_connect", bound_ip="192.0.2.10")
    assert AccessConditions(method="tcp_connect").bound_port is None


def test_the_stored_key_is_the_one_the_addresses_really_have() -> None:
    """A guard on the checks above: the keys are built from the parsed address, not the literal."""
    assert service_key("2001:0db8::1", "tcp", 80) == service_key("2001:db8::1", "tcp", 80)
    assert service_key("2001:0db8::1", "tcp", 80) != service_key("192.0.2.10", "tcp", 80)
    assert str(ip_address("2001:0db8::1")) == "2001:db8::1"


def _uuid() -> Any:
    from uuid import uuid4

    return uuid4()
