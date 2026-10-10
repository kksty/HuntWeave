"""Service facts, entry identity, observations and read-only cross-Run lineage.

Three things live here, and they are separate on purpose:

* **The identity functions.** ``host_key`` / ``service_key`` / ``web_entry_key`` are
  pure programs over the facts one observation carries. They decide *which* host, service
  and web entry a record is
  about. Nothing in them calls a model, and nothing in them consults today's configuration: the same
  arguments always produce the same key, so two Runs observing the same entry agree, and two
  different entries never collapse into one.
* **The observation contract.** An observation is append-only. It names the Run and call that
  produced it, when it was taken, under which access conditions, and which resolver version read it.
  A correction, a retraction and a contradiction are *new records that point at the old one* — there
  is no update path, because overwriting the original would lose the very disagreement the record
  exists to keep.
* **The four records of one research relation** (ADR-0015): what service a call was *actually bound*
  to, what a Run *covered*, which entry is the *navigation anchor*, and what was *charged*. They are
  four tables, each with its own rule version and input versions, and none is derived from the
  current navigation pointer.

``PROJECT.md`` section 4.2 fixes the locator: ``IP + transport + port`` is the service locating key,
and an HTTP entry additionally carries the real connection IP, port, scheme, Host/SNI and path. The
builders below are that sentence, made executable.
"""

import hashlib
import json
import posixpath
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huntweave.contracts.errors import ServiceError

#: Version of the identity computation and of the record shapes in this module. It travels on every
#: identity row and every observation, so an observation read back later says which resolver
#: produced it. Bump it when a key composition, a normalization rule or a stored shape changes in a
#: way that is not interchangeable with the previous version.
FACTS_IDENTITY_VERSION = 1

#: The transport a service key is composed with. Only TCP exists in this build; the field is still
#: written down rather than assumed, because ``IP + transport + port`` is the locator and a key that
#: silently means TCP cannot be extended to another transport without changing meanings.
TransportName = Literal["tcp"]

#: What one observation record is, relative to what came before it.
#:
#: ``observation`` is a first reading. ``correction`` restates the same subject with what the writer
#: now believes is the right content. ``retraction`` withdraws an earlier record without claiming a
#: replacement. ``contradiction`` records a reading that *disagrees* with an earlier one and keeps
#: both: it is what makes "矛盾可并存? a stored fact rather than a note.
ObservationKind = Literal["observation", "correction", "retraction", "contradiction"]

#: How one observation was taken. The access conditions are part of the record because the same
#: address read through a different route is not the same fact: a plain TCP connect, an HTTP request
#: with an explicit Host, and an authenticated session see different things.
AccessMethod = Literal["tcp_connect", "http_request", "tls_handshake", "tool_output", "import"]

#: Where a frozen cross-Run reference points. Only the kinds this build can freeze are listed; a
#: reference to anything else is refused rather than stored as an undefined pointer.
ReferenceKind = Literal["observation", "audit_event", "tool_result", "evidence"]

def canonical_ip(value: str) -> str:
    """One IP written one way, or a refusal.

    ``ipaddress`` is the only authority here: it is what the Run input parser already uses
    (``runs/inputs.py``), so the identities computed from an observation and the addresses a Run was
    authorized against come from the same normalization. An IPv6 zone identifier is refused rather
    than stripped: ``fe80::1%eth0`` is not a globally meaningful address, and shortening it to
    ``fe80::1`` would assert a fact the observer did not report.
    """
    if "%" in value:
        raise ServiceError("invalid_ip", 422)
    try:
        return str(ip_address(value))
    except ValueError:
        raise ServiceError("invalid_ip", 422) from None


