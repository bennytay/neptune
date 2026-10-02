"""Alignment records (ADR 0050): shape, strict readers, the schema, and what they refuse.

MVL-82 acceptance: each kind carries provenance, ``Knowledge`` fields and a validity window on a
named clock; an identity link is never a merge; inferred alignment is refused here.
"""

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.alignment import (
    ALIGNMENT_SINCE,
    ClockAnchor,
    ClockMapping,
    FrameBinding,
    FrameBindingBasis,
    IdentityLink,
    LinkBasis,
    MappingMethod,
    MemberRole,
    RunAssembly,
    RunMember,
    SnapshotBinding,
    SnapshotKind,
    ValidityWindow,
    clock_mapping_from_json,
    frame_binding_from_json,
    identity_link_from_json,
    run_assembly_from_json,
    snapshot_binding_from_json,
)
from neptune.model.frames import FrameRef
from neptune.model.ids import LogicalId, RecordId
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, JsonPointer, Provenance
from neptune.model.record import Family, SchemaVersionError
from neptune.model.schema import canonical_schema
from neptune.model.time import Duration, Timestamp

MANIFEST = b'{"robots": [{"tag": "AMR-11", "serial": "SN-4410"}]}'
SOURCE = content_id(MANIFEST)
OTHER = content_id(b"another declaration")
ADAPTER = transform_record(adapter_id="manifest", adapter_version="1.0.0", config={})
VALIDATOR = Draft202012Validator(canonical_schema())


def rid(name: str) -> RecordId:
    return evidence_record_id(
        "timestamp_domain", EvidenceRef(OTHER, (JsonPointer(f"/{name}"),)), ADAPTER
    )


BOOT, GPS, RUN, SNAPSHOT, GRAPH, TRANSFORM, CALIBRATION, REVISION = (
    rid(name)
    for name in ("boot", "gps", "run", "snapshot", "graph", "transform", "calibration", "rev")
)
TAG = LogicalId("fleet.asset_tag", "AMR-11")
SERIAL = LogicalId("serial", "SN-4410")


def cite(pointer: str, kind: AssertionKind = AssertionKind.STATED) -> Provenance:
    return Provenance(EvidenceRef(SOURCE, (JsonPointer(pointer),)), ADAPTER.id, kind)


def make(cls: Any, pointer: str, **fields: Any) -> Any:
    where = cite(pointer)
    return cls(id=evidence_record_id(cls.kind, where.evidence, ADAPTER), provenance=where, **fields)


def window(start: int | None = 10, end: int | None = 20, clock: RecordId = BOOT) -> ValidityWindow:
    return ValidityWindow(
        clock,
        Known(Timestamp(start, clock)) if start is not None else KnownAbsent(cite("/from")),
        Known(Timestamp(end, clock)) if end is not None else Unknown(),
    )


def link(**changes: Any) -> Any:
    fields: dict[str, Any] = {
        "left": TAG,
        "right": Known(SERIAL, cite("/robots/0/serial")),
        "basis": LinkBasis.CO_DECLARED,
        "identifier": NotApplicable(),
        "evidence": (),
        "validity": Known(window()),
    }
    return make(IdentityLink, "/robots/0", **{**fields, **changes})


def mapping(**changes: Any) -> Any:
    fields: dict[str, Any] = {
        "source": BOOT,
        "target": GPS,
        "method": MappingMethod.STATED,
        "anchor": Known(ClockAnchor(Timestamp(0, BOOT), Timestamp(1_000, GPS))),
        "rate": Known(Fraction(1_000_001, 1_000_000)),
        "residual_bound": Known(Duration(3, GPS)),
        "validity": Known(window(None, 50)),
    }
    return make(ClockMapping, "/sync", **{**fields, **changes})


def binding(**changes: Any) -> Any:
    fields: dict[str, Any] = {
        "parent": FrameRef("base_link", GRAPH),
        "child": FrameRef("camera", GRAPH),
        "transform": TRANSFORM,
        "basis": FrameBindingBasis.CALIBRATION,
        "calibration": Known(CALIBRATION),
        "validity": Unknown(),
    }
    return make(FrameBinding, "/extrinsics/0", **{**fields, **changes})


def assembly(**changes: Any) -> Any:
    member = RunMember(REVISION, MemberRole.RECORDING, EvidenceRef(SOURCE, (JsonPointer("/f"),)))
    fields: dict[str, Any] = {
        "run": RUN,
        "rule": "manifest.files",
        "members": (member,),
        "validity": NotApplicable(),
    }
    return make(RunAssembly, "/files", **{**fields, **changes})


def snapshot(**changes: Any) -> Any:
    fields: dict[str, Any] = {
        "run": RUN,
        "snapshot": SNAPSHOT,
        "snapshot_kind": SnapshotKind.SOFTWARE_CONFIGURATION,
        "validity": Known(window(10, None)),
    }
    return make(SnapshotBinding, "/software", **{**fields, **changes})


