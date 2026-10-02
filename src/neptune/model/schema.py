"""The canonical model's JSON Schema, generated from its Python types (ADR 0017 §1, ADR 0021).

The Python types are the source of truth. This module walks every record kind's dataclass fields
and type hints and writes the language-neutral contract a non-Python consumer validates against
(JSON Schema, draft 2020-12). A test regenerates it and fails on any drift from the committed
``docs/schema/canonical.schema.json``; ``make schema`` rewrites that file.

Most JSON keys are field names. The few types whose JSON has another shape (a location's ``kind``
tag, a record range's ticks without their domain, a cell whose header is absent) are described
explicitly in ``_OVERRIDES``, next to the code that writes them.

The schema checks shapes, types, tags, enums and id syntax. The Python readers check more:
ordering, uniqueness, value ranges, non-empty text and every cross-field rule. A line the schema
accepts may still be refused by a reader; a line a reader accepts always passes the schema.
"""

import dataclasses
import enum
import json
import sys
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from neptune.model.configuration import ConfigAlias, ValueDigest
from neptune.model.finding import IngestFinding
from neptune.model.ids import ConfigHash, ContentId, ExternalObjectRef, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import KIND_SINCE as _KIND_SINCE
from neptune.model.kinds import RECORD_KINDS as _KINDS
from neptune.model.knowledge import Known
from neptune.model.lists import LIST_STATES_SINCE, ListedMarker
from neptune.model.package import IngestReceipt, PackageManifest, ReceiptEnvelope
from neptune.model.provenance import (
    AdapterLocator,
    EvidenceRef,
    Provenance,
    RecordRange,
    RowCell,
    VideoFrame,
)
from neptune.model.record import OLDEST_READABLE_VERSION, SCHEMA_VERSION
from neptune.model.reference import FrameTransform
from neptune.model.scalars import NonFinite
from neptune.model.source import LocalPath, RawLocalPath
from neptune.model.time import Timestamp
from neptune.model.units import Unit

DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID: Final = f"urn:neptune:schema:canonical:{SCHEMA_VERSION}"

# Every record kind a package's tables hold, in the order of ADR 0017's families.
RECORD_KINDS: Final[tuple[type, ...]] = tuple(cls for cls, _ in _KINDS.values())
# The package's own documents (ADR 0022): one file each, with the records' envelope.
DOCUMENT_KINDS: Final[tuple[type, ...]] = (PackageManifest, IngestReceipt, ReceiptEnvelope)

_SHA256: Final = "sha256:[0-9a-f]{64}"
_STRING: Final[JsonObject] = {"type": "string"}
_INTEGER: Final[JsonObject] = {"type": "integer"}
_SCALAR: Final[JsonObject] = {"type": ["string", "number", "boolean"]}


def _obj(properties: Mapping[str, JsonValue], optional: tuple[str, ...] = ()) -> JsonObject:
    """A closed object: exactly ``properties``, all required but ``optional``."""
    return {
        "additionalProperties": False,
        "properties": dict(properties),
        "required": sorted(set(properties) - set(optional)),
        "type": "object",
    }


def _const(value: str) -> JsonObject:
    return {"const": value}