def canonical_host(value: str | None) -> str | None:
    """A host/SNI name written one way, or ``None`` when the record carries no name at all.

    ``None`` is "this entry was addressed by address", a statement about how it was
    reached. An empty name, a bare root label and a name that is nothing but a trailing
    dot are all "a name was recorded and it was not a name", so they are ``None`` too:
    none of them is a name this platform may key an entry on, and inventing one would
    create an entry nobody connected to.

    Case, a trailing root dot, surrounding whitespace and an IDNA-encoded form are all the same
    name, so they are folded together \u2014 otherwise the same application observed twice through
    differently spelled Host headers would become two entries, which is the over-split twin of the
    over-merge this module exists to prevent.
    """
    if value is None:
        return None
    name = value.strip()
    if name.endswith("."):
        name = name[:-1]
    if not name:
        return None
    lowered = name.lower()
    try:
        # A non-ASCII name is stored in its IDNA form, so `BÜCHER.example` and
        # `xn--bcher-kva.example` are one host rather than two.
        return lowered.encode("idna").decode("ascii")
    except UnicodeError:
        raise ServiceError("invalid_request", 422) from None


#: The characters a percent-escape may stand for and still be folded into the path it names. The
#: unreserved set of RFC 3986 plus the reserved characters that *cannot* change what resource the
#: path addresses. `%2F` and `%3F` are deliberately absent: decoding them would turn
#: `/a%2Fb` into `/a/b`, which is a different resource, and that is exactly the over-merge this
#: function has to avoid.
_FOLDABLE_ESCAPES = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@"
)


def _fold_escapes(path: str) -> str:
    """Percent-decode only the escapes that cannot name a different resource."""
    out: list[str] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == "%" and index + 2 < len(path) + 1:
            escape = path[index + 1 : index + 3]
            if len(escape) == 2 and all(c in "0123456789abcdefABCDEF" for c in escape):
                decoded = chr(int(escape, 16))
                if decoded in _FOLDABLE_ESCAPES:
                    out.append(decoded)
                    index += 3
                    continue
                # `%2F` and friends keep their spelling: they are part of what the path names.
                out.append(path[index : index + 3].upper())
                index += 3
                continue
        out.append(char)
        index += 1
    return "".join(out)


def canonical_path(value: str) -> str:
    """A request path written one way, or a refusal.

    The things that make two spellings of one path equal are folded: percent-escapes that cannot
    name a different resource, a duplicate slash, a ``.`` or ``..`` segment, and a trailing slash.
    The trailing slash is included deliberately: ``/login`` and ``/login/`` are the same resource to
    almost every server, so treating them as two entries would double-count one application — the
    over-split twin of the over-merge this function exists to prevent. A server that really does
    distinguish them is a server whose two responses differ in *content*, and content is observed,
    not keyed.

    Everything else is kept, including the query string \u2014 a handler that answers
    differently for ``?id=1`` and ``?id=2`` addresses two resources, and merging them would claim
    coverage that was never measured.

    A path must be absolute and must not begin with ``//``: the latter names another authority, and
    an identity computed from it would belong to an entry nobody connected to.
    """
    if not value.startswith("/"):
        raise ServiceError("invalid_request", 422)
    if value.startswith("//") or value.startswith("/\\"):
        raise ServiceError("invalid_request", 422)
    if any(char < " " or char == "\x7f" for char in value):
        raise ServiceError("invalid_request", 422)
    folded = _fold_escapes(value)
    path, separator, query = folded.partition("?")
    normalized = posixpath.normpath(path)
    # `normpath` keeps `/` for the root and returns `.` for an empty path; both become `/`.
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/") or "/"
    return normalized + (separator + query if separator else "")


#: The separator between an identity key's kind, its version and its parts. PostgreSQL text columns
#: cannot store a NUL byte, and a key that cannot be written is not an identity, so the
#: separator is a
#: printable character. It is not what keeps the parts unambiguous — the length prefixes below are.
KEY_SEPARATOR = ";"


