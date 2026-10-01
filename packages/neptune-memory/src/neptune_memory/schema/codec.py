"""Strict JSON parsing of graph-schema objects: claims, findings and graph documents (ADR 0006).

``to_json`` on each type writes the published shape; this module reads it back. Parsing is strict,
because a graph document is input like any other: unknown keys, missing keys, wrong types and a
stored id that does not match the content are ``ValueError``s, never silently repaired.

A *graph document* is one resolved history: every claim version, every finding, the graph-schema
version and the resolver configuration that produced it (its *generation*, ADR 0006 §7).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from neptune.identity.ids import config_hash
from neptune.model.ids import ConfigHash, parse_config_hash, parse_record_id
from neptune.model.knowledge import AssertionKind, Knowledge
from neptune.model.knowledge import from_json as knowledge_from_json
from neptune.model.provenance import evidence_ref_from_json
from neptune.model.scalars import real_from_json
from neptune.model.time import timestamp_from_json
from neptune.model.units import Unit, unit_from_json
from neptune_memory.schema import GRAPH_SCHEMA_VERSION
from neptune_memory.schema.claim import (
    Claim,
    ClaimAssertionKind,
    ClaimObject,
    ClaimProvenance,
    LedgerRecordRef,
    LiteralValue,
    ModelRef,
    TypedLiteral,
    ValueType,
    parse_claim_id,
)
from neptune_memory.schema.interval import OPEN, LedgerTx, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import (
    FindingCode,
    FindingProvenance,
    Resolution,
    ResolutionFinding,
    parse_finding_id,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.time import Timestamp

GRAPH_DOCUMENT_KIND: Final = "memory.graph"


def _object(data: object, what: str) -> Mapping[str, JsonValue]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a JSON object, got {type(data).__name__}")
    return data


def _exact(
    data: object, what: str, required: set[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, JsonValue]:
    obj = _object(data, what)
    missing = sorted(required - obj.keys())
    extra = sorted(obj.keys() - required - optional)
    if missing or extra:
        raise ValueError(f"{what}: missing keys {missing}, unexpected keys {extra}")
    return obj


def _str(value: JsonValue, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    return value


def _list(value: JsonValue, what: str) -> Sequence[JsonValue]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{what} must be an array")
    return value


def _int(value: JsonValue, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    return value


def _no_provenance(_: JsonObject) -> NoReturn:
    raise ValueError("this value inherits the claim's provenance; it carries none of its own")


def _tx(value: JsonValue, what: str) -> LedgerTx:
    return ledger_tx(_int(value, what))


def _tx_end(value: JsonValue, what: str) -> LedgerTx | Open:
    return OPEN if value == "open" else _tx(value, what)


def node_from_json(data: JsonValue) -> NodeRef:
    obj = _exact(data, "node", {"kind", "node_id", "node_type"})
    if obj["kind"] != "node":
        raise ValueError(f"node kind must be 'node', got {obj['kind']!r}")
    return NodeRef(NodeType(_str(obj["node_type"], "node_type")), _str(obj["node_id"], "node_id"))


def _literal_value(datatype: ValueType, value: JsonValue) -> LiteralValue:
    if datatype is ValueType.INSTANT:
        return timestamp_from_json(value)
    if datatype in (ValueType.REAL, ValueType.QUANTITY) and not (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        return real_from_json(value)
    if isinstance(value, str | bool | int | float):
        return value
    raise ValueError(f"not a {datatype} value: {value!r}")


def object_from_json(data: JsonValue) -> ClaimObject:
    kind = _object(data, "object").get("kind")
    if kind == "node":
        return node_from_json(data)
    if kind == "record":
        obj = _exact(data, "record ref", {"kind", "record_id"})
        return LedgerRecordRef(parse_record_id(_str(obj["record_id"], "record_id")))
    if kind == "literal":
        obj = _exact(data, "literal", {"datatype", "kind", "unit", "value"})
        datatype = ValueType(_str(obj["datatype"], "datatype"))
        unit: Knowledge[Unit] = knowledge_from_json(obj["unit"], unit_from_json, _no_provenance)
        return TypedLiteral(datatype, _literal_value(datatype, obj["value"]), unit)
    raise ValueError(f"object kind must be node, record or literal, got {kind!r}")


def _assertion_kind(value: JsonValue) -> ClaimAssertionKind:
    text = _str(value, "assertion_kind")
    if text == "inferred":
        return "inferred"
    return AssertionKind(text)


def _confidence(value: float | int | str | bool | JsonValue | None) -> float:
    if not isinstance(value, float):
        raise ValueError(f"confidence must be a JSON number with a fraction: {value!r}")
    return value


def model_from_json(data: JsonValue) -> ModelRef:
    obj = _exact(data, "model", {"model_id", "model_version"})
    return ModelRef(_str(obj["model_id"], "model_id"), _str(obj["model_version"], "model_version"))


def provenance_from_json(data: JsonValue) -> ClaimProvenance:
    obj = _exact(
        data,
        "claim provenance",
        {"config_hash", "consolidator_id", "consolidator_version", "evidence", "records"},
        frozenset({"model"}),
    )
    return ClaimProvenance(
        evidence=tuple(evidence_ref_from_json(e) for e in _list(obj["evidence"], "evidence")),
        records=tuple(parse_record_id(_str(r, "record")) for r in _list(obj["records"], "records")),
        consolidator_id=_str(obj["consolidator_id"], "consolidator_id"),
        consolidator_version=_str(obj["consolidator_version"], "consolidator_version"),
        config_hash=parse_config_hash(_str(obj["config_hash"], "config_hash")),
        model=model_from_json(obj["model"]) if "model" in obj else None,
    )


def _valid(data: JsonValue) -> tuple[Timestamp, Timestamp | Open]:
    obj = _exact(data, "valid", {"end", "start"})
    end = obj["end"]
    return timestamp_from_json(obj["start"]), OPEN if end == "open" else timestamp_from_json(end)


def claim_from_json(data: JsonValue) -> Claim:
    """A claim exactly as ``Claim.to_json`` wrote it; its stored ``id`` must match its content."""
    obj = _exact(
        data,
        "claim",
        {
            "assertion_kind",
            "confidence",
            "id",
            "object",
            "predicate",
            "provenance",
            "recorded_at",
            "subject",
            "superseded_at",
            "supersedes",
            "valid",
        },
    )
    valid_from, valid_to = _valid(obj["valid"])
    claim = Claim(
        subject=node_from_json(obj["subject"]),
        predicate=_str(obj["predicate"], "predicate"),
        object=object_from_json(obj["object"]),
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=_tx(obj["recorded_at"], "recorded_at"),
        assertion_kind=_assertion_kind(obj["assertion_kind"]),
        confidence=knowledge_from_json(obj["confidence"], _confidence, _no_provenance),
        provenance=provenance_from_json(obj["provenance"]),
        superseded_at=_tx_end(obj["superseded_at"], "superseded_at"),
        supersedes=tuple(
            parse_claim_id(_str(i, "supersedes")) for i in _list(obj["supersedes"], "supersedes")
        ),
    )
    if parse_claim_id(_str(obj["id"], "id")) != claim.id:
        raise ValueError(f"claim id {obj['id']!r} does not match its content ({claim.id})")
    return claim


def finding_provenance_from_json(data: JsonValue) -> FindingProvenance:
    obj = _exact(data, "finding provenance", {"config_hash", "resolver_id", "resolver_version"})
    return FindingProvenance(
        _str(obj["resolver_id"], "resolver_id"),
        _str(obj["resolver_version"], "resolver_version"),
        parse_config_hash(_str(obj["config_hash"], "config_hash")),
    )


def finding_from_json(data: JsonValue) -> ResolutionFinding:
    """A finding exactly as ``ResolutionFinding.to_json`` wrote it; its ``id`` must match."""
    obj = _exact(
        data,
        "finding",
        {"claim", "code", "id", "others", "provenance", "recorded_at", "superseded_at"},
    )
    finding = ResolutionFinding(
        code=FindingCode(_str(obj["code"], "code")),
        claim=parse_claim_id(_str(obj["claim"], "claim")),
        others=tuple(parse_claim_id(_str(o, "others")) for o in _list(obj["others"], "others")),
        provenance=finding_provenance_from_json(obj["provenance"]),
        recorded_at=_tx(obj["recorded_at"], "recorded_at"),
        superseded_at=_tx_end(obj["superseded_at"], "superseded_at"),
    )
    if parse_finding_id(_str(obj["id"], "id")) != finding.id:
        raise ValueError(f"finding id {obj['id']!r} does not match its content ({finding.id})")
    return finding


@dataclass(frozen=True)
class GraphDocument:
    """One resolved history and the resolver configuration that produced it.

    ``generation`` is ``config_hash(resolver_config)``: the store generation (ADR 0006 §7). Two
    documents with different generations are different graphs, never merged or diffed by id.
    """

    resolution: Resolution
    resolver_config: JsonObject

    @property
    def generation(self) -> ConfigHash:
        return config_hash(self.resolver_config)

    @property
    def head(self) -> LedgerTx:
        """The latest transaction the document knows: the highest ``recorded_at``, or 0."""
        stamps = [c.recorded_at for c in self.resolution.claims]
        stamps += [f.recorded_at for f in self.resolution.findings]
        return ledger_tx(max(stamps, default=0))

    def to_json(self) -> JsonObject:
        return {
            "claims": [claim.to_json() for claim in self.resolution.claims],
            "findings": [finding.to_json() for finding in self.resolution.findings],
            "generation": self.generation,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "kind": GRAPH_DOCUMENT_KIND,
            "resolver_config": self.resolver_config,
        }


def graph_from_json(data: JsonValue) -> GraphDocument:
    """A graph document; its version, generation, ids and order are all checked."""
    obj = _exact(
        data,
        "graph document",
        {"claims", "findings", "generation", "graph_schema_version", "kind", "resolver_config"},
    )
    if obj["kind"] != GRAPH_DOCUMENT_KIND:
        raise ValueError(f"graph document kind must be {GRAPH_DOCUMENT_KIND!r}")
    if _int(obj["graph_schema_version"], "graph_schema_version") != GRAPH_SCHEMA_VERSION:
        raise ValueError(f"graph_schema_version {obj['graph_schema_version']!r} is not supported")
    claims = tuple(claim_from_json(c) for c in _list(obj["claims"], "claims"))
    findings = tuple(finding_from_json(f) for f in _list(obj["findings"], "findings"))
    if list(claims) != sorted(claims, key=lambda c: (c.recorded_at, c.id)):
        raise ValueError("claims must be ordered by (recorded_at, id)")
    if list(findings) != sorted(findings, key=lambda f: (f.recorded_at, f.claim, f.code, f.others)):
        raise ValueError("findings must be ordered by (recorded_at, claim, code, others)")
    document = GraphDocument(
        Resolution(claims, findings), dict(_object(obj["resolver_config"], "resolver_config"))
    )
    if parse_config_hash(_str(obj["generation"], "generation")) != document.generation:
        raise ValueError("generation does not match the resolver configuration")
    _check_consistent(document)
    return document


def _check_consistent(document: GraphDocument) -> None:
    """One history: unique ids, every reference resolvable, findings from this generation."""
    claims, findings = document.resolution.claims, document.resolution.findings
    ids = {c.id for c in claims}
    if len(ids) != len(claims):
        raise ValueError("a claim id appears twice")
    if len({f.id for f in findings}) != len(findings):
        raise ValueError("a finding id appears twice")
    dangling = sorted({i for c in claims for i in c.supersedes} - ids)
    dangling += sorted({i for f in findings for i in (f.claim, *f.others)} - ids)
    if dangling:
        raise ValueError(f"references to claims the document does not hold: {dangling[:3]}")
    foreign = [f.id for f in findings if f.provenance.config_hash != document.generation]
    if foreign:
        raise ValueError(f"findings from another generation: {foreign[:3]}")
