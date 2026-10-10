"""Phase 0 of P2 has to be checkable against the real code, not against the design document.

`docs/specs/0006-state-model-and-delivery.md` section 10 asks for a source inventory, an old-value
mapping, the six axes, the frozen-manifest input schema and the locked confirmation levels, and
`PROJECT.md` section 14 requires that preparation to be reviewed against actual code before a
slice opens. A stack of prose is exactly the kind of evidence that rots quietly: the migrations
gain a revision, the ORM gains a class, and the inventory keeps describing last month.

The checks here read the code instead of trusting the inventory:

* every table the ORM declares and every table the migration chain creates is either carried by an
  ORM class or recorded as a raw-SQL-only table;
* the migration chain is linear and has exactly one head, and that head is the revision the build
  says it serves;
* the inventory document's table list matches the schema the code produces, so prose cannot drift
  from the migrations;
* the fixed response samples load, and every reason code an error sample uses is defined in the
  source — no sample may invent a code.

Nothing here needs PostgreSQL or the Runner: these are source-level checks, which is what a
mapping/design slice can prove. The runtime half of Phase 0 is recorded, with its limits, in
`docs/validation/0023-phase0-source-inventory.md`.
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal, get_args

import pytest
from pydantic import BaseModel, ValidationError

from huntweave.contracts.capabilities import Capabilities, ReadinessGateName
from huntweave.contracts.orchestration import EvidenceView, ToolCallView
from huntweave.contracts.phase0 import (
    AXES,
    CLAIM_CLASSES,
    CLAIM_VERDICTS,
    CONFIRMATION_LEVELS,
    LEGACY_OBJECTS_ABSENT,
    LEGACY_OBJECTS_PRESENT,
    LEGACY_VALUE_MAPPINGS,
    MANIFEST_INPUTS,
    NOT_IN_MANIFEST,
    axis,
    legacy_mapping_can_produce_claim_verdict,
    manifest_field_names,
    manifest_gaps,
)
from huntweave.execution.capabilities import gate_reason_codes

REPOSITORY = Path(__file__).resolve().parents[2]
BACKEND = REPOSITORY / "backend"
SOURCE = BACKEND / "src" / "huntweave"
MODELS_FILE = SOURCE / "storage" / "models.py"
VERSIONS_DIR = BACKEND / "migrations" / "versions"
DATABASE_FILE = SOURCE / "storage" / "database.py"
INVENTORY_DOC = REPOSITORY / "docs" / "specs" / "0010-phase0-source-inventory.md"
PHASE0_RECORD = REPOSITORY / "docs" / "validation" / "0023-phase0-source-inventory.md"
SAMPLES_DIR = Path(__file__).resolve().parent / "data" / "phase0"


def read_text(path: Path) -> str:
    """Documents are read as bytes-to-UTF-8: their counts are asserted, so decoding matters."""
    return path.read_text(encoding="utf-8")

# The schema the business role must find behind `alembic_version`. Read from `database.py` rather
# than repeated here: a new migration has to add its id in the same commit, and this check is how
# that promise stays a promise.
BUSINESS_REVISIONS_PATTERN = re.compile(r"^BUSINESS_REVISIONS = \((.*?)\)", re.M | re.S)
ORM_TABLE_PATTERN = re.compile(r'^    __tablename__ = "([a-z_]+)"', re.M)
REVISION_PATTERN = re.compile(r'^revision = "([^"]+)"', re.M)
DOWN_REVISION_PATTERN = re.compile(r'^down_revision = (?:None|"([^"]+)")', re.M)
# `op.create_table("name", ...)` for the multi-line form and `op.create_table(\n "name",` for the
# one the migration chain uses when it builds a table from a column dictionary.
OP_CREATE_TABLE_PATTERN = re.compile(r"op\.create_table\(\s*\"([a-z_]+)\"")
# One migration builds seven tables from a column dictionary, so the names are the dict keys
# rather than a literal argument. Missing them would make this check blind to most of the schema.
TABLE_DICT_PATTERN = re.compile(r"^    tables = \{(.*?)^    \}", re.M | re.S)
TABLE_DICT_KEY_PATTERN = re.compile(r'^        "([a-z_]+)": \[', re.M)
DDL_CREATE_TABLE_PATTERN = re.compile(
    r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+(?:\.[a-z_]+)?)", re.I
)


def created_table_names(text: str) -> set[str]:
    """Every table one migration file creates, however it spells the call."""
    names = set(OP_CREATE_TABLE_PATTERN.findall(text))
    for block in TABLE_DICT_PATTERN.findall(text):
        names.update(TABLE_DICT_KEY_PATTERN.findall(block))
    for match in DDL_CREATE_TABLE_PATTERN.findall(text):
        names.add(match.split(".")[-1])
    return names

# Tables the migrations create but no ORM class maps. Declared here as the expected set, so a new
# raw-SQL table fails this check instead of quietly missing from the inventory.
RAW_SQL_ONLY_TABLES = {"runtime_processes"}
# Tables a migration creates and a later migration drops. They are part of the real history — 0007
# removed the login throttle on purpose — so the inventory records them as dropped rather than
# pretending they were never there.
DROPPED_TABLES = {"login_buckets"}
# Tables the schema carries that are not business objects: Alembic's own revision table lives in
# the business schema because that is where `alembic_version` is created.
NON_BUSINESS_TABLES = {"alembic_version"}
# Objects the checkpoint schema owns, in its own schema (harness/checkpoints.py).
CHECKPOINT_SCHEMA_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}

SampleKind = Literal["response", "error"]


class ErrorEnvelope(BaseModel):
    """The one error body the platform serves (`api/app.py:84-90`)."""

    model_config = {"extra": "forbid"}

    reason_code: str


#: Which contract a response sample has to load as. A sample names its model here; the sample
#: cannot choose a laxer parser than the production one.
MODEL_FOR: dict[str, type[BaseModel]] = {
    "capabilities": Capabilities,
    "evidence": EvidenceView,
    "tool_call": ToolCallView,
    "error_envelope": ErrorEnvelope,
}

#: The must-fail states of `0006` section 11 this slice ships a fixed sample for, and what each
#: sample has to show. `not_ready` and `verdict_unknown` are states a normal response carries;
#: the rest are refusals the caller has to be able to tell apart.
REQUIRED_SAMPLE_STATES: dict[str, str] = {
    "not_ready": "real execution not ready, with each unmet gate naming its reason",
    "unknown": "an execution fact left unknown, never promoted to a claim verdict",
    "stop_unconfirmed": "a stop the execution side has not confirmed",
    "version_conflict": "a stale version, refused before anything is written",
    "evidence_missing": "evidence named but not readable, with its own reason",
    "permission_denied": "an authorization refusal, distinct from policy and model refusals",
    "invalid_call_state": "a well-formed request against a call in the wrong state",
    "reconciliation_conflict": "a second verdict on a call that already carries one",
}


def read_sample(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sample_paths() -> list[Path]:
    return sorted(SAMPLES_DIR.glob("*.json"))


def orm_table_names() -> set[str]:
    return set(ORM_TABLE_PATTERN.findall(MODELS_FILE.read_text(encoding="utf-8")))


def migration_tables() -> dict[str, set[str]]:
    """Every table the migration chain creates, with the revisions that create it."""
    found: dict[str, set[str]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        for name in created_table_names(path.read_text(encoding="utf-8")):
            found.setdefault(name, set()).add(path.name)
    return found


def migration_chain() -> dict[str, str | None]:
    """Each revision and the revision it revises, read from the migration files themselves."""
    chain: dict[str, str | None] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        revision = REVISION_PATTERN.search(text)
        down = DOWN_REVISION_PATTERN.search(text)
        assert revision is not None, f"{path.name} declares no revision"
        assert down is not None, f"{path.name} declares no down_revision"
        chain[revision.group(1)] = down.group(1)
    return chain


def business_revisions() -> tuple[str, ...]:
    text = DATABASE_FILE.read_text(encoding="utf-8")
    match = BUSINESS_REVISIONS_PATTERN.search(text)
    assert match is not None, "storage/database.py declares no BUSINESS_REVISIONS"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def reason_code_sources() -> dict[str, set[str]]:
    """Reason codes the backend really raises, as ``code -> {file:line}``.

    The scan is anchored on the constructs that state a reason — `ServiceError("...", status)` and
    the execution side's own refusal types — so it reads what the code can say rather than every
    lower-snake-case literal in the tree.
    """
    pattern = re.compile(
        r"(?:ServiceError|RunnerRejected|RunnerUnavailable|SandboxRejected|ArchiveRejected)"
        r'\(\s*"([a-z][a-z0-9_]*)"'
    )
    found: dict[str, set[str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for code in pattern.findall(line):
                relative = path.relative_to(REPOSITORY).as_posix()
                found.setdefault(code, set()).add(f"{relative}:{number}")
    return found


def doc_table_inventory() -> dict[str, str]:
    """The inventory document's per-table rows, as ``table -> row text``.

    The rows are the contract with a human reader, so they are parsed from the document rather
    than restated in this test: if the document and the schema disagree, this fails. A table row is
    recognised by its shape — a backticked lower-snake-case name in the first cell and at least
    three more cells — so prose that happens to be a one-cell bullet is not mistaken for a table.
    """
    text = INVENTORY_DOC.read_text(encoding="utf-8")
    rows: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        name = cells[0].strip("`")
        if re.fullmatch(r"[a-z_]+", name) and re.fullmatch(r"`[A-Za-z_]+`|\*\*无\*\*.*", cells[1]):
            rows[name] = line
    return rows


def implemented_reason_codes() -> set[str]:
    return set(reason_code_sources())


def test_every_table_has_either_an_orm_class_or_a_recorded_raw_sql_owner() -> None:
    """A table nothing maps is a table nothing migrates, and it must be named as such."""
    orm = orm_table_names()
    created = set(migration_tables())
    unmapped = created - orm - NON_BUSINESS_TABLES - DROPPED_TABLES
    assert unmapped == RAW_SQL_ONLY_TABLES, (
        "tables created by the migrations with no ORM class have to be listed as raw-SQL-only: "
        f"found {sorted(unmapped)}, expected {sorted(RAW_SQL_ONLY_TABLES)}"
    )
    assert orm <= created | DROPPED_TABLES, (
        "an ORM class declares a table no migration creates: "
        f"{sorted(orm - created - DROPPED_TABLES)}"
    )


def test_the_migration_chain_is_linear_and_has_exactly_one_head() -> None:
    """`p2-execution-batches.md` section 4: one head, and its id in `BUSINESS_REVISIONS`."""
    chain = migration_chain()
    revisions = set(chain)
    parents = {parent for parent in chain.values() if parent is not None}
    heads = revisions - parents
    assert len(heads) == 1, f"the migration chain has {len(heads)} heads: {sorted(heads)}"
    roots = [revision for revision, parent in chain.items() if parent is None]
    assert len(roots) == 1, f"the migration chain has {len(roots)} roots: {sorted(roots)}"
    # A parent that is not itself a revision would leave a dangling chain.
    assert parents <= revisions, f"unknown down_revision: {sorted(parents - revisions)}"
    assert heads <= set(business_revisions()), (
        f"the head {sorted(heads)} is not in BUSINESS_REVISIONS {business_revisions()}"
    )


def test_the_inventory_document_lists_the_tables_the_code_really_has() -> None:
    """The document's table list is the inventory; it may not drift from the schema."""
    documented = set(doc_table_inventory()) - DROPPED_TABLES
    expected = orm_table_names() | RAW_SQL_ONLY_TABLES | NON_BUSINESS_TABLES
    assert documented == expected, (
        "the inventory document and the schema disagree: "
        f"missing from the document {sorted(expected - documented)}, "
        f"documented but not in the schema {sorted(documented - expected)}"
    )
    # A dropped table stays in the history: 0007 removed the login throttle on purpose, and the
    # inventory records that rather than pretending the table never existed.
    assert DROPPED_TABLES <= set(doc_table_inventory())
    # The checkpoint schema is owned by the library and verified at startup, so the document names
    # it without pretending the business migrations create it.
    text = INVENTORY_DOC.read_text(encoding="utf-8")
    for table in CHECKPOINT_SCHEMA_TABLES:
        assert table in text, f"the inventory document never mentions checkpoint table {table}"