def _key(kind: str, parts: list[tuple[str, str]]) -> str:
    """``kind:vN;<length>:<name>=<value>;...`` — one identity, unambiguous by construction.

    Each part is length-prefixed, so ``7:ip=1.2.3.4`` says "the next seven characters
    are this part". A value that contains the separator, an ``=`` or even the shape of
    another part cannot be read as
    one: a plain join would let a hostname of ``x;ip=evil`` produce the same key as a different
    (ip, host) pair, which is exactly the collision an identity has to be immune to.

    The version is stamped here rather than kept beside the key, so a key read back from the
    database says which resolver produced it and today's build cannot silently re-key an old
    record.
    """
    encoded = []
    for name, value in parts:
        field = f"{name}={value}"
        encoded.append(f"{len(field)}:{field}")
    return f"{kind}:v{FACTS_IDENTITY_VERSION}{KEY_SEPARATOR}" + KEY_SEPARATOR.join(encoded)


def host_key(address: str) -> str:
    """The identity of a host: the canonical address, and nothing else.

    A host is an address. A hostname discovered along the way is a *clue* about it, not part of what
    makes it the same host: the same address reached under two names is one host, and two addresses
    that answer to one name are two.
    """
    return _key("host", [("ip", canonical_ip(address))])


def service_key(address: str, transport: TransportName, port: int) -> str:
    """``IP + transport + port`` —the service locating key of ``PROJECT.md`` section 4.2.

    The service key is deliberately *coarser* than the web entry key: it names the socket, not the
    application. Two virtual hosts on one socket are two web entries and one service, which is what
    makes "I reached the box" and "I reached the application" different statements.
    """
    if not 1 <= int(port) <= 65535:
        raise ServiceError("invalid_ports", 422)
    if transport != "tcp":
        raise ServiceError("invalid_request", 422)
    return _key(
        "service",
        [
            ("ip", canonical_ip(address)),
            ("transport", transport),
            ("port", str(int(port))),
        ],
    )


def web_entry_key(
    address: str,
    port: int,
    transport: TransportName,
    scheme: Literal["http", "https"],
    host: str | None,
    path: str,
) -> str:
    """The identity of one web entry: everything that decides which application answers.

    The composition is the whole point of issue #44's first criterion. Two requests that reach the
    same socket but address different applications must not become one entry, so the key carries
    the **host/SNI name** and the **path** on top of the service locator. Two requests to the same
    application over **different schemes** are also two entries: ``http://h/`` and ``https://h/``
    can be served by different software and have different coverage, so folding them would claim a
    TLS entry was reached because a cleartext one was.

    That is also why the key is *not* ``service_key + path``: the host is part of this identity, not
    a descriptive attribute of it.

    What does **not** enter the key, and why:

    * the Run, the call and the observation time \u2014 the identity axis is about the subject,
      not the study of it; carrying the Run would make every Run invent its own host and destroy
      the cross-Run comparison this module exists for;
    * the resolver version as a *component* \u2014 it is recorded beside the key as
      ``identity_version``, so re-reading an old record says which program produced its key instead
      of silently re-keying it;
    * page titles, server banners, versions and confidence \u2014 those are observations *about* the
      entry, and a banner change must not create a second entry.
    """
    return _key(
        "entry",
        [
            ("ip", canonical_ip(address)),
            ("transport", transport),
            ("port", str(int(port))),
            ("scheme", scheme),
            ("host", canonical_host(host) or "-"),
            ("path", canonical_path(path)),
        ],
    )


def clue_key_of(address: str, transport: str, port: str) -> str:
    """The identity of one lead within one Run: the address, the transport and the port.

    Two mentions of one address — in a redirect body and in the certificate of the same connection —
    are one lead, so a reader is not shown the same address five times and made to think five things
    were found.
    """
    return _key("clue", [("address", address), ("transport", transport), ("port", port)])


