"""JSON form, strict decoding and JSON Schema for the catalog API records, from their type hints.

One walker over the hints of ``neptune_ledger.api.types`` keeps the three in step, so a field
cannot be added to a record without appearing in its schema. The rules (ADR 0004 §3):

- every key of a record is required, except ``X | None`` fields, which are omitted when ``None``;
  there is no ``null`` (canonical JSON, root ADR 0002);
- unknown keys are errors; ``bool`` is not an ``int``; ``Annotated`` constraints are enforced
  when encoding and when decoding;
- ``Knowledge[T]`` uses the compiler's JSON shape and carries no provenance, so ``KnownAbsent``
  (which requires provenance) never appears;
- a union of records is decoded by trying each member; exactly one must accept the object.
  Records with a ``tag`` class variable write it under ``tag_field``.
"""

import dataclasses
import re
import types
import typing
from collections.abc import Callable, Mapping
from functools import cache
from typing import (
    Annotated,
    Any,
    Final,
    Literal,
    TypeGuard,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from neptune.identity import canonical_json
from neptune.model import knowledge as knowledge_mod
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Inherited,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune_ledger.api import types as api_types
from neptune_ledger.api.types import API_MAJOR, CATALOG_API_VERSION, Constraint, StatedProvenance

T = TypeVar("T")
SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
_KNOWLEDGE_STATES: Final = ("known", "unknown", "not_covered", "not_applicable", "ambiguous")


class CodecError(ValueError):
    """A value or document does not fit the catalog API's types."""


# --- Hint inspection ---------------------------------------------------------------------------


def _split(hint: Any) -> tuple[Any, tuple[Constraint, ...]]:
    """``Annotated[X, c1, c2]`` -> ``(X, (c1, c2))``; nested ``Annotated`` flatten."""
    constraints: list[Constraint] = []
    while get_origin(hint) is Annotated:
        base, *extra = get_args(hint)
        constraints = [e for e in extra if isinstance(e, Constraint)] + constraints
        hint = base
    return hint, tuple(constraints)


def _is_record(hint: Any) -> TypeGuard[type[Any]]:
    """A record type: a dataclass class (not an instance)."""
    return isinstance(hint, type) and dataclasses.is_dataclass(hint)


def _is_union(hint: Any) -> bool:
    return get_origin(hint) in (Union, types.UnionType)


def _knowledge_inner(hint: Any) -> Any | None:
    """``T`` if ``hint`` is ``Knowledge[T]`` (a union holding ``Known[T]``), else ``None``."""
    if not _is_union(hint):
        return None
    for arg in get_args(hint):
        if get_origin(arg) is Known:
            return get_args(arg)[0]
    return None


def _optional_inner(hint: Any) -> Any | None:
    """``X`` if ``hint`` is ``X | None``, else ``None``."""
    if not _is_union(hint):
        return None
    args = get_args(hint)
    if type(None) not in args:
        return None
    rest = tuple(a for a in args if a is not type(None))
    return rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007 - built from a runtime tuple


def _is_json_object(hint: Any) -> bool:
    return get_origin(hint) in (Mapping, dict)


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls, include_extras=True)


def _fields(cls: type) -> list[tuple[str, Any]]:
    hints = _hints(cls)
    return [(f.name, hints[f.name]) for f in dataclasses.fields(cls)]


def _tag(cls: type) -> tuple[str, str] | None:
    tag = getattr(cls, "tag", None)
    field = getattr(cls, "tag_field", None)
    return (field, tag) if isinstance(tag, str) and isinstance(field, str) else None


# --- Constraints -------------------------------------------------------------------------------