def test_the_inventory_document_records_no_object_the_code_does_not_have() -> None:
    """`0006` section 10's claim that Claim/Finding/Review do not exist must stay true."""
    text = INVENTORY_DOC.read_text(encoding="utf-8")
    for absent in LEGACY_OBJECTS_ABSENT:
        assert absent in text, f"the inventory document never records {absent} as absent"
    for present in LEGACY_OBJECTS_PRESENT:
        assert present in text, f"the inventory document never records {present} as present"


def test_the_inventory_document_lists_every_frozen_manifest_input() -> None:
    """A manifest input the document does not name is an input nobody locks."""
    text = INVENTORY_DOC.read_text(encoding="utf-8")
    missing = [name for name in manifest_field_names() if f"`{name}`" not in text]
    assert missing == [], f"frozen-manifest inputs absent from the inventory document: {missing}"


def test_every_sample_loads_into_the_contract_it_names() -> None:
    """A sample nobody can load is a picture of a response, not a response."""
    paths = sample_paths()
    assert paths, "no fixed samples are present"
    for path in paths:
        sample = read_sample(path)
        assert sample["name"] == path.stem, f"{path.name} disagrees with its own name"
        assert sample["kind"] in get_args(SampleKind)
        model_name = sample["materializes"]["model"]
        assert model_name in MODEL_FOR, f"{path.name} names an unknown contract {model_name}"
        MODEL_FOR[model_name].model_validate(sample["payload"])
        assert sample["describes"].strip(), f"{path.name} does not say what it shows"
        assert sample["source"]["file"].strip(), f"{path.name} names no source"