def identity_version_of(key: str) -> int:
    """The version stamped into a key, read back from the key itself.

    Reading it out of the key rather than from the current constant is what keeps an old record
    honest: a row written under version 1 keeps saying so after the program moves on. A string that
    is not a key this module could have built is refused rather than read as version zero.
    """
    if ":v" not in key:
        raise ServiceError("invalid_request", 422)
    head, _, rest = key.partition(":v")
    version, separator, _parts = rest.partition(KEY_SEPARATOR)
    if not head or not separator or not version.isdigit():
        raise ServiceError("invalid_request", 422)
    return int(version)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccessConditions(Contract):
    """Under what conditions one observation was taken.

    Kept because a fact read through a different route is a different fact. ``authenticated`` means
    the observer could see something an anonymous request could not —it is recorded as a condition
    of *this observation*, and it grants nothing to the Run that reads it later.
    """

    method: AccessMethod
    #: The ticket's own binding: which authorized endpoint the observing call was
    #: dispatched against.
    #: Recorded so a reader can tell "we reached what we were pointed at" from "we followed a
    #: redirect somewhere else".
    bound_ip: str | None = None
    bound_port: int | None = None
    scope_version: int | None = Field(default=None, ge=1, strict=True)
    authenticated: bool = False
    request_headers: dict[str, str] = Field(default_factory=dict)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _a_bound_endpoint_is_a_pair(self) -> "AccessConditions":
        if (self.bound_ip is None) != (self.bound_port is None):
            raise ValueError("A bound endpoint is a pair: give both the address and the port")
        return self


class ConnectionFact(Contract):
    """How one observer really reached the subject.

    Every field here is something the observer had to know to connect at all. ``address`` is the
    **real connection target**, which is not always the ticket's binding: a redirected request, a
    proxy hop or a page link can lead somewhere else, and that somewhere else is a clue, not an
    authorization (``PROJECT.md`` section 4.2). Keeping the two apart is what lets the platform say
    "we connected here" and "we were allowed to connect here" as two different sentences.
    """

    address: str
    transport: TransportName = "tcp"
    port: int = Field(ge=1, le=65535, strict=True)
    scheme: Literal["http", "https"] | None = None
    host: str | None = None
    path: str | None = None
    #: ``host`` is the HTTP Host header; ``sni`` is the TLS server name. They are usually equal and
    #: are not the same field: a client may send a Host header for one name and negotiate SNI for
    #: another, and an identity that assumes they agree would hide that.
    sni: str | None = None

    @model_validator(mode="after")
    def _a_web_entry_names_its_scheme_and_path(self) -> "ConnectionFact":
        if self.scheme is not None and self.path is None:
            raise ValueError("An HTTP observation names the path it requested")
        if self.path is not None and self.scheme is None:
            raise ValueError("A path without a scheme names no web entry")
        return self

    @property
    def host_identity(self) -> str:
        return host_key(self.address)

    @property
    def service_identity(self) -> str:
        return service_key(self.address, self.transport, self.port)

    @property
    def web_entry_identity(self) -> str | None:
        """The entry key, or ``None`` when this is not an HTTP observation.

        A TCP connect to a port is a service fact and says nothing about which application is
        behind it, so it produces no web entry rather than one with an empty Host.
        """
        if self.scheme is None or self.path is None:
            return None
        return web_entry_key(
            self.address, self.port, self.transport, self.scheme, self.host, self.path
        )

    @property
    def service_view(self) -> "ServiceIdentity":
        return ServiceIdentity(
            key=self.service_identity,
            address=canonical_ip(self.address),
            transport=self.transport,
            port=int(self.port),
        )

    @property
    def web_entry_view(self) -> "WebEntryIdentity | None":
        key = self.web_entry_identity
        if key is None or self.scheme is None or self.path is None:
            return None
        return WebEntryIdentity(
            key=key,
            service_key_value=self.service_identity,
            scheme=self.scheme,
            host=canonical_host(self.host),
            sni=canonical_host(self.sni),
            path=canonical_path(self.path),
        )


@dataclass(frozen=True)
class HostIdentity:
    """A host as a program computes it: the key, plus the descriptive parts of the same address."""

    key: str
    address: str
    version: int = FACTS_IDENTITY_VERSION


@dataclass(frozen=True)
class ServiceIdentity:
    """One service: the socket the locator names, with the parts kept beside the key for display."""

    key: str
    address: str
    transport: str
    port: int
    version: int = FACTS_IDENTITY_VERSION


