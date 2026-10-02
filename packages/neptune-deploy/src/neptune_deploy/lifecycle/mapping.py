"""Declared mapping files: which table columns become which lifecycle fields (ADR 0002 §3).

A mapping file is JSON::

    {
      "schema": "neptune-deploy.lifecycle-mapping/1",
      "id": "cmms.generic", "version": "1", "description": "...",
      "zone": "Europe/Berlin",                       # default civil zone for its time fields
      "rules": [{
        "id": "work_order", "kind": "maintenance_event",
        "requires": ["WO Number"],                   # columns a table must have for the rule
        "where": {"column": "WO Type", "in": ["PM", "CM"]},   # optional: rows it applies to
        "ignore": ["Technician"],                    # columns left unmapped on purpose
        "fields": {"identifiers": [{"column": "WO Number", "namespace": "cmms.work_order"}], ...}
      }]
    }

A column is a header cell's text for a headed table, or a JSON pointer for a table of JSON
objects; ``*`` in a pointer matches one segment and a trailing ``**`` any rest, in ``ignore``
only. Field specs follow the field's shape (``shapes``):

- text, number, unit: ``{"column"}``; id: ``{"column", "namespace"}``; version:
  ``{"column", "scheme"}`` with scheme ``declared``, ``firmware`` or ``build``; time:
  ``{"column", "format", "zone"}``, ``format`` a pattern or a list of them (``times``). Each may add
  ``"required": true``: a blank is then a finding as well as ``Unknown``.
- ids: a list of ``{"column", "namespace", "split"}``; statements: a list of
  ``{"column", "split"}``.
  ``split`` is a declared delimiter; each part is trimmed and cites its span in the cell.
- a part: an object of the part's fields; a list of parts: a list of such objects. A score is
  ``{"column"}``: its name is the column's own label.

Anything else is a ``MappingError``, raised before any record is read: a mapping file is the
operator's declaration, and a wrong one fails loudly.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeAlias

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.lifecycle.shapes import KINDS, Shape, fields_of, is_score
from neptune_deploy.lifecycle.times import check_format

MAPPING_SCHEMA: Final = "neptune-deploy.lifecycle-mapping/1"
VERSION_SCHEMES: Final = ("build", "declared", "firmware")
MAX_MAPPING_BYTES: Final = 1024 * 1024
_TOKEN: Final = re.compile(r"[a-z][a-z0-9_.\-]*")


class MappingError(ValueError):
    """A mapping file that cannot be applied, with where in it the problem is."""


@dataclass(frozen=True)
class Scalar:
    """One cell read into one field."""

    column: str
    required: bool = False
    namespace: str | None = None  # id
    formats: tuple[str, ...] = ()  # time
    zone: str | None = None  # time
    scheme: str | None = None  # version


@dataclass(frozen=True)
class ListCell:
    """One cell read into a list field, split on ``split`` if declared."""

    column: str
    namespace: str | None = None  # ids
    split: str | None = None


@dataclass(frozen=True)
class Part:
    """One part spelled out field by field (a score holds ``score`` instead)."""

    cls: type[Any]
    fields: Mapping[str, "Spec"]
    score: Scalar | None = None


Spec: TypeAlias = Scalar | tuple[ListCell, ...] | Part | tuple[Part, ...]


@dataclass(frozen=True)
class Where:
    column: str
    values: frozenset[str]


@dataclass(frozen=True)
class Rule:
    id: str
    kind: type[Any]
    requires: tuple[str, ...]
    where: Where | None
    ignore: tuple[str, ...]
    fields: Mapping[str, Spec]

    def columns(self) -> frozenset[str]:
        """Every column the rule reads, its ``requires`` and ``where`` included."""
        out = set(self.requires)
        if self.where is not None:
            out.add(self.where.column)
        for spec in self.fields.values():
            out.update(spec_columns(spec))
        return frozenset(out)


@dataclass(frozen=True)
class LifecycleMapping:
    """A mapping file, parsed and checked. ``document`` is its parsed JSON, ``sha256`` the content
    id of its bytes: both enter the transform's config (ADR 0002 §7)."""

    id: str
    version: str
    rules: tuple[Rule, ...]
    document: JsonObject
    sha256: ContentId


def spec_columns(spec: Spec) -> set[str]:
    if isinstance(spec, Scalar):
        return {spec.column}
    if isinstance(spec, Part):
        out = {spec.score.column} if spec.score else set()
        for inner in spec.fields.values():
            out |= spec_columns(inner)
        return out
    out = set()
    for item in spec:
        out |= spec_columns(item) if isinstance(item, Part) else {item.column}
    return out


# --- Reading -----------------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise MappingError(f"key {key!r} repeats")
        out[key] = value
    return out


def _reject_constant(token: str) -> None:
    raise MappingError(f"{token} is not JSON")


