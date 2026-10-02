"""Deployment lifecycle records (ADR 0051): every value stated and stored as declared, strict JSON,
written at the schema version that added them, and carried by a package unchanged.
"""

import io
import typing
from dataclasses import fields, replace
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import LogicalId
from neptune.model.kinds import KIND_SINCE, RECORD_KINDS, kinds_at, package_version
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotCovered,
    Unknown,
)
from neptune.model.lifecycle import (
    LIFECYCLE_KINDS,
    LIFECYCLE_SINCE,
    ChangeItem,
    Decision,
    Hazard,
    IncidentRecord,
    Quantity,
    Score,
    ZoneLimit,
)
from neptune.model.provenance import ByteRange, EvidenceRef, JsonPointer, Provenance
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.reference import TimestampDomain
from neptune.model.scalars import NonFinite
from neptune.model.schema import canonical_schema
from neptune.model.source import LocalPath
from neptune.model.time import SECOND, ClockRole, Epoch, Timescale, Timestamp
from neptune.model.units import unit_from_json
from neptune.model.versions import SemanticVersion
from neptune.store.package import RECEIPT_TEXT, package_files, read_files, table_path

FORMS: Final = b'{"forms": [{"id": "COM-0042"}, {"id": "INC-0007"}]}\n'
SOURCE: Final = content_id(FORMS)
ADAPTER: Final = transform_record(adapter_id="deploy.forms", adapter_version="0.1.0", config={})
STATED: Final = AssertionKind.STATED
VALIDATOR: Final = Draft202012Validator(canonical_schema())


def at(pointer: str) -> Provenance:
    return Provenance(
        EvidenceRef(SOURCE, (ByteRange(0, len(FORMS)), JsonPointer(pointer))), ADAPTER.id, STATED
    )


CLOCK_AT: Final = at("")
CLOCK: Final = TimestampDomain(
    id=evidence_record_id("timestamp_domain", CLOCK_AT.evidence, ADAPTER),
    provenance=CLOCK_AT,
    field="date-time",
    scope=(),
    role=Known(ClockRole.DOCUMENT),
    resolution=Known(SECOND),
    epoch=Known(Epoch.UNIX),
    timescale=Known(Timescale.POSIX),
    declared_monotonic=Unknown(),
)


# --- Samples, built from each class's declared field types --------------------------------------


def sample(tp: Any, name: str, *, sparse: bool = False) -> Any:
    """A value of type ``tp``: every state stated (``sparse``: every state Unknown, lists empty)."""
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is tuple:
        if sparse:
            return ()
        item = args[0]
        if typing.get_args(item)[:1] == (Known[LogicalId],):
            return (
                Known(LogicalId("cmms", f"{name}-1"), at(f"/{name}/0")),
                Known(LogicalId("cmms", f"{name}-2"), at(f"/{name}/1")),
            )
        if typing.get_args(item)[:1] == (Known[str],):
            # Source order, and a text may repeat: two identical commands are two statements.
            return (Known(f"{name} b", at(f"/{name}/0")), Known(f"{name} b"), Known(f"{name} a"))
        return (sample(item, name, sparse=sparse),)
    if isinstance(tp, type) and hasattr(tp, "_CODECS"):
        return tp(**{f.name: sample(_hints(tp)[f.name], f.name, sparse=sparse) for f in fields(tp)})
    if tp is str:
        return name
    if sparse:
        return Unknown()
    (value_type, *_) = typing.get_args(args[0]) if args else (None,)
    values: dict[Any, Any] = {
        str: f"declared {name}",
        LogicalId: LogicalId("site.zone", f"{name.upper()}-A"),
        Timestamp: Timestamp(1_790_762_400, CLOCK.id),
        float: 1.5,
        type(unit_from_json("m.s^-1")): unit_from_json("m.s^-1"),
    }
    if float in typing.get_args(value_type):  # Real
        value_type = float
    if value_type in values:
        return Known(values[value_type], at(f"/{name}"))
    return Known(SemanticVersion("4.2.1"), at(f"/{name}"))  # a VersionPrimitive


def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def record(cls: Any, pointer: str = "/forms/0", *, sparse: bool = False, **change: Any) -> Any:
    provenance = at(pointer)
    values = {
        f.name: sample(_hints(cls)[f.name], f.name, sparse=sparse)
        for f in fields(cls)
        if f.name not in {"id", "provenance"}
    }
    return cls(
        id=evidence_record_id(cls.kind, provenance.evidence, ADAPTER),
        provenance=provenance,
        **{**values, **change},
    )


