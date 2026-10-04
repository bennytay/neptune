"""Session proposals: the derived layer's first record kinds (ADR 0036).

A proposal says "these files are one session, because …". A grouper infers it from a layout
(``neptune.derived.grouping``); no evidence declares it, so it is never a ``Run`` (ADR 0018), and
it lives in the package's ``derived/`` tables, never beside the evidence in ``records/``.

- ``SessionProposal``: one candidate session. ``members`` are the files it groups, each with
  the rule that placed it and that rule's confidence; ``includes`` are proposals it holds whole,
  by id, never by listing their files again. Its *extent* is its members and the extents of what
  it includes. ``rule`` and ``confidence`` are the rule that formed it; ``reasons`` explain it.
  ``contested`` names exactly the proposals whose extents share a file with its own: each offers
  another reading of those files, none was chosen, and a file lies in two extents only when both
  proposals are contested.
- ``UnassignedFile``: a file no proposal places in every reading, and why: ``ambiguous`` with
  the proposals that could each hold it, or ``unknown`` when none could. Every file of a layout
  lies in some proposal's extent or is unassigned, and is unassigned at most once; a file both
  held and unassigned is held only by contested proposals and ambiguous among others (the
  readings that do not hold it), so a missing placement is never a blank.

Derived records carry their own envelope: ``kind``, ``schema_version`` (``DERIVED_SCHEMA_VERSION``,
not the canonical model's) and ``assertion_kind``: ``"inferred"``, or ``"stated"`` for a session
the user declared, which carries its ``DeclaredSession`` as provenance. Neither is evidence about
the sources, and no reader can take one for it. They point at evidence (each member's
``SourceRevision`` id), never the reverse. Ids derive from content with ``identity.ids.record_id``,
as findings' do: the same transform proposing the same files under the same rule at the same place
is the same proposal.
"""

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final, TypeAlias

from neptune.derived.bindings import BINDING_KIND, inferred_snapshot_binding_from_json
from neptune.derived.clocks import (
    DOMAIN_KIND,
    MAPPING_KIND,
    inferred_clock_mapping_from_json,
    inferred_timestamp_domain_from_json,
)
from neptune.derived.frames import (
    EDGE_KIND,
    GROUP_KIND,
    LINK_KIND,
    REFERENCE_KIND,
    TREE_KIND,
    frame_edge_from_json,
    frame_group_from_json,
    frame_link_from_json,
    frame_tree_from_json,
    spatial_reference_from_json,
)
from neptune.derived.media import MEDIA_KIND, media_stream_from_json
from neptune.derived.provenance import DERIVED_SCHEMA_VERSION as DERIVED_SCHEMA_VERSION
from neptune.derived.provenance import INFERRED
from neptune.derived.provenance import derived_object as _derived_object
from neptune.derived.schemas import (
    DEFINITION_KIND,
    LAYOUT_KIND,
    definition_layout_from_json,
    stream_layout_from_json,
)
from neptune.derived.semantics import SEMANTIC_KIND, stream_semantic_from_json
from neptune.discovery.layout import ROOT
from neptune.identity.ids import record_id
from neptune.model._fields import exact_object, json_array, json_str
from neptune.model.ids import RecordId, check_text, check_token, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.source import LocalPath, RawLocalPath, local_location, location_from_json

PROPOSAL_KIND: Final = "session_proposal"
UNASSIGNED_KIND: Final = "session_unassigned"
# A session the user declares is their statement, not an inference (ADR 0036 §6).
STATED: Final = str(AssertionKind.STATED)


class Role(StrEnum):
    """What a member is to its session, by its name: a recording or something beside one."""

    RECORDING = "recording"  # a recording format by name, or a rosbag2 directory's storage
    CONTEXT = "context"  # everything else: configs, notes, media, documents


class Status(StrEnum):
    PROPOSED = "proposed"  # no other proposal reads any of its files differently
    CONTESTED = "contested"  # another proposal reads some of its files differently; none chosen


class Placement(StrEnum):
    AMBIGUOUS = "ambiguous"  # several proposals could each hold the file; none was chosen
    UNKNOWN = "unknown"  # no rule places the file in any session


class LinkRelation(StrEnum):
    ALIAS = "alias"  # the link's target, read lexically, is the session's directory or a member
    INSIDE = "inside"  # the link sits inside the session's directory; it was not followed


@dataclass(frozen=True)
class Root:
    """The ingest root as a session's directory: it has no location of its own."""

    def to_json(self) -> JsonObject:
        return {"kind": "root"}


ROOT_DIRECTORY: Final = Root()
Directory: TypeAlias = Root | LocalPath | RawLocalPath


