"""The catalog rows one verified package produces (Ledger ADR 0002 §5, ADR 0005 §2, §3, ADR 0009).

A pure function of the package's bytes and this Ledger version: every value comes from the record
lines that registration hashed, read once, and the kind-specific projections come from the spec
this Ledger ships (``projection.shipped_spec``), never from the compiler's live schema. So the same
package gives the same rows in any catalog, whatever was registered before it. It indexes the
package's own tables, exactly the kinds of its schema version (Ledger ADR 0008 §2), never the
compiler's whole list. Records are ordered by kind, then record id (ADR 0009 §4).
"""

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

from neptune.identity import canonical_json
from neptune_ledger.catalog.projection import Spec, shipped_spec

# The fields that state an entry's world time (ADR 0003 §3): start, end, and the fallback that
# stands in for both when neither is Known.
WORLD_TIME: Final[dict[str, tuple[str, str, str | None]]] = {
    "run": ("first", "last", None),
    "stream": ("first", "last", None),
    "calibration": ("valid_from", "valid_until", "performed"),
}


@dataclass(frozen=True)
class RecordRow:
    """One ``record`` row without its tenant and registration key, plus its logical ids."""

    kind: str
    record_id: str
    line: int
    schema_version: int
    source_content_id: str | None
    source_locator: str | None
    transform_id: str | None
    assertion_kind: str | None
    world_clock: str | None
    world_first: int | None
    world_last: int | None
    ambiguous_pointers: tuple[str, ...]
    body_digest: str
    logical_ids: tuple[tuple[str, str, str], ...]  # (pointer, namespace, value)
    # ADR 0009: the body as canonical JSON text for the jsonb column (None when it holds U+0000),
    # the Unknown pointers, and one value per ``projection_columns()`` column, in that order.
    body: str | None = None
    unknown_pointers: tuple[str, ...] = ()
    projected: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TransformRow:
    transform_id: str
    adapter_id: str
    adapter_version: str
    config_hash: str
    libraries: str
    upstream: tuple[str, ...]


@dataclass(frozen=True)
class PackageRows:
    """Everything registration writes for one package, in a deterministic order."""

    package_id: str
    schema_version: int
    receipt_id: str
    sources: tuple[tuple[str, int, str], ...]  # (content id, size, storage)
    locations: tuple[tuple[str, str, str, tuple[str, ...]], ...]  # (revision, content, loc, sup)
    absences: tuple[tuple[str, str, tuple[str, ...]], ...]  # (absence id, location, supersedes)
    transforms: tuple[TransformRow, ...]
    clocks: tuple[tuple[str, str, tuple[str, ...]], ...]  # (clock id, field, scope)
    records: tuple[RecordRow, ...]


def canonical(value: Any) -> str:
    """Canonical JSON text (root ADR 0002), as stored in the catalog's text columns."""
    return canonical_json.dumps(value).decode("utf-8")


def package_rows(
    package_id: str, manifest: Mapping[str, Any], lines: Mapping[str, tuple[bytes, ...]]
) -> PackageRows:
    """The rows of one package whose manifest and record lines were verified."""
    spec = shipped_spec()
    # Every table the package holds: the compiler's kind list is not closed (ADR 0009 §3). A kind
    # the spec does not know is indexed with its common columns and no projections.
    tables: dict[str, list[Any]] = {
        kind: [canonical_json.loads(line) for line in lines[kind]] for kind in lines
    }
    records = tuple(
        sorted(
            (
                _record_row(spec, kind, number, line, body)
                for kind in tables
                for number, (line, body) in enumerate(
                    zip(lines[kind], tables[kind], strict=True), 1
                )
            ),
            key=lambda row: (row.kind, row.record_id, row.line),
        )
    )
    return PackageRows(
        package_id=package_id,
        schema_version=manifest["schema_version"],
        receipt_id=manifest["receipt"],
        sources=tuple((s["content_id"], s["size"], s["storage"]) for s in manifest["sources"]),
        locations=tuple(
            (r["id"], r["content_id"], canonical(r["location"]), tuple(r["supersedes"]))
            for r in tables.get("source_revision", [])
        ),
        absences=tuple(
            (a["id"], canonical(a["location"]), tuple(a["supersedes"]))
            for a in tables.get("source_absence", [])
        ),
        transforms=tuple(
            TransformRow(
                t["id"],
                t["adapter_id"],
                t["adapter_version"],
                t["config_hash"],
                canonical(t["libraries"]),
                tuple(t["upstream"]),
            )
            for t in tables.get("transform_record", [])
        ),
        clocks=tuple(
            (d["id"], d["field"], tuple(d["scope"])) for d in tables.get("timestamp_domain", [])
        ),
        records=records,
    )


