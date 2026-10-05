"""The graph-schema JSON Schema (draft 2020-12), published under ``contracts/graph-schema/``.

It describes exactly what ``to_json`` writes: a claim, a resolver finding, a predicate
vocabulary and a graph document (``codec.GraphDocument``). Compiler types a claim reuses
(``Timestamp``, ``EvidenceRef``, ``Unit``, ``NonFinite`` and the ids) are copied from the
compiler's package-schema export under their own names, so one evidence ref validates the same
in both contracts. The Python readers in ``codec`` are stricter (ids are recomputed).

``scripts/contracts.py check-owner --package neptune-memory`` fails when this export drifts from
the registry's latest version; ``bump`` publishes a new one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from neptune.model.schema import canonical_schema
from neptune_memory.schema import GRAPH_SCHEMA_VERSION
from neptune_memory.schema.claim import ValueType
from neptune_memory.schema.clock_map import MapMethod
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import Cardinality
from neptune_memory.schema.supersede import FindingCode

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue

DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID: Final = f"urn:neptune:schema:graph:{GRAPH_SCHEMA_VERSION}"
# Compiler definitions a claim embeds; their transitive references come along.
COMPILER_DEFS: Final = (
    "ClockAnchor",
    "ConfigHash",
    "Duration",
    "EvidenceRef",
    "Fraction",
    "FrameRef",
    "NonFinite",
    "RecordId",
    "Timestamp",
    "Unit",
)
_INT64_MAX: Final = 2**63 - 1


def _ref(name: str) -> JsonObject:
    return {"$ref": f"#/$defs/{name}"}


def _obj(properties: dict[str, JsonValue], optional: tuple[str, ...] = ()) -> JsonObject:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(k for k in properties if k not in optional),
        "type": "object",
    }


def _const(value: str) -> JsonObject:
    return {"const": value}


def _pattern(prefix: str) -> JsonObject:
    return {"pattern": f"^{prefix}sha256:[0-9a-f]{{64}}$", "type": "string"}


def _array(items: JsonObject, *, min_items: int = 0) -> JsonObject:
    out: dict[str, JsonValue] = {"items": items, "type": "array"}
    if min_items:
        out["minItems"] = min_items
    return out


def _compiler_defs() -> dict[str, JsonValue]:
    """``COMPILER_DEFS`` and every definition they reference, from the compiler's export."""
    defs: dict[str, Any] = canonical_schema()["$defs"]  # type: ignore[assignment]
    wanted: dict[str, JsonValue] = {}
    stack = list(COMPILER_DEFS)
    while stack:
        name = stack.pop()
        if name in wanted:
            continue
        wanted[name] = defs[name]
        stack.extend(_refs(defs[name]))
    return wanted


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"].removeprefix("#/$defs/")] if "$ref" in node else []
        return found + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def _literal(datatype: ValueType, value: JsonObject, unit: JsonObject) -> JsonObject:
    return _obj(
        {"datatype": _const(str(datatype)), "kind": _const("literal"), "unit": unit, "value": value}
    )


def _inherited(value: JsonObject) -> JsonObject:
    """A ``Knowledge`` state whose provenance is the claim's: no state carries its own."""
    return {
        "anyOf": [
            _obj({"knowledge": _const("known"), "value": value}),
            _obj({"knowledge": {"enum": ["not_applicable", "not_covered", "unknown"]}}),
            _obj(
                {
                    "candidates": _array(_obj({"value": value}), min_items=2),
                    "knowledge": _const("ambiguous"),
                }
            ),
        ]
    }


