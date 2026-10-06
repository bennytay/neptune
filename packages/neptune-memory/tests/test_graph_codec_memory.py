"""Strict JSON round trips for claims, findings and graph documents (ADR 0006)."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from memory_golden_fixtures import built
from memory_schema_builders import BOOT_CLOCK, INFERRED, MODEL, OBSERVED, at, claim, node
from neptune.identity import canonical_json
from neptune.model.frames import FrameRef
from neptune.model.ids import ConfigHash
from neptune.model.knowledge import Ambiguous, Known, Unknown
from neptune.model.scalars import NonFinite
from neptune.model.units import unit_from_text
from neptune_memory.schema.claim import Claim, LedgerRecordRef, ModelRef, TypedLiteral, ValueType
from neptune_memory.schema.codec import (
    GraphDocument,
    claim_from_json,
    finding_from_json,
    graph_from_json,
)
from neptune_memory.schema.export import graph_schema
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.reader import EpisodeFilter, SpatialView, result_to_json
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import (
    FindingCode,
    FindingProvenance,
    ResolutionFinding,
    parse_finding_id,
    resolve,
    resolver_config,
)

ARM = node(NodeType.MACHINE, "serial:UR10E-2041")
CAMERA = node(NodeType.SENSOR, "serial:CAM-7")
CELL = node(NodeType.ZONE, "cell-3")
RECORD = LedgerRecordRef(
    "rec:sha256:" + "a" * 64  # type: ignore[arg-type]
)
KG = unit_from_text("kg")
AMBIGUOUS = unit_from_text("lb")  # lb or lbf: the declared text has two readings
SCHEMA = graph_schema()


def _round(value: Any) -> Any:
    return canonical_json.loads(canonical_json.dumps(value))


def _validate(name: str, value: Any) -> None:
    Draft202012Validator({**SCHEMA, "anyOf": [{"$ref": f"#/$defs/{name}"}]}).validate(value)


CLAIMS = [
    claim(ARM, "located_at", CELL, 0, 10, tx=1, kind=OBSERVED),
    claim(ARM, "rated_payload", TypedLiteral(ValueType.QUANTITY, 14.5, KG), 0, tx=1),
    claim(ARM, "rated_payload", TypedLiteral(ValueType.QUANTITY, 14, AMBIGUOUS), 0, tx=1),
    claim(ARM, "rated_payload", TypedLiteral(ValueType.QUANTITY, 3, Unknown()), 0, tx=1),
    claim(ARM, "maintenance_state", TypedLiteral(ValueType.TEXT, ""), 0, tx=2),
    claim(ARM, "evidenced_by", RECORD, 5, tx=2, clock=BOOT_CLOCK),
    claim(ARM, "located_at", CELL, 0, tx=3, kind=INFERRED, confidence=Known(0.25)),
    claim(ARM, "located_at", CELL, 1, tx=3, kind=INFERRED, confidence=Unknown()),
    claim(CAMERA, "mounted_on", ARM, 0, tx=4),
]


@pytest.mark.parametrize("original", CLAIMS, ids=lambda c: c.predicate)
def test_claims_round_trip_and_validate(original: Claim) -> None:
    data = _round(original.to_json())
    _validate("Claim", data)
    assert claim_from_json(data) == original


def test_literal_values_of_every_datatype_round_trip() -> None:
    for literal in (
        TypedLiteral(ValueType.REAL, NonFinite.POSITIVE_INFINITY),
        TypedLiteral(ValueType.REAL, 0.5),
        TypedLiteral(ValueType.INTEGER, 7),
        TypedLiteral(ValueType.BOOLEAN, False),
        TypedLiteral(ValueType.INSTANT, at(42)),
        TypedLiteral(ValueType.QUANTITY, NonFinite.NAN, KG),
    ):
        original = replace(CLAIMS[4], object=literal)
        data = _round(original.to_json())
        _validate("Claim", data)
        assert claim_from_json(data) == original


def test_history_with_closures_findings_and_models_round_trips() -> None:
    guess = claim(ARM, "located_at", CELL, 0, 20, tx=1, kind=INFERRED)
    other = claim(ARM, "located_at", node(NodeType.SITE, "yard"), 5, 8, tx=2, ev=1)
    elsewhere = claim(ARM, "located_at", CELL, 3, tx=3, clock=BOOT_CLOCK, ev=2)
    priorities = {"memory.test": 0}
    resolution = resolve([guess, other, elsewhere], CORE_PREDICATES, priorities)
    closures = [c for c in resolution.claims if c.provenance.consolidator_id == "memory.supersede"]
    assert closures and all(c.provenance.model == MODEL for c in closures)
    assert resolution.findings
    document = GraphDocument(resolution, resolver_config(CORE_PREDICATES, priorities), ledger_tx(3))
    data = _round(document.to_json())
    Draft202012Validator(SCHEMA).validate(data)
    assert graph_from_json(data) == document


def _corrupt(data: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(data)
    target = out
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return out


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("id",), "claim:sha256:" + "0" * 64),
        (("extra",), 1),
        (("provenance", "model"), {"model_id": "x", "model_version": "1"}),
        (("provenance", "evidence"), []),
        (("assertion_kind",), "guessed"),
        (("recorded_at",), -1),
        (("recorded_at",), True),
        (("superseded_at",), "closed"),
        (("subject", "node_type"), "spaceship"),
        (("object", "kind"), "blob"),
        (("valid",), {"start": {"domain_id": "x", "ticks": 0}, "end": "open"}),
        (("confidence",), {"knowledge": "known", "value": 1}),
        (("predicate",), KeyError),
    ],
)
def test_malformed_claims_are_refused(path: tuple[str, ...], value: Any) -> None:
    data = _round(CLAIMS[0].to_json())
    with pytest.raises((ValueError, TypeError, KeyError)):
        claim_from_json(_corrupt(data, path, value))


def test_a_quantity_unit_cannot_carry_its_own_provenance() -> None:
    data = _round(CLAIMS[1].to_json())
    data["object"]["unit"]["provenance"] = {"assertion_kind": "stated"}
    with pytest.raises(ValueError):
        claim_from_json(data)


# --- Findings ----------------------------------------------------------------------------------

PROVENANCE = FindingProvenance("memory.supersede", "2", ConfigHash("sha256:" + "b" * 64))


def _finding(**changes: Any) -> ResolutionFinding:
    base: dict[str, Any] = {
        "code": built().resolution.findings[0].code,
        "claim": CLAIMS[0].id,
        "others": (CLAIMS[1].id,),
        "provenance": PROVENANCE,
        "recorded_at": ledger_tx(3),
    }
    return ResolutionFinding(**(base | changes))


def test_finding_id_ignores_bookkeeping_and_covers_provenance() -> None:
    finding = _finding()
    parse_finding_id(finding.id)
    assert replace(finding, superseded_at=ledger_tx(9)).id == finding.id
    assert replace(finding, recorded_at=ledger_tx(4)).id == finding.id
    other = replace(PROVENANCE, config_hash=ConfigHash("sha256:" + "c" * 64))
    assert replace(finding, provenance=other).id != finding.id
    data = _round(replace(finding, superseded_at=ledger_tx(9)).to_json())
    _validate("ResolutionFinding", data)
    assert finding_from_json(data) == replace(finding, superseded_at=ledger_tx(9))
    data["id"] = "finding:sha256:" + "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        finding_from_json(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"others": (CLAIMS[0].id,)},
        {"others": (CLAIMS[2].id, CLAIMS[1].id)},
        {"claim": "claim:nope"},
        {"provenance": None},
        {"recorded_at": ledger_tx(3), "superseded_at": ledger_tx(2)},
        {"code": "clock_mismatch"},
    ],
)
def test_malformed_findings_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        _finding(**changes)


# --- Graph documents ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("graph_schema_version", 1),
        ("graph_schema", "1.9.0"),
        ("graph_schema", "3.0.0"),
        ("graph_schema", "2.0"),
        ("graph_schema", "2.01.0"),
        ("graph_schema", 2),
        ("generation", "sha256:" + "0" * 64),
        ("kind", "memory.other"),
        ("claims", "none"),
    ],
)
def test_malformed_graph_documents_are_refused(key: str, value: Any) -> None:
    data = _round(built().to_json())
    data[key] = value
    with pytest.raises(ValueError):
        graph_from_json(data)


def test_graph_documents_must_hold_together() -> None:
    data = _round(built().to_json())
    duplicated = {**data, "claims": [data["claims"][0], *data["claims"]]}
    with pytest.raises(ValueError, match="twice"):
        graph_from_json(duplicated)
    findings = data["findings"]
    orphaned = {**data, "claims": [c for c in data["claims"] if c["id"] != findings[0]["claim"]]}
    with pytest.raises(ValueError, match="does not hold"):
        graph_from_json(orphaned)
    foreign = copy.deepcopy(data)
    foreign["findings"][0]["provenance"]["config_hash"] = "sha256:" + "0" * 64
    foreign["findings"][0]["id"] = _refind(foreign)
    with pytest.raises(ValueError, match="another generation"):
        graph_from_json(foreign)


def _refind(data: dict[str, Any]) -> str:
    raw = dict(data["findings"][0])
    raw.pop("id")
    probe = ResolutionFinding(
        code=FindingCode(raw["code"]),
        claim=raw["claim"],
        others=tuple(raw["others"]),
        provenance=FindingProvenance(
            raw["provenance"]["resolver_id"],
            raw["provenance"]["resolver_version"],
            ConfigHash(raw["provenance"]["config_hash"]),
        ),
        recorded_at=ledger_tx(raw["recorded_at"]),
    )
    return probe.id


def test_graph_document_order_is_checked() -> None:
    data = _round(built().to_json())
    data["claims"] = list(reversed(data["claims"]))
    with pytest.raises(ValueError, match="ordered"):
        graph_from_json(data)


def test_ambiguous_units_are_kept_as_declared() -> None:
    assert isinstance(AMBIGUOUS, Ambiguous)


def test_model_ref_is_strict() -> None:
    with pytest.raises(ValueError):
        ModelRef("", "1")
    assert OPEN.to_json() == "open"


# --- head (review of PR #60) --------------------------------------------------------------------


def test_head_is_explicit_and_may_follow_the_last_claim() -> None:
    data = _round(built().to_json())
    assert data["head"] == 5
    later = {**data, "head": 9}  # transactions 6..9 produced no claims
    assert graph_from_json(later).head == 9
    with pytest.raises(ValueError, match="after its head"):
        graph_from_json({**data, "head": 3})  # the history records transaction 4
    with pytest.raises(ValueError):
        graph_from_json({k: v for k, v in data.items() if k != "head"})


# --- Schema rules the codec enforces (review of PR #60) -----------------------------------------


def _claim_errors(data: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator({**SCHEMA, "anyOf": [{"$ref": "#/$defs/Claim"}]})
    return [e.message for e in validator.iter_errors(data)]


@pytest.mark.parametrize("original", CLAIMS, ids=lambda c: f"{c.predicate}-{c.assertion_kind}")
def test_schema_accepts_what_the_codec_accepts(original: Claim) -> None:
    assert _claim_errors(_round(original.to_json())) == []


def test_schema_refuses_an_inferred_claim_without_a_model() -> None:
    data = _round(CLAIMS[6].to_json())  # inferred
    del data["provenance"]["model"]
    assert _claim_errors(data)
    with pytest.raises(ValueError):
        claim_from_json(data)


def test_schema_refuses_an_inferred_claim_with_confidence_not_applicable() -> None:
    data = _round(CLAIMS[6].to_json())
    data["confidence"] = {"knowledge": "not_applicable"}
    assert _claim_errors(data)


def test_schema_refuses_a_deterministic_claim_with_a_model() -> None:
    data = _round(CLAIMS[0].to_json())  # observed
    data["provenance"]["model"] = MODEL.to_json()
    assert _claim_errors(data)
    with pytest.raises(ValueError):
        claim_from_json(data)


def test_schema_refuses_a_deterministic_claim_with_a_confidence() -> None:
    data = _round(CLAIMS[0].to_json())
    data["confidence"] = {"knowledge": "known", "value": 0.9}
    assert _claim_errors(data)


# --- Reader results (review of PR #60) ----------------------------------------------------------


def _valid(name: str, value: Any) -> list[str]:
    validator = Draft202012Validator({**SCHEMA, "anyOf": [{"$ref": f"#/$defs/{name}"}]})
    return [e.message for e in validator.iter_errors(_round(value))]


def test_every_reader_result_validates_against_the_schema() -> None:
    golden = built()
    reader = ReferenceReader(golden)
    nodes = {c.subject for c in golden.resolution.claims}
    for tx in range(golden.head + 1):
        at_tx = ledger_tx(tx)
        for subject in nodes:
            assert _valid("ClaimsResult", reader.claims(subject, None, at_tx).to_json()) == []
            neighbours = reader.neighbours(subject, 2, at_tx)
            assert _valid("NeighboursResult", neighbours.to_json()) == []
            view = reader.node(subject, at_tx)
            assert _valid("NodeResult", result_to_json(view, lambda v: v.to_json())) == []
    episodes = reader.episodes(EpisodeFilter(as_of=golden.head))
    assert _valid("EpisodesResult", result_to_json(episodes, list)) == []
    frame = FrameRef("map", "rec:sha256:" + "f" * 64)  # type: ignore[arg-type]
    spatial = SpatialView(next(iter(nodes)), frame, golden.head, golden.resolution.claims[:1])
    assert _valid("SpatialResult", result_to_json(Known(spatial), lambda v: v.to_json())) == []
    assert _valid("EpisodesResult", {"knowledge": "known", "value": "none"}) != []
    assert _valid("ClaimsResult", {"as_of": 1, "claims": []}) != []