def test_error_samples_carry_the_single_error_envelope_and_no_prose() -> None:
    """The platform's refusals have one shape; a sample may not invent a second one."""
    for path in sample_paths():
        sample = read_sample(path)
        if sample["kind"] == "error":
            assert isinstance(sample["reason_code"], str), f"{path.name} states no reason code"
            assert set(sample["payload"]) == {"reason_code"}, (
                f"{path.name} carries fields the error envelope does not have: "
                f"{sorted(set(sample['payload']) - {'reason_code'})}"
            )
            Envelope = ErrorEnvelope.model_validate(sample["payload"])
            assert Envelope.reason_code == sample["reason_code"]
            assert sample["reason_code"] in implemented_reason_codes(), (
                f"{path.name} uses the reason code {sample['reason_code']!r}, which the backend "
                "never raises. Either use a real code or file it as a proposal first."
            )
        else:
            assert sample["reason_code"] is None, (
                f"{path.name} is a response and cannot carry a reason code"
            )


def test_the_required_must_fail_states_each_have_a_loadable_sample() -> None:
    """`0006` section 11's must-fail states, each demonstrated rather than asserted."""
    covered: dict[str, str] = {}
    for path in sample_paths():
        sample = read_sample(path)
        state = sample["state"]["must_fail"]
        assert state not in covered, (
            f"{state} is demonstrated twice ({covered[state]}, {path.name})"
        )
        covered[state] = path.name
    assert set(covered) == set(REQUIRED_SAMPLE_STATES), (
        "the fixed samples do not cover exactly the must-fail states: "
        f"missing {sorted(set(REQUIRED_SAMPLE_STATES) - set(covered))}, "
        f"unexpected {sorted(set(covered) - set(REQUIRED_SAMPLE_STATES))}"
    )
    for state, meaning in REQUIRED_SAMPLE_STATES.items():
        sample = read_sample(SAMPLES_DIR / covered[state])
        assert sample["describes"].strip(), f"{state} has no description of {meaning}"


