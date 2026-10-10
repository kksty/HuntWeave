"""Phase 0 inventory for P2: the six axes, the frozen-manifest inputs, and legacy-value mapping.

This module is the machine-readable half of the Phase 0 delivery required by
`docs/specs/0006-state-model-and-delivery.md` section 10 and
`docs/specs/0003-agent-research.md` section 8.1, inventoried against the real code in
`docs/specs/0010-phase0-source-inventory.md`. It deliberately **adds** contracts and changes no
existing field's meaning: nothing in P2 is implemented yet, so this module states what a later
slice must fill in rather than pretending the data exists.

Three inventories live here, and each one exists so a check can refuse a drift instead of a
reviewer having to notice it:

* :data:`AXES` — the six dimensions of `0006` section 1: their value sets, the carriers that exist
  today, the ones that do not, the reference direction, and who writes them.
* :data:`MANIFEST_INPUTS` — the complete input list of a frozen manifest. Each field says whether
  the platform can already produce it. **An ``absent`` or ``partial`` field is a gap, not a
  placeholder**: a manifest assembled before the producing slice lands must record the field as
  missing rather than invent a value for it.
* :data:`LEGACY_VALUE_MAPPINGS` / :data:`LEGACY_OBJECTS_PRESENT` — what the old values would become
  *if records carrying them existed*. The current database has no Finding, Claim, Review or
  admission record at all, so every mapping is recorded as not executed and no migration fact is
  generated from it (`0006` section 0.1, `0003` section 8.1).

The reason codes a fixed response sample may use are not re-listed here: they are read from their
real definitions so a sample cannot invent one. See
`backend/tests/test_phase0_contracts.py::test_fixed_samples_use_only_reason_codes_defined_in_source`.
"""

from dataclasses import dataclass
from typing import Literal

#: The six axes of `0006` section 1 are domain dimensions, not tables. This is the schema version
#: of the inventory itself, so a later slice that adds a field can say which inventory it read.
PHASE0_INVENTORY_SCHEMA_VERSION = 1

#: Version of the frozen-manifest input list. It is *not* the manifest's own `schema_version` at
#: runtime; it says which revision of this catalogue a manifest was frozen against.
FROZEN_MANIFEST_SCHEMA_VERSION = 1

#: Whether the platform can already produce one field of the frozen manifest.
#: ``available``: a real carrier exists today. ``partial``: a carrier exists but not the fact the
#: manifest needs. ``absent``: no carrier exists; a later slice has to create it.
Availability = Literal["available", "partial", "absent"]

#: A1. `lapsed` is separate from the verdict: a claim keeps how it was judged and whether that
#: judgement still applies.
CLAIM_VERDICTS: tuple[str, ...] = ("undetermined", "supported", "refuted", "confirmed")
CLAIM_LAPSE_STATE = "lapsed"

#: A2. Processing state and result are stored separately, and the three review kinds coexist.
REVIEW_PROCESSING_STATES: tuple[str, ...] = ("requested", "in_review", "completed", "on_hold")
REVIEW_RESULTS: tuple[str, ...] = (
    "auto_reviewed",
    "independent_reviewed",
    "human_confirmed",
    "disputed",
    "sla_escalated",
)

#: A3. Attempt lifecycle; the ending reason is recorded beside it, never instead of it.
ATTEMPT_STATES: tuple[str, ...] = (
    "active",
    "awaiting_model",
    "awaiting_prep",
    "blocked",
    "terminated_for_constraint",
    "ended",
)

#: A4. Admission binds a Finding to complete input versions, so it is not a synonym for a verdict.
ADMISSION_STATES: tuple[str, ...] = (
    "pending_review",
    "admitted_current",
    "exited_invalid",
    "revoked",
    "expired",
)

#: A6. The per-IP physical state. `quarantine` and `degraded` both still hold global capacity.
IP_RESOURCE_STATES: tuple[str, ...] = ("normal", "quarantine", "degraded")

#: `0006` section 3.1: `required_confirmation_level` is set by a versioned contract, never by the
#: model. The field does not exist in code yet (verified match count 0).
CONFIRMATION_LEVELS: tuple[str, ...] = ("auto", "independent", "human")

