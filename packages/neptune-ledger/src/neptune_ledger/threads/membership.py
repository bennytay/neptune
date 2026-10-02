"""Thread membership of one package's records (Ledger ADR 0003 §2, ADR 0010 §2).

A pure function of the package's verified record lines and this Ledger version: which threads
each record opens or joins, in which role, and which threads an ``Ambiguous`` field names. The
field table of ADR 0003 §2 is the only source of membership; nothing is matched by name, content
or similarity, and no two keys are ever joined. ``part_of`` follows a tier-2 reference only to a
record of the same package, because tier-2 ids are lineage-scoped.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from neptune.identity import canonical_json
from neptune_ledger.api import codec
from neptune_ledger.api.types import DeclaredKey, EvidenceAnchor, ThreadKey, ThreadKind
from neptune_ledger.catalog.index import WORLD_TIME, world_time

# The record kinds ADR 0003 §2's table reads; every other kind is in no thread.
THREAD_RECORD_KINDS: Final = frozenset(
    {
        "asset",
        "calibration",
        "document_block",
        "document_record",
        "hardware_component",
        "hardware_configuration",
        "image",
        "machine",
        "run",
        "site",
        "software_configuration",
        "stream",
        "video",
    }
)
# Kinds whose thread a later record joins ``part_of`` by tier-2 id (ADR 0003 §2): the referencing
# field, the referenced kind, and the thread kind of the referenced record.
PART_OF: Final = {
    "stream": ("run", "run", "run"),
    "hardware_component": ("configuration", "hardware_configuration", "configuration"),
    "document_block": ("document", "document_record", "document"),
}
# A software item's version fields that key a software_version thread, and the value's own field
# holding the identifier (ADR 0003 §2; root ADR 0014's version kinds).
SOFTWARE_FIELDS: Final = ("commit", "digest")
SOFTWARE_VALUE: Final = {
    "git_commit": "sha",
    "container_image_digest": "digest",
    "model_checkpoint_hash": "digest",
}
_GROUNDED: Final = frozenset({"observed", "stated"})


@dataclass(frozen=True)
class ThreadRow:
    """A thread key, once per thread id: ``key`` is the canonical JSON ``thread_id`` hashes."""

    thread_id: str
    kind: str
    key: str


@dataclass(frozen=True)
class MemberRow:
    """One entry of a thread in this package (``thread_member`` without tenant and package)."""

    thread_id: str
    record_id: str
    kind: str
    roles: tuple[str, ...]
    transform_id: str
    source_content_id: str
    world_clock: str | None
    world_first: int | None
    world_last: int | None
    world: str


@dataclass(frozen=True)
class UnresolvedRow:
    """A record whose ``Ambiguous`` field at ``pointer`` names the thread among its candidates."""

    thread_id: str
    record_id: str
    kind: str
    pointer: str


@dataclass(frozen=True)
class ThreadRows:
    """Everything one package adds to the thread index, each tuple sorted by its key."""

    threads: tuple[ThreadRow, ...]
    members: tuple[MemberRow, ...]
    unresolved: tuple[UnresolvedRow, ...]


def thread_rows(lines: Mapping[str, tuple[bytes, ...]]) -> ThreadRows:
    """The thread rows of one package, from its verified record lines (any kind order)."""
    records: dict[str, list[Any]] = {
        kind: [canonical_json.loads(line) for line in lines[kind]]
        for kind in sorted(lines)
        if kind in THREAD_RECORD_KINDS
    }
    keys: dict[str, ThreadKey] = {}
    roles: dict[tuple[str, str], set[str]] = {}
    info: dict[str, tuple[str, Any]] = {}
    unresolved: set[tuple[str, str, str, str]] = set()
    # The threads each record is the subject of, by record id: the targets of part_of.
    subject_of: dict[str, list[str]] = {}

    def join(key: ThreadKey, kind: str, record: Any, role: str) -> None:
        thread_id = key.thread_id
        keys[thread_id] = key
        roles.setdefault((thread_id, record["id"]), set()).add(role)
        info[record["id"]] = (kind, record)
        if role == "subject":
            subject_of.setdefault(record["id"], []).append(thread_id)

    def named(key: ThreadKey, kind: str, record: Any, pointer: str) -> None:
        keys[key.thread_id] = key
        unresolved.add((key.thread_id, record["id"], kind, pointer))

    for kind, rows in records.items():
        for record in rows:
            anchor = _eligible(record)
            if anchor is None:
                continue
            for thread_kind, role, pointer, field in _declared(kind, record):
                for key in _keys(thread_kind, field, record):
                    join(key, kind, record, role)
                for key in _candidate_keys(thread_kind, field):
                    named(key, kind, record, pointer)
            for thread_kind in _anchored(kind, record):
                join(ThreadKey(cast("ThreadKind", thread_kind), anchor), kind, record, "subject")
    for kind, (field, target_kind, thread_kind) in sorted(PART_OF.items()):
        targets = {r["id"] for r in records.get(target_kind, [])}
        for record in records.get(kind, []):
            target = record.get(field)
            if target not in targets or _eligible(record) is None:
                continue
            for thread_id in subject_of.get(target, []):
                if keys[thread_id].kind == thread_kind:
                    join(keys[thread_id], kind, record, "part_of")
    members = tuple(
        _member(thread_id, record_id, tuple(sorted(held)), *info[record_id])
        for (thread_id, record_id), held in sorted(roles.items())
    )
    return ThreadRows(
        threads=tuple(
            ThreadRow(thread_id, key.kind, codec.dumps(key).decode("utf-8"))
            for thread_id, key in sorted(keys.items())
        ),
        members=members,
        unresolved=tuple(UnresolvedRow(*row) for row in sorted(unresolved)),
    )


def _member(
    thread_id: str, record_id: str, roles: tuple[str, ...], kind: str, record: Any
) -> MemberRow:
    clock, first, last = world_time(kind, record)
    anchor = _anchor(record)
    assert anchor is not None
    return MemberRow(
        thread_id=thread_id,
        record_id=record_id,
        kind=kind,
        roles=roles,
        transform_id=record["provenance"]["transform"],
        source_content_id=anchor.source,
        world_clock=clock,
        world_first=first,
        world_last=last,
        world=canonical_json.dumps(world_json(kind, record)).decode("utf-8"),
    )


def _anchor(record: Any) -> EvidenceAnchor | None:
    """The record-level evidence anchor ``(source content id, locator)`` (ADR 0003 §1)."""
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping) or not isinstance(provenance.get("transform"), str):
        return None
    evidence = provenance.get("evidence")
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("source"), str):
        return None
    locator = evidence.get("locator")
    if not isinstance(locator, list) or not locator:
        return None
    return EvidenceAnchor(evidence["source"], tuple(locator))


def _eligible(record: Any) -> EvidenceAnchor | None:
    """The record's anchor when it may be in a thread at all: it has a record-level evidence
    anchor and stated or observed provenance. Inferred records never open or join one."""
    if record.get("provenance", {}).get("assertion_kind") not in _GROUNDED:
        return None
    return _anchor(record)


def _declared(kind: str, record: Any) -> Iterator[tuple[str, str, str, Any]]:
    """``(thread kind, role, pointer, field)`` of every declared-key field (ADR 0003 §2)."""
    if kind == "machine":
        for i, field in enumerate(record.get("identifiers", [])):
            yield "machine", "subject", f"/identifiers/{i}", field
    elif kind in ("run", "hardware_configuration", "software_configuration", "calibration"):
        yield "machine", "cites", "/machine", record.get("machine")
    if kind == "hardware_component" and record.get("category") == "sensor":
        for i, field in enumerate(record.get("identifiers", [])):
            yield "sensor", "subject", f"/identifiers/{i}", field
    elif kind in ("image", "video"):
        capture = record.get("capture")
        stated = capture.get("device_identifiers", []) if isinstance(capture, Mapping) else []
        for i, field in enumerate(stated):
            yield "sensor", "cites", f"/capture/device_identifiers/{i}", field
    elif kind == "site":
        for i, field in enumerate(record.get("identifiers", [])):
            yield "site", "subject", f"/identifiers/{i}", field
        yield "site", "cites", "/parent", record.get("parent")
    elif kind == "asset":
        for i, field in enumerate(record.get("identifiers", [])):
            yield "asset", "subject", f"/identifiers/{i}", field
        yield "site", "cites", "/site", record.get("site")
        yield "asset", "cites", "/parent", record.get("parent")
    elif kind == "run":
        yield "run", "subject", "/logical_id", record.get("logical_id")
    elif kind == "software_configuration":
        for i, item in enumerate(record.get("software", [])):
            if isinstance(item, Mapping):
                for name in SOFTWARE_FIELDS:
                    yield "software_version", "cites", f"/software/{i}/{name}", item.get(name)


def _anchored(kind: str, record: Any) -> Iterator[str]:
    """The anchored thread kinds a record opens as its ``subject`` (ADR 0003 §2)."""
    if kind == "run" and _grounded_value(record.get("logical_id"), record) is None:
        yield "run"
    elif kind == "stream":
        yield "stream"
    elif kind in ("hardware_configuration", "software_configuration", "calibration"):
        yield "configuration"
    elif kind == "document_record":
        yield "document"


def _grounded_value(field: Any, record: Any) -> Any | None:
    """A ``Known`` field's value when its provenance (its own, else the record's) is stated or
    observed; ``None`` for every other state and for inferred values."""
    if not isinstance(field, Mapping) or field.get("knowledge") != "known":
        return None
    provenance = field.get("provenance", record.get("provenance"))
    if not isinstance(provenance, Mapping) or provenance.get("assertion_kind") not in _GROUNDED:
        return None
    return field.get("value")


def _key(thread_kind: str, value: Any) -> ThreadKey | None:
    """The declared key a stated value gives, or None when the value is not one."""
    if not isinstance(value, Mapping):
        return None
    kind = cast("ThreadKind", thread_kind)
    if kind == "software_version":
        name = SOFTWARE_VALUE.get(value.get("kind", ""))
        text = value.get(name) if name else None
        return ThreadKey(kind, DeclaredKey(value["kind"], text)) if text else None
    if value.keys() == {"namespace", "value"}:
        return ThreadKey(kind, DeclaredKey(value["namespace"], value["value"]))
    return None


def _keys(thread_kind: str, field: Any, record: Any) -> list[ThreadKey]:
    key = _key(thread_kind, _grounded_value(field, record))
    return [key] if key is not None else []


def _candidate_keys(thread_kind: str, field: Any) -> list[ThreadKey]:
    """The keys an ``Ambiguous`` field's candidates name, inferred candidates left out."""
    if not isinstance(field, Mapping) or field.get("knowledge") != "ambiguous":
        return []
    out = []
    for candidate in field.get("candidates", []):
        provenance = candidate.get("provenance") if isinstance(candidate, Mapping) else None
        if isinstance(provenance, Mapping) and provenance.get("assertion_kind") not in _GROUNDED:
            continue
        key = _key(thread_kind, candidate.get("value") if isinstance(candidate, Mapping) else None)
        if key is not None:
            out.append(key)
    return out


def _known(field: Any) -> bool:
    return isinstance(field, Mapping) and field.get("knowledge") == "known"


def world_json(kind: str, record: Any) -> dict[str, Any]:
    """``ThreadEntry.world`` as JSON (ADR 0003 §3): ``not_applicable`` for kinds without world
    time, ``unknown`` when no bound is Known, else the start point and the end field verbatim."""
    if kind not in WORLD_TIME:
        return {"knowledge": "not_applicable"}
    start_name, end_name, fallback = WORLD_TIME[kind]
    start_field, end_field = record.get(start_name), record.get(end_name)
    if fallback is not None and not _known(start_field):
        start_field = record.get(fallback)
        if not _known(end_field) and _known(start_field):
            end_field = start_field
    if not _known(start_field):
        start_field = end_field  # a point at the end
    if not _known(start_field):
        return {"knowledge": "unknown"}
    value = start_field["value"]
    start = {"domain_id": value["domain_id"], "ticks": value["ticks"]}
    end = end_field if isinstance(end_field, Mapping) else {"knowledge": "not_covered"}
    return {"knowledge": "known", "value": {"end": end, "start": start}}