def test_a_not_ready_response_states_every_gate_of_the_execution_boundary() -> None:
    """Not ready is a per-gate answer, so a reader never has to guess what still blocks."""
    sample = read_sample(SAMPLES_DIR / "not_ready.json")
    payload = sample["payload"]
    assert payload["real_execution_ready"] is False
    assert payload["mode"] == "demonstration"
    gates = [gate["gate"] for gate in payload["gates"]]
    assert gates == list(get_args(ReadinessGateName))
    for gate in payload["gates"]:
        if not gate["ready"]:
            assert gate["reason_code"], f"unmet gate {gate['gate']} names no reason"


def test_the_not_ready_sample_only_uses_reason_codes_the_platform_can_produce() -> None:
    """A gate's reason code is part of the public response, so a sample may not invent one.

    This is the guard for a real defect: the first revision of this sample used
    `lab_host_required` / `deployment_revert_unverified`, which no code path can produce, and the
    old assertion only checked "the string is non-empty" — so a fabricated value was frozen into
    the fixed sample and every check stayed green. The authoritative strings are resolved from the
    `_gate(...)` calls that compute them, and the gate/ready pairing is checked too: claiming an
    unmet gate is actually `ready` (or the reverse) is the same class of error.
    """
    sample = read_sample(SAMPLES_DIR / "not_ready.json")
    produced = gate_reason_codes()
    assert set(produced) == set(get_args(ReadinessGateName)), (
        "every gate must have exactly one reason code in the `_gate(...)` table"
    )
    for gate in sample["payload"]["gates"]:
        expected = produced[gate["gate"]]
        if gate["ready"]:
            assert gate["reason_code"] is None, (
                f"{gate['gate']} is ready but still names a reason"
            )
        else:
            assert gate["reason_code"] == expected, (
                f"{gate['gate']} names {gate['reason_code']!r}; the platform produces {expected!r}"
            )