KINDS: Final = [pytest.param(cls, id=cls.kind) for cls in LIFECYCLE_KINDS]


# --- The kinds ---------------------------------------------------------------------------------


def test_eight_world_kinds_added_in_one_schema_version() -> None:
    assert [cls.kind for cls in LIFECYCLE_KINDS] == [
        "commissioning_baseline",
        "authorisation_envelope",
        "intervention",
        "maintenance_event",
        "requalification_record",
        "incident_record",
        "change_record",
        "risk_assessment",
    ]
    for cls in LIFECYCLE_KINDS:
        assert RECORD_KINDS[cls.kind][0] is cls
        assert cls.family is Family.WORLD  # type: ignore[attr-defined]
        assert KIND_SINCE[cls.kind] == LIFECYCLE_SINCE
    assert LIFECYCLE_SINCE <= SCHEMA_VERSION
    added = set(kinds_at(LIFECYCLE_SINCE)) - set(kinds_at(LIFECYCLE_SINCE - 1))
    assert {cls.kind for cls in LIFECYCLE_KINDS} <= added
    assert package_version(["site", "incident_record"]) == LIFECYCLE_SINCE


def test_every_kind_shares_the_fields_that_place_it_in_a_deployment() -> None:
    for cls in LIFECYCLE_KINDS:
        names = [f.name for f in fields(cls)]
        assert names[:7] == [
            "id",
            "provenance",
            "identifiers",
            "site",
            "machines",
            "configuration",
            "related",
        ]


@pytest.mark.parametrize("cls", KINDS)
@pytest.mark.parametrize("sparse", [False, True], ids=["stated", "sparse"])
def test_records_round_trip_strictly_and_validate(cls: Any, sparse: bool) -> None:
    built = record(cls, sparse=sparse)
    line = canonical_json.dumps(built.to_json())
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    assert data["kind"] == cls.kind and data["schema_version"] == LIFECYCLE_SINCE
    read = RECORD_KINDS[cls.kind][1]
    assert read(data) == built
    assert canonical_json.dumps(read(data).to_json()) == line  # byte-identical
    assert list(VALIDATOR.iter_errors(data)) == []


@pytest.mark.parametrize("cls", KINDS)
def test_the_envelope_and_keys_are_checked_strictly(cls: Any) -> None:
    data = record(cls).to_json()
    read = RECORD_KINDS[cls.kind][1]
    with pytest.raises(SchemaVersionError, match=f"from schema version {LIFECYCLE_SINCE}"):
        read({**data, "schema_version": LIFECYCLE_SINCE - 1})  # no older reader ever wrote one
    with pytest.raises(SchemaVersionError, match="newer"):
        read({**data, "schema_version": SCHEMA_VERSION + 1, "later": 1})
    with pytest.raises(ValueError):
        read({**data, "severity_rank": 2})  # nothing derived rides along
    for key in data:
        if key not in {"kind", "schema_version"}:
            with pytest.raises(ValueError):
                read({k: v for k, v in data.items() if k != key})


@pytest.mark.parametrize("cls", KINDS)
def test_same_declaration_same_bytes(cls: Any) -> None:
    assert canonical_json.dumps(record(cls).to_json()) == canonical_json.dumps(
        record(cls).to_json()
    )
    other = record(cls, "/forms/1")
    assert other.id != record(cls).id  # another form is another record, never a counter


# --- Values stay as declared -------------------------------------------------------------------


def test_severity_and_scores_are_declared_text_never_ranked() -> None:
    incident = record(
        IncidentRecord,
        severity=Ambiguous((Candidate("S2", at("/severity")), Candidate("2", at("/class")))),
        root_cause=KnownAbsent(at("/root_cause")),
    )
    data = canonical_json.loads(canonical_json.dumps(incident.to_json()))
    assert isinstance(data, dict)
    assert data["severity"]["knowledge"] == "ambiguous"
    assert IncidentRecord.from_json(data) == incident
    hazard = Hazard(
        hazard=Known("crush between arm and fixture"),
        scores=(Score("PLr", Known("d")), Score("severity", Known("S2"))),
        mitigations=(Known("light curtain"),),
    )
    assert Hazard.from_json(hazard.to_json()) == hazard
    with pytest.raises(ValueError, match="unique"):
        replace(hazard, scores=(Score("PLr", Known("d")), Score("PLr", Known("e"))))