#: The class names of `0006` section 3.1's default mapping. The levels themselves are locked by the
#: slice owning the criteria contract, not by this inventory.
CLAIM_CLASSES: tuple[str, ...] = (
    "exec-cap(server)",
    "exec-cap(browser)",
    "authz-fail",
    "injection",
    "info-disc",
    "misconfig",
    "comp-fit",
)


@dataclass(frozen=True)
class AxisSpec:
    """One of the six dimensions, and the state of its carriers in the current build."""

    axis: str
    values: tuple[str, ...]
    #: Existing tables/contracts that carry part of this axis today, by real name.
    existing_carriers: tuple[str, ...]
    #: What has no carrier at all yet. Named objects, not a to-do list.
    missing_carriers: tuple[str, ...]
    #: Which axis this one points at, or that it points at nothing. `0006` section 2 requires
    #: A4 -> A1 as a one-way reference: A1 must never point back at A4.
    references: tuple[str, ...]
    #: The single writer of business changes for this axis.
    writers: tuple[str, ...]
    #: Facts that must stay separately queryable rather than collapsing into this axis.
    separate_facts: tuple[str, ...]


#: `0006` section 1. Existing carriers verified against `storage/models.py` and the migration chain
#: (see `docs/specs/0010-phase0-source-inventory.md`); no axis gets a table of its own.
AXES: tuple[AxisSpec, ...] = (
    AxisSpec(
        axis="A1",
        values=(*CLAIM_VERDICTS, CLAIM_LAPSE_STATE),
        existing_carriers=(),
        missing_carriers=("Claim", "ClaimEvaluation"),
        references=(),
        writers=("runs",),
        separate_facts=(
            "how the claim was judged",
            "whether that judgement is still current",
        ),
    ),
    AxisSpec(
        axis="A2",
        values=(*REVIEW_PROCESSING_STATES, *REVIEW_RESULTS),
        existing_carriers=(),
        missing_carriers=("Review", "HumanDecision"),
        references=("A1",),
        writers=("runs",),
        separate_facts=(
            "programmatic validation",
            "independent review",
            "human confirmation",
        ),
    ),
    AxisSpec(
        axis="A3",
        values=ATTEMPT_STATES,
        # No Attempt exists yet; ToolCall carries the call half of this axis today.
        existing_carriers=("tool_calls",),
        missing_carriers=("Attempt", "AttemptOutcome"),
        references=(),
        writers=("runs", "Runner"),
        separate_facts=("attempt ended", "one call finished", "claim holds"),
    ),
    AxisSpec(
        axis="A4",
        values=ADMISSION_STATES,
        existing_carriers=(),
        missing_carriers=("Finding", "AdmissionDecision", "EvidenceBinding"),
        # The one-way requirement of `0006` section 2: A4 points at A1, and A1 never points here.
        references=("A1",),
        writers=("runs",),
        separate_facts=("claim confirmed", "formally admitted"),
    ),
    AxisSpec(
        axis="A5",
        values=("proposed", "collected", "referenced", "superseded", "challenged", "gc_pending"),
        existing_carriers=("evidence", "tool_results", "retention_decisions"),
        missing_carriers=("EvidenceBinding", "retention protection record with validity_until"),
        references=(),
        writers=("Runner", "runs"),
        separate_facts=("stored", "readable", "currently proves the claim"),
    ),
    AxisSpec(
        axis="A6",
        values=(*IP_RESOURCE_STATES,),
        existing_carriers=("budget_reservations", "tool_calls.runtime", "retention_decisions"),
        missing_carriers=("physical execution quota ledger", "per-IP occupancy record"),
        references=(),
        writers=("runs", "Runner"),
        separate_facts=("logical processing finished", "physical execution stopped"),
    ),
)


@dataclass(frozen=True)
class ManifestField:
    """One input of the frozen manifest, and whether the current build can produce it.

    ``source`` names the real carrier, or the owning slice when there is none. ``availability`` is
    the honest answer about today's code, so a manifest frozen before P2-D6 can say what it could
    not lock instead of writing a value nobody produced.

    ``search`` is the token a check looks for in the backend source outside this module. For an
    ``absent`` field it is the field name itself, so "no carrier exists" is a fact a check can
    confirm rather than a claim a reviewer has to trust. A field whose carrier is named something
    else states that name, because the search has to look for a real identifier.
    """

    name: str
    type: str
    availability: Availability
    source: str
    search: str = ""