@dataclass(frozen=True)
class WebEntryIdentity:
    """One web entry: which application answers on a socket, under which name and path."""

    key: str
    service_key_value: str
    scheme: str
    host: str | None
    sni: str | None
    path: str
    version: int = FACTS_IDENTITY_VERSION


class ObservationWrite(Contract):
    """One appended observation, as a caller states it.

    There is no field for the record's own id and none for the previous record: the platform derives
    both, so a caller cannot rewrite history by naming it. ``content`` is text because the original
    wording is the thing that must survive; a structured reading of it goes in ``facts`` and never
    replaces it.
    """

    run_id: UUID
    call_id: UUID | None = None
    connection: ConnectionFact
    access: AccessConditions
    kind: ObservationKind = "observation"
    content: str = Field(min_length=1, max_length=200_000)
    facts: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, strict=True)
    parser_version: str = Field(default="human-v1", min_length=1, max_length=80)
    #: Required for a correction, a retraction and a contradiction: the record this one is about.
    #: A retraction with nothing to retract would withdraw nothing, and an unreferenced
    #: contradiction would be an observation with a misleading name.
    previous_id: UUID | None = None

    @model_validator(mode="after")
    def _the_kind_says_what_it_points_at(self) -> "ObservationWrite":
        if self.kind != "observation" and self.previous_id is None:
            raise ValueError(
                f"A {self.kind} names the record it corrects, retracts or contradicts"
            )
        if self.kind == "observation" and self.previous_id is not None:
            raise ValueError(
                "A plain observation supersedes nothing; use a correction or retraction"
            )
        return self


class LineageWrite(Contract):
    """One explicit cross-Run reference, as the operator states it.

    The reference is deliberately about *one frozen object at one version*. There is no field for
    "inherit the source Run's results", because that is the thing this record exists to prevent: a
    reference carries a snapshot and a purpose, and nothing else travels with it.
    """

    target_run_id: UUID
    source_run_id: UUID
    source_kind: ReferenceKind
    source_object_id: UUID
    #: The version the operator read and wants frozen. It is recorded next to the
    #: snapshot, so a reader can
    #: check that the snapshot really belongs to the version that was named.
    source_version: int = Field(ge=1, strict=True)
    purpose: str = Field(min_length=1, max_length=500)
    #: What the source object says, verbatim, at the moment of reference. The source may
    #: keep moving;
    #: this text does not.
    snapshot: str = Field(min_length=1, max_length=200_000)
    #: The Run version the reference was made against, so a stale request is refused rather than
    #: silently filed under a Run that has already moved.
    target_run_version: int = Field(ge=1, strict=True)


class ServiceAttachmentWrite(Contract):
    """One call's *actual* service binding, as its evidence states it.

    This is the first of ADR-0015's four records: which service a call really acted on. It is a
    record of its own because "the call was dispatched against service A" and "the call reached
    service B" are different facts, and the second is the one a reader needs to attribute results.
    """

    run_id: UUID
    call_id: UUID
    observation_id: UUID
    entry_key: str | None = None
    #: Whether this binding was written down before the call acted (``planned``) or from its result
    #: afterwards (``observed``). A planned binding is an intention; only an observed one is a fact.
    basis: Literal["planned", "observed"]
    rule_version: int = Field(default=FACTS_IDENTITY_VERSION, ge=1, strict=True)


class CoverageWrite(Contract):
    """What a Run covered on one service —the second record, and not the first.

    ``directly_verified`` separates "we tested this service" from "we passed through it while
    testing another", which is what makes "用A 的条件验验B 只覆盖B" checkable instead of a slogan.
    Coverage names the service, and may name the entry it was measured on; it never names an anchor,
    because an anchor is a navigation choice and this is a measurement.
    """

    run_id: UUID
    service_key_value: str
    call_id: UUID | None = None
    entry_key: str | None = None
    directly_verified: bool
    rule_version: int = Field(default=FACTS_IDENTITY_VERSION, ge=1, strict=True)
    input_versions: dict[str, int] = Field(default_factory=dict)