class _Builder:
    """Collects named definitions while walking types, so each is written once."""

    def __init__(self) -> None:
        self.defs: dict[str, JsonObject] = {}

    def ref(self, name: str, build: Callable[[], JsonObject]) -> JsonObject:
        if name not in self.defs:
            self.defs[name] = {}  # reserve the name first: definitions may be recursive
            self.defs[name] = build()
        return {"$ref": f"#/$defs/{name}"}

    # --- Types ---------------------------------------------------------------------------------

    def schema(self, tp: Any) -> JsonObject:
        """The schema of values of type ``tp`` as ``to_json`` writes them."""
        if tp in _NEWTYPES:
            name, pattern = _NEWTYPES[tp]
            return self.ref(name, lambda: {"pattern": f"^{pattern}$", "type": "string"})
        if isinstance(tp, type) and tp in _OVERRIDES:
            return self.ref(tp.__name__, lambda: _OVERRIDES[tp](self))
        if tp is str:
            return dict(_STRING)
        if tp is bool:
            return {"type": "boolean"}
        if tp is int:
            return dict(_INTEGER)
        if tp is float:
            return {"type": "number"}
        origin, args = typing.get_origin(tp), typing.get_args(tp)
        if origin is typing.Annotated:
            if any(isinstance(meta, ListedMarker) for meta in tp.__metadata__):
                return self._listed(tp.__origin__)
            return self.schema(tp.__origin__)
        if origin in (typing.Union, types.UnionType):
            return self._union(args)
        if origin is tuple:
            return self._tuple(args)
        if origin is not None and isinstance(origin, type) and issubclass(origin, Mapping):
            return {"type": "object"}  # a JSON object as the source or the config holds it
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            return self.ref(tp.__name__, lambda: {"enum": [member.value for member in tp]})
        if dataclasses.is_dataclass(tp) and isinstance(tp, type):
            return self.ref(tp.__name__, lambda: self._dataclass(tp))
        raise TypeError(f"no JSON Schema for {tp!r}")

    def _union(self, args: tuple[Any, ...]) -> JsonObject:
        known = [arg for arg in args if typing.get_origin(arg) is Known]
        if known:
            (value_type,) = typing.get_args(known[0])
            return self._knowledge(value_type)
        members = [self.schema(arg) for arg in args]
        return {"anyOf": members}

    def _tuple(self, args: tuple[Any, ...]) -> JsonObject:
        if len(args) != 2 or args[1] is not Ellipsis:
            raise TypeError(f"only homogeneous tuples are records' collections, got {args!r}")
        item = args[0]
        if typing.get_origin(item) is tuple and typing.get_args(item)[:1] == (str,):
            # Sorted (name, value) pairs are written as a JSON object: metadata, libraries.
            return {"additionalProperties": self.schema(typing.get_args(item)[1]), "type": "object"}
        return {"items": self.schema(item), "type": "array"}

    def _listed(self, knowledge: Any) -> JsonObject:
        """A ``Listed`` field (ADR 0061 §4, §5): the bare array, or a state other than
        ``known_absent`` and ``ambiguous``; a ``known`` object cites its own provenance."""
        (known,) = [arg for arg in typing.get_args(knowledge) if typing.get_origin(arg) is Known]
        (items,) = typing.get_args(known)
        name = "Listed_" + _name(typing.get_args(items)[0])

        def build() -> JsonObject:
            provenance = self.schema(Provenance)
            array = self._tuple(typing.get_args(items))
            return {
                "anyOf": [
                    array,  # Known, inheriting the record's provenance
                    _obj({"knowledge": _const("known"), "provenance": provenance, "value": array}),
                    _obj(
                        {
                            "knowledge": {"enum": ["unknown", "not_covered"]},
                            "provenance": provenance,
                        },
                        optional=("provenance",),
                    ),
                    _obj({"knowledge": _const("not_applicable")}),
                ]
            }

        return self.ref(name, build)

    def _knowledge(self, value_type: Any) -> JsonObject:
        """``Knowledge[T]`` (ADR 0011): six states, tagged by ``knowledge``."""
        value = self.schema(value_type)
        name = "Knowledge_" + _name(value_type)

        def build() -> JsonObject:
            provenance = self.schema(Provenance)
            candidate = _obj({"provenance": provenance, "value": value}, optional=("provenance",))
            return {
                "anyOf": [
                    _obj(
                        {"knowledge": _const("known"), "provenance": provenance, "value": value},
                        optional=("provenance",),
                    ),
                    _obj({"knowledge": _const("known_absent"), "provenance": provenance}),
                    _obj(
                        {
                            "knowledge": {"enum": ["unknown", "not_covered"]},
                            "provenance": provenance,
                        },
                        optional=("provenance",),
                    ),
                    _obj({"knowledge": _const("not_applicable")}),
                    _obj(
                        {
                            "candidates": {"items": candidate, "minItems": 2, "type": "array"},
                            "knowledge": _const("ambiguous"),
                        }
                    ),
                ]
            }

        return self.ref(name, build)

    def _dataclass(self, cls: type) -> JsonObject:
        hints = typing.get_type_hints(cls, localns=_JSON_NAMES, include_extras=True)
        # A document's ``version`` is written as its envelope's ``schema_version``, below.
        properties: dict[str, JsonValue] = {
            field.name: self.schema(hints[field.name])
            for field in dataclasses.fields(cls)
            if field.init
            and (cls, field.name) not in _FIELD_OVERRIDES
            and not (cls in DOCUMENT_KINDS and field.name == "version")
        }
        for (owner, field_name), build in _FIELD_OVERRIDES.items():
            if owner is cls:
                properties[field_name] = build(self)
        kind = getattr(cls, "kind", None)
        if isinstance(kind, str) and "kind" not in properties:
            properties["kind"] = _const(kind)
        # The envelope (ADR 0017 §2): a record at the version that added its kind, a package's
        # document at the package's version (ADR 0037 §1).
        if hasattr(cls, "family") and isinstance(kind, str):
            since = _KIND_SINCE[kind]
            if isinstance(getattr(cls, "schema_version", None), property):
                # Written at a later version when it uses a later shape (ADR 0061 §6).
                properties["schema_version"] = {"enum": sorted({since, LIST_STATES_SINCE})}
            else:
                properties["schema_version"] = _const_int(since)
        elif cls in DOCUMENT_KINDS:
            versions: JsonObject = {
                "maximum": SCHEMA_VERSION,
                "minimum": OLDEST_READABLE_VERSION,
                "type": "integer",
            }
            properties["schema_version"] = versions
        return _obj(properties)