def test_the_documented_manifest_input_counts_match_the_contract() -> None:
    """The Phase 0 documents quote counts, and the counts have to be the contract's own.

    The first revision of `0010` (§1/§7.2/§7.4/§13) and `0023` said "43 inputs, 8/16/19" for the
    frozen manifest. The real catalogue holds **49 fields (6 available / 13 partial / 30 absent)**;
    43 is `manifest_gaps()` and 8/16/19 were the `gaps` split, so the field count and the gap count
    had been conflated. No check noticed, because the only manifest assertion tested name
    uniqueness and that the design-pinned fields stay available — never the totals. This binds the
    documented totals to the contract so a future edit to either side fails loudly.
    """
    fields = [field for group in MANIFEST_INPUTS for field in group.fields]
    counts = Counter(field.availability for field in fields)
    assert len(fields) == len(manifest_field_names()) == 49
    assert counts == {"available": 6, "partial": 13, "absent": 30}
    assert len(manifest_gaps()) == 43
    # The documents quote these; bind the quoted numbers to the contract so neither side can move
    # alone. Only the body is checked: a record's correction log legitimately quotes the old numbers
    # as a statement of what was fixed, and repeating them there is required, not drift. Everything
    # from the correction heading onwards is therefore out of scope for the stale-form scan.
    correction_heading = "## 两轴评审与本记录的更正"
    record_text = read_text(PHASE0_RECORD)
    body, _, log = record_text.partition(correction_heading)
    assert log, f"{PHASE0_RECORD.name} lost its correction section"
    drift_forms = {
        INVENTORY_DOC: read_text(INVENTORY_DOC),
        PHASE0_RECORD: body,
    }
    for path, text in drift_forms.items():
        assert "49" in text, f"{path.name} no longer states the field total"
        assert "43" in text, f"{path.name} no longer states the gap total"
        for stale in ("43 个字段", "43 个输入", "8 / 16 / 19", "8/16/19"):
            assert stale not in text, f"{path.name} still asserts the stale count {stale!r}"


def test_an_unknown_call_is_not_read_as_a_claim_verdict() -> None:
    """The 0006 section 0.1 rule, applied to the sample that demonstrates it."""
    sample = read_sample(SAMPLES_DIR / "verdict_unknown.json")
    call = ToolCallView.model_validate(sample["payload"])
    assert call.status == "unknown"
    assert call.reconciliation is None
    assert call.conditions == ["outcome_unsettled"]
    assert not legacy_mapping_can_produce_claim_verdict("unknown")
    assert call.observation is not None
    assert call.observation.stop_confirmed is False