def _check(value: Any, constraints: tuple[Constraint, ...], where: str) -> None:
    for c in constraints:
        # fullmatch + ASCII: Python's ``$`` also matches before a final newline and ``\d`` takes
        # any Unicode digit; JSON Schema (ECMA-262) does neither. Every pattern is anchored.
        if c.pattern is not None and not re.fullmatch(c.pattern, value, re.ASCII):
            raise CodecError(f"{where}: {value!r} does not match {c.pattern}")
        if c.enum is not None and value not in c.enum:
            raise CodecError(f"{where}: {value!r} is not one of the allowed values")
        if c.minimum is not None and value < c.minimum:
            raise CodecError(f"{where}: {value!r} is below {c.minimum}")
        if c.maximum is not None and value > c.maximum:
            raise CodecError(f"{where}: {value!r} is above {c.maximum}")
        if c.min_length is not None and len(value) < c.min_length:
            raise CodecError(f"{where}: shorter than {c.min_length}")
        if c.min_items is not None and len(value) < c.min_items:
            raise CodecError(f"{where}: fewer than {c.min_items} items")
        if c.unique_items and len({canonical_json.dumps(_plain(v)) for v in value}) != len(value):
            raise CodecError(f"{where}: items must be unique")
        for key in c.required:
            if not isinstance(value.get(key), str) or not value[key]:
                raise CodecError(f"{where}: needs a non-empty string {key!r}")


def _plain(value: Any) -> JsonValue:
    """A JSON value for uniqueness checks: records encode, everything else is already JSON."""
    return to_json(value) if dataclasses.is_dataclass(value) else value


# --- Encoding ----------------------------------------------------------------------------------


def to_json(value: object) -> JsonValue:
    """The JSON form of a catalog API record (a frozen dataclass of ``api.types``)."""
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        raise CodecError(f"not a catalog API record: {type(value).__name__}")
    return _encode(type(value), value, type(value).__name__)


def dumps(value: object) -> bytes:
    """Canonical JSON bytes of a record: the same record always gives the same bytes."""
    return canonical_json.dumps(to_json(value))


def _stated(constraints: tuple[Constraint, ...]) -> bool:
    return any(c.as_stated for c in constraints)


def _check_provenance(provenance: object, stated: bool, where: str) -> None:
    """The Ledger's own states carry none; a restated package field keeps the package's."""
    if isinstance(provenance, Inherited):
        return
    if not stated:
        raise CodecError(f"{where}: catalog API knowledge carries no provenance")
    if not isinstance(provenance, StatedProvenance):
        raise CodecError(f"{where}: restated provenance must be StatedProvenance")


def _encode(hint: Any, value: Any, where: str) -> JsonValue:
    hint, constraints = _split(hint)
    inner = _knowledge_inner(hint)
    if inner is not None:
        return _encode_knowledge(inner, value, _stated(constraints), where)
    optional = _optional_inner(hint)
    if optional is not None:
        if value is None:
            raise CodecError(f"{where}: None is written by omitting the key")
        return _encode(optional, value, where)
    if _is_union(hint):
        for member in get_args(hint):
            if isinstance(value, member):
                return _encode(member, value, where)
        raise CodecError(f"{where}: {type(value).__name__} is not one of {hint}")
    out: JsonValue
    if _is_record(hint):
        if type(value) is not hint:
            raise CodecError(f"{where}: expected {hint.__name__}, got {type(value).__name__}")
        obj: dict[str, JsonValue] = {}
        tag = _tag(hint)
        if tag is not None:
            obj[tag[0]] = tag[1]
        for name, field_hint in _fields(hint):
            item = getattr(value, name)
            if item is None and _optional_inner(_split(field_hint)[0]) is not None:
                continue
            obj[name] = _encode(field_hint, item, f"{where}.{name}")
        out = obj
    elif get_origin(hint) is Literal:
        if value not in get_args(hint) or not isinstance(value, str):
            raise CodecError(f"{where}: {value!r} is not one of {get_args(hint)}")
        out = value
    elif get_origin(hint) is tuple:
        if not isinstance(value, tuple):
            raise CodecError(f"{where}: expected a tuple, got {type(value).__name__}")
        item_hint = get_args(hint)[0]
        out = [_encode(item_hint, v, f"{where}[{i}]") for i, v in enumerate(value)]
    elif _is_json_object(hint):
        if not isinstance(value, Mapping):
            raise CodecError(f"{where}: expected a JSON object")
        try:
            out = canonical_json.loads(canonical_json.dumps(value))
        except canonical_json.CanonicalJsonError as exc:
            raise CodecError(f"{where}: {exc}") from exc
    elif hint is bool:
        if not isinstance(value, bool):
            raise CodecError(f"{where}: expected a bool")
        out = value
    elif hint is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise CodecError(f"{where}: expected an int, got {type(value).__name__}")
        out = value
    elif hint is str:
        if not isinstance(value, str):
            raise CodecError(f"{where}: expected a str, got {type(value).__name__}")
        out = value
    else:
        raise CodecError(f"{where}: unsupported type {hint!r}")
    _check(value, constraints, where)
    return out