def _const_int(value: int) -> JsonObject:
    return {"const": value}


def _name(tp: Any) -> str:
    """A readable, stable definition name for a type."""
    if tp in _NEWTYPES:
        return _NEWTYPES[tp][0]
    if isinstance(tp, type):
        return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(
            tp, tp.__name__
        )
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is tuple:
        return "list_of_" + _name(args[0])
    if origin in (typing.Union, types.UnionType):
        known = [arg for arg in args if typing.get_origin(arg) is Known]
        if known:  # Knowledge[T]
            return "Knowledge_" + _name(typing.get_args(known[0])[0])
        return "_or_".join(_name(arg) for arg in args)
    raise TypeError(f"no name for {tp!r}")


# ``JsonValue`` is a recursive alias written as a string; these names let it resolve.
_JSON_NAMES: Final[dict[str, Any]] = {
    "JsonValue": JsonValue,
    "Mapping": Mapping,
    "Sequence": Sequence,
}
_NEWTYPES: Final[Mapping[Any, tuple[str, str]]] = {
    RecordId: ("RecordId", f"rec:{_SHA256}"),
    ContentId: ("ContentId", _SHA256),
    ConfigHash: ("ConfigHash", _SHA256),
    ValueDigest: ("ValueDigest", _SHA256),
}


# --- Types whose JSON is not their fields ------------------------------------------------------


def _local_path(builder: _Builder) -> JsonObject:
    return _obj({"kind": _const("local"), "path": dict(_STRING)})


def _raw_local_path(builder: _Builder) -> JsonObject:
    return _obj(
        {"kind": _const("local_raw"), "path_hex": {"pattern": "^([0-9a-f]{2})+$", "type": "string"}}
    )


def _external_object(builder: _Builder) -> JsonObject:
    return _obj(
        {
            "connector_id": dict(_STRING),
            "kind": _const("external"),
            "object_id": dict(_STRING),
            "revision_token": dict(_STRING),
        }
    )


def _record_range(builder: _Builder) -> JsonObject:
    return _obj(
        {
            "channel": dict(_STRING),
            "domain_id": builder.schema(RecordId),
            "end": dict(_INTEGER),
            "kind": _const(RecordRange.kind),
            "start": dict(_INTEGER),
        }
    )


def _video_frame(builder: _Builder) -> JsonObject:
    return _obj(
        {
            "domain_id": builder.schema(RecordId),
            "index": dict(_INTEGER),
            "kind": _const(VideoFrame.kind),
            "pts": dict(_INTEGER),
            "track": dict(_INTEGER),
        }
    )