@dataclass(frozen=True)
class ManifestFieldGroup:
    """A group of `0006` section 9's manifest table."""

    group: str
    fields: tuple[ManifestField, ...]


def _field(
    name: str, type_: str, availability: Availability, source: str, search: str = ""
) -> ManifestField:
    return ManifestField(
        name=name,
        type=type_,
        availability=availability,
        source=source,
        search=search or name,
    )


#: The complete input list of a frozen manifest. Groups follow `0006` section 9's table; the
#: per-field status is this slice's own finding against the code.
MANIFEST_INPUTS: tuple[ManifestFieldGroup, ...] = (
    ManifestFieldGroup(
        group="identity",
        fields=(
            _field("manifest_id", "UUID", "absent", "P2-D6 (#70) owns the manifest record"),
            _field(
                "schema_version",
                "int",
                "available",
                "FROZEN_MANIFEST_SCHEMA_VERSION",
                "contracts/phase0.py",
            ),
            _field("run_id", "UUID", "available", "runs.id", "run_id"),
            _field(
                "run_version",
                "int",
                "partial",
                # The column exists; nothing freezes its value at freeze time, which is the fact
                # the manifest needs. The search token is the real attribute, not the field name.
                "runs.version exists as run.version; freezing the value does not",
                "run.version",
            ),
            _field("scope_version", "int", "available", "runs.scope_version", "scope_version"),
            _field(
                "frozen_at",
                "datetime",
                "partial",
                "database_now()/clock_timestamp() exists; no manifest writes it yet",
                "clock_timestamp",
            ),
            _field(
                "authoritative_source",
                "Literal['versioned_record']",
                "partial",
                "0006 section 9 names it; no field carries it",
                "AuditEvent",
            ),
        ),
    ),
    ManifestFieldGroup(
        group="rules",
        fields=(
            _field(
                "contract_versions",
                "dict[str, int]",
                "partial",
                "EXECUTION_POLICY_VERSION is the only versioned contract today",
                "EXECUTION_POLICY_VERSION",
            ),
            _field("criteria_versions", "dict[str, int]", "absent", "P2-D2 (#56) owns them"),
            _field("requirement_set_versions", "dict[str, int]", "absent", "P2-D2 (#56) owns them"),
            _field(
                "admission_policy_version",
                "int",
                "absent",
                "P2-D4 (#64) owns the admission policy",
            ),
            _field(
                "completion_rule_version",
                "int",
                "absent",
                "0006 section 4 requires it; nothing defines it yet",
            ),
        ),
    ),
    ManifestFieldGroup(
        group="claims",
        fields=(
            _field("claim_id", "UUID", "absent", "P2-D2 (#56) owns Claim"),
            _field("claim_version", "int", "absent", "P2-D2 (#56) owns Claim"),
            _field("evaluation_id", "UUID", "absent", "P2-D2 (#56) owns ClaimEvaluation"),
            _field("evaluation_version", "int", "absent", "P2-D2 (#56) owns ClaimEvaluation"),
            _field("verdict_at_freeze", "str", "absent", "P2-D2 (#56) owns ClaimEvaluation"),
            _field("lapsed_at_freeze", "bool", "absent", "P2-D2 (#56) owns ClaimEvaluation"),
            _field(
                "confirmation_level_met",
                "Literal['auto','independent','human']",
                "absent",
                "required_confirmation_level does not exist yet",
            ),
            _field(
                "confirming_review_id",
                "UUID | None",
                "absent",
                "P2-D3 (#60) owns Review",
            ),
        ),
    ),
    ManifestFieldGroup(
        group="materials",
        fields=(
            _field(
                "evidence_ids",
                "list[UUID]",
                "available",
                "Evidence.id, cited by several views",
                "evidence_ids",
            ),
            _field(
                "evidence_collected_versions",
                "dict[UUID, int]",
                "partial",
                "evidence.metadata_json carries acquisition facts as free-form JSON",
                "metadata_json",
            ),
            _field(
                "evidence_hashes",
                "dict[UUID, str]",
                "available",
                "ExecutionEvidence.sha256, archived by the Runner",
                "sha256",
            ),
            _field("bindings", "list[EvidenceBinding]", "absent", "P2-D1 (#52) owns the binding"),
            _field("fragment_versions", "dict[str, int]", "absent", "P2-D1 (#52) owns fragments"),
            _field(
                "integrity_at_freeze",
                "dict[UUID, str]",
                "partial",
                "readable/truncated/redacted travel on EvidenceView, not frozen",
                "truncated",
            ),
            _field(
                "validity_until",
                "datetime | None",
                "absent",
                "P2-D5 (#67) owns evidence validity",
            ),
            _field(
                "retention_until",
                "datetime | None",
                "partial",
                "retained artifacts carry expires_at; evidence rows do not",
                "expires_at",
            ),
            _field(
                "retention_protection_refs",
                "list[str]",
                "partial",
                "retention_decisions records pins and deletes",
                "RetainedArtifact",
            ),
        ),
    ),
    ManifestFieldGroup(
        group="decisions_and_admission",
        fields=(
            _field("review_ids", "list[UUID]", "absent", "P2-D3 (#60) owns Review"),
            _field("human_decision_ids", "list[UUID]", "absent", "P2-D4 (#64) owns HumanDecision"),
            _field(
                "severity_assessment_ids",
                "list[UUID]",
                "absent",
                "P2-D4 (#64) owns SeverityAssessment",
            ),
            _field("admission_decision_ids", "list[UUID]", "absent", "P2-D4 (#64) owns admission"),
        ),
    ),
    ManifestFieldGroup(
        group="scope_and_statistics",
        fields=(
            _field(
                "coverage_records",
                "list[Coverage]",
                "absent",
                "P2-F (#61) owns the projection",
            ),
            _field(
                "denominator_version",
                "int",
                "absent",
                "0006 section 9 requires total_basis beside it",
            ),
            _field(
                "total_basis",
                "Literal['exact','estimate','unknown']",
                "absent",
                "0006 section 9 requires it on read responses first",
            ),
            _field("primary_anchor_version", "int", "absent", "no anchor version exists yet"),
            _field(
                "settlement_reference",
                "str",
                "partial",
                "budget_reservations.id is the unique settlement reference today",
                "BudgetReservation",
            ),
            _field(
                "legacy_summary_version",
                "int",
                "partial",
                "runs.reason_code and interruption_records exist; no versioned summary",
                "InterruptionRecord",
            ),
        ),
    ),
    ManifestFieldGroup(
        group="graph_and_environment",
        fields=(
            _field(
                "graph_revision",
                "str",
                "absent",
                "P2-F (#61) owns GraphRevision",
            ),
            _field(
                "projection_schema_version",
                "int",
                "partial",
                "verify_checkpoint_schema derives it from the library; it is not persisted",
                "PostgresSaver",
            ),
            _field("filter_scope", "dict[str, Any]", "absent", "P2-F (#61) owns the projection"),
            _field(
                "snapshot_artifact_hashes",
                "dict[str, str]",
                "absent",
                "P2-D6 (#70) owns export",
            ),
            _field(
                "processed_cursor",
                "int",
                "available",
                "event_cursors.cursor",
                "EventCursor",
            ),
            _field(
                "environment_versions",
                "dict[str, str]",
                "partial",
                "tool_calls.runtime carries image_digests per call",
                "image_digests",
            ),
            _field(
                "tool_artifact_versions",
                "list[ToolVersionView]",
                "partial",
                "contracts/retention.py ToolVersionView reads the current state, not a frozen one",
                "ToolVersionView",
            ),
        ),
    ),
    ManifestFieldGroup(
        group="validity_and_retention",
        fields=(
            _field(
                "manifest_validity_until",
                "datetime | None",
                "absent",
                "no manifest exists; an absent value must mean 'no single validity window'",
            ),
            _field(
                "manifest_retention_until",
                "datetime | None",
                "absent",
                "same: named explicitly rather than filled with a date",
            ),
            _field(
                "per_input_invalidation_rules",
                "dict[str, str]",
                "absent",
                "0006 section 9 requires each input to name its own rule",
            ),
        ),
    ),
)