def directory_location(path: bytes) -> Directory:
    """The one representation of a root-relative directory path given as bytes."""
    return ROOT_DIRECTORY if path == ROOT else local_location(path)


def directory_from_json(data: JsonValue) -> Directory:
    if isinstance(data, Mapping) and data.get("kind") == "root":
        exact_object(data, "root directory", {"kind"})
        return ROOT_DIRECTORY
    location = location_from_json(data)
    if not isinstance(location, (LocalPath, RawLocalPath)):
        raise ValueError(f"a session's directory is local, got {location!r}")
    return location


def _local(data: JsonValue, what: str) -> LocalPath | RawLocalPath:
    location = location_from_json(data)
    if not isinstance(location, (LocalPath, RawLocalPath)):
        raise ValueError(f"{what} must be a local location, got {location!r}")
    return location


def _confidence(value: object) -> float:
    if not isinstance(value, float) or not math.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError(f"a confidence is a float in (0, 1], got {value!r}")
    return value


def bytes_json(name: str, value: bytes) -> JsonObject:
    """``{name: text}`` when ``value`` is UTF-8, else ``{name_hex: hex}`` (ADR 0010 §1)."""
    try:
        return {name: value.decode("utf-8")}
    except UnicodeDecodeError:
        return {f"{name}_hex": value.hex()}


def _bytes_from_json(obj: Mapping[str, JsonValue], name: str) -> bytes:
    if name in obj:
        return json_str(obj[name], name).encode("utf-8")
    return bytes.fromhex(json_str(obj[f"{name}_hex"], f"{name}_hex"))


@dataclass(frozen=True)
class DeclaredSession:
    """A session the user declares: every file at or below ``paths`` (root-relative, ``/``-
    separated, as ``LocalPath`` text); paths are kept sorted. A proposal of it is ``stated``,
    with the declaration as its provenance, and is set against the rules' readings, never
    above them (ADR 0036 §6)."""

    name: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        check_text("name", self.name)
        if not isinstance(self.paths, tuple) or not self.paths:
            raise ValueError("a declared session names at least one path")
        for path in self.paths:
            LocalPath(path)  # relative, no '.', '..' or empty parts, no NUL
        if len(set(self.paths)) != len(self.paths):
            raise ValueError(f"declared session {self.name!r} names a path twice")
        object.__setattr__(self, "paths", tuple(sorted(self.paths)))

    def to_json(self) -> JsonObject:
        return {"name": self.name, "paths": list(self.paths)}


def declared_session_from_json(data: JsonValue) -> DeclaredSession:
    entry = exact_object(data, "declared session", {"name", "paths"})
    paths = json_array(entry["paths"], "paths")
    return DeclaredSession(
        json_str(entry["name"], "name"), tuple(json_str(p, "path") for p in paths)
    )


@dataclass(frozen=True)
class SessionMember:
    """One file a proposal groups: its revision, where it is, its role, and what placed it."""

    revision: RecordId
    location: LocalPath | RawLocalPath
    role: Role
    rule: str
    confidence: float

    def __post_init__(self) -> None:
        parse_record_id(self.revision)
        if not isinstance(self.location, (LocalPath, RawLocalPath)):
            raise TypeError(f"a member has a local location, got {self.location!r}")
        if not isinstance(self.role, Role):
            raise TypeError(f"role must be a Role, got {self.role!r}")
        check_token("rule", self.rule)
        _confidence(self.confidence)

    def to_json(self) -> JsonObject:
        return {
            "confidence": self.confidence,
            "location": self.location.to_json(),
            "revision": self.revision,
            "role": str(self.role),
            "rule": self.rule,
        }


def session_member_from_json(data: JsonValue) -> SessionMember:
    obj = exact_object(
        data, "session member", {"confidence", "location", "revision", "role", "rule"}
    )
    return SessionMember(
        revision=parse_record_id(json_str(obj["revision"], "revision")),
        location=_local(obj["location"], "a member's location"),
        role=Role(json_str(obj["role"], "role")),
        rule=json_str(obj["rule"], "rule"),
        confidence=_confidence(obj["confidence"]),
    )


@dataclass(frozen=True)
class SessionLink:
    """A symlink a session involves. ``target`` is the link's contents exactly as stored."""

    location: LocalPath | RawLocalPath
    target: bytes
    relation: LinkRelation

    def __post_init__(self) -> None:
        if not isinstance(self.location, (LocalPath, RawLocalPath)):
            raise TypeError(f"a link has a local location, got {self.location!r}")
        if not isinstance(self.target, bytes) or not self.target:
            raise ValueError("a link's target is non-empty bytes")
        if not isinstance(self.relation, LinkRelation):
            raise TypeError(f"relation must be a LinkRelation, got {self.relation!r}")

    def key(self) -> tuple[bytes, str]:
        return (self.location.raw, str(self.relation))

    def to_json(self) -> JsonObject:
        return {
            "location": self.location.to_json(),
            "relation": str(self.relation),
            **bytes_json("target", self.target),
        }