def projection_columns(spec: Spec | None = None) -> tuple[str, ...]:
    """The kind-specific projection columns of ``record``, in the order ``projected`` holds."""
    return _columns(spec or shipped_spec())


@cache
def _columns(spec: Spec) -> tuple[str, ...]:
    return tuple(name for name, _ in spec.columns())


def _record_row(spec: Spec, kind: str, number: int, line: bytes, record: Any) -> RecordRow:
    source, locator, transform, assertion = provenance_summary(kind, record)
    clock, first, last = world_time(kind, record)
    ambiguous, unknown, logical = fields(record, spec.opaque_fields(kind))
    return RecordRow(
        kind=kind,
        record_id=record["content_id"] if kind == "source_artifact" else record["id"],
        line=number,
        schema_version=record["schema_version"],
        source_content_id=source,
        source_locator=locator,
        transform_id=transform,
        assertion_kind=assertion,
        world_clock=clock,
        world_first=first,
        world_last=last,
        ambiguous_pointers=tuple(ambiguous),
        body_digest="sha256:" + hashlib.sha256(line).hexdigest(),
        logical_ids=tuple(logical),
        body=None if _holds_nul(record) else line.decode("utf-8"),
        unknown_pointers=tuple(unknown),
        projected=projected(spec, kind, record),
    )


def projected(spec: Spec, kind: str, record: Any) -> tuple[Any, ...]:
    """One value per projection column (ADR 0009 §3); None where the kind does not fill it.

    A logical id fills ``<filter>_namespace`` and ``<filter>_value`` only when Known; a record id
    or record id list fills ``<filter>_ids`` as stated, in order.
    """
    values: dict[str, Any] = {}
    for p in spec.projections:
        if p.kind != kind:
            continue
        stated = record.get(p.field)
        if stated is None:  # a package of an older schema version does not state the field
            continue
        if p.shape == "logical_id":
            if _state(stated) == "known":
                namespace, value = p.columns
                values[namespace] = stated["value"]["namespace"]
                values[value] = stated["value"]["value"]
        else:
            ids = [stated] if p.shape == "record_id" else list(stated)
            (column,) = p.columns
            values[column] = [*values.get(column, []), *ids]
    return tuple(values.get(name) for name in projection_columns(spec))