def _encode_knowledge(inner: Any, value: Any, stated: bool, where: str) -> JsonValue:
    match value:
        case Known(provenance=p) | Unknown(provenance=p) | NotCovered(provenance=p):
            _check_provenance(p, stated, where)
        case Ambiguous(candidates=candidates):
            for candidate in candidates:
                _check_provenance(candidate.provenance, stated, where)
        case NotApplicable():
            pass
        case KnownAbsent(provenance=p):
            if not stated:
                raise CodecError(f"{where}: the Ledger never determines KnownAbsent itself")
            _check_provenance(p, stated, where)
        case _:
            raise CodecError(f"{where}: expected a Knowledge state, got {type(value).__name__}")
    return knowledge_mod.to_json(value, lambda v: _encode(inner, v, f"{where}.value"))


# --- Decoding ----------------------------------------------------------------------------------


def from_json(cls: type[T], data: JsonValue) -> T:
    """Decode a record strictly; raises ``CodecError`` naming the first offending path."""
    if not dataclasses.is_dataclass(cls):
        raise CodecError(f"not a catalog API record type: {cls!r}")
    return _decode(cls, data, cls.__name__)  # type: ignore[no-any-return]


def loads(cls: type[T], data: bytes) -> T:
    """Decode canonical JSON bytes into a record."""
    try:
        value = canonical_json.loads(data)
    except canonical_json.CanonicalJsonError as exc:
        raise CodecError(str(exc)) from exc
    return from_json(cls, value)


def _reject_provenance(_: JsonObject) -> Any:
    raise CodecError("catalog API knowledge carries no provenance")


def _keep_provenance(document: JsonObject) -> Any:
    try:
        return StatedProvenance(canonical_json.loads(canonical_json.dumps(document)))  # type: ignore[arg-type]
    except (ValueError, TypeError, AttributeError) as exc:
        raise CodecError(f"provenance: {exc}") from exc


def _decode(hint: Any, data: Any, where: str) -> Any:
    hint, constraints = _split(hint)
    inner = _knowledge_inner(hint)
    if inner is not None:
        try:
            return knowledge_mod.from_json(
                data,
                lambda d: _decode(inner, d, f"{where}.value"),
                _keep_provenance if _stated(constraints) else _reject_provenance,
            )
        except CodecError:
            raise
        except (ValueError, TypeError) as exc:
            raise CodecError(f"{where}: {exc}") from exc
    optional = _optional_inner(hint)
    if optional is not None:
        return _decode(optional, data, where)
    if _is_union(hint):
        found: list[Any] = []
        for member in get_args(hint):
            try:
                found.append(_decode(member, data, where))
            except CodecError:
                continue
        if len(found) != 1:
            raise CodecError(f"{where}: matches {len(found)} of {hint}, needs exactly one")
        return found[0]
    value: Any
    if _is_record(hint):
        if not isinstance(data, Mapping):
            raise CodecError(f"{where}: expected an object")
        fields = _fields(hint)
        allowed = {name for name, _ in fields}
        tag = _tag(hint)
        if tag is not None:
            if data.get(tag[0]) != tag[1]:
                raise CodecError(f"{where}: {tag[0]} must be {tag[1]!r}")
            allowed.add(tag[0])
        extra = sorted(set(data) - allowed)
        if extra:
            raise CodecError(f"{where}: unexpected keys {extra}")
        kwargs: dict[str, Any] = {}
        for name, field_hint in fields:
            if name in data:
                kwargs[name] = _decode(field_hint, data[name], f"{where}.{name}")
            elif _optional_inner(_split(field_hint)[0]) is not None:
                kwargs[name] = None
            elif name == "api_version":
                raise CodecError(f"{where}: missing api_version")
            else:
                raise CodecError(f"{where}: missing {name!r}")
        value = hint(**kwargs)
    elif get_origin(hint) is Literal:
        if not isinstance(data, str) or data not in get_args(hint):
            raise CodecError(f"{where}: {data!r} is not one of {get_args(hint)}")
        value = data
    elif get_origin(hint) is tuple:
        if isinstance(data, str | bytes) or not isinstance(data, list | tuple):
            raise CodecError(f"{where}: expected an array")
        item_hint = get_args(hint)[0]
        value = tuple(_decode(item_hint, v, f"{where}[{i}]") for i, v in enumerate(data))
    elif _is_json_object(hint):
        if not isinstance(data, Mapping):
            raise CodecError(f"{where}: expected an object")
        try:
            value = canonical_json.loads(canonical_json.dumps(data))
        except canonical_json.CanonicalJsonError as exc:
            raise CodecError(f"{where}: {exc}") from exc
    elif hint is bool:
        if not isinstance(data, bool):
            raise CodecError(f"{where}: expected a boolean")
        value = data
    elif hint is int:
        if not isinstance(data, int) or isinstance(data, bool):
            raise CodecError(f"{where}: expected an integer")
        value = data
    elif hint is str:
        if not isinstance(data, str):
            raise CodecError(f"{where}: expected a string")
        value = data
    else:
        raise CodecError(f"{where}: unsupported type {hint!r}")
    _check(value, constraints, where)
    return value