def _memory_defs() -> dict[str, JsonValue]:
    not_applicable = _obj({"knowledge": _const("not_applicable")})
    number = {"anyOf": [{"type": "number"}, _ref("NonFinite")]}
    quantity_unit: JsonObject = {
        "anyOf": [
            _obj({"knowledge": _const("known"), "value": _ref("Unit")}),
            _obj({"knowledge": _const("unknown")}),
            _obj(
                {
                    "candidates": _array(_obj({"value": _ref("Unit")}), min_items=2),
                    "knowledge": _const("ambiguous"),
                }
            ),
        ],
        "description": "a quantity's unit exactly as declared; it inherits the claim's provenance",
    }
    na: JsonObject = _ref("NotApplicable")
    return {
        "ClockMap": {
            **_obj(
                {
                    "anchor": _inherited(_ref("ClockAnchor")),
                    "chain": _array(_ref("RecordId")),
                    "method": {"enum": sorted(str(m) for m in MapMethod)},
                    "rate": _inherited(_ref("Fraction")),
                    "residual_bound": _inherited(_ref("Duration")),
                    "target": _ref("RecordId"),
                    "via": _array(_ref("RecordId")),
                }
            ),
            "description": (
                "a clock mapping onto target as the evidence states it: target(t) = anchor.target"
                " + rate * (t - anchor.source), within residual_bound target ticks; or, composed,"
                " the chain of mapping records it follows (via: the clocks between) and no"
                " parameters of its own (ADR 0011 §2)"
            ),
        },
        "ClaimAssertionKind": {"enum": ["inferred", "observed", "stated"]},
        "Cardinality": {"enum": sorted(str(c) for c in Cardinality)},
        "Claim": {
            # ADR 0006 §3: inferred <=> provenance.model; deterministic => confidence
            # not_applicable (and an inferred claim's confidence is known or unknown).
            "allOf": [
                {
                    "else": {
                        "properties": {
                            "confidence": _ref("NotApplicable"),
                            "provenance": {"not": {"required": ["model"]}},
                        }
                    },
                    "if": {"properties": {"assertion_kind": _const("inferred")}},
                    "then": {
                        "properties": {
                            "confidence": {"not": _ref("NotApplicable")},
                            "provenance": {"required": ["model"]},
                        }
                    },
                }
            ],
            **_obj(
                {
                    "assertion_kind": _ref("ClaimAssertionKind"),
                    "confidence": _ref("Confidence"),
                    "id": _ref("ClaimId"),
                    "object": _ref("ClaimObject"),
                    "predicate": _ref("Token"),
                    "provenance": _ref("ClaimProvenance"),
                    "recorded_at": _ref("LedgerTx"),
                    "subject": _ref("NodeRef"),
                    "superseded_at": _ref("TxEnd"),
                    "supersedes": _array(_ref("ClaimId")),
                    "valid": _ref("Interval"),
                }
            ),
            "description": (
                "subject predicate object over a valid interval, recorded at a Ledger transaction"
                " (ADR 0002 §2). id hashes everything but recorded_at, superseded_at, supersedes."
                " An inferred claim names its model in provenance; an observed or stated one has"
                " confidence not_applicable and no model (ADR 0006 §3)."
            ),
        },
        "ClaimId": _pattern("claim:"),
        "ClaimObject": {"anyOf": [_ref("NodeRef"), _ref("TypedLiteral"), _ref("LedgerRecordRef")]},
        "ClaimProvenance": _obj(
            {
                "config_hash": _ref("ConfigHash"),
                "consolidator_id": _ref("Token"),
                "consolidator_version": {"minLength": 1, "type": "string"},
                "evidence": _array(_ref("EvidenceRef"), min_items=1),
                "model": _ref("ModelRef"),
                "records": _array(_ref("RecordId")),
            },
            optional=("model",),
        ),
        "Confidence": {
            "anyOf": [
                not_applicable,
                _obj({"knowledge": _const("unknown")}),
                _obj(
                    {
                        "knowledge": _const("known"),
                        "value": {"maximum": 1, "minimum": 0, "type": "number"},
                    }
                ),
            ]
        },
        "FindingCode": {"enum": sorted(str(c) for c in FindingCode)},
        "FindingId": _pattern("finding:"),
        "FindingProvenance": _obj(
            {
                "config_hash": _ref("ConfigHash"),
                "resolver_id": _ref("Token"),
                "resolver_version": {"minLength": 1, "type": "string"},
            }
        ),
        "Graph": {
            **_obj(
                {
                    "claims": _array(_ref("Claim")),
                    "findings": _array(_ref("ResolutionFinding")),
                    "generation": _ref("ConfigHash"),
                    "graph_schema_version": {"const": GRAPH_SCHEMA_VERSION},
                    "head": _ref("LedgerTx"),
                    "kind": _const("memory.graph"),
                    "resolver_config": _ref("ResolverConfig"),
                }
            ),
            "description": (
                "one resolved history: every claim version ordered by (recorded_at, id), every"
                " finding, and the resolver configuration whose hash is its generation (ADR 0006)"
            ),
        },
        "Interval": _obj(
            {
                "end": {"anyOf": [_ref("Timestamp"), _const("open")]},
                "start": _ref("Timestamp"),
            }
        ),
        "LedgerRecordRef": _obj({"kind": _const("record"), "record_id": _ref("RecordId")}),
        "LedgerTx": {"maximum": _INT64_MAX, "minimum": 0, "type": "integer"},
        "ModelRef": _obj(
            {
                "model_id": {"minLength": 1, "type": "string"},
                "model_version": {"minLength": 1, "type": "string"},
            }
        ),
        "NodeRef": _obj(
            {
                "kind": _const("node"),
                "node_id": {"minLength": 1, "type": "string"},
                "node_type": _ref("NodeType"),
            }
        ),
        "NodeType": {"enum": sorted(str(t) for t in NodeType)},
        "NotApplicable": not_applicable,
        "PredicateRegistry": _obj({"predicates": _array(_ref("PredicateSpec"))}),
        "PredicateSpec": _obj(
            {
                "cardinality": _ref("Cardinality"),
                "description": {"minLength": 1, "type": "string"},
                "domain": _array(_ref("NodeType"), min_items=1),
                "name": _ref("Token"),
                "range": _array({"anyOf": [_ref("NodeType"), _ref("ValueType")]}, min_items=1),
                "version": {"minimum": 1, "type": "integer"},
            }
        ),
        "ResolutionFinding": {
            **_obj(
                {
                    "claim": _ref("ClaimId"),
                    "code": _ref("FindingCode"),
                    "id": _ref("FindingId"),
                    "others": _array(_ref("ClaimId")),
                    "provenance": _ref("FindingProvenance"),
                    "recorded_at": _ref("LedgerTx"),
                    "superseded_at": _ref("TxEnd"),
                }
            ),
            "description": (
                "a resolver finding, bi-temporal like a claim; id hashes code, claim, others and"
                " provenance (ADR 0006 §5)"
            ),
        },
        "ResolverConfig": _obj(
            {
                "priorities": {
                    "additionalProperties": {"type": "integer"},
                    "propertyNames": _ref("Token"),
                    "type": "object",
                },
                "vocabulary": _ref("PredicateRegistry"),
                "vocabulary_version": {"minimum": 1, "type": "integer"},
            }
        ),
        "Token": {"pattern": "^[a-z][a-z0-9_.\\-]*$", "type": "string"},
        "TxEnd": {"anyOf": [_ref("LedgerTx"), _const("open")]},
        "TypedLiteral": {
            "anyOf": [
                _literal(ValueType.TEXT, {"type": "string"}, na),
                _literal(ValueType.INTEGER, {"type": "integer"}, na),
                _literal(ValueType.REAL, number, na),
                _literal(ValueType.BOOLEAN, {"type": "boolean"}, na),
                _literal(ValueType.QUANTITY, number, quantity_unit),
                _literal(ValueType.INSTANT, _ref("Timestamp"), na),
                _literal(ValueType.CLOCK_MAP, _ref("ClockMap"), na),
            ]
        },
        "ValueType": {"enum": sorted(str(t) for t in ValueType)},
        **_result_defs(),
    }