def _row_cell(builder: _Builder) -> JsonObject:
    # column_name is omitted when the table has no header (NO_HEADER).
    return _obj(
        {
            "column": dict(_INTEGER),
            "column_name": dict(_STRING),
            "kind": _const(RowCell.kind),
            "row": dict(_INTEGER),
        },
        optional=("column_name",),
    )


def _adapter_locator(builder: _Builder) -> JsonObject:
    return {
        "additionalProperties": dict(_SCALAR),
        "properties": {
            "kind": {"pattern": "^[a-z][a-z0-9_.\\-]*:[a-z][a-z0-9_]*$", "type": "string"}
        },
        "required": ["kind"],
        "type": "object",
    }


def _fraction(builder: _Builder) -> JsonObject:
    positive: JsonObject = {"minimum": 1, "type": "integer"}
    return _obj({"denominator": positive, "numerator": positive})


def _unit(builder: _Builder) -> JsonObject:
    return {"description": "a catalogued unit's canonical symbol (ADR 0013)", "type": "string"}


def _non_finite(builder: _Builder) -> JsonObject:
    return _obj({"non_finite": {"enum": [member.value for member in NonFinite]}})


def _config_alias(builder: _Builder) -> JsonObject:
    target: JsonObject = {"items": {"type": ["string", "integer"]}, "type": "array"}
    alias: JsonObject = {"anchor": dict(_STRING), "key": {"type": "boolean"}, "target": target}
    return _obj({**alias, "type": _const("alias")})


_OVERRIDES: Final[Mapping[type, Callable[[_Builder], JsonObject]]] = {
    LocalPath: _local_path,
    RawLocalPath: _raw_local_path,
    ExternalObjectRef: _external_object,
    RecordRange: _record_range,
    VideoFrame: _video_frame,
    RowCell: _row_cell,
    AdapterLocator: _adapter_locator,
    Fraction: _fraction,
    Unit: _unit,
    NonFinite: _non_finite,
    ConfigAlias: _config_alias,
}


def _finding_subject(builder: _Builder) -> JsonObject:
    evidence = _obj({"kind": _const("evidence"), "ref": builder.schema(EvidenceRef)})
    locations = [builder.schema(tp) for tp in (LocalPath, RawLocalPath, ExternalObjectRef)]
    return {"anyOf": [evidence, *locations]}


def _validity(builder: _Builder) -> JsonObject:
    return {
        "anyOf": [
            _obj({"kind": _const("static")}),
            _obj({"kind": _const("stamped"), "stamp": builder.schema(Timestamp)}),
        ]
    }


_FIELD_OVERRIDES: Final[Mapping[tuple[type, str], Callable[[_Builder], JsonObject]]] = {
    (IngestFinding, "subject"): _finding_subject,
    (FrameTransform, "validity"): _validity,
}


# --- The schema --------------------------------------------------------------------------------


def canonical_schema() -> JsonObject:
    """The whole contract: any one line of any record table, and each kind by name."""
    builder = _Builder()
    kinds = [builder.schema(kind) for kind in RECORD_KINDS]
    for document in DOCUMENT_KINDS:  # validated by name: #/$defs/IngestReceipt
        builder.schema(document)
    return {
        "$defs": dict(sorted(builder.defs.items())),
        "$id": SCHEMA_ID,
        "$schema": DIALECT,
        "anyOf": kinds,
        "description": (
            "One line of a Neptune ingest package's record tables (ADR 0002, ADR 0017). The "
            "package's manifest, receipt and receipt envelope are #/$defs/PackageManifest, "
            "#/$defs/IngestReceipt and #/$defs/ReceiptEnvelope (ADR 0022). Generated from "
            "neptune.model; the Python readers are stricter (ADR 0021)."
        ),
        "title": f"Neptune canonical record, schema version {SCHEMA_VERSION}",
    }


def render() -> str:
    """The committed file's exact text."""
    return json.dumps(canonical_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m neptune.model.schema <output path>\n")
        return 2
    Path(argv[0]).write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
