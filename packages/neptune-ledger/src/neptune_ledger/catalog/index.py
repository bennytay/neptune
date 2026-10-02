"""The catalog rows one verified package produces (Ledger ADR 0002 §5, ADR 0005 §2, §3).

A pure function of the package's bytes: every value comes from the record lines that registration
hashed, read once, so the same package gives the same rows in any catalog. These are the rows
migrations 0001 to 0003 define, over the package's own tables: exactly the kinds of its schema
version (Ledger ADR 0008 §2), never the compiler's whole list. Kind-specific projections and the
derived thread index belong to later migrations (MVL-91, ADR 0005 §6); they extend
``package_rows`` without changing these.
"""

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from neptune.identity import canonical_json

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
    tables: dict[str, list[Any]] = {
        kind: [canonical_json.loads(line) for line in lines[kind]] for kind in sorted(lines)
    }
    records = tuple(
        _record_row(kind, number, line, body)
        for kind in sorted(lines)
        for number, (line, body) in enumerate(zip(lines[kind], tables[kind], strict=True), 1)
    )
    return PackageRows(
        package_id=package_id,
        schema_version=manifest["schema_version"],
        receipt_id=manifest["receipt"],
        sources=tuple((s["content_id"], s["size"], s["storage"]) for s in manifest["sources"]),
        locations=tuple(
            (r["id"], r["content_id"], canonical(r["location"]), tuple(r["supersedes"]))
            for r in tables["source_revision"]
        ),
        absences=tuple(
            (a["id"], canonical(a["location"]), tuple(a["supersedes"]))
            for a in tables["source_absence"]
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
            for t in tables["transform_record"]
        ),
        clocks=tuple((d["id"], d["field"], tuple(d["scope"])) for d in tables["timestamp_domain"]),
        records=records,
    )


def _record_row(kind: str, number: int, line: bytes, record: Any) -> RecordRow:
    source, locator, transform, assertion = provenance_summary(kind, record)
    clock, first, last = world_time(kind, record)
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
        ambiguous_pointers=tuple(ambiguous_pointers(record)),
        body_digest="sha256:" + hashlib.sha256(line).hexdigest(),
        logical_ids=tuple(logical_ids(record)),
    )


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _walk(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    """Every JSON object in ``value`` with its JSON pointer, outermost first."""
    if isinstance(value, dict):
        yield pointer, value
        for key in sorted(value):
            yield from _walk(value[key], f"{pointer}/{_escape(key)}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{pointer}/{index}")


def ambiguous_pointers(record: Any) -> list[str]:
    """JSON pointers of every ``Ambiguous`` field; candidates inside one are not fields."""
    found = [p for p, obj in _walk(record) if obj.get("knowledge") == "ambiguous"]
    return sorted(p for p in found if not any(p.startswith(q + "/") for q in found))


def logical_ids(record: Any) -> list[tuple[str, str, str]]:
    """Every Known logical id ``{namespace, value}`` with the pointer it is stated at."""
    return [
        (p, obj["value"]["namespace"], obj["value"]["value"])
        for p, obj in _walk(record)
        if obj.get("knowledge") == "known"
        and isinstance(obj.get("value"), dict)
        and obj["value"].keys() == {"namespace", "value"}
        and isinstance(obj["value"]["namespace"], str)
        and isinstance(obj["value"]["value"], str)
    ]


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