#: `0006` section 3.2: the queue's capacity and backpressure *policy* is configuration, not a
#: frozen input. The manifest may reference the snapshot it was frozen against, but the policy
#: numbers themselves never enter the frozen list.
NOT_IN_MANIFEST: tuple[tuple[str, str], ...] = (
    ("queue_capacity_policy", "versioned queue capacity/backpressure policy (0006 section 3.2)"),
    ("queue_depth_snapshot", "live pending count and oldest wait; a reference may be frozen"),
    ("retention_policy_thresholds", "RetentionLimits are the running deployment's policy"),
    ("readiness_gate_results", "ADR-0010 gates are re-read now, never frozen as a past fact"),
    ("resource_quota_settings", "physical quota settings are configuration (ADR-0016)"),
    ("workspace_paths", "filesystem locations of the deployment"),
    ("model_provider_credentials", "never in a manifest, a log, or a model context"),
)


@dataclass(frozen=True)
class LegacyValueMapping:
    """What one old value would become, and what it must not be read as producing.

    ``executable`` is ``False`` for every row in this inventory: the current database holds no
    record carrying these values, so a mapping is a rule for a conversion job that has nothing to
    convert. That is the point of the row, not a defect in it.
    """

    old_object: str
    old_value: str
    target_axis: str
    target_value: str
    required_inputs: tuple[str, ...]
    forbidden_fact: str
    executable: bool = False


