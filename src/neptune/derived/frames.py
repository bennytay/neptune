"""Frame graphs across sources, and whether two spatial observations are comparable (ADR 0068).

A frame is named in one graph (ADR 0007): a calibration file's, a URDF's, one recording's tf.
Nothing in a source relates two graphs, and a recording's tf is a stream, not records. The
frame-alignment pass (``neptune.derived.spatial``, transform ``neptune.frames``) reads both and
writes five derived tables, every line ``inferred``:

- ``frame_tree``: the frames one run's ROS messages name, as one graph: its ``tf`` and
  ``tf_static`` streams and every stream whose header names a frame (rule ``ros.run_frames``).
  Its id is the ``frame_graph_id`` of those frames.
- ``frame_edge``: one ``parent → child`` pair a tree's transform stream holds, with how many
  transforms state it and over which instants of the stream's clock 0, whether it is static (the
  tf2 static topic) and what its values mean as far as the type's definition says (direction
  ``child_to_parent``; translation unit and quaternion algebra unknown: REP-103 is convention).
- ``frame_link``: two frames of two graphs (or one tree) that a rule proposes are one frame:
  ``same_name`` (a declared graph's frame and a tree's, named alike) or ``leading_slash``
  (``/base_link`` and ``base_link``). A proposal: ``compare`` uses it only when asked and then
  marks its answer inferred.
- ``frame_group``: frames that transforms, edges and stated bindings join, with the group's
  ``origin`` (its one root, ``Ambiguous`` roots, or ``Unknown`` in a loop) and ``earth`` (what
  places the group on the earth: ``Unknown``, as no supported source states it).
- ``spatial_reference``: what one stream's or record's spatial values are expressed in: the
  frames its rows name (with counts, and the rows that name none), a CRS, or the geodetic
  definition of its type (``sensor_msgs/NavSatFix``).

``FrameIndex.compare(a, b)`` answers the question downstream asks: are values in ``a`` and in
``b`` comparable, and why. ``Comparable`` gives the path of transforms, bindings, edges and links
between them, whether any step is inferred, whether a step changes over time (and, given an
instant, whether every such step covers it, through the clock graph when the clocks differ), and
the caveats a consumer composing the path must resolve (an unknown direction, unit or quaternion
algebra). ``NotComparable`` says why not: no chain of transforms joins them, a frame or CRS is not
known, two CRSs differ, a frame has no georeference, or a step does not cover the instant.
Nothing is composed or converted: the answer is a path, never a pose.
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final

from neptune.derived.clocks import Aligned, ClockGraph
from neptune.derived.provenance import (
    DERIVED_SCHEMA_VERSION,
    INFERRED,
    InferredProvenance,
    derived_object,
)
from neptune.model._fields import check_type, enum_decoder, json_array, json_int, json_str
from neptune.model.alignment import FrameBinding
from neptune.model.frames import (
    FrameRef,
    HomogeneousMatrix,
    Pose,
    Quaternion,
    TransformDirection,
    frame_ref_from_json,
)
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Grounding,
    Knowledge,
    Known,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.reference import FrameTransform
from neptune.model.spatial import CrsCode, crs_code_from_json
from neptune.model.time import Timestamp, timestamp_from_json

TREE_KIND: Final = "frame_tree"
EDGE_KIND: Final = "frame_edge"
LINK_KIND: Final = "frame_link"
GROUP_KIND: Final = "frame_group"
REFERENCE_KIND: Final = "spatial_reference"
RUN_FRAMES: Final = "ros.run_frames"


def _inherited_only(data: JsonObject) -> Grounding:
    raise ValueError("a state of a derived record inherits the record's provenance")


def _knowledge(data: JsonValue, decode: Callable[[JsonValue], Any]) -> Knowledge[Any]:
    return from_json(data, decode, _inherited_only)


def _envelope(kind: str, record_id: RecordId, provenance: InferredProvenance) -> JsonObject:
    return {
        "assertion_kind": INFERRED,
        "evidence": [ref.to_json() for ref in provenance.evidence],
        "id": record_id,
        "kind": kind,
        "schema_version": DERIVED_SCHEMA_VERSION,
        "transform": provenance.transform,
    }


def _common(obj: Mapping[str, JsonValue]) -> tuple[RecordId, RecordId, tuple[EvidenceRef, ...]]:
    return (
        parse_record_id(json_str(obj["id"], "id")),
        parse_record_id(json_str(obj["transform"], "transform")),
        tuple(evidence_ref_from_json(r) for r in json_array(obj["evidence"], "evidence")),
    )


def _ids(data: JsonValue, what: str) -> tuple[RecordId, ...]:
    found = tuple(parse_record_id(json_str(item, what)) for item in json_array(data, what))
    if list(found) != sorted(set(found)):
        raise ValueError(f"{what} are sorted and unique")
    return found


def _check(record_id: RecordId, transform: RecordId, evidence: tuple[EvidenceRef, ...]) -> None:
    parse_record_id(record_id)
    InferredProvenance(evidence, transform)  # checks both


# --- frame_tree ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameTree:
    """The frames one run's ROS messages in one tf namespace name, as one graph (rule
    ``ros.run_frames``): ROS names a frame by its id within one tf tree, so the ids a run's
    transforms and headers write under one namespace are one graph's. ``namespace`` is the tf
    topics' (``/robot1`` for ``/robot1/tf``, ``/`` for ``/tf``); two namespaces are two trees.
    ``streams`` are those that name frames, sorted; the evidence cites each."""

    kind: ClassVar[str] = TREE_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    run: RecordId
    namespace: str
    streams: tuple[RecordId, ...]
    rule: str = RUN_FRAMES

    def __post_init__(self) -> None:
        _check(self.id, self.transform, self.evidence)
        parse_record_id(self.run)
        if not self.namespace:
            raise ValueError("a frame tree names its tf namespace ('/' for the root)")
        if not self.streams or list(self.streams) != sorted(set(self.streams)):
            raise ValueError("a frame tree names its streams, sorted and unique")
        if self.rule != RUN_FRAMES:
            raise ValueError(f"a frame tree's rule is {RUN_FRAMES!r}, got {self.rule!r}")

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            **_envelope(self.kind, self.id, self.provenance),
            "namespace": self.namespace,
            "rule": self.rule,
            "run": self.run,
            "streams": list(self.streams),
        }


def frame_tree_from_json(data: JsonValue) -> FrameTree:
    obj = derived_object(
        data, TREE_KIND, {"evidence", "id", "namespace", "rule", "run", "streams", "transform"}
    )
    record_id, transform, evidence = _common(obj)
    return FrameTree(
        record_id,
        transform,
        evidence,
        parse_record_id(json_str(obj["run"], "run")),
        json_str(obj["namespace"], "namespace"),
        _ids(obj["streams"], "streams"),
        json_str(obj["rule"], "rule"),
    )


# --- frame_edge ---------------------------------------------------------------------------------


class Persistence(StrEnum):
    STATIC = "static"  # on tf2's static topic: holds until restated
    DYNAMIC = "dynamic"  # sampled: holds at its samples' instants


@dataclass(frozen=True)
class FrameEdge:
    """One ``parent → child`` pair one transform stream of a tree holds.

    ``samples`` transforms state it, the first and last at ``first`` and ``last`` on the stream's
    clock 0 (its rows' order clock). Their values stay in the stream's series, untouched. What
    the values mean, as far as the message type's definition says: ``direction``
    ``child_to_parent`` (tf2: the child's pose in the parent); ``translation_unit`` and
    ``quaternion_convention`` ``Unknown`` (REP-103 metres and Hamilton quaternions are
    convention, not evidence, ADR 0007 §4). ``persistence`` is ``static`` on tf2's static topic.
    """

    kind: ClassVar[str] = EDGE_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    tree: RecordId
    stream: RecordId
    parent: FrameRef
    child: FrameRef
    persistence: Persistence
    direction: Knowledge[TransformDirection]
    translation_unit: Knowledge[str]
    quaternion_convention: Knowledge[str]
    samples: int
    first: Timestamp
    last: Timestamp

    def __post_init__(self) -> None:
        _check(self.id, self.transform, self.evidence)
        parse_record_id(self.tree)
        parse_record_id(self.stream)
        for ref in (self.parent, self.child):
            if not isinstance(ref, FrameRef) or ref.frame_graph_id != self.tree:
                raise ValueError("an edge's frames are frames of its tree")
        if self.parent == self.child:
            raise ValueError(f"an edge joins two frames; {self.parent.frame_id!r} is both")
        if not isinstance(self.persistence, Persistence):
            raise TypeError(f"persistence must be a Persistence, got {self.persistence!r}")
        check_type("direction", self.direction, TransformDirection)
        check_type("translation_unit", self.translation_unit, str)
        check_type("quaternion_convention", self.quaternion_convention, str)
        if isinstance(self.samples, bool) or not isinstance(self.samples, int) or self.samples < 1:
            raise ValueError(f"an edge has at least one sample, got {self.samples!r}")
        if self.first.domain_id != self.last.domain_id or self.last.ticks < self.first.ticks:
            raise ValueError("an edge's first and last are on one clock, first before last")

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            **_envelope(self.kind, self.id, self.provenance),
            "child": self.child.to_json(),
            "direction": to_json(self.direction, str),
            "first": self.first.to_json(),
            "last": self.last.to_json(),
            "parent": self.parent.to_json(),
            "persistence": str(self.persistence),
            "quaternion_convention": to_json(self.quaternion_convention),
            "samples": self.samples,
            "stream": self.stream,
            "translation_unit": to_json(self.translation_unit),
            "tree": self.tree,
        }


_EDGE_KEYS: Final = {
    "child",
    "direction",
    "evidence",
    "first",
    "id",
    "last",
    "parent",
    "persistence",
    "quaternion_convention",
    "samples",
    "stream",
    "transform",
    "translation_unit",
    "tree",
}


def _text(data: JsonValue) -> str:
    return json_str(data, "text")


def frame_edge_from_json(data: JsonValue) -> FrameEdge:
    obj = derived_object(data, EDGE_KIND, _EDGE_KEYS)
    record_id, transform, evidence = _common(obj)
    return FrameEdge(
        record_id,
        transform,
        evidence,
        parse_record_id(json_str(obj["tree"], "tree")),
        parse_record_id(json_str(obj["stream"], "stream")),
        frame_ref_from_json(obj["parent"]),
        frame_ref_from_json(obj["child"]),
        Persistence(json_str(obj["persistence"], "persistence")),
        _knowledge(obj["direction"], enum_decoder(TransformDirection)),
        _knowledge(obj["translation_unit"], _text),
        _knowledge(obj["quaternion_convention"], _text),
        json_int(obj["samples"], "samples"),
        timestamp_from_json(obj["first"]),
        timestamp_from_json(obj["last"]),
    )


# --- frame_link ---------------------------------------------------------------------------------


class LinkRule(StrEnum):
    SAME_NAME = "same_name"  # a declared graph's frame and a run tree's, one verbatim name
    LEADING_SLASH = "leading_slash"  # one name with and one without a leading "/"


@dataclass(frozen=True)
class FrameLink:
    """A proposal that ``left`` and ``right`` are one frame, by ``rule``. Never a merge: both
    stay frames of their own graphs, and ``compare`` crosses a link only when asked."""

    kind: ClassVar[str] = LINK_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    left: FrameRef
    right: FrameRef
    rule: LinkRule

    def __post_init__(self) -> None:
        _check(self.id, self.transform, self.evidence)
        if not isinstance(self.left, FrameRef) or not isinstance(self.right, FrameRef):
            raise TypeError("a link joins two FrameRefs")
        if not _ref_key(self.left) < _ref_key(self.right):
            raise ValueError("a link's left sorts before its right, and they differ")
        if not isinstance(self.rule, LinkRule):
            raise TypeError(f"rule must be a LinkRule, got {self.rule!r}")

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            **_envelope(self.kind, self.id, self.provenance),
            "left": self.left.to_json(),
            "right": self.right.to_json(),
            "rule": str(self.rule),
        }


def frame_link_from_json(data: JsonValue) -> FrameLink:
    obj = derived_object(data, LINK_KIND, {"evidence", "id", "left", "right", "rule", "transform"})
    record_id, transform, evidence = _common(obj)
    return FrameLink(
        record_id,
        transform,
        evidence,
        frame_ref_from_json(obj["left"]),
        frame_ref_from_json(obj["right"]),
        LinkRule(json_str(obj["rule"], "rule")),
    )


# --- frame_group --------------------------------------------------------------------------------


def _ref_key(ref: FrameRef) -> tuple[str, str]:
    return (ref.frame_graph_id, ref.frame_id)


@dataclass(frozen=True)
class FrameGroup:
    """Frames joined by transforms, tree edges and stated bindings, never by a link.

    ``members`` are sorted by graph then name. ``origin`` is the group's root (the frame no
    transform names as a child): ``Known`` when there is one, ``Ambiguous`` when several (a frame
    with two parents, a binding between two graphs' roots), ``Unknown`` when every frame has a
    parent (a loop). ``earth`` is the CRS that places the group on the earth: nothing a
    supported source declares does, so it is ``Unknown``; a frame of this group and a CRS are
    therefore never comparable."""

    kind: ClassVar[str] = GROUP_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    members: tuple[FrameRef, ...]
    origin: Knowledge[FrameRef]
    earth: Knowledge[CrsCode]
    dynamic: bool

    def __post_init__(self) -> None:
        _check(self.id, self.transform, self.evidence)
        keys = [_ref_key(ref) for ref in self.members]
        if not keys or keys != sorted(set(keys)):
            raise ValueError("a group's members are sorted and unique, at least one")
        check_type("origin", self.origin, FrameRef)
        if isinstance(self.origin, Known) and self.origin.value not in self.members:
            raise ValueError("a group's origin is one of its members")
        check_type("earth", self.earth, CrsCode)
        if not isinstance(self.dynamic, bool):
            raise TypeError("dynamic is a bool")

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            **_envelope(self.kind, self.id, self.provenance),
            "dynamic": self.dynamic,
            "earth": to_json(self.earth, CrsCode.to_json),
            "members": [ref.to_json() for ref in self.members],
            "origin": to_json(self.origin, FrameRef.to_json),
        }


def _bool(data: JsonValue) -> bool:
    if not isinstance(data, bool):
        raise ValueError(f"expected a boolean, got {data!r}")
    return data


def frame_group_from_json(data: JsonValue) -> FrameGroup:
    keys = {"dynamic", "earth", "evidence", "id", "members", "origin", "transform"}
    obj = derived_object(data, GROUP_KIND, keys)
    record_id, transform, evidence = _common(obj)
    return FrameGroup(
        record_id,
        transform,
        evidence,
        tuple(frame_ref_from_json(m) for m in json_array(obj["members"], "members")),
        _knowledge(obj["origin"], frame_ref_from_json),
        _knowledge(obj["earth"], crs_code_from_json),
        _bool(obj["dynamic"]),
    )


# --- spatial_reference --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameCount:
    """A frame a subject's values are expressed in, and on how many of its rows (``None`` for a
    record, which is one value)."""

    frame: FrameRef
    rows: int | None = None

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"frame": self.frame.to_json()}
        if self.rows is not None:
            out["rows"] = self.rows
        return out


def _frame_count_from_json(data: JsonValue) -> FrameCount:
    if not isinstance(data, Mapping) or not {"frame"} <= set(data) <= {"frame", "rows"}:
        raise ValueError(f"a frame count is {{frame, rows?}}, got {data!r}")
    rows = json_int(data["rows"], "rows") if "rows" in data else None
    return FrameCount(frame_ref_from_json(data["frame"]), rows)


@dataclass(frozen=True)
class SpatialReference:
    """What one subject's spatial values are expressed in.

    - ``frames``: the frames its rows name (a stream's ``header.frame_id``, each with its row
      count) or the frame a record declares; ``unset`` rows name none (an empty frame id).
    - ``crs``: a record's declared CRS (``NotApplicable`` where it states it has none, as a
      source's stated absence); ``NotApplicable`` for a stream.
    - ``geodetic``: the message type whose definition makes the values geodetic coordinates
      (``sensor_msgs/NavSatFix``: latitude, longitude and altitude on the WGS 84 ellipsoid); its
      CRS is the type's, which no CRS code states.
    A subject with no frame, no CRS and no geodetic type has no origin: a finding says so.
    """

    kind: ClassVar[str] = REFERENCE_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    subject: RecordId
    frames: tuple[FrameCount, ...]
    unset: int
    crs: Knowledge[CrsCode]
    geodetic: str | None

    def __post_init__(self) -> None:
        _check(self.id, self.transform, self.evidence)
        parse_record_id(self.subject)
        keys = [_ref_key(count.frame) for count in self.frames]
        if keys != sorted(set(keys)):
            raise ValueError("a reference's frames are sorted and unique")
        if isinstance(self.unset, bool) or not isinstance(self.unset, int) or self.unset < 0:
            raise ValueError("unset is a non-negative row count")
        check_type("crs", self.crs, CrsCode)

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    @property
    def has_origin(self) -> bool:
        return bool(self.frames) or isinstance(self.crs, Known | Ambiguous) or bool(self.geodetic)

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            **_envelope(self.kind, self.id, self.provenance),
            "crs": to_json(self.crs, CrsCode.to_json),
            "frames": [count.to_json() for count in self.frames],
            "subject": self.subject,
            "unset": self.unset,
        }
        if self.geodetic is not None:
            out["geodetic"] = self.geodetic
        return out


def spatial_reference_from_json(data: JsonValue) -> SpatialReference:
    keys = {"crs", "evidence", "frames", "id", "subject", "transform", "unset"}
    if isinstance(data, Mapping) and "geodetic" in data:
        keys.add("geodetic")
    obj = derived_object(data, REFERENCE_KIND, keys)
    record_id, transform, evidence = _common(obj)
    return SpatialReference(
        record_id,
        transform,
        evidence,
        parse_record_id(json_str(obj["subject"], "subject")),
        tuple(_frame_count_from_json(f) for f in json_array(obj["frames"], "frames")),
        json_int(obj["unset"], "unset"),
        _knowledge(obj["crs"], crs_code_from_json),
        json_str(obj["geodetic"], "geodetic") if "geodetic" in obj else None,
    )


# --- Comparing ----------------------------------------------------------------------------------


class StepKind(StrEnum):
    TRANSFORM = "transform"  # a declared FrameTransform
    BINDING = "binding"  # a stated FrameBinding: two graphs' frames are one
    EDGE = "edge"  # a run tree's frame_edge
    LINK = "link"  # an inferred frame_link


class Caveat(StrEnum):
    """What a consumer must resolve before composing a path's values."""

    DIRECTION_UNKNOWN = "direction_unknown"
    DIRECTION_AMBIGUOUS = "direction_ambiguous"
    TRANSLATION_UNIT_UNKNOWN = "translation_unit_unknown"
    ROTATION_ORDER_UNKNOWN = "rotation_order_unknown"
    QUATERNION_CONVENTION_UNKNOWN = "quaternion_convention_unknown"
    TIME_DEPENDENT = "time_dependent"  # a step changes over time; no instant was given


class Basis(StrEnum):
    SAME_FRAME = "same_frame"
    CONNECTED = "connected"  # a chain of steps
    SAME_CRS = "same_crs"  # one CRS code, verbatim
    SAME_DEFINITION = "same_definition"  # one geodetic message type


class Reason(StrEnum):
    DISCONNECTED = "disconnected"  # no chain of steps joins the frames
    FRAME_UNKNOWN = "frame_unknown"  # a subject names no frame, or several
    CRS_UNKNOWN = "crs_unknown"
    CRS_AMBIGUOUS = "crs_ambiguous"
    CRS_DIFFERS = "crs_differs"  # two CRS codes: comparing needs a reprojection Neptune never does
    NO_GEOREFERENCE = "no_georeference"  # a frame and an earth reference: nothing places one
    OUTSIDE_COVERAGE = "outside_coverage"  # a time-dependent step has no sample around the instant
    UNSYNCHRONISED = "unsynchronised"  # the instant's clock does not align to a step's


@dataclass(frozen=True)
class Step:
    """One hop: ``via`` (a record or derived line id) from ``start`` to ``end``. ``inverse``: the
    hop runs against the declared parent → child (or left → right) order."""

    kind: StepKind
    via: RecordId
    start: FrameRef
    end: FrameRef
    inverse: bool


@dataclass(frozen=True)
class Comparable:
    basis: Basis
    path: tuple[Step, ...] = ()
    inferred: bool = False
    caveats: tuple[Caveat, ...] = ()


@dataclass(frozen=True)
class NotComparable:
    reason: Reason
    detail: str = ""


@dataclass(frozen=True)
class FrameAt:
    """A frame, optionally at an instant (for paths through steps that change over time)."""

    frame: FrameRef
    at: Timestamp | None = None


@dataclass(frozen=True)
class EarthAt:
    """An earth reference: a CRS (as a record states it) or a geodetic message type."""

    crs: Knowledge[CrsCode]
    geodetic: str | None = None


SpatialRef = FrameAt | EarthAt


@dataclass(frozen=True)
class _Hop:
    kind: StepKind
    via: RecordId
    to: FrameRef
    inverse: bool
    caveats: tuple[Caveat, ...]
    window: tuple[Timestamp, Timestamp] | None  # a dynamic edge: its first and last samples


def _transform_caveats(transform: FrameTransform) -> tuple[Caveat, ...]:
    found: list[Caveat] = []
    if isinstance(transform.direction, Ambiguous):
        found.append(Caveat.DIRECTION_AMBIGUOUS)
    elif not isinstance(transform.direction, Known):
        found.append(Caveat.DIRECTION_UNKNOWN)
    value = transform.value
    unit = value.translation.unit if isinstance(value, Pose) else value.translation_unit
    if not isinstance(unit, Known):
        found.append(Caveat.TRANSLATION_UNIT_UNKNOWN)
    if isinstance(value, HomogeneousMatrix):
        if not isinstance(value.layout, Known):
            found.append(Caveat.ROTATION_ORDER_UNKNOWN)
    else:
        rotation = value.rotation
        if isinstance(rotation, Quaternion):
            if not isinstance(rotation.order, Known):
                found.append(Caveat.ROTATION_ORDER_UNKNOWN)
            if not isinstance(rotation.convention, Known):
                found.append(Caveat.QUATERNION_CONVENTION_UNKNOWN)
        else:
            order = getattr(rotation, "layout", None) or getattr(rotation, "sequence", None)
            if order is not None and not isinstance(order, Known):
                found.append(Caveat.ROTATION_ORDER_UNKNOWN)
    return tuple(found)


def _edge_caveats(edge: FrameEdge) -> tuple[Caveat, ...]:
    found: list[Caveat] = []
    if not isinstance(edge.direction, Known):
        found.append(Caveat.DIRECTION_UNKNOWN)
    if not isinstance(edge.translation_unit, Known):
        found.append(Caveat.TRANSLATION_UNIT_UNKNOWN)
    if not isinstance(edge.quaternion_convention, Known):
        found.append(Caveat.QUATERNION_CONVENTION_UNKNOWN)
    return tuple(found)


class FrameIndex:
    """Frames as nodes; declared transforms, stated bindings and tree edges as evidence steps;
    links as inferred steps. Every step is usable both ways (an inverse is a step backward)."""

    def __init__(
        self,
        transforms: Iterable[FrameTransform] = (),
        bindings: Iterable[FrameBinding] = (),
        edges: Iterable[FrameEdge] = (),
        links: Iterable[FrameLink] = (),
        groups: Iterable[FrameGroup] = (),
    ) -> None:
        self._steps: dict[FrameRef, list[_Hop]] = {}
        self._links: dict[FrameRef, list[_Hop]] = {}
        by_id = {t.id: t for t in transforms}
        for transform in sorted(by_id.values(), key=lambda t: t.id):
            caveats = _transform_caveats(transform)
            self._add(StepKind.TRANSFORM, transform.id, transform.parent, transform.child, caveats)
        for binding in sorted({b.id: b for b in bindings}.values(), key=lambda b: b.id):
            bound = by_id.get(binding.transform)
            if bound is None:
                continue  # the transform it names is not in the package: no step
            for mine, theirs in ((binding.parent, bound.parent), (binding.child, bound.child)):
                if mine != theirs:
                    self._add(StepKind.BINDING, binding.id, mine, theirs, ())
        for edge in sorted({e.id: e for e in edges}.values(), key=lambda e: e.id):
            window = None if edge.persistence is Persistence.STATIC else (edge.first, edge.last)
            self._add(StepKind.EDGE, edge.id, edge.parent, edge.child, _edge_caveats(edge), window)
        for link in sorted({x.id: x for x in links}.values(), key=lambda x: x.id):
            self._links.setdefault(link.left, []).append(
                _Hop(StepKind.LINK, link.id, link.right, False, (), None)
            )
            self._links.setdefault(link.right, []).append(
                _Hop(StepKind.LINK, link.id, link.left, True, (), None)
            )
        self._earth = {ref: group.earth for group in groups for ref in group.members}

    def _add(
        self,
        kind: StepKind,
        via: RecordId,
        parent: FrameRef,
        child: FrameRef,
        caveats: tuple[Caveat, ...],
        window: tuple[Timestamp, Timestamp] | None = None,
    ) -> None:
        self._steps.setdefault(parent, []).append(_Hop(kind, via, child, False, caveats, window))
        self._steps.setdefault(child, []).append(_Hop(kind, via, parent, True, caveats, window))

    def frames(self) -> list[FrameRef]:
        return sorted({*self._steps, *self._links}, key=_ref_key)

    def compare(
        self,
        a: SpatialRef,
        b: SpatialRef,
        *,
        links: bool = False,
        clocks: ClockGraph | None = None,
    ) -> Comparable | NotComparable:
        """Whether values in ``a`` and ``b`` are comparable, and why (module docstring).

        Frames are joined breadth-first through evidence steps (declared transforms, stated
        bindings, tree edges), and only if none joins them and ``links`` is set, through links
        too, which makes the answer ``inferred``. An instant on either side bounds every
        time-dependent step: the step must have samples at or around it, on its stream's clock,
        reached through ``clocks`` when the instant is on another clock.
        """
        if isinstance(a, EarthAt) and isinstance(b, EarthAt):
            return _earth(a, b)
        if isinstance(a, EarthAt) or isinstance(b, EarthAt):
            frame = a if isinstance(a, FrameAt) else b
            assert isinstance(frame, FrameAt)
            earth = self._earth.get(frame.frame, Unknown())
            return NotComparable(
                Reason.NO_GEOREFERENCE,
                f"nothing places frame {frame.frame.frame_id!r} on the earth (its group's earth"
                f" reference is {earth.state})",
            )
        if a.frame == b.frame:
            return Comparable(Basis.SAME_FRAME)
        at = a.at if a.at is not None else b.at
        found = self._search(a.frame, b.frame, at, clocks, use_links=False)
        if isinstance(found, NotComparable) and found.reason is Reason.DISCONNECTED and links:
            found = self._search(a.frame, b.frame, at, clocks, use_links=True)
        return found

    def _search(
        self,
        start: FrameRef,
        goal: FrameRef,
        at: Timestamp | None,
        clocks: ClockGraph | None,
        use_links: bool,
    ) -> Comparable | NotComparable:
        previous: dict[FrameRef, tuple[FrameRef, _Hop] | None] = {start: None}
        queue = deque([start])
        blocked: NotComparable | None = None
        while queue:
            here = queue.popleft()
            hops = list(self._steps.get(here, ()))
            if use_links:
                hops += self._links.get(here, ())
            for hop in hops:
                if hop.to in previous:
                    continue
                if hop.window is not None and at is not None:
                    problem = _covers(hop.window, at, clocks)
                    if problem is not None:
                        blocked = blocked or problem
                        continue
                previous[hop.to] = (here, hop)
                if hop.to == goal:
                    return _path(previous, goal, at)
                queue.append(hop.to)
        if blocked is not None:
            return blocked
        return NotComparable(
            Reason.DISCONNECTED,
            f"no chain of {'steps' if use_links else 'transforms'} joins"
            f" {start.frame_id!r} and {goal.frame_id!r}",
        )


def _path(
    previous: Mapping[FrameRef, "tuple[FrameRef, _Hop] | None"],
    goal: FrameRef,
    at: Timestamp | None,
) -> Comparable:
    steps: list[Step] = []
    caveats: list[Caveat] = []
    inferred = False
    here = goal
    while (entry := previous[here]) is not None:
        before, hop = entry
        steps.append(Step(hop.kind, hop.via, before, here, hop.inverse))
        caveats += hop.caveats
        inferred = inferred or hop.kind is StepKind.LINK
        if hop.window is not None and at is None:
            caveats.append(Caveat.TIME_DEPENDENT)
        here = before
    steps.reverse()
    return Comparable(Basis.CONNECTED, tuple(steps), inferred, tuple(sorted(set(caveats), key=str)))


def _covers(
    window: tuple[Timestamp, Timestamp], at: Timestamp, clocks: ClockGraph | None
) -> NotComparable | None:
    first, last = window
    instant = at
    if at.domain_id != first.domain_id:
        if clocks is None:
            return NotComparable(
                Reason.UNSYNCHRONISED, "the instant is on another clock and no clock graph is given"
            )
        aligned = clocks.align(at, first.domain_id)
        if not isinstance(aligned, Aligned):
            return NotComparable(
                Reason.UNSYNCHRONISED, f"the clocks do not align: {aligned.reason}"
            )
        instant = aligned.instant
        span = aligned.window()
        if span is not None and (span[1].ticks < first.ticks or span[0].ticks > last.ticks):
            return NotComparable(Reason.OUTSIDE_COVERAGE, "the instant is outside a step's samples")
        if span is not None:
            return None
    if not first.ticks <= instant.ticks <= last.ticks:
        return NotComparable(Reason.OUTSIDE_COVERAGE, "the instant is outside a step's samples")
    return None


def _earth(a: EarthAt, b: EarthAt) -> Comparable | NotComparable:
    if a.geodetic is not None or b.geodetic is not None:
        if a.geodetic is not None and a.geodetic == b.geodetic:
            return Comparable(Basis.SAME_DEFINITION)
        return NotComparable(
            Reason.CRS_UNKNOWN, "a geodetic type's coordinates are stated by no CRS code"
        )
    for side in (a.crs, b.crs):
        if isinstance(side, Ambiguous):
            return NotComparable(Reason.CRS_AMBIGUOUS, "a CRS has several readings")
        if not isinstance(side, Known):
            return NotComparable(Reason.CRS_UNKNOWN, f"a CRS is {side.state}")
    assert isinstance(a.crs, Known) and isinstance(b.crs, Known)
    if a.crs.value == b.crs.value:
        return Comparable(Basis.SAME_CRS)
    return NotComparable(
        Reason.CRS_DIFFERS,
        f"{a.crs.value.authority}:{a.crs.value.code} and {b.crs.value.authority}:"
        f"{b.crs.value.code} differ; Neptune never reprojects",
    )


def references(reference: SpatialReference) -> list[SpatialRef]:
    """The references one subject's values may be in: one per frame its rows name, or its CRS
    or geodetic type. A subject with none has no reference to compare."""
    found: list[SpatialRef] = [FrameAt(count.frame) for count in reference.frames]
    if reference.geodetic is not None or isinstance(reference.crs, Known | Ambiguous):
        found.append(EarthAt(reference.crs, reference.geodetic))
    return found


def frame_index(records: Iterable[object], derived: Iterable[object] = ()) -> FrameIndex:
    """The index of a package's declared transforms and bindings and the edges, links and groups
    its derived tables hold (``neptune.derived.sessions.read_derived``), for ``compare``."""
    every = [*records, *derived]
    return FrameIndex(
        [r for r in every if isinstance(r, FrameTransform)],
        [r for r in every if isinstance(r, FrameBinding)],
        [r for r in every if isinstance(r, FrameEdge)],
        [r for r in every if isinstance(r, FrameLink)],
        [r for r in every if isinstance(r, FrameGroup)],
    )


def compare_subjects(
    index: FrameIndex,
    a: SpatialReference,
    b: SpatialReference,
    *,
    links: bool = False,
    clocks: ClockGraph | None = None,
) -> list[tuple[SpatialRef, SpatialRef, Comparable | NotComparable]]:
    """Every pair of the two subjects' references, compared. A subject that names no frame and
    states no CRS or geodetic type gives one ``frame_unknown`` answer: there is nothing to
    compare it by."""
    left, right = references(a), references(b)
    if not left or not right:
        missing = a.subject if not left else b.subject
        detail = f"{missing} names no frame and states no CRS"
        return [
            (
                left[0] if left else EarthAt(Unknown()),
                right[0] if right else EarthAt(Unknown()),
                NotComparable(Reason.FRAME_UNKNOWN, detail),
            )
        ]
    return [(x, y, index.compare(x, y, links=links, clocks=clocks)) for x in left for y in right]