# --- JSON Schema -------------------------------------------------------------------------------


def _constraint_keywords(constraints: tuple[Constraint, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in constraints:
        if c.description is not None:
            out["description"] = c.description
        if c.pattern is not None:
            out["pattern"] = c.pattern
        if c.enum is not None:
            out["enum"] = sorted(c.enum)
        if c.minimum is not None:
            out["minimum"] = c.minimum
        if c.maximum is not None:
            out["maximum"] = c.maximum
        if c.min_length is not None:
            out["minLength"] = c.min_length
        if c.min_items is not None:
            out["minItems"] = c.min_items
        if c.unique_items:
            out["uniqueItems"] = True
        if c.required:
            out["required"] = sorted(c.required)
            out["properties"] = {k: {"minLength": 1, "type": "string"} for k in c.required}
    return out


def _doc(cls: type) -> str:
    text = (cls.__doc__ or "").strip()
    return " ".join(text.split("\n\n", 1)[0].split())


def _type_name(hint: Any) -> str:
    base, constraints = _split(hint)
    named = [c.name for c in constraints if c.name is not None]
    if named:
        return named[-1]
    if _is_record(base):
        return base.__name__
    return {str: "String", int: "Integer", bool: "Boolean"}.get(base, "Value")


class _SchemaBuilder:
    def __init__(self) -> None:
        self.defs: dict[str, Any] = {}

    def ref(self, name: str, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if name not in self.defs:
            self.defs[name] = {}  # reserve first: records may refer to themselves
            self.defs[name] = build()
        return {"$ref": f"#/$defs/{name}"}

    def schema(self, hint: Any) -> dict[str, Any]:
        base, constraints = _split(hint)
        named = [c for c in constraints if c.name is not None]
        if named and not _is_record(base):
            name = named[-1].name
            assert name is not None
            return self.ref(name, lambda: self._schema(base, constraints))
        return self._schema(base, constraints)

    def _schema(self, hint: Any, constraints: tuple[Constraint, ...]) -> dict[str, Any]:
        keywords = _constraint_keywords(constraints)
        inner = _knowledge_inner(hint)
        if inner is not None:
            if _stated(constraints):
                name = f"StatedKnowledgeOf{_type_name(inner)}"
                return self.ref(name, lambda: self._knowledge(inner, stated=True))
            return self.ref(f"KnowledgeOf{_type_name(inner)}", lambda: self._knowledge(inner))
        optional = _optional_inner(hint)
        if optional is not None:
            return self.schema(optional)
        if _is_union(hint):
            return {"oneOf": [self.schema(member) for member in get_args(hint)]}
        if _is_record(hint):
            cls = hint
            return self.ref(cls.__name__, lambda: self._record(cls))
        if get_origin(hint) is Literal:
            return {"enum": sorted(get_args(hint)), "type": "string", **keywords}
        if get_origin(hint) is tuple:
            return {"items": self.schema(get_args(hint)[0]), "type": "array", **keywords}
        if _is_json_object(hint):
            return {"type": "object", **keywords}
        primitive = {bool: "boolean", int: "integer", str: "string"}.get(hint)
        if primitive is None:
            raise CodecError(f"no JSON Schema for {hint!r}")
        return {"type": primitive, **keywords}

    def _record(self, cls: type) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        tag = _tag(cls)
        if tag is not None:
            properties[tag[0]] = {"const": tag[1]}
            required.append(tag[0])
        for name, field_hint in _fields(cls):
            properties[name] = self.schema(field_hint)
            if _optional_inner(_split(field_hint)[0]) is None:
                required.append(name)
        return {
            "additionalProperties": False,
            "description": _doc(cls),
            "properties": properties,
            "required": sorted(required),
            "type": "object",
        }

    def _knowledge(self, inner: Any, *, stated: bool = False) -> dict[str, Any]:
        value = self.schema(inner)
        provenance = {
            "description": "The package's provenance object, verbatim (package-schema).",
            "properties": {"assertion_kind": {"enum": ["observed", "stated"]}},
            "required": ["assertion_kind"],
            "type": "object",
        }

        def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
            if stated and "provenance" not in properties:
                properties = {**properties, "provenance": provenance}
            return {
                "additionalProperties": False,
                "properties": properties,
                "required": sorted(required),
                "type": "object",
            }

        def state(tag: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
            properties = {"knowledge": {"const": tag}, **(extra or {})}
            return obj(properties, list(properties))

        candidate = obj({"value": value}, ["value"])
        states = [
            state("known", {"value": value}),
            state("unknown"),
            state("not_covered"),
            {
                "additionalProperties": False,
                "properties": {"knowledge": {"const": "not_applicable"}},
                "required": ["knowledge"],
                "type": "object",
            },
            state(
                "ambiguous", {"candidates": {"items": candidate, "minItems": 2, "type": "array"}}
            ),
        ]
        if stated:
            states.append(state("known_absent", {"provenance": provenance}))
            description = (
                "A package field's Knowledge state restated verbatim, provenance included."
            )
        else:
            description = (
                "A Knowledge state of the Ledger's own (no provenance; never known_absent)."
            )
        return {"description": description, "oneOf": states}


def _json(value: Any) -> JsonValue:
    """``value`` typed as JSON: the builder only produces JSON-shaped dicts, lists and scalars."""
    return value  # type: ignore[no-any-return]


@cache
def _catalog_schema_text() -> bytes:
    builder = _SchemaBuilder()
    for cls in (*api_types.REQUEST_TYPES, *api_types.RESPONSE_TYPES):
        builder.schema(cls)
    schema: dict[str, Any] = {
        "$defs": builder.defs,
        "$id": f"urn:neptune:catalog-api:{API_MAJOR}",
        "$schema": SCHEMA_DIALECT,
        "description": (
            "Requests and responses of the Neptune Ledger catalog API "
            "(packages/neptune-ledger/docs/catalog-api.md). Validate a document against "
            "#/$defs/<RecordName>."
        ),
        "title": f"Neptune catalog API {API_MAJOR}.x",
    }
    return canonical_json.dumps(_json(schema))


def catalog_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of every request and response record.

    The registry's ``schema_export`` (contracts/catalog-api/contract.toml). A fresh copy per call.
    """
    schema = canonical_json.loads(_catalog_schema_text())
    assert isinstance(schema, dict)
    return schema


def decode_as(hint: Any, data: JsonValue) -> Any:
    """Decode ``data`` as any API type hint (``Knowledge[WorldTime]``, ``tuple[X, ...]``)."""
    return _decode(hint, data, "value")


# Public names for the Arrow module, which maps QueryRow's hints to columns.
split_annotated = _split
optional_inner = _optional_inner

__all__ = [
    "CATALOG_API_VERSION",
    "CodecError",
    "catalog_schema",
    "dumps",
    "from_json",
    "loads",
    "to_json",
]