def parse_mapping(data: bytes, name: str = "<mapping>") -> LifecycleMapping:
    """Parse and check a mapping file's bytes."""
    if len(data) > MAX_MAPPING_BYTES:
        raise MappingError(f"{name}: larger than {MAX_MAPPING_BYTES} bytes")
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MappingError(f"{name}: not a JSON document: {exc}") from exc
    except MappingError as exc:
        raise MappingError(f"{name}: {exc}") from exc
    try:
        canonical_json.dumps(document)
    except ValueError as exc:  # floats out of range, nulls
        raise MappingError(f"{name}: {exc}") from exc
    try:
        return _mapping(document, content_id(data))
    except MappingError as exc:
        raise MappingError(f"{name}: {exc}") from exc


def load_mapping(path: Path) -> LifecycleMapping:
    """Read a mapping file from disk (a preset or the operator's own)."""
    with path.open("rb") as stream:
        data = stream.read(MAX_MAPPING_BYTES + 1)
    return parse_mapping(data, str(path))


def _object(value: Any, where: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MappingError(f"{where}: expected an object")
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing or extra:
        raise MappingError(f"{where}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise MappingError(f"{where}: expected non-empty text")
    return value


def _token(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise MappingError(f"{where}: expected a token matching {_TOKEN.pattern}")
    return value


def _texts(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MappingError(f"{where}: expected a list")
    items = tuple(_text(item, f"{where}[{i}]") for i, item in enumerate(value))
    if len(set(items)) != len(items):
        raise MappingError(f"{where}: repeats an entry")
    return items


def _mapping(document: Any, sha256: ContentId) -> LifecycleMapping:
    obj = _object(
        document, "mapping", {"schema", "id", "version", "rules"}, {"description", "zone"}
    )
    if obj["schema"] != MAPPING_SCHEMA:
        raise MappingError(f"schema must be {MAPPING_SCHEMA!r}, got {obj['schema']!r}")
    zone = _text(obj["zone"], "zone") if "zone" in obj else None
    if "description" in obj:
        _text(obj["description"], "description")
    rules_json = obj["rules"]
    if not isinstance(rules_json, list) or not rules_json:
        raise MappingError("rules: expected a non-empty list")
    rules = tuple(_rule(rule, f"rules[{i}]", zone) for i, rule in enumerate(rules_json))
    ids = [rule.id for rule in rules]
    if len(set(ids)) != len(ids):
        raise MappingError(f"rule ids repeat: {ids}")
    return LifecycleMapping(
        id=_token(obj["id"], "id"),
        version=_text(obj["version"], "version"),
        rules=rules,
        document=document,
        sha256=sha256,
    )


def _rule(value: Any, where: str, zone: str | None) -> Rule:
    obj = _object(value, where, {"id", "kind", "requires", "fields"}, {"where", "ignore"})
    kind = obj["kind"]
    if kind not in KINDS:
        raise MappingError(f"{where}.kind: {kind!r} is not a lifecycle kind: {sorted(KINDS)}")
    requires = _texts(obj["requires"], f"{where}.requires")
    if not requires:
        raise MappingError(f"{where}.requires: name at least one column")
    selector = None
    if "where" in obj:
        w = _object(obj["where"], f"{where}.where", {"column", "in"}, set())
        values = _texts(w["in"], f"{where}.where.in")
        if not values:
            raise MappingError(f"{where}.where.in: name at least one value")
        selector = Where(_text(w["column"], f"{where}.where.column"), frozenset(values))
    fields = _fields(KINDS[kind], obj["fields"], f"{where}.fields", zone)
    _check_clocks(fields, where)
    return Rule(
        id=_token(obj["id"], f"{where}.id"),
        kind=KINDS[kind],
        requires=requires,
        where=selector,
        ignore=_texts(obj.get("ignore", []), f"{where}.ignore"),
        fields=fields,
    )


def _fields(cls: type[Any], value: Any, where: str, zone: str | None) -> dict[str, Spec]:
    shapes = {shape.name: shape for shape in fields_of(cls)}
    if not isinstance(value, dict):
        raise MappingError(f"{where}: expected an object of fields")
    unknown = sorted(value.keys() - shapes.keys())
    if unknown:
        raise MappingError(f"{where}: {cls.__name__} has no fields {unknown}: {sorted(shapes)}")
    out: dict[str, Spec] = {}
    for name in sorted(value):
        shape, spec, at = shapes[name], value[name], f"{where}.{name}"
        match shape.shape:
            case Shape.IDS | Shape.STATEMENTS:
                if not isinstance(spec, list):
                    raise MappingError(f"{at}: expected a list of cells")
                ids = shape.shape is Shape.IDS
                out[name] = tuple(_list_cell(s, f"{at}[{i}]", ids) for i, s in enumerate(spec))
            case Shape.PART:
                assert shape.part is not None
                out[name] = _part(shape.part, spec, at, zone)
            case Shape.ITEMS:
                assert shape.part is not None
                if not isinstance(spec, list):
                    raise MappingError(f"{at}: expected a list of parts")
                items = tuple(_part(shape.part, s, f"{at}[{i}]", zone) for i, s in enumerate(spec))
                labels = [item.score.column for item in items if item.score is not None]
                if len(set(labels)) != len(labels):
                    raise MappingError(f"{at}: a score's name is its column, so columns are unique")
                out[name] = items
            case Shape.LABEL:
                raise MappingError(f"{at}: a label is the column's own name, not mapped")
            case _:
                out[name] = _scalar(shape.shape, spec, at, zone)
    return out


def _times(spec: Spec) -> list[Scalar]:
    if isinstance(spec, Scalar):
        return [spec] if spec.formats else []
    if isinstance(spec, Part):
        return [t for inner in spec.fields.values() for t in _times(inner)]
    return [t for item in spec if isinstance(item, Part) for t in _times(item)]


def _check_clocks(fields: Mapping[str, Spec], where: str) -> None:
    """A column read as a time twice in one rule is read the same way, so one cell is one clock."""
    seen: dict[str, tuple[tuple[str, ...], str | None]] = {}
    for spec in fields.values():
        for time in _times(spec):
            reading = (time.formats, time.zone)
            if seen.setdefault(time.column, reading) != reading:
                raise MappingError(f"{where}: column {time.column!r} is read as two clocks")


def _list_cell(value: Any, where: str, ids: bool) -> ListCell:
    obj = _object(value, where, {"column", "namespace"} if ids else {"column"}, {"split"})
    split = _text(obj["split"], f"{where}.split") if "split" in obj else None
    if split is not None and not split.strip():
        raise MappingError(f"{where}.split: a delimiter is not only whitespace")
    namespace = _token(obj["namespace"], f"{where}.namespace") if ids else None
    return ListCell(_text(obj["column"], f"{where}.column"), namespace, split)


def _part(cls: type[Any], value: Any, where: str, zone: str | None) -> Part:
    if is_score(cls):
        return Part(cls, {}, _scalar(Shape.TEXT, value, where, zone))
    return Part(cls, _fields(cls, value, where, zone))


def _scalar(shape: Shape, value: Any, where: str, zone: str | None) -> Scalar:
    extra = {
        Shape.ID: {"namespace"},
        Shape.TIME: {"format"},
        Shape.VERSION: {"scheme"},
    }.get(shape, set())
    optional = {"required", "zone"} if shape is Shape.TIME else {"required"}
    obj = _object(value, where, {"column"} | extra, optional)
    required = obj.get("required", False)
    if not isinstance(required, bool):
        raise MappingError(f"{where}.required: expected true or false")
    scalar = Scalar(column=_text(obj["column"], f"{where}.column"), required=required)
    if shape is Shape.ID:
        return Scalar(scalar.column, required, namespace=_token(obj["namespace"], where))
    if shape is Shape.VERSION:
        if obj["scheme"] not in VERSION_SCHEMES:
            raise MappingError(f"{where}.scheme: one of {VERSION_SCHEMES}")
        return Scalar(scalar.column, required, scheme=obj["scheme"])
    if shape is Shape.TIME:
        formats = obj["format"] if isinstance(obj["format"], list) else [obj["format"]]
        checked = _texts(formats, f"{where}.format")
        for pattern in checked:
            try:
                check_format(pattern)
            except ValueError as exc:
                raise MappingError(f"{where}.format: {exc}") from exc
        declared = _text(obj["zone"], f"{where}.zone") if "zone" in obj else zone
        if declared is None:
            raise MappingError(f"{where}: a time needs its civil zone declared (or 'unstated')")
        return Scalar(scalar.column, required, formats=checked, zone=declared)
    return scalar


def config_of(mapping: LifecycleMapping, base_package: ContentId) -> JsonObject:
    """The transform config of one mapping applied to one package (ADR 0002 §7)."""
    document: JsonValue = mapping.document
    return {"base_package": base_package, "mapping": document, "mapping_sha256": mapping.sha256}


def match_pattern(pattern: str, column: str) -> bool:
    """``ignore`` patterns: exact, or JSON pointers with ``*`` (one segment) and a trailing
    ``**`` (any rest)."""
    if pattern == column:
        return True
    if not pattern.startswith("/") or not column.startswith("/"):
        return False
    want, have = pattern.split("/")[1:], column.split("/")[1:]
    for i, segment in enumerate(want):
        if segment == "**" and i == len(want) - 1:
            return len(have) >= i
        if i >= len(have) or (segment != "*" and segment != have[i]):
            return False
    return len(want) == len(have)