def test_quantities_keep_their_declared_number_and_unit() -> None:
    limit = ZoneLimit(
        zone=Known(LogicalId("wms.zone", "PICK-A")),
        speed_limit=Quantity(Known(4.0), Known(unit_from_json("km.h^-1"))),
    )
    assert limit.to_json()["speed_limit"] == {
        "unit": {"knowledge": "known", "value": "km.h^-1"},
        "value": {"knowledge": "known", "value": 4.0},
    }
    assert ZoneLimit.from_json(limit.to_json()) == limit
    unbounded = Quantity(Known(NonFinite.POSITIVE_INFINITY), NotCovered())
    assert Quantity.from_json(unbounded.to_json()) == unbounded


BAD: Final = [
    ("blank text", "location", Known("")),
    ("text of the wrong type", "location", Known(3)),
    ("a time that is not a Timestamp", "occurred", Known("2026-09-30")),
    ("an id that is not a LogicalId", "zone", Known("PICK-A")),
    ("a bare text, not a state", "severity", "S2"),
    ("a bare id, not a state", "zone", LogicalId("site.zone", "DOCK-1")),
    ("a list, not a tuple", "assets", []),
    ("an unstated id", "assets", (Unknown(),)),
    (
        "unsorted ids",
        "machines",
        (Known(LogicalId("m", "b")), Known(LogicalId("m", "a"))),
    ),
    ("a repeated id", "machines", (Known(LogicalId("m", "a")), Known(LogicalId("m", "a")))),
]


@pytest.mark.parametrize(("what", "name", "value"), BAD, ids=[b[0] for b in BAD])
def test_malformed_values_are_refused(what: str, name: str, value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        record(IncidentRecord, **{name: value})


def test_malformed_parts_are_refused() -> None:
    with pytest.raises((TypeError, ValueError)):
        ChangeItem(Known(""), Known("map"), Known("r12"), Known("r13"))
    with pytest.raises(ValueError, match="Timestamp"):
        Decision(Known("approved"), Known("site lead"), Known(1_790_762_400))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="float"):
        Quantity(Known(True), Known(unit_from_json("kg")))
    with pytest.raises(ValueError, match="non-empty"):
        Score("", Known("d"))
    with pytest.raises(ValueError, match="states"):
        Hazard(Known("pinch"), (), (Unknown(),))
    with pytest.raises(ValueError):
        ZoneLimit.from_json({"zone": {"knowledge": "unknown"}})
    with pytest.raises(TypeError):
        record(IncidentRecord, timeline=(Score("t", Known("x")),))


def test_inferred_provenance_has_no_place_on_a_lifecycle_record() -> None:
    stated = record(IncidentRecord)
    with pytest.raises(TypeError, match="derived"):
        replace(stated, provenance="inferred")


# --- In a package ------------------------------------------------------------------------------


def package_records(*extra: Any) -> list[Any]:
    ledger = SourceLedger()
    ledger.observe(LocalPath("deploy/forms.json"), digest_stream(io.BytesIO(FORMS)))
    return [*ledger.artifacts(), *ledger.revisions(), ADAPTER, CLOCK, *extra]


def test_a_package_carries_every_kind_unchanged_and_its_receipt_lists_them() -> None:
    records = [record(cls) for cls in LIFECYCLE_KINDS]
    files = package_files(package_records(*records))
    package = read_files(files)
    assert package.manifest.version == LIFECYCLE_SINCE
    assert package.receipt.version == LIFECYCLE_SINCE
    assert {r for r in package.records if r.kind in RECORD_KINDS} >= set(records)
    for built in records:
        assert files[table_path(built.kind)] == canonical_json.dumps(built.to_json()) + b"\n"
    text = files[RECEIPT_TEXT]
    assert isinstance(text, bytes)
    for cls in LIFECYCLE_KINDS:
        assert f"| `{cls.kind}` | 1 |".encode() in text
    assert package.files() == files  # read, rebuilt, byte-identical


def test_a_package_without_lifecycle_records_keeps_its_version_and_bytes() -> None:
    files = package_files(package_records())
    package = read_files(files)
    assert package.manifest.version < LIFECYCLE_SINCE
    for cls in LIFECYCLE_KINDS:
        assert table_path(cls.kind) not in files
