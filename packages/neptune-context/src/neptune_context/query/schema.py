"""The query's JSON Schema (draft 2020-12, ADR 0002 §6), in ``docs/schema/query.schema.json``.

It describes exactly what ``codec.to_json`` writes and ``decode`` reads: members, types, enums
(subject kinds, predicates, text fields and channels from the pinned upstream vocabularies), id
patterns and numeric bounds. Rules that relate members (one clock per diff unless bridged, one
frame and unit across regions, an anchored graph clause, a non-empty query) are ``validate``'s:
a document the schema accepts can still be refused, never the reverse.

Regenerate with ``uv run python -m neptune_context.query.schema
packages/neptune-context/docs/schema/query.schema.json``; a test fails when the file is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from neptune_context.query.codec import ANY, OPEN, QUERY_ID_PATTERN
from neptune_context.query.model import (
    HEAD,
    INT64_MAX,
    INT64_MIN,
    MAX_BRIDGES,
    MAX_BYTES,
    MAX_EXPLAIN,
    MAX_HOPS,
    MAX_ITEMS,
    MAX_LATENCY_MS,
    MAX_REGIONS,
    MAX_SAME_AS_DEPTH,
    MAX_SUBJECTS,
    MAX_TEXT_CHARS,
    MAX_TOKENS,
    MAX_ZONES,
    QUERY_VERSION,
    Direction,
    TextChannel,
    TextField,
)
from neptune_context.query.validate import PREDICATES, SUBJECT_KINDS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune.model.jsonvalue import JsonObject, JsonValue

DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID: Final = f"urn:neptune:schema:query:{QUERY_VERSION}"
# Looser than ``validate.is_declared_id`` (a value that is not blank or padded), never stricter.
_DECLARED_ID: Final = r"^[a-z][a-z0-9_.\-]*:[\s\S]+$"


def _ref(name: str) -> JsonObject:
    return {"$ref": f"#/$defs/{name}"}


def _obj(properties: dict[str, JsonValue], optional: tuple[str, ...] = ()) -> JsonObject:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(k for k in properties if k not in optional),
        "type": "object",
    }


def _int(low: int, high: int) -> JsonObject:
    return {"maximum": high, "minimum": low, "type": "integer"}


def _set(items: JsonValue, high: int, *, low: int = 0) -> JsonObject:
    return {"items": items, "maxItems": high, "minItems": low, "type": "array", "uniqueItems": True}


def _enum(values: Iterable[object]) -> JsonObject:
    return {"enum": sorted(str(v) for v in values), "type": "string"}


def _sha(prefix: str) -> JsonObject:
    return {"pattern": f"^{prefix}:sha256:[0-9a-f]{{64}}$", "type": "string"}


def _defs() -> dict[str, JsonValue]:
    ticks = _int(INT64_MIN, INT64_MAX)
    transaction = _int(0, INT64_MAX)
    coordinate: JsonObject = {"type": "number"}
    return {
        "Budget": _obj(
            {
                "bytes": _int(1, MAX_BYTES),
                "items": _int(1, MAX_ITEMS),
                "latency_ms": _int(1, MAX_LATENCY_MS),
                "tokens": _int(1, MAX_TOKENS),
            },
            optional=("bytes", "latency_ms", "tokens"),
        ),
        "CivilClock": _obj(
            {
                "epoch": {"type": "string"},
                "kind": {"const": "civil"},
                "resolution": _ref("Resolution"),
                "timescale": {"type": "string"},
            }
        ),
        "ClaimId": _sha("claim"),
        "Clock": {"oneOf": [_ref("DomainClock"), _ref("CivilClock")]},
        "ClockBridge": _obj(
            {"mapping_id": _ref("RecordId"), "source": _ref("Clock"), "target": _ref("Clock")}
        ),
        "DeclaredId": {"pattern": _DECLARED_ID, "type": "string"},
        "Diff": _obj(
            {
                "after": _ref("DiffPoint"),
                "before": _ref("DiffPoint"),
                "kind": {"const": "diff"},
                "subject": _ref("Subject"),
            }
        ),
        "DiffPoint": {"oneOf": [transaction, _ref("Instant")]},
        "DomainClock": _obj({"domain_id": _ref("RecordId"), "kind": {"const": "domain"}}),
        "During": _obj(
            {
                "clock": _ref("Clock"),
                "end": {"oneOf": [ticks, {"const": OPEN}]},
                "start": ticks,
            }
        ),
        "Explain": {"oneOf": [_ref("Why"), _ref("Diff")]},
        "FrameBridge": _obj(
            {
                "child": _ref("FrameRef"),
                "parent": _ref("FrameRef"),
                "transform_id": _ref("RecordId"),
            }
        ),
        "FrameRef": _obj(
            {"frame_id": {"minLength": 1, "type": "string"}, "graph_id": _ref("RecordId")}
        ),
        "Graph": _obj(
            {
                "direction": _enum(Direction),
                "hops": _int(1, MAX_HOPS),
                "predicates": {
                    "oneOf": [
                        {"const": ANY},
                        _set(_enum(PREDICATES), len(PREDICATES), low=1),
                    ]
                },
            }
        ),
        "Instant": _obj({"clock": _ref("Clock"), "ticks": ticks}),
        "Query": _obj(
            {
                "as_of": {"oneOf": [{"const": HEAD}, transaction]},
                "budget": _ref("Budget"),
                "clock_bridges": _set(_ref("ClockBridge"), MAX_BRIDGES),
                "during": _ref("During"),
                "explain": {"items": _ref("Explain"), "maxItems": MAX_EXPLAIN, "type": "array"},
                "frame_bridges": _set(_ref("FrameBridge"), MAX_BRIDGES),
                "graph": _ref("Graph"),
                "include_inferred": {"type": "boolean"},
                "query_version": {"const": QUERY_VERSION},
                "regions": _set(_ref("Region"), MAX_REGIONS),
                "site": _ref("Site"),
                "subjects": _set(_ref("Subject"), MAX_SUBJECTS),
                "text": _ref("Text"),
            },
            optional=("during", "graph", "site", "text"),
        ),
        "QueryId": {"pattern": QUERY_ID_PATTERN, "type": "string"},
        "RecordId": _sha("rec"),
        "Region": _obj(
            {
                "frame": _ref("FrameRef"),
                "shape": {"oneOf": [_ref("Box"), _ref("Sphere")]},
                "unit": {"minLength": 1, "type": "string"},
            }
        ),
        "Box": _obj({"kind": {"const": "box"}, "max": _ref("Vec3"), "min": _ref("Vec3")}),
        "Resolution": _obj({"denominator": _int(1, INT64_MAX), "numerator": _int(1, INT64_MAX)}),
        "Site": _obj({"site": _ref("DeclaredId"), "zones": _set(_ref("DeclaredId"), MAX_ZONES)}),
        "Sphere": _obj(
            {
                "center": _ref("Vec3"),
                "kind": {"const": "sphere"},
                "radius": {"exclusiveMinimum": 0, "type": "number"},
            }
        ),
        "Subject": _obj(
            {
                "declared_id": _ref("DeclaredId"),
                "kind": _enum(SUBJECT_KINDS),
                "same_as_depth": _int(0, MAX_SAME_AS_DEPTH),
            },
            optional=("declared_id",),
        ),
        "Text": _obj(
            {
                "channels": _set(_enum(TextChannel), len(TextChannel), low=1),
                "fields": _set(_enum(TextField), len(TextField), low=1),
                "text": {"maxLength": MAX_TEXT_CHARS, "minLength": 1, "type": "string"},
            }
        ),
        "Vec3": {"items": coordinate, "maxItems": 3, "minItems": 3, "type": "array"},
        "Why": _obj({"claim_id": _ref("ClaimId"), "kind": {"const": "why"}}),
    }


def query_schema() -> JsonObject:
    """The draft 2020-12 schema of one query document; ``$defs`` sorted, so export is stable."""
    defs = _defs()
    return {
        "$defs": {name: defs[name] for name in sorted(defs)},
        "$id": SCHEMA_ID,
        "$ref": "#/$defs/Query",
        "$schema": DIALECT,
        "title": f"Neptune context query, query_version {QUERY_VERSION}",
    }


def schema_bytes() -> bytes:
    """The exported file's bytes: sorted keys, two-space indent, one trailing newline."""
    text = json.dumps(query_schema(), indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m neptune_context.query.schema <output.json>\n")
        return 2
    Path(argv[0]).write_bytes(schema_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
