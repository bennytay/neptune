"""The context packet's JSON Schema (draft 2020-12): what ``ContextPacket.to_json`` writes.

Upstream definitions a packet embeds are copied from their owners' exports under their own
names: a claim, a resolver finding, a node and a model ref from graph-schema's export at the
pinned version (``neptune_context.pinned``, ADR 0006 §9), never Memory's live code,
and the compiler types those reuse (``EvidenceRef``, ``FrameRef``, ``Timestamp``, the ids), so
one evidence ref validates the same in all three contracts. The Python reader
(``packets.codec.decode``) is stricter: it recomputes ids, budgets and cross-references.

The ``query-packet`` contract publishes this export (with the query's) under
``contracts/query-packet/`` when it gets its first version (ADR 0003 §9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from neptune_context import pinned
from neptune_context.packets.model import (
    MAX_ITEMS,
    PACKET_KIND,
    PACKET_VERSION,
    TOKENIZER,
    Channel,
    EvidenceStatus,
    GapCode,
    Limit,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue

DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID: Final = f"urn:neptune:schema:context-packet:{PACKET_VERSION}"
# Memory definitions a packet embeds; their transitive references come along.
UPSTREAM_DEFS: Final = (
    "Claim",
    "ClaimId",
    "Confidence",
    "ConfigHash",
    "ContentId",
    "EvidenceRef",
    "FrameRef",
    "LedgerTx",
    "ModelRef",
    "NodeRef",
    "NotApplicable",
    "RecordId",
    "ResolutionFinding",
    "Timestamp",
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


def _const(value: str | int) -> JsonObject:
    return {"const": value}


def _array(items: JsonObject, *, min_items: int = 0, max_items: int | None = None) -> JsonObject:
    out: dict[str, JsonValue] = {"items": items, "type": "array"}
    if min_items:
        out["minItems"] = min_items
    if max_items is not None:
        out["maxItems"] = max_items
    return out


def _int(minimum: int = 0) -> JsonObject:
    return {"maximum": _INT64_MAX, "minimum": minimum, "type": "integer"}


def _ticks() -> JsonObject:
    return {"maximum": _INT64_MAX, "minimum": -(2**63), "type": "integer"}


def _enum(values: list[str]) -> JsonObject:
    return {"enum": sorted(values)}


def _knowledge(value: JsonObject) -> JsonObject:
    """A ``Knowledge`` state whose provenance is the item's own: no ``provenance`` member, and no
    ``known_absent`` (which needs its own grounding)."""
    return {
        "anyOf": [
            _obj({"knowledge": _const("known"), "value": value}),
            _obj({"knowledge": _enum(["not_covered", "unknown"])}),
            _ref("NotApplicable"),
            _obj(
                {
                    "candidates": _array(_obj({"value": value}), min_items=2),
                    "knowledge": _const("ambiguous"),
                }
            ),
        ]
    }


def _upstream_defs() -> dict[str, JsonValue]:
    defs: dict[str, Any] = pinned.graph_schema_defs()
    wanted: dict[str, JsonValue] = {}
    stack = list(UPSTREAM_DEFS)
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


def _item(kind: str, body: dict[str, JsonValue], description: str) -> JsonObject:
    envelope: dict[str, JsonValue] = {
        "assertion_kind": _enum(["inferred", "observed", "stated"]),
        "confidence": _ref("Confidence"),
        "id": {"pattern": "^item:sha256:[0-9a-f]{64}$", "type": "string"},
        "kind": _const(kind),
        "provenance": _ref("ItemProvenance"),
        "relevance": _ref("Relevance"),
    }
    return {**_obj({**envelope, **body}), "description": description}


def _packet_defs() -> dict[str, JsonValue]:
    node_k = _knowledge(_ref("NodeRef"))
    return {
        "ArrowHandle": _obj(
            {
                "package_id": _ref("ContentId"),
                "path": {"pattern": "^series/[0-9a-f]{64}\\.parquet$", "type": "string"},
            }
        ),
        "BudgetUse": _obj(
            {
                "dropped": _int(),
                "exhausted": _array(_enum([str(x) for x in Limit])),
                "limits": _obj(
                    {
                        "bytes": _int(1),
                        "items": _int(1),
                        "latency_ms": _int(1),
                        "tokens": _int(1),
                    },
                    optional=("bytes", "latency_ms", "tokens"),
                ),
                "tokenizer": _const(TOKENIZER),
                "used": _obj({"bytes": _int(), "items": _int(), "tokens": _int()}),
            }
        ),
        "ChannelHit": _obj(
            {
                "channel": _enum([str(c) for c in Channel]),
                "rank": _int(1),
                "score": {"minimum": 0, "type": "number"},
            }
        ),
        "Item": {
            "oneOf": [
                _ref(name)
                for name in (
                    "ClaimItem",
                    "ConfigurationItem",
                    "DocumentSpanItem",
                    "EvidenceItem",
                    "FrameItem",
                    "SceneItem",
                    "SeriesWindowItem",
                )
            ]
        },
        "ClaimItem": _item(
            "claim",
            {"claim": _ref("Claim")},
            "a Memory claim as known at as_of; the envelope repeats the claim's epistemics",
        ),
        "ConfigurationItem": _item(
            "configuration",
            {
                "claims": _array(_ref("ClaimId")),
                "record": _ref("RecordId"),
                "record_kind": {"pattern": "^[a-z][a-z0-9_.-]*$", "type": "string"},
                "subject": node_k,
            },
            "a configuration record and what it configures",
        ),
        "DocumentSpanItem": _item(
            "document_span",
            {
                "document": _ref("RecordId"),
                "evidence": _ref("EvidenceRef"),
                "text": _knowledge({"type": "string"}),
            },
            "a span of a document or table record and its extracted text",
        ),
        "EvidenceItem": _item(
            "evidence",
            {
                "evidence": _ref("EvidenceRef"),
                "size": _knowledge(_int()),
                "status": _enum([str(s) for s in EvidenceStatus]),
            },
            "source bytes and what the Ledger's resolve said at the snapshot",
        ),
        "FrameItem": _item(
            "frame",
            {
                "at": _knowledge(_ref("Timestamp")),
                "encoding": _knowledge({"minLength": 1, "type": "string"}),
                "evidence": _ref("EvidenceRef"),
                "frame": _knowledge(_ref("FrameRef")),
                "stream": _knowledge(_ref("RecordId")),
            },
            "one sensor sample at one instant",
        ),
        "SceneItem": _item(
            "scene",
            {
                "claims": _array(_ref("ClaimId")),
                "frame": _ref("FrameRef"),
                "nodes": _array(_ref("NodeRef")),
                "records": _array(_ref("RecordId")),
                "site": node_k,
            },
            "a spatial subgraph in one named frame",
        ),
        "SeriesWindowItem": _item(
            "series_window",
            {
                "arrow": _ref("ArrowHandle"),
                "clock": _ref("RecordId"),
                "end": _ticks(),
                "start": _ticks(),
                "stream": _ref("RecordId"),
            },
            "a window [start, end) of one stream on one of its clocks",
        ),
        "Gap": _obj(
            {
                "at": {"pattern": "^(/.*)?$", "type": "string"},
                "channel": _enum([str(c) for c in Channel]),
                "code": _enum([str(c) for c in GapCode]),
                "detail": {"minLength": 1, "type": "string"},
                "refs": _array({"minLength": 1, "type": "string"}),
            },
            optional=("channel",),
        ),
        "Header": _obj(
            {
                "as_of": _ref("LedgerTx"),
                "budget": _ref("BudgetUse"),
                "during": _obj(
                    {
                        "domain_id": _ref("RecordId"),
                        "end": {"anyOf": [_ticks(), _const("open")]},
                        "start": _ticks(),
                    }
                ),
                "head": _ref("LedgerTx"),
                "inference_included": {"type": "boolean"},
                "ledger_snapshot": _obj(
                    {
                        "catalog_api_version": {
                            "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$",
                            "type": "string",
                        }
                    }
                ),
                "memory_snapshot": _obj(
                    {
                        "as_of": _ref("LedgerTx"),
                        "generation": _ref("ConfigHash"),
                        "graph_schema_version": _int(1),
                    }
                ),
                "produced_by": _obj(
                    {
                        "config_hash": _ref("ConfigHash"),
                        "engine_id": {"pattern": "^[a-z][a-z0-9_.-]*$", "type": "string"},
                        "engine_version": {"minLength": 1, "type": "string"},
                    }
                ),
                "query_id": {"pattern": "^query:sha256:[0-9a-f]{64}$", "type": "string"},
            },
            optional=("during",),
        ),
        "ItemProvenance": _obj(
            {
                "evidence": _array(_ref("EvidenceRef"), min_items=1),
                "model": _ref("ModelRef"),
                "records": _array(_ref("RecordId")),
                "transform": _obj(
                    {
                        "config_hash": _ref("ConfigHash"),
                        "producer_id": {"pattern": "^[a-z][a-z0-9_.-]*$", "type": "string"},
                        "producer_version": {"minLength": 1, "type": "string"},
                    }
                ),
            },
            optional=("model",),
        ),
        "ContextPacket": _obj(
            {
                "findings": _array(_ref("ResolutionFinding")),
                "gaps": _array(_ref("Gap")),
                "header": _ref("Header"),
                "id": {"pattern": "^packet:sha256:[0-9a-f]{64}$", "type": "string"},
                "items": _array(_ref("Item"), max_items=MAX_ITEMS),
                "kind": _const(PACKET_KIND),
                "packet_version": _const(PACKET_VERSION),
                "superseded_since": _array(
                    _obj(
                        {
                            "by": _array(_ref("ClaimId"), min_items=1),
                            "claim": _ref("ClaimId"),
                            "superseded_at": _ref("LedgerTx"),
                        }
                    )
                ),
            }
        ),
        "Relevance": _obj(
            {
                "hits": _array(_ref("ChannelHit"), min_items=1),
                "score": {"minimum": 0, "type": "number"},
            }
        ),
    }


def packet_schema() -> JsonObject:
    """The whole contract: a packet document, and each part by name (``#/$defs/ClaimItem``)."""
    defs = {**_upstream_defs(), **_packet_defs()}
    return {
        "$defs": dict(sorted(defs.items())),
        "$id": SCHEMA_ID,
        "$schema": DIALECT,
        "anyOf": [_ref("ContextPacket")],
        "description": (
            "Neptune Context's context packet (neptune-context ADR 0003): a header, items, claims"
            " superseded since as_of, resolver findings and gaps. Claims, findings and the"
            " compiler types they reuse are copied from Memory's graph-schema export. Generated"
            " from neptune_context.packets; the Python reader in neptune_context.packets.codec"
            " is stricter (ids, budgets and cross-references are recomputed)."
        ),
        "title": f"Neptune Context packet, version {PACKET_VERSION}",
    }