def test_a_stop_the_execution_side_has_not_confirmed_holds_capacity() -> None:
    """`0006` section 7: an unconfirmed stop is not a released quota."""
    sample = read_sample(SAMPLES_DIR / "stop_unconfirmed.json")
    call = ToolCallView.model_validate(sample["payload"])
    assert call.observation is not None
    assert call.observation.stop_confirmed is False
    assert "stop_unconfirmed" in call.conditions
    assert call.status not in {"succeeded", "failed", "cancelled"}


def test_the_legacy_mappings_generate_nothing_because_no_record_carries_the_old_values() -> None:
    """`0003` section 8.1: no old data means no migration fact, not a completed evaluation."""
    assert len(LEGACY_VALUE_MAPPINGS) == 11
    assert all(not mapping.executable for mapping in LEGACY_VALUE_MAPPINGS)
    for mapping in LEGACY_VALUE_MAPPINGS:
        assert mapping.required_inputs, (
            f"{mapping.old_object}.{mapping.old_value} names no input it would need"
        )
        assert mapping.forbidden_fact.strip(), (
            f"{mapping.old_object}.{mapping.old_value} does not say what it must not produce"
        )


@pytest.mark.parametrize(
    "execution_fact",
    ["unknown", "incomplete", "undetermined", "any lifecycle status"],
)
def test_execution_facts_never_become_a_claim_verdict(execution_fact: str) -> None:
    """An execution status answers "did this run", never "is this claim true"."""
    assert not legacy_mapping_can_produce_claim_verdict(execution_fact)


@pytest.mark.parametrize("old_value", ["supported", "verified", "refuted", "suspected"])
def test_claim_shaped_old_values_are_mapped_to_a_verdict_but_never_executed(old_value: str) -> None:
    mapping = next(item for item in LEGACY_VALUE_MAPPINGS if item.old_value == old_value)
    assert mapping.target_axis == "A1"
    assert not mapping.executable


def test_an_unknown_old_value_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(KeyError):
        legacy_mapping_can_produce_claim_verdict("no_such_legacy_value")


def test_the_six_axes_keep_a4_pointing_at_a1_and_never_the_reverse() -> None:
    """`0006` section 2 and section 11: A1 and A4 must not depend on each other."""
    assert [spec.axis for spec in AXES] == ["A1", "A2", "A3", "A4", "A5", "A6"]
    assert axis("A4").references == ("A1",)
    assert axis("A1").references == ()
    for spec in AXES:
        assert spec.values, f"{spec.axis} declares no values"
        assert spec.writers, f"{spec.axis} names no writer"
        assert spec.separate_facts, f"{spec.axis} does not say which facts stay separate"
        # No axis gets a table of its own: a carrier is an existing object, never an axis name.
        assert all(not carrier.startswith("A") for carrier in spec.existing_carriers)


def test_the_axes_that_have_no_carrier_say_so_instead_of_borrowing_one() -> None:
    """A1/A2/A4 have no table; claiming one would be the defect this slice exists to avoid."""
    for name in ("A1", "A2", "A4"):
        spec = axis(name)
        assert spec.existing_carriers == (), f"axis {name} claims a carrier"
        assert spec.missing_carriers, f"axis {name} names nothing it still needs"


def test_the_frozen_manifest_inputs_are_a_closed_list_with_honest_availability() -> None:
    """Every input says whether this build can produce it. A gap is recorded, not filled."""
    names = manifest_field_names()
    assert len(names) == len(set(names)), "the manifest catalogue repeats a field name"
    statuses = {field.availability for group in MANIFEST_INPUTS for field in group.fields}
    assert statuses <= {"available", "partial", "absent"}
    gaps = manifest_gaps()
    assert gaps, "a Phase 0 inventory with no gaps would be claiming P2 is already implemented"
    for field in gaps:
        assert field.source.strip(), f"{field.name} records no owner or carrier"
    # The fields the design already pins to a real carrier have to stay available: losing one
    # would break a live read path, not just a plan.
    available = {
        field.name
        for group in MANIFEST_INPUTS
        for field in group.fields
        if field.availability == "available"
    }
    assert {
        "schema_version",
        "run_id",
        "scope_version",
        "evidence_ids",
        "processed_cursor",
    } <= available