SAMPLES = [
    (link(), identity_link_from_json),
    (
        link(
            right=Ambiguous((Candidate(SERIAL), Candidate(LogicalId("serial", "SN-4411")))),
            basis=LinkBasis.SHARED_IDENTIFIER,
            identifier=Known(SERIAL),
            evidence=(EvidenceRef(OTHER, (ByteRange(0, 4),)),),
            validity=NotCovered(),
        ),
        identity_link_from_json,
    ),
    (mapping(), clock_mapping_from_json),
    (
        mapping(
            method=MappingMethod.CO_SAMPLED,
            rate=Unknown(),
            residual_bound=Unknown(),
            validity=Unknown(),
        ),
        clock_mapping_from_json,
    ),
    (binding(), frame_binding_from_json),
    (
        binding(basis=FrameBindingBasis.TRANSFORM_MESSAGE, calibration=NotApplicable()),
        frame_binding_from_json,
    ),
    (assembly(), run_assembly_from_json),
    (snapshot(), snapshot_binding_from_json),
]
IDS = [f"{sample.kind}-{i}" for i, (sample, _) in enumerate(SAMPLES)]


@pytest.mark.parametrize(("sample", "read"), SAMPLES, ids=IDS)
def test_each_kind_round_trips_byte_identically_and_validates(sample: Any, read: Any) -> None:
    line = canonical_json.dumps(sample.to_json())
    again = read(canonical_json.loads(line))
    assert again == sample
    assert canonical_json.dumps(again.to_json()) == line
    VALIDATOR.validate(canonical_json.loads(line))


@pytest.mark.parametrize(("sample", "read"), SAMPLES, ids=IDS)
def test_each_kind_is_an_alignment_evidence_record_with_a_validity(sample: Any, read: Any) -> None:
    cls = type(sample)
    assert RECORD_KINDS[cls.kind] == (cls, read)
    assert cls.family is Family.ALIGNMENT
    data = sample.to_json()
    assert {"id", "provenance", "validity"} <= data.keys()
    assert data["provenance"]["assertion_kind"] in {"observed", "stated"}


@pytest.mark.parametrize(("sample", "read"), SAMPLES, ids=IDS)
def test_readers_refuse_extra_or_missing_keys_and_newer_versions(sample: Any, read: Any) -> None:
    data = sample.to_json()
    with pytest.raises(ValueError, match="unexpected"):
        read({**data, "merged": True})
    with pytest.raises(ValueError, match="missing"):
        read({k: v for k, v in data.items() if k != "validity"})
    with pytest.raises(SchemaVersionError):
        read({**data, "schema_version": 10**6})
    for older in range(1, ALIGNMENT_SINCE):  # the kinds are from version 3 on (ADR 0050 §9)
        with pytest.raises(SchemaVersionError, match="from schema version 3"):
            read({**data, "schema_version": older})
    assert data["schema_version"] == ALIGNMENT_SINCE == 3


@pytest.mark.parametrize(("sample", "read"), SAMPLES, ids=IDS)
def test_inferred_alignment_belongs_in_derived(sample: Any, read: Any) -> None:
    inferred = InferredProvenance((cite("/x").evidence,), ADAPTER.id)
    with pytest.raises(TypeError, match="derived"):
        replace(sample, provenance=inferred)
    data = sample.to_json()
    with pytest.raises(ValueError):
        read({**data, "provenance": {**data["provenance"], "assertion_kind": "inferred"}})


def test_the_same_declaration_gives_the_same_record_and_bytes() -> None:
    assert link() == link()
    assert canonical_json.dumps(mapping().to_json()) == canonical_json.dumps(mapping().to_json())
    assert link().id != snapshot().id


# --- Validity windows ---------------------------------------------------------------------------


def test_a_window_is_half_open_and_never_empty() -> None:
    window(10, 11)  # one tick
    with pytest.raises(ValueError, match="before its end"):
        window(10, 10)
    with pytest.raises(ValueError, match="before its end"):
        window(20, 10)


def test_a_window_bounds_are_on_its_clock() -> None:
    with pytest.raises(ValueError, match="not on its clock"):
        ValidityWindow(BOOT, Known(Timestamp(1, GPS)), Unknown())
    with pytest.raises(ValueError, match="not on its clock"):
        ValidityWindow(
            BOOT,
            Unknown(),
            Ambiguous((Candidate(Timestamp(1, BOOT)), Candidate(Timestamp(2, GPS)))),
        )


def test_open_and_unstated_bounds_stay_distinct() -> None:
    stated_open = ValidityWindow(BOOT, KnownAbsent(cite("/open")), Known(Timestamp(5, BOOT)))
    unstated = ValidityWindow(BOOT, Unknown(), Known(Timestamp(5, BOOT)))
    assert stated_open.to_json()["start"] != unstated.to_json()["start"]