def session_link_from_json(data: JsonValue) -> SessionLink:
    if not isinstance(data, Mapping):
        raise ValueError("a session link must be a JSON object")
    target = "target" if "target" in data else "target_hex"
    obj = exact_object(data, "session link", {"location", "relation", target})
    return SessionLink(
        location=_local(obj["location"], "a link's location"),
        target=_bytes_from_json(obj, "target"),
        relation=LinkRelation(json_str(obj["relation"], "relation")),
    )


@dataclass(frozen=True)
class Reason:
    """Why: the rule that fired, one deterministic line for people, and the facts it read."""

    rule: str
    message: str
    details: JsonObject

    def __post_init__(self) -> None:
        check_token("rule", self.rule)
        check_text("message", self.message)
        if "\n" in self.message or "\r" in self.message:
            raise ValueError("a reason's message is one line")
        if not isinstance(self.details, Mapping):
            raise TypeError("details must be a JSON object")
        for key in self.details:
            check_token("details key", key)

    def to_json(self) -> JsonObject:
        return {"details": dict(self.details), "message": self.message, "rule": self.rule}


def reason_from_json(data: JsonValue) -> Reason:
    obj = exact_object(data, "reason", {"details", "message", "rule"})
    details = obj["details"]
    if not isinstance(details, Mapping):
        raise ValueError("a reason's details must be a JSON object")
    return Reason(
        rule=json_str(obj["rule"], "rule"),
        message=json_str(obj["message"], "message"),
        details=dict(details),
    )


def proposal_id(
    transform: RecordId,
    rule: str,
    directory: Directory,
    revisions: Iterable[RecordId],
    includes: Iterable[RecordId] = (),
) -> RecordId:
    """The id of the proposal ``transform`` makes of these files and the proposals it includes,
    under ``rule``, at ``directory``.

    It covers what the proposal is, not what is said about it (confidence, reasons, links, and
    which proposals contest it), so the id never depends on the rest of the grouping.
    """
    return record_id(
        PROPOSAL_KIND,
        {
            "directory": directory.to_json(),
            "includes": sorted(set(includes)),
            "members": sorted(set(revisions)),
            "rule": rule,
            "transform": transform,
        },
    )


@dataclass(frozen=True)
class SessionProposal:
    """One candidate session (module docstring). Build with ``session_proposal``."""

    kind: ClassVar[str] = PROPOSAL_KIND
    id: RecordId
    transform: RecordId
    rule: str
    confidence: float
    status: Status
    directory: Directory
    members: tuple[SessionMember, ...]
    includes: tuple[RecordId, ...]
    links: tuple[SessionLink, ...]
    reasons: tuple[Reason, ...]
    contested: tuple[RecordId, ...]
    declared: tuple[DeclaredSession, ...] = ()

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        parse_record_id(self.transform)
        check_token("rule", self.rule)
        _confidence(self.confidence)
        if not isinstance(self.status, Status):
            raise TypeError(f"status must be a Status, got {self.status!r}")
        if not isinstance(self.directory, (Root, LocalPath, RawLocalPath)):
            raise TypeError(f"directory must be the root or a local path, got {self.directory!r}")
        if not isinstance(self.members, tuple) or not self.members:
            raise ValueError("a proposal groups at least one file")
        for member in self.members:
            if not isinstance(member, SessionMember):
                raise TypeError(f"members must be SessionMembers, got {member!r}")
        paths = [member.location.raw for member in self.members]
        if paths != sorted(set(paths)):
            raise ValueError("members must be sorted by path, each path once")
        if len({member.revision for member in self.members}) != len(self.members):
            raise ValueError("members must name each revision once")
        for inner in self.includes:
            parse_record_id(inner)
        if list(self.includes) != sorted(set(self.includes)) or self.id in self.includes:
            raise ValueError("includes must be other proposals' ids, sorted, each once")
        if not set(self.includes) <= set(self.contested):
            raise ValueError("a proposal contests every proposal it includes")
        links = [link.key() for link in self.links]
        if links != sorted(set(links)):
            raise ValueError("links must be sorted by path and relation, each once")
        for reason in self.reasons:
            if not isinstance(reason, Reason):
                raise TypeError(f"reasons must be Reasons, got {reason!r}")
        for other in self.contested:
            parse_record_id(other)
        if list(self.contested) != sorted(set(self.contested)) or self.id in self.contested:
            raise ValueError("contested must be other proposals' ids, sorted, each once")
        if (self.status is Status.CONTESTED) != bool(self.contested):
            raise ValueError("a proposal is contested exactly when another proposal contests it")
        for declaration in self.declared:
            if not isinstance(declaration, DeclaredSession):
                raise TypeError(f"declared must be DeclaredSessions, got {declaration!r}")
        names = [declaration.name for declaration in self.declared]
        if names != sorted(set(names)):
            raise ValueError("declared must be sorted by name, each name once")
        expected = proposal_id(
            self.transform, self.rule, self.directory, self.revisions(), self.includes
        )
        if self.id != expected:
            raise ValueError(f"proposal {self.id}: id does not match its content")

    @property
    def assertion_kind(self) -> str:
        """``stated`` for a session the user declared (``declared`` is its provenance, under the
        transform whose config holds it); ``inferred`` for every reading a rule made."""
        return STATED if self.declared else INFERRED

    def revisions(self) -> tuple[RecordId, ...]:
        return tuple(sorted(member.revision for member in self.members))

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": self.assertion_kind,
            "confidence": self.confidence,
            "contested": list(self.contested),
            "declared": [declaration.to_json() for declaration in self.declared],
            "directory": self.directory.to_json(),
            "id": self.id,
            "includes": list(self.includes),
            "kind": self.kind,
            "links": [link.to_json() for link in self.links],
            "members": [member.to_json() for member in self.members],
            "reasons": [reason.to_json() for reason in self.reasons],
            "rule": self.rule,
            "schema_version": DERIVED_SCHEMA_VERSION,
            "status": str(self.status),
            "transform": self.transform,
        }