def _holds_nul(value: Any) -> bool:
    """Whether a string or key anywhere in ``value`` holds U+0000, which jsonb cannot store.

    Iterative: a free-form field may nest as deep as the compiler's reader allows, past Python's
    recursion limit.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


# The keys a Knowledge value's JSON may have (the compiler's Knowledge[T] shape). An object with
# any other key, such as an adapter locator step that carries a "knowledge" property, is not a
# field's state.
_KNOWLEDGE_KEYS: Final = frozenset({"candidates", "knowledge", "provenance", "value"})


def _state(obj: Any) -> str | None:
    """The Knowledge state of ``obj``, or None when it is not a Knowledge value."""
    if not isinstance(obj, dict) or not obj.keys() <= _KNOWLEDGE_KEYS:
        return None
    state = obj.get("knowledge")
    return state if isinstance(state, str) else None


def _walk(
    value: Any, pointer: str = "", opaque: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Any]]:
    """Every Knowledge value in ``value`` with its JSON pointer, outermost first.

    An ``Ambiguous`` value is yielded but not entered: its candidates are not fields. ``opaque``
    names top-level fields the schema declares free-form (``transform_record.config``,
    ``ingest_finding.details``, ``stream.metadata`` …): their content is data, not fields, so it
    is not walked (ADR 0009 §2) and a Knowledge-shaped object in it is never mistaken for a
    field's state. Iterative, so depth never meets Python's recursion limit.
    """
    stack: list[tuple[str, Any]] = [(pointer, value)]
    while stack:
        here, item = stack.pop()
        children: list[tuple[str, Any]] = []
        if isinstance(item, dict):
            state = _state(item)
            if state is not None:
                yield here, item
                if state == "ambiguous":
                    continue
            children = [
                (f"{here}/{_escape(key)}", item[key])
                for key in sorted(item)
                if here or key not in opaque
            ]
        elif isinstance(item, list):
            children = [(f"{here}/{index}", child) for index, child in enumerate(item)]
        stack.extend(reversed(children))


def fields(
    record: Any, opaque: frozenset[str] = frozenset()
) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
    """One walk: the ``Ambiguous`` pointers, the ``Unknown`` pointers and the Known logical ids
    ``(pointer, namespace, value)``, each sorted by pointer."""
    ambiguous: list[str] = []
    unknown: list[str] = []
    logical: list[tuple[str, str, str]] = []
    for pointer, obj in _walk(record, "", opaque):
        state = obj["knowledge"]
        if state == "ambiguous":
            ambiguous.append(pointer)
        elif state == "unknown":
            unknown.append(pointer)
        elif (
            state == "known"
            and isinstance(obj.get("value"), dict)
            and obj["value"].keys() == {"namespace", "value"}
            and isinstance(obj["value"]["namespace"], str)
            and isinstance(obj["value"]["value"], str)
        ):
            logical.append((pointer, obj["value"]["namespace"], obj["value"]["value"]))
    return sorted(ambiguous), sorted(unknown), sorted(logical)


def ambiguous_pointers(record: Any, opaque: frozenset[str] = frozenset()) -> list[str]:
    """JSON pointers of every ``Ambiguous`` field; candidates inside one are not fields."""
    return fields(record, opaque)[0]


def unknown_pointers(record: Any, opaque: frozenset[str] = frozenset()) -> list[str]:
    """JSON pointers of every ``Unknown`` field, outside ``Ambiguous`` candidates (ADR 0009 §2)."""
    return fields(record, opaque)[1]


def logical_ids(record: Any, opaque: frozenset[str] = frozenset()) -> list[tuple[str, str, str]]:
    """Every Known logical id ``{namespace, value}`` with the pointer it is stated at."""
    return fields(record, opaque)[2]


def _known_time(record: Any, name: str | None) -> tuple[str, int] | None:
    node = record.get(name) if name else None
    if isinstance(node, dict) and node.get("knowledge") == "known":
        return node["value"]["domain_id"], node["value"]["ticks"]
    return None


def world_time(kind: str, record: Any) -> tuple[str | None, int | None, int | None]:
    """``(clock, s, e)`` by ADR 0003 §3; ``e`` is None (open) unless Known on ``s``'s clock."""
    if kind not in WORLD_TIME:
        return None, None, None
    start_field, end_field, fallback = WORLD_TIME[kind]
    start, end = _known_time(record, start_field), _known_time(record, end_field)
    if start is None and (performed := _known_time(record, fallback)) is not None:
        start = performed
        end = end or performed
    if start is None:
        start = end  # a point at the end
    if start is None:
        return None, None, None
    closed = end is not None and end[0] == start[0]
    return start[0], start[1], end[1] if closed and end is not None else None


def provenance_summary(
    kind: str, record: Any
) -> tuple[str | None, str | None, str | None, str | None]:
    """``(source content id, locator JSON, transform id, assertion kind)`` (ADR 0002 §5)."""
    if kind == "ingest_finding":
        subject = record["subject"]
        if subject["kind"] != "evidence":
            return None, None, record["transform"], None
        ref = subject["ref"]
        return ref["source"], canonical(ref["locator"]), record["transform"], None
    provenance = record.get("provenance")
    if provenance is None:  # source_artifact, source_revision, source_absence, transform_record
        return None, None, None, None
    evidence = provenance["evidence"]
    return (
        evidence["source"],
        canonical(evidence["locator"]),
        provenance["transform"],
        provenance["assertion_kind"],
    )