# --- Identity links: never a merge --------------------------------------------------------------


def test_an_identity_link_relates_two_ids_and_merges_nothing() -> None:
    with pytest.raises(ValueError, match="both sides"):
        link(right=Known(TAG))
    with pytest.raises(ValueError, match="both sides"):
        link(right=Ambiguous((Candidate(TAG), Candidate(SERIAL))))
    data: Any = link().to_json()
    assert data["left"] != data["right"]["value"]  # both ids are kept, neither replaces the other


@pytest.mark.parametrize(
    "right", [Unknown(), NotCovered(), NotApplicable(), KnownAbsent(cite("/r"))]
)
def test_the_other_side_is_stated(right: Any) -> None:
    with pytest.raises(ValueError, match="Known or Ambiguous"):
        link(right=right)


def test_the_basis_decides_identifier_and_evidence() -> None:
    with pytest.raises(ValueError, match="no shared identifier"):
        link(identifier=Known(SERIAL))
    with pytest.raises(ValueError, match="Known or Ambiguous"):
        link(basis=LinkBasis.SHARED_IDENTIFIER, evidence=(cite("/o").evidence,))
    with pytest.raises(ValueError, match="right side's declaration"):
        link(basis=LinkBasis.SHARED_IDENTIFIER, identifier=Known(SERIAL))
    with pytest.raises(ValueError, match="repeats"):
        link(evidence=(cite("/robots/0").evidence,))


# --- Clock mappings -----------------------------------------------------------------------------


def test_a_clock_mapping_is_increasing_between_two_clocks() -> None:
    with pytest.raises(ValueError, match="two clocks"):
        mapping(target=BOOT, anchor=Unknown(), residual_bound=Unknown())
    with pytest.raises(ValueError, match="positive"):
        mapping(rate=Known(Fraction(0)))
    with pytest.raises(ValueError, match="positive"):
        mapping(rate=Known(Fraction(-1)))
    with pytest.raises(ValueError, match="source and target clocks"):
        mapping(anchor=Known(ClockAnchor(Timestamp(0, GPS), Timestamp(0, BOOT))))
    with pytest.raises(ValueError, match="non-negative duration on the target"):
        mapping(residual_bound=Known(Duration(-1, GPS)))
    with pytest.raises(ValueError, match="non-negative duration on the target"):
        mapping(residual_bound=Known(Duration(1, BOOT)))
    with pytest.raises(ValueError, match="validity must be on"):
        mapping(validity=Known(window(clock=GPS)))
    assert mapping(residual_bound=Known(Duration(0, GPS))).residual_bound == Known(Duration(0, GPS))


def test_a_rate_is_written_in_lowest_terms() -> None:
    data: Any = mapping().to_json()
    data["rate"]["value"] = {"denominator": 2_000_000, "numerator": 2_000_002}
    with pytest.raises(ValueError, match="lowest terms"):
        clock_mapping_from_json(data)


# --- Frame bindings, run assemblies, snapshot bindings ------------------------------------------


def test_a_frame_binding_names_one_edge_of_one_graph() -> None:
    with pytest.raises(ValueError, match="one graph"):
        binding(child=FrameRef("camera", TRANSFORM))
    with pytest.raises(ValueError, match="two frames"):
        binding(child=FrameRef("base_link", GRAPH))
    with pytest.raises(ValueError, match="Known or Ambiguous"):
        binding(calibration=Unknown())
    with pytest.raises(ValueError, match="names no calibration"):
        binding(basis=FrameBindingBasis.ROBOT_DESCRIPTION)


def test_a_run_assembly_lists_each_member_once_in_order() -> None:
    first = RunMember(BOOT, MemberRole.RECORDING, cite("/a").evidence)
    second = RunMember(GPS, MemberRole.CONTEXT, cite("/b").evidence)
    ordered = tuple(sorted((first, second), key=lambda m: m.revision))
    assert assembly(members=ordered).members == ordered
    with pytest.raises(ValueError, match="sorted"):
        assembly(members=ordered[::-1])
    with pytest.raises(ValueError, match="sorted"):
        assembly(members=(first, first))
    with pytest.raises(ValueError, match="at least one"):
        assembly(members=())
    with pytest.raises(ValueError):
        assembly(rule="Not A Token")


def test_a_snapshot_binding_names_a_machine_context_kind() -> None:
    data = snapshot().to_json()
    for kind in SnapshotKind:
        assert kind.value in RECORD_KINDS
    with pytest.raises(ValueError):
        snapshot_binding_from_json({**data, "snapshot_kind": "stream"})
    with pytest.raises(TypeError, match="SnapshotKind"):
        snapshot(snapshot_kind="calibration")