def session_proposal(
    *,
    transform: RecordId,
    rule: str,
    confidence: float,
    directory: Directory,
    members: Iterable[SessionMember],
    includes: Iterable[RecordId] = (),
    links: Iterable[SessionLink] = (),
    reasons: Iterable[Reason] = (),
    contested: Iterable[RecordId] = (),
    declared: Iterable[DeclaredSession] = (),
) -> SessionProposal:
    """A proposal with its id derived, its members, includes and links in canonical order."""
    ordered = tuple(sorted(members, key=lambda member: member.location.raw))
    held = tuple(sorted(set(includes)))
    others = tuple(sorted(set(contested)))
    return SessionProposal(
        id=proposal_id(transform, rule, directory, (member.revision for member in ordered), held),
        transform=transform,
        rule=rule,
        confidence=confidence,
        status=Status.CONTESTED if others else Status.PROPOSED,
        directory=directory,
        members=ordered,
        includes=held,
        links=tuple(sorted(set(links), key=SessionLink.key)),
        reasons=tuple(reasons),
        contested=others,
        declared=tuple(sorted(declared, key=lambda declaration: declaration.name)),
    )


def session_proposal_from_json(data: JsonValue) -> SessionProposal:
    """Parse strictly; the id must recompute from the content."""
    obj = _derived_object(
        data,
        PROPOSAL_KIND,
        {
            "confidence",
            "contested",
            "declared",
            "directory",
            "id",
            "includes",
            "links",
            "members",
            "reasons",
            "rule",
            "status",
            "transform",
        },
        frozenset({INFERRED, STATED}),
    )
    proposal = SessionProposal(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        rule=json_str(obj["rule"], "rule"),
        confidence=_confidence(obj["confidence"]),
        status=Status(json_str(obj["status"], "status")),
        directory=directory_from_json(obj["directory"]),
        members=tuple(session_member_from_json(m) for m in json_array(obj["members"], "members")),
        includes=tuple(
            parse_record_id(json_str(inner, "includes"))
            for inner in json_array(obj["includes"], "includes")
        ),
        links=tuple(session_link_from_json(link) for link in json_array(obj["links"], "links")),
        reasons=tuple(reason_from_json(r) for r in json_array(obj["reasons"], "reasons")),
        contested=tuple(
            parse_record_id(json_str(other, "contested"))
            for other in json_array(obj["contested"], "contested")
        ),
        declared=tuple(
            declared_session_from_json(item) for item in json_array(obj["declared"], "declared")
        ),
    )
    if obj["assertion_kind"] != proposal.assertion_kind:
        raise ValueError("a proposal is stated exactly when it carries the declarations it states")
    return proposal


def unassigned_id(transform: RecordId, revision: RecordId) -> RecordId:
    return record_id(UNASSIGNED_KIND, {"revision": revision, "transform": transform})