def test_the_queue_policy_stays_out_of_the_frozen_manifest() -> None:
    """`0006` section 3.2: the policy is configuration; only a snapshot reference may be frozen."""
    excluded = {name for name, _ in NOT_IN_MANIFEST}
    assert "queue_capacity_policy" in excluded
    assert not excluded & set(manifest_field_names())


def test_the_confirmation_levels_and_claim_classes_are_the_locked_vocabulary() -> None:
    assert CONFIRMATION_LEVELS == ("auto", "independent", "human")
    assert CLAIM_VERDICTS == ("undetermined", "supported", "refuted", "confirmed")
    assert len(CLAIM_CLASSES) == 7
    assert set(CLAIM_CLASSES) == {
        "exec-cap(server)",
        "exec-cap(browser)",
        "authz-fail",
        "injection",
        "info-disc",
        "misconfig",
        "comp-fit",
    }


def test_a_sample_cannot_load_into_a_laxer_contract_than_the_production_one() -> None:
    """The envelope forbids extra fields, so a sample cannot smuggle a second body shape in."""
    with pytest.raises(ValidationError):
        ErrorEnvelope.model_validate({"reason_code": "version_conflict", "detail": "extra"})


def source_tokens_outside_this_slice() -> set[str]:
    """Every identifier the backend source contains, apart from this slice's own files.

    The catalogue's ``search`` token is looked up here. Excluding `contracts/phase0.py` and this
    test matters: otherwise a field would "exist" because the inventory mentions it, which is the
    circularity the availability flags exist to avoid.
    """
    tokens: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        tokens.add(path.relative_to(SOURCE).as_posix())
        if path.name == "phase0.py":
            # The catalogue's own contract file is the carrier for constants it defines, such as
            # the manifest schema version. It is named explicitly as a search token rather than
            # being matched automatically, so a field still cannot point at itself by accident.
            continue
        tokens.update(re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", path.read_text(encoding="utf-8")))
    return tokens


def test_a_field_claimed_available_or_partial_has_a_carrier_in_the_real_source() -> None:
    """Availability is a claim about code, so it is checked against code."""
    tokens = source_tokens_outside_this_slice()
    missing = sorted(
        field.search
        for group in MANIFEST_INPUTS
        for field in group.fields
        if field.availability in {"available", "partial"} and field.search not in tokens
    )
    assert missing == [], (
        "these fields are marked available or partial, but their carrier token is nowhere in "
        f"backend/src: {missing}. Either the carrier is gone, or the flag overstates what exists."
    )


def test_a_field_claimed_absent_is_genuinely_nowhere_in_the_real_source() -> None:
    """`absent` means no carrier: if a P2 slice adds one, this check makes it say so."""
    tokens = source_tokens_outside_this_slice()
    present = sorted(
        field.search
        for group in MANIFEST_INPUTS
        for field in group.fields
        if field.availability == "absent" and field.search in tokens
    )
    assert present == [], (
        "these fields are marked absent but a carrier now exists in backend/src: "
        f"{present}. Update the catalogue in the same commit that adds the carrier."
    )


def test_the_reason_code_scan_finds_the_codes_the_samples_depend_on() -> None:
    """A guard on the scan itself: if it stops reading the source, the checks above go blind."""
    codes = implemented_reason_codes()
    for expected in (
        "version_conflict",
        "invalid_call_state",
        "reconciliation_conflict",
        "authentication_required",
        "execution_stop_unconfirmed",
        "real_execution_not_ready",
    ):
        assert expected in codes, f"the scan no longer finds {expected}"
        sources = reason_code_sources()[expected]
        assert sources, f"{expected} has no recorded source"
        assert all(":" in location for location in sources)