#: `0006` section 0.1. The old Finding values below come from the design documents, not from
#: imported records; the ToolCall rows are the values the real schema can hold.
LEGACY_VALUE_MAPPINGS: tuple[LegacyValueMapping, ...] = (
    LegacyValueMapping(
        old_object="Finding",
        old_value="suspected",
        target_axis="A1",
        target_value="undetermined",
        required_inputs=("actual supporting evidence",),
        forbidden_fact="supported without evidence",
    ),
    LegacyValueMapping(
        old_object="Finding",
        old_value="supported",
        target_axis="A1",
        target_value="supported",
        required_inputs=("supporting reference", "the conditions it was obtained under"),
        forbidden_fact="a confirmation or review that was never recorded",
    ),
    LegacyValueMapping(
        old_object="Finding",
        old_value="verified",
        target_axis="A1",
        target_value="confirmed",
        required_inputs=(
            "applicable contract",
            "verifiable evaluation inputs",
            "the required confirmation level actually met",
        ),
        forbidden_fact="an independent or human review generated from the old label",
    ),
    LegacyValueMapping(
        old_object="Finding",
        old_value="inconclusive",
        target_axis="A1",
        target_value="supported",
        required_inputs=("supporting evidence",),
        forbidden_fact="refuted",
    ),
    LegacyValueMapping(
        old_object="Finding",
        old_value="refuted",
        target_axis="A1",
        target_value="refuted",
        required_inputs=("counterevidence", "the conditions", "the refuted claim version"),
        forbidden_fact="a refutation drawn from an execution failure",
    ),
    LegacyValueMapping(
        old_object="Finding",
        old_value="admitted",
        target_axis="A4",
        target_value="admitted_current",
        required_inputs=("every current admission threshold (0004 section 3)",),
        forbidden_fact="admission inferred from an attempt or from the old label",
    ),
    LegacyValueMapping(
        old_object="Finding",
        old_value="stale",
        target_axis="A1",
        target_value="lapsed",
        required_inputs=("the actual reason: validity expiry, unreadable, correction, version",),
        forbidden_fact="a guessed expiry reason or date",
    ),
    LegacyValueMapping(
        old_object="ToolCall",
        old_value="unknown",
        target_axis="A3",
        target_value="stays an execution fact",
        required_inputs=("reconciliation verdict", "the execution side's stop fact"),
        forbidden_fact="A1 refuted, or released physical occupancy",
    ),
    LegacyValueMapping(
        old_object="ToolCall",
        old_value="incomplete",
        target_axis="A3",
        target_value="stays an execution fact",
        required_inputs=("reconciliation verdict",),
        forbidden_fact="A1 refuted, or a success result",
    ),
    LegacyValueMapping(
        old_object="ReconciliationDecision",
        old_value="undetermined",
        target_axis="A3",
        target_value="stays an execution fact",
        required_inputs=("an operator verdict on one call",),
        forbidden_fact="an A1 undetermined claim verdict",
    ),
    LegacyValueMapping(
        old_object="ResearchTask/Run",
        old_value="any lifecycle status",
        target_axis="A3",
        target_value="keeps its own lifecycle",
        required_inputs=("the recorded calls and the research boundary",),
        forbidden_fact="an Attempt confirmation drawn from the last call",
    ),
)