@dataclass(frozen=True)
class UnassignedFile:
    """A file no proposal holds. ``reason`` is a token naming why; ``candidates`` are the
    proposals that could each hold it: two or more when ``ambiguous``, none when ``unknown``.
    """

    kind: ClassVar[str] = UNASSIGNED_KIND
    id: RecordId
    transform: RecordId
    revision: RecordId
    location: LocalPath | RawLocalPath
    placement: Placement
    reason: str
    candidates: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.transform)
        parse_record_id(self.revision)
        if not isinstance(self.location, (LocalPath, RawLocalPath)):
            raise TypeError(f"a file has a local location, got {self.location!r}")
        if not isinstance(self.placement, Placement):
            raise TypeError(f"placement must be a Placement, got {self.placement!r}")
        check_token("reason", self.reason)
        for candidate in self.candidates:
            parse_record_id(candidate)
        if list(self.candidates) != sorted(set(self.candidates)):
            raise ValueError("candidates must be sorted, each once")
        if self.placement is Placement.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("an ambiguous placement names at least two candidate proposals")
        if self.placement is Placement.UNKNOWN and self.candidates:
            raise ValueError("an unknown placement names no candidates")
        if self.id != unassigned_id(self.transform, self.revision):
            raise ValueError(f"unassigned file {self.id}: id does not match its content")

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": INFERRED,
            "candidates": list(self.candidates),
            "id": self.id,
            "kind": self.kind,
            "location": self.location.to_json(),
            "placement": str(self.placement),
            "reason": self.reason,
            "revision": self.revision,
            "schema_version": DERIVED_SCHEMA_VERSION,
            "transform": self.transform,
        }


def unassigned_file(
    *,
    transform: RecordId,
    revision: RecordId,
    location: LocalPath | RawLocalPath,
    reason: str,
    candidates: Iterable[RecordId] = (),
) -> UnassignedFile:
    ordered = tuple(sorted(set(candidates)))
    return UnassignedFile(
        id=unassigned_id(transform, revision),
        transform=transform,
        revision=revision,
        location=location,
        placement=Placement.AMBIGUOUS if ordered else Placement.UNKNOWN,
        reason=reason,
        candidates=ordered,
    )


def unassigned_file_from_json(data: JsonValue) -> UnassignedFile:
    obj = _derived_object(
        data,
        UNASSIGNED_KIND,
        {"candidates", "id", "location", "placement", "reason", "revision", "transform"},
    )
    return UnassignedFile(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        revision=parse_record_id(json_str(obj["revision"], "revision")),
        location=_local(obj["location"], "an unassigned file's location"),
        placement=Placement(json_str(obj["placement"], "placement")),
        reason=json_str(obj["reason"], "reason"),
        candidates=tuple(
            parse_record_id(json_str(candidate, "candidate"))
            for candidate in json_array(obj["candidates"], "candidates")
        ),
    )


# Every derived kind this package defines, with its strict reader: the derived tables a package
# may hold (``store.package`` checks their structure; these readers check their meaning).
DERIVED_KINDS: Final[Mapping[str, Callable[[JsonValue], Any]]] = {
    PROPOSAL_KIND: session_proposal_from_json,
    UNASSIGNED_KIND: unassigned_file_from_json,
    DEFINITION_KIND: definition_layout_from_json,  # ADR 0049
    LAYOUT_KIND: stream_layout_from_json,
    SEMANTIC_KIND: stream_semantic_from_json,
    MEDIA_KIND: media_stream_from_json,  # ADR 0056
    DOMAIN_KIND: inferred_timestamp_domain_from_json,  # ADR 0060
    MAPPING_KIND: inferred_clock_mapping_from_json,
    BINDING_KIND: inferred_snapshot_binding_from_json,  # ADR 0064
    TREE_KIND: frame_tree_from_json,  # ADR 0068
    EDGE_KIND: frame_edge_from_json,
    LINK_KIND: frame_link_from_json,
    GROUP_KIND: frame_group_from_json,
    REFERENCE_KIND: spatial_reference_from_json,
}


def read_derived(tables: Mapping[str, Iterable[JsonValue]]) -> tuple[Any, ...]:
    """Parse a package's derived tables (``IngestPackage.derived``) strictly, every kind known.

    A table of a kind this reader does not define is refused, never skipped.
    """
    records: list[Any] = []
    for kind, lines in sorted(tables.items()):
        reader = DERIVED_KINDS.get(kind)
        if reader is None:
            raise ValueError(f"a derived table of unknown kind {kind!r}")
        records.extend(reader(line) for line in lines)
    return tuple(records)