class NavigationWrite(Contract):
    """A versioned choice of which entry is a Run's primary anchor —the third record.

    Changing the anchor is a navigation adjustment and nothing else: no new attempt, no new charge,
    no rewritten coverage. That is why this record carries a version and a reason but no cost and no
    coverage field to accidentally update.
    """

    run_id: UUID
    entry_key: str
    service_key_value: str
    reason: str = Field(min_length=1, max_length=500)
    run_version: int = Field(ge=1, strict=True)
    rule_version: int = Field(default=FACTS_IDENTITY_VERSION, ge=1, strict=True)


class SettlementWrite(Contract):
    """What was charged for one call —the fourth record, and the only one that may charge.

    ``call_id`` is the settlement's identity because a call's budget is reserved once
    (``budget_reservations.call_id`` is unique) and settled once. A cross-service call therefore
    produces **one** settlement and several service attachments; service-level cost is a derived
    statistic with its own rule version and is never used to enforce a budget (ADR-0015).
    """

    call_id: UUID
    run_id: UUID
    cost_units: int = Field(ge=0, strict=True)
    output_bytes: int = Field(default=0, ge=0, strict=True)
    rule_version: int = Field(default=FACTS_IDENTITY_VERSION, ge=1, strict=True)
    #: The service the call *directly* verified, named by its computed key. It is first in the
    #: derived shares and is the one marked ``anchor``. A key naming no service of this Run's
    #: project is refused rather than stored as a share pointing at nothing.
    primary_service_key: str = Field(min_length=1, max_length=200)
    #: The other services the same call reached, named by their computed keys. Each becomes a share
    #: marked ``through``: a cross-service call is charged once and reported once per service
    #: (issue #44 criterion 3, *不按主锚点重复结算*). They are refused on exactly the same terms as
    #: the primary key, and the service layer re-checks every one of them rather than trusting this
    #: route's pre-check.
    extra_service_keys: list[str] = Field(default_factory=list, max_length=64)


class ClueWrite(Contract):
    """An address a Run ran into that is **not** part of its authorization.

    ``PROJECT.md`` section 4.2: a new domain or IP in a redirect, a certificate, a response body or
    a page link is a lead only, and never joins the authorized scope by itself. This record is where
    such an address is put, and the type has no field that could widen a scope even by mistake.
    """

    run_id: UUID
    call_id: UUID | None = None
    address: str
    transport: TransportName = "tcp"
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    discovered_via: Literal[
        "redirect", "certificate", "response_body", "page_link", "tool_output", "operator"
    ]
    note: str = Field(default="", max_length=500)


@dataclass(frozen=True)
class ClueDecision:
    """Whether a discovered address is outside the authorization, and why.

    ``outside`` is the answer this module computes; a caller that records a clue does not get to
    decide it. That is the difference between "we noticed an address" and "we decided it is in
    scope".
    """

    address: str
    outside: bool
    reason: str

    @property
    def joins_the_scope(self) -> bool:
        return False


def clue_is_outside_scope(
    clue: ClueWrite, targets: list[str] | tuple[str, ...], ports: list[int] | tuple[int, ...]
) -> ClueDecision:
    """Decide, from the Run's own authorized snapshot, whether a discovered address is outside it.

    The comparison is against the addresses and ports the Run was authorized with and nothing else:
    no hostname resolution, no netblock, no "it looked related". An address that *is* in the
    authorized set is still not promoted to a scope \u2014 the snapshot is the only thing that
    grants \u2014 but the record then says the lead was inside the authorization rather than outside
    it, so a reader is not told something false.
    """
    try:
        address = canonical_ip(clue.address)
    except ServiceError:
        # A name rather than an address (a domain from a certificate or a link) is outside every
        # IP-only authorization by construction. It is recorded as a clue and resolves to nothing.
        return ClueDecision(clue.address, True, "named_address_not_an_authorized_ip")
    if address not in set(targets):
        return ClueDecision(address, True, "address_not_in_authorization")
    if clue.port is not None and clue.port not in set(ports):
        return ClueDecision(address, True, "port_not_in_authorization")
    return ClueDecision(address, False, "already_inside_authorization")


