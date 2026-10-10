"""The resumable timeline contract: a state snapshot's cursor, and the increments continuing it.

`PROJECT.md` section 12.1 asks for SSE with a cursor/`Last-Event-ID` catch-up, and `0006` section 9
asks for a snapshot plus a *continuous* increment: a client merges only increments that belong to
the same snapshot context, are contiguous and de-duplicated, and a version it cannot reconcile, a
historical gap or a cross-snapshot page returns an explicit reload requirement rather than a silent
half-state.

Those two sentences are one protocol, and this module is its wire form. Three positions have to be
distinguishable by a client that has only the response in front of it:

`committed`
    The highest cursor a writer has claimed. It can be ahead of what is readable, because the writer
    may still hold the Run's cursor row lock; a client must never treat it as "everything here is
    fetchable".
`published`
    The highest cursor a *fresh* read can actually continue through. `published == committed` means
    the client's cursor is current; `published < committed` means a write is in flight.
`retained_from`
    The highest cursor whose events retention removed. `after < retained_from` is a *gap*: the
    events are gone, and the only correct continuation is to take the state snapshot again and
    resume from its cursor (`resync_required`).

`RunSnapshot.cursor` (contracts/orchestration.py) is the `committed` position at snapshot time, so
the snapshot and this page agree on the coordinate without the snapshot having to restate the other
two. A client that concatenated a page from before a resync with one from after it would stitch two
different snapshot contexts together; `snapshot_cursor` and `resync_required` are what let it know
that it must not.

Evidence bytes are deliberately *not* file-listed or keyset-paginated here: byte ranges are read
with `offset` on the evidence endpoint (`EvidenceView.offset`/`next_offset`), which is a different
addressing scheme for a different kind of object (`0006` section 9). A keyset-paginated list of
evidence or history objects is a later slice's contract, not this one's, and mixing the two would
make "continue from here" mean two things at once.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from huntweave.contracts.orchestration import AuditEventView
from huntweave.contracts.runs import Contract

#: Version of this streaming contract. It changes when a field's *meaning* changes, not when a field
#: is added: a client that reads this number is deciding whether it understands the response at all.
EVENT_STREAM_CONTRACT_VERSION = 1

#: Why a client is being told to reload rather than to continue.
ReloadReason = Literal[
    # The cursor is older than the retained timeline: the events were pruned and cannot be replayed.
    "cursor_expired",
    # The cursor is ahead of anything claimed, so the client's coordinate is not from this timeline.
    "cursor_ahead",
    # A page belongs to a different snapshot context than the one the client is merging into.
    "snapshot_mismatch",
    # The page is not contiguous with what the client already has (a hole it did not know about).
    "history_gap",
]


class EventHistoryView(Contract):
    """One response of the resumable timeline: a contiguous page plus the positions placing it.

    ``events`` is contiguous from ``after``: the platform walks the Run's cursors and stops at the
    first hole, so a client that appends this page in order cannot skip an event that exists. A hole
    (a cursor a writer claimed and never filled) is therefore reported as ``published`` being short
    of ``committed``, not as a page with a jump in it.
    """

    run_id: UUID
    contract_version: int = Field(default=EVENT_STREAM_CONTRACT_VERSION, ge=1, strict=True)
    events: list[AuditEventView] = Field(default_factory=list)
    #: The cursor the client asked from, echoed so a response cannot be mis-stitched onto another.
    after: int = Field(ge=0, strict=True)
    #: The highest cursor a writer has claimed; may be ahead of ``published``.
    committed: int = Field(ge=0, strict=True)
    #: The highest cursor this read could continue through; never ahead of ``committed``.
    published: int = Field(ge=0, strict=True)
    #: The highest cursor whose events retention removed. ``after < retained_from`` is a gap.
    retained_from: int = Field(ge=0, strict=True)
    #: Where to continue from when this page is consumed.
    next_cursor: int = Field(ge=0, strict=True)
    #: True when the cursor is below the retained timeline: those events will never come back.
    gap: bool = False
    #: True when the client must take the state snapshot again instead of continuing from ``after``.
    resync_required: bool = False
    reload_reason: ReloadReason | None = None


class EventStreamView(Contract):
    """What the stream reports when it opens: the coordinate its increments belong to.

    A stream that begins with increments but not with its own position leaves the client guessing
    whether the first increment continues its snapshot or belongs to a timeline that was pruned
    underneath it. This frame is that answer, and it is the only frame the client needs to choose
    between continuing and resyncing.
    """

    run_id: UUID
    contract_version: int = Field(default=EVENT_STREAM_CONTRACT_VERSION, ge=1, strict=True)
    #: The snapshot coordinate the client should be holding: a mismatch is visible before merging.
    snapshot_cursor: int = Field(ge=0, strict=True)
    committed: int = Field(ge=0, strict=True)
    published: int = Field(ge=0, strict=True)
    retained_from: int = Field(ge=0, strict=True)
    #: True when this connection cannot serve the client's cursor and the client must resync first.
    resync_required: bool = False
    reload_reason: ReloadReason | None = None
    opened_at: datetime


class EventStreamFailureView(Contract):
    """The terminal frame of a stream that is ending: why this session may not keep reading.

    A stream that ends because the session expired, was revoked, or lost its storage must say which
    of those happened. "The stream closed" is not a fact an operator can act on, and treating a
    revocation as a network blip is how a console keeps showing a timeline nobody may read.
    """

    reason_code: str
    #: How long the platform may keep serving an open stream after revocation, in seconds.
    revocation_limit_seconds: int | None = Field(default=None, ge=0, strict=True)
    detail: dict[str, Any] = Field(default_factory=dict)


class EventRetentionView(Contract):
    """The Run's timeline retention state, as this platform will actually enforce it.

    It separates the two authorities explicitly, because a prune that removed events would otherwise
    be indistinguishable from a prune that removed the record of an action: `events_are_authority`
    is false by construction, and a reader can check that the business record is untouched without
    having to trust a sentence in a document.
    """

    run_id: UUID
    #: Highest claimed cursor. Equal to the snapshot's `cursor` for the same moment.
    committed: int = Field(ge=0, strict=True)
    #: Highest cursor whose events were removed; 0 means nothing was ever pruned.
    retained_from: int = Field(ge=0, strict=True)
    #: How many events are still held for this Run.
    retained_events: int = Field(ge=0, strict=True)
    #: How many events were removed. The platform counts what it did, not what it intended to do.
    pruned_events: int = Field(ge=0, strict=True)
    #: The policy this deployment runs under, stated with the observation. `policy_enabled` false
    #: means "keep everything": a timeline that is never pruned cannot produce a gap.
    policy_enabled: bool
    keep_events: int = Field(ge=1, strict=True)
    #: False by construction: the versioned business record is the authority, the event log is the
    #: incremental projection and the catch-up cursor (0006 section 9).
    events_are_authority: Literal[False] = False
    #: The sources that answer "what did this Run do" without the event log.
    authoritative_sources: list[str] = Field(default_factory=list)