#: Objects that exist in the real schema today, so a conversion job has something to read. Every
#: other object in `0006` section 0.1's table is a design-document object with no records.
LEGACY_OBJECTS_PRESENT: tuple[str, ...] = (
    "Run",
    "ResearchTask",
    "AgentSession",
    "Decision",
    "ToolCall",
    "ToolResult",
    "Evidence",
    "ReconciliationDecision",
)

#: Objects a conversion job would read if records existed. None of them exists in code or in the
#: schema: verified by an exact match count of 0 for each class name in `backend/src`.
LEGACY_OBJECTS_ABSENT: tuple[str, ...] = (
    "Claim",
    "ClaimEvaluation",
    "Attempt",
    "AttemptOutcome",
    "Finding",
    "EvidenceBinding",
    "Review",
    "HumanDecision",
    "SeverityAssessment",
    "AdmissionDecision",
    "FrozenManifest",
    "Host",
    "Service",
    "WebEndpoint",
)


def manifest_field_names() -> tuple[str, ...]:
    """Every field name in the frozen-manifest input list, in catalogue order."""
    return tuple(field.name for group in MANIFEST_INPUTS for field in group.fields)


def manifest_gaps() -> tuple[ManifestField, ...]:
    """The inputs this build cannot produce yet. A manifest must name these, not fill them."""
    return tuple(
        field
        for group in MANIFEST_INPUTS
        for field in group.fields
        if field.availability != "available"
    )


def legacy_mapping_can_produce_claim_verdict(old_value: str) -> bool:
    """Whether an old value may become an A1 verdict.

    The rule of `0006` sections 0.1 and 11: an execution fact is never promoted into a claim
    judgement. An execution-status value (``unknown``, ``incomplete``, a reconciliation outcome,
    a task or Run lifecycle status) answers "did this call run", not "is this claim true", so it
    cannot become a verdict however the conversion job is written.
    """
    for mapping in LEGACY_VALUE_MAPPINGS:
        if mapping.old_value == old_value:
            return mapping.target_axis == "A1" and mapping.target_value in CLAIM_VERDICTS
    raise KeyError(old_value)


def axis(name: str) -> AxisSpec:
    """One axis by its identifier. An unknown axis is refused rather than guessed at."""
    for spec in AXES:
        if spec.axis == name:
            return spec
    raise KeyError(name)


__all__ = [
    "ADMISSION_STATES",
    "ATTEMPT_STATES",
    "AXES",
    "Availability",
    "AxisSpec",
    "CLAIM_CLASSES",
    "CLAIM_LAPSE_STATE",
    "CLAIM_VERDICTS",
    "CONFIRMATION_LEVELS",
    "FROZEN_MANIFEST_SCHEMA_VERSION",
    "IP_RESOURCE_STATES",
    "LEGACY_OBJECTS_ABSENT",
    "LEGACY_OBJECTS_PRESENT",
    "LEGACY_VALUE_MAPPINGS",
    "MANIFEST_INPUTS",
    "NOT_IN_MANIFEST",
    "PHASE0_INVENTORY_SCHEMA_VERSION",
    "REVIEW_PROCESSING_STATES",
    "REVIEW_RESULTS",
    "LegacyValueMapping",
    "ManifestField",
    "ManifestFieldGroup",
    "axis",
    "legacy_mapping_can_produce_claim_verdict",
    "manifest_field_names",
    "manifest_gaps",
]