def facts_hash(
    connection: ConnectionFact,
    kind: ObservationKind,
    content: str,
    *,
    run_id: UUID | None = None,
    observed_at: datetime | None = None,
) -> str:
    """The digest that decides whether two observation records state the same thing.

    Only the *subject and the claim* enter the payload: where it connects, what kind of record it
    is, and what it says. The Run, the call, the time, the access conditions and the parser version
    are deliberately outside.

    The observer's own fields are **parameters** rather than absent arguments so that exclusion
    is a statement the code makes and a check can hold: a mutation that starts folding the Run or
    the time into the payload changes the digest, and
    `test_the_key_does_not_depend_on_the_run_or_the_time` fails. An earlier revision took only the
    three facts and left the exclusion to a docstring — the mutation stayed green, which is the
    "check exists but holds nothing" failure this repository has been bitten by before.

    What the exclusion buys: the same reading taken twice by one call is one record, while the same
    reading taken by a second Run is a second, independently sourced record — the behaviour
    ``PROJECT.md`` section 4.2 asks for with "相同 IP/端口的不同运行观察不相互覆盖".
    """
    del run_id, observed_at
    payload = {
        "connection": connection.model_dump(mode="json"),
        "kind": kind,
        "content": content,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def snapshot_digest(snapshot: str) -> str:
    """Hash of a frozen cross-Run snapshot, so a reader can prove the bytes did not change."""
    return hashlib.sha256(snapshot.encode()).hexdigest()


#: The facts one observation row carries, as the API and the checks read them back.
class ObservationView(Contract):
    id: UUID
    run_id: UUID
    call_id: UUID | None
    host_key_value: str
    service_key_value: str
    entry_key: str | None
    kind: ObservationKind
    content: str
    facts: dict[str, Any]
    confidence: float | None
    connection: ConnectionFact
    access: AccessConditions
    parser_version: str
    resolver_version: int
    identity_version: int
    facts_hash: str
    previous_id: UUID | None
    observed_at: datetime
    #: Derived on read, never stored as the record's identity: a superseded or retracted record is
    #: still the original record, and this only says how it currently stands.
    state: Literal["current", "superseded", "retracted", "contradicted"] = "current"


class HostView(Contract):
    key: str
    address: str
    identity_version: int
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int


class ServiceView(Contract):
    key: str
    host_key_value: str
    address: str
    transport: str
    port: int
    identity_version: int
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int


class WebEntryView(Contract):
    key: str
    service_key_value: str
    scheme: str
    host: str | None
    sni: str | None
    path: str
    identity_version: int
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int


class LineageView(Contract):
    """One explicit historical reference, frozen.

    ``snapshot`` is what the source said at the moment of reference. The two booleans are always
    false and are part of the interface on purpose: a reader of the API should be able to see, in
    the response itself, that a reference does not hand over credentials or a scope, rather than
    having to trust a document that says so.
    """

    id: UUID
    target_run_id: UUID
    source_run_id: UUID
    source_kind: ReferenceKind
    source_object_id: UUID
    source_version: int
    purpose: str
    snapshot: str
    snapshot_sha256: str
    identity_version: int
    retained_until: datetime
    created_at: datetime
    created_by_session_id: UUID
    inherits_credentials: Literal[False] = False
    inherits_authorization: Literal[False] = False


class ServiceFactsPage(Contract):
    """One page of a project's service facts, with the Run version it was read at.

    The version travels with the read so a mutation can be fenced against what the reader saw,
    which is the existing ``version_conflict`` rule rather than a new one.
    """

    project_id: UUID
    run_version: int
    hosts: list[HostView]
    services: list[ServiceView]
    entries: list[WebEntryView]