def _not_covered_or(value: JsonObject) -> JsonObject:
    return {
        "anyOf": [
            _obj({"knowledge": _const("known"), "value": value}),
            _obj({"knowledge": _const("not_covered")}),
        ]
    }


def _result_defs() -> dict[str, JsonValue]:
    """``MemoryReader`` results as ``to_json`` writes them (ADR 0006 §8), so ``check-owner``
    covers a renamed or retyped result field like any other part of the contract."""
    claims = _array(_ref("Claim"))
    findings = _array(_ref("ResolutionFinding"))
    return {
        "ClaimsResult": _obj(
            {
                "as_of": _ref("LedgerTx"),
                "claims": claims,
                "findings": findings,
                "other_clocks": claims,
            }
        ),
        "EpisodeView": _obj({"claims": claims, "episode": _ref("NodeRef")}),
        "EpisodesResult": _not_covered_or(_array(_ref("EpisodeView"))),
        "Neighbour": _obj(
            {"depth": {"minimum": 1, "type": "integer"}, "node": _ref("NodeRef"), "via": claims}
        ),
        "NeighboursResult": _obj(
            {
                "as_of": _ref("LedgerTx"),
                "findings": findings,
                "hops": {"minimum": 0, "type": "integer"},
                "neighbours": _array(_ref("Neighbour")),
                "start": _ref("NodeRef"),
            }
        ),
        "NodeResult": _not_covered_or(_ref("NodeView")),
        "NodeView": _obj(
            {
                "as_of": _ref("LedgerTx"),
                "claims": claims,
                "findings": findings,
                "incoming": claims,
                "node": _ref("NodeRef"),
            }
        ),
        "SpatialResult": _not_covered_or(_ref("SpatialView")),
        "SpatialView": _obj(
            {
                "as_of": _ref("LedgerTx"),
                "claims": claims,
                "frame": _ref("FrameRef"),
                "site": _ref("NodeRef"),
            }
        ),
    }


def graph_schema() -> JsonObject:
    """The whole contract: a graph document, and each part by name (``#/$defs/Claim``)."""
    defs = {**_compiler_defs(), **_memory_defs()}
    return {
        "$defs": dict(sorted(defs.items())),
        "$id": SCHEMA_ID,
        "$schema": DIALECT,
        "anyOf": [_ref("Graph")],
        "description": (
            "Neptune Memory's graph schema (neptune-memory ADR 0002, 0005, 0006): a graph"
            " document; claims, findings and the vocabulary are #/$defs/Claim,"
            " #/$defs/ResolutionFinding and #/$defs/PredicateRegistry; MemoryReader results are"
            " #/$defs/NodeResult, ClaimsResult, NeighboursResult, EpisodesResult and"
            " SpatialResult. Generated from neptune_memory.schema; the Python readers in"
            " neptune_memory.schema.codec are stricter."
        ),
        "title": f"Neptune Memory graph schema, version {GRAPH_SCHEMA_VERSION}",
    }
