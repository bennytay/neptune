"""The frame-alignment pass over a package's records and series (ADR 0068).

``align_frames`` takes the records a package holds and a way to read its streams' series, and
returns, under its transform (``neptune.frames``), the tables ``neptune.derived.frames`` defines
and the findings of what does not hold together:

- **run trees**: per run, the frames its ``tf2_msgs/TFMessage`` streams and its streams' headers
  name, one graph (``frame_tree``); each ``parent → child`` pair a transform stream holds is a
  ``frame_edge``, static on tf2's static topic, with its samples' first and last instants;
- **links**: frames of a declared graph (a calibration's, a URDF's) named as a run tree's frame
  is (``same_name``), and names that differ by a leading ``/`` (``leading_slash``);
- **groups**: frames joined by declared transforms, stated bindings and tree edges, never by a
  link, each with its origin (one root, several, or none in a loop) and an ``Unknown`` earth
  reference;
- **spatial references**: per stream with a header and rows, the frames its rows name and how
  often (and the rows naming none), its geodetic type; per spatial artifact, site or asset, its
  declared frame and CRS;
- **findings**: a run's frames in groups no transform joins (``disconnected``), a frame with two
  parents, a loop, a static transform restated with other values, rows naming no frame, names
  differing by a leading ``/``, a subject with no frame and no CRS (``origin_unknown``).

Everything is ``inferred``: a run's frames being one graph is ROS's convention, a topic being
static is tf2's, a name match is a proposal. The pass reads only the time and value columns its
rules name, decodes nothing (the adapters did, ADR 0068 §1), never composes a transform, and
writes no source value: every line is a new record beside the evidence.
"""

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from neptune.derived.frames import (
    EDGE_KIND,
    GROUP_KIND,
    LINK_KIND,
    REFERENCE_KIND,
    TREE_KIND,
    FrameCount,
    FrameEdge,
    FrameGroup,
    FrameLink,
    FrameTree,
    LinkRule,
    Persistence,
    SpatialReference,
)
from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.ids import record_id
from neptune.identity.provenance import transform_record
from neptune.model.alignment import FrameBinding
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.frames import FrameRef, TransformDirection
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, TransformRecord
from neptune.model.reference import Frame, FrameGraph, FrameTransform, TimestampDomain
from neptune.model.run import Stream
from neptune.model.spatial import CrsCode
from neptune.model.time import Timestamp
from neptune.model.world import Asset, Site, SpatialArtifact

FRAMES_ID: Final = "neptune.frames"
FRAMES_VERSION: Final = "0.1.0"
_PREFIX: Final = "neptune.frames."
TF_TYPES: Final = frozenset({"tf2_msgs/TFMessage", "tf/tfMessage"})
GEODETIC_TYPES: Final = frozenset({"sensor_msgs/NavSatFix"})
HEADER_STAMP: Final = "header.stamp"
_LISTED: Final = 20  # entries a finding's details list before they are counted

_T = "value/transforms[]."
PARENT, CHILD = _T + "header.frame_id", _T + "child_frame_id"
VALUES: Final = tuple(
    _T + name
    for name in (
        "transform.translation.x",
        "transform.translation.y",
        "transform.translation.z",
        "transform.rotation.x",
        "transform.rotation.y",
        "transform.rotation.z",
        "transform.rotation.w",
    )
)
FRAME, CHILD_FRAME = "value/header.frame_id", "value/child_frame_id"
_KNOWN: Final = "known"

# The rows of a stream's series, only the named columns (those the series has), in any order.
RowReader = Callable[[Stream, Sequence[str]], Iterable[Mapping[str, object]]]


def _state(row: Mapping[str, object], column: str) -> bool:
    return column in row and row.get(f"state/{column}", _KNOWN) == _KNOWN


def _type_name(stream: Stream) -> str | None:
    """``pkg/Name`` for a declared ``pkg/msg/Name`` or ``pkg/Name``."""
    if not isinstance(stream.schema_name, Known):
        return None
    parts = stream.schema_name.value.split("/")
    if len(parts) == 3 and parts[1] == "msg":
        parts = [parts[0], parts[2]]
    return "/".join(parts)


def _topic(stream: Stream) -> str:
    return stream.topic.value if isinstance(stream.topic, Known) else ""


def is_static_topic(topic: str) -> bool:
    """tf2 publishes static transforms on ``tf_static``, under any namespace."""
    return topic.rstrip("/").split("/")[-1] == "tf_static"


def _same(a: tuple[object, ...], b: tuple[object, ...]) -> bool:
    """Two transforms' values are one statement: equal numbers, NaN counted equal to NaN (a
    restated NaN is the same statement, not a change)."""

    def norm(value: object) -> object:
        return "nan" if isinstance(value, float) and math.isnan(value) else value

    return [norm(x) for x in a] == [norm(x) for x in b]


def _key(ref: FrameRef) -> tuple[str, str]:
    return (ref.frame_graph_id, ref.frame_id)


def _sorted_refs(refs: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    return tuple(sorted(set(refs), key=lambda ref: canonical_json.dumps(ref.to_json())))


@dataclass
class _Edge:
    stream: Stream
    parent: str
    child: str
    samples: int = 0
    first: int | None = None
    last: int | None = None
    value: tuple[object, ...] | None = None
    changed: bool = False


@dataclass
class _Tree:
    run: RecordId
    id: RecordId
    streams: list[Stream] = field(default_factory=list)
    frames: set[str] = field(default_factory=set)
    edges: dict[tuple[RecordId, str, str], _Edge] = field(default_factory=dict)
    unset: dict[RecordId, int] = field(default_factory=dict)
    untimed: int = 0
    unrepresentable: set[str] = field(default_factory=set)


class _Union:
    """Union-find over hashable nodes, deterministic: the smaller key is the representative."""

    def __init__(self) -> None:
        self.parent: dict[FrameRef, FrameRef] = {}

    def add(self, node: FrameRef) -> None:
        self.parent.setdefault(node, node)

    def find(self, node: FrameRef) -> FrameRef:
        self.add(node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, a: FrameRef, b: FrameRef) -> bool:
        """Join ``a`` and ``b``; ``False`` when they already were (a second path: a loop)."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        low, high = sorted((ra, rb), key=_key)
        self.parent[high] = low
        return True


@dataclass(frozen=True)
class FrameAlignment:
    transform: TransformRecord
    trees: tuple[FrameTree, ...]
    edges: tuple[FrameEdge, ...]
    links: tuple[FrameLink, ...]
    groups: tuple[FrameGroup, ...]
    references: tuple[SpatialReference, ...]
    findings: tuple[IngestFinding, ...]

    def tables(self) -> dict[str, Iterator[JsonObject]]:
        """The package's five derived tables, each in id order (ADR 0036 §8)."""
        tables: dict[
            str, Iterable[FrameTree | FrameEdge | FrameLink | FrameGroup | SpatialReference]
        ]
        tables = {
            TREE_KIND: self.trees,
            EDGE_KIND: self.edges,
            LINK_KIND: self.links,
            GROUP_KIND: self.groups,
            REFERENCE_KIND: self.references,
        }
        return {
            kind: (line.to_json() for line in sorted(lines, key=lambda r: r.id))
            for kind, lines in tables.items()
        }

    def summary(self) -> JsonObject:
        return {
            "edges": len(self.edges),
            "findings": len(self.findings),
            "groups": len(self.groups),
            "links": len(self.links),
            "references": len(self.references),
            "trees": len(self.trees),
        }


_Input = (
    Stream
    | TimestampDomain
    | FrameGraph
    | Frame
    | FrameTransform
    | FrameBinding
    | SpatialArtifact
    | Site
    | Asset
)


def frame_records(records: Iterable[object]) -> Iterator[_Input]:
    """The records ``align_frames`` reads. A caller gathering a package chunk by chunk keeps only
    these, not every record."""
    return (r for r in records if isinstance(r, _Input))


class _Pass:
    def __init__(self, upstream: Iterable[RecordId]) -> None:
        self.transform = transform_record(
            adapter_id=FRAMES_ID,
            adapter_version=FRAMES_VERSION,
            config={},
            upstream=sorted(set(upstream)),
        )
        self.found: list[IngestFinding] = []
        self.evidence: dict[FrameRef, list[EvidenceRef]] = defaultdict(list)

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        message: str,
        details: Mapping[str, JsonValue],
        records: Iterable[RecordId],
    ) -> None:
        self.found.append(
            ingest_finding(
                code=_PREFIX + code,
                category=category,
                severity=severity,
                subject=subject,
                transform=self.transform,
                message=message,
                details=details,
                records=sorted(set(records)),
            )
        )

    def ref(self, tree: _Tree, frame: str) -> FrameRef | None:
        try:
            return FrameRef(frame, tree.id)
        except ValueError:
            tree.unrepresentable.add(frame[:64])
            return None

    # -- reading the trees --

    def read_transforms(self, tree: _Tree, stream: Stream, rows: RowReader) -> None:
        static = is_static_topic(_topic(stream))
        columns = (
            "time/0",
            "state/time/0",
            PARENT,
            f"state/{PARENT}",
            CHILD,
            f"state/{CHILD}",
            *VALUES,
            *(f"state/{column}" for column in VALUES),
        )
        for row in rows(stream, columns):
            if not (_state(row, PARENT) and _state(row, CHILD)):
                continue
            parents, children = row[PARENT], row[CHILD]
            if not isinstance(parents, list | tuple) or not isinstance(children, list | tuple):
                continue
            tick = row.get("time/0") if _state(row, "time/0") else None
            values = [row.get(c) if _state(row, c) else None for c in VALUES]
            for k, (parent, child) in enumerate(zip(parents, children, strict=False)):
                if not isinstance(parent, str) or not isinstance(child, str):
                    continue
                if not parent or not child:
                    tree.unset[stream.id] = tree.unset.get(stream.id, 0) + 1
                    continue
                if parent == child:
                    continue
                if not isinstance(tick, int) or isinstance(tick, bool):
                    tree.untimed += 1  # no instant to place it at: no sample of any edge
                    continue
                edge = tree.edges.setdefault(
                    (stream.id, parent, child), _Edge(stream, parent, child)
                )
                edge.samples += 1
                edge.first = tick if edge.first is None else min(edge.first, tick)
                edge.last = tick if edge.last is None else max(edge.last, tick)
                if static:
                    value = tuple(
                        v[k] if isinstance(v, list | tuple) and k < len(v) else None for v in values
                    )
                    if edge.value is None:
                        edge.value = value
                    elif not _same(value, edge.value):
                        edge.changed = True
                tree.frames.update((parent, child))

    def read_headers(
        self, tree: _Tree, stream: Stream, rows: RowReader
    ) -> tuple[dict[str, int], int, int]:
        """The frames a header stream's rows name (header and, for odometry, child), counted,
        the rows whose header names none, and the rows read."""
        counts: dict[str, int] = {}
        unset = read = 0
        columns = (FRAME, f"state/{FRAME}", CHILD_FRAME, f"state/{CHILD_FRAME}")
        for row in rows(stream, columns):
            read += 1
            for column in (FRAME, CHILD_FRAME):
                if not _state(row, column):
                    continue
                value = row[column]
                if not isinstance(value, str):
                    continue
                if not value:
                    if column == FRAME:
                        unset += 1
                    continue
                counts[value] = counts.get(value, 0) + 1
        tree.frames.update(counts)
        if unset:
            tree.unset[stream.id] = tree.unset.get(stream.id, 0) + unset
        return counts, unset, read


def _crs(state: Knowledge[CrsCode]) -> Knowledge[CrsCode]:
    """A record's CRS as a derived state: its value or candidates, without their provenance (a
    derived state inherits the line's); a stated absence is ``NotApplicable``."""
    match state:
        case Known(value=value):
            return Known(value)
        case Ambiguous(candidates=candidates):
            return Ambiguous(tuple(Candidate(c.value) for c in candidates))
        case KnownAbsent():
            return NotApplicable()
        case NotCovered():
            return NotCovered()
        case NotApplicable():
            return NotApplicable()
        case _:
            return Unknown()


def align_frames(records: Iterable[object], rows: RowReader) -> FrameAlignment | None:
    """Align a package's frames (module docstring); ``None`` when nothing in it is spatial: no
    stream names a frame, no record declares one or a CRS. Deterministic: the same records and
    rows give the same lines and findings."""
    records = list(records)
    by_id: dict[RecordId, _Input] = {r.id: r for r in frame_records(records)}
    streams = sorted((r for r in by_id.values() if isinstance(r, Stream)), key=lambda s: s.id)
    domains = {r.id: r for r in by_id.values() if isinstance(r, TimestampDomain)}
    transforms = sorted(
        (r for r in by_id.values() if isinstance(r, FrameTransform)), key=lambda r: r.id
    )
    bindings = sorted(
        (r for r in by_id.values() if isinstance(r, FrameBinding)), key=lambda r: r.id
    )
    frames = sorted((r for r in by_id.values() if isinstance(r, Frame)), key=lambda r: r.id)
    artifacts = sorted(
        (r for r in by_id.values() if isinstance(r, SpatialArtifact | Site | Asset)),
        key=lambda r: r.id,
    )
    tf_streams: list[Stream] = []
    header_streams: list[Stream] = []
    for stream in streams:
        name = _type_name(stream)
        if name in TF_TYPES:
            tf_streams.append(stream)
        elif any(
            clock in domains and domains[clock].field == HEADER_STAMP for clock in stream.clocks
        ):
            header_streams.append(stream)
    spatial_artifacts = [
        a
        for a in artifacts
        if isinstance(a, SpatialArtifact) or isinstance(a.location, Known)  # a located site
    ]
    if not (tf_streams or header_streams or transforms or frames or spatial_artifacts):
        return None
    upstream = [s.provenance.transform for s in (*tf_streams, *header_streams)]
    upstream += [r.provenance.transform for r in transforms]
    upstream += [r.provenance.transform for r in bindings]
    upstream += [r.provenance.transform for r in frames]
    upstream += [a.provenance.transform for a in spatial_artifacts]
    work = _Pass(upstream)
    return _run(
        work, tf_streams, header_streams, transforms, bindings, frames, spatial_artifacts, rows
    )


def _run(
    work: _Pass,
    tf_streams: list[Stream],
    header_streams: list[Stream],
    transforms: list[FrameTransform],
    bindings: list[FrameBinding],
    frames: list[Frame],
    artifacts: list[SpatialArtifact | Site | Asset],
    rows: RowReader,
) -> FrameAlignment:
    transform_id = work.transform.id
    trees: dict[RecordId, _Tree] = {}

    def tree_of(stream: Stream) -> _Tree:
        if stream.run not in trees:
            tree_id = record_id(TREE_KIND, {"run": stream.run, "transform": transform_id})
            trees[stream.run] = _Tree(stream.run, tree_id)
        return trees[stream.run]

    for stream in tf_streams:
        tree = tree_of(stream)
        tree.streams.append(stream)
        work.read_transforms(tree, stream, rows)
    header_counts: dict[RecordId, tuple[dict[str, int], int, int]] = {}
    for stream in header_streams:
        tree = tree_of(stream)
        tree.streams.append(stream)
        header_counts[stream.id] = work.read_headers(tree, stream, rows)

    union = _Union()
    parents: dict[FrameRef, set[FrameRef]] = defaultdict(set)
    dynamic: set[FrameRef] = set()
    edge_lines: list[FrameEdge] = []
    tree_lines: list[FrameTree] = []
    tree_frames: dict[RecordId, list[FrameRef]] = {}
    for run in sorted(trees):
        tree = trees[run]
        evidence = _sorted_refs(s.provenance.evidence for s in tree.streams)
        tree_lines.append(
            FrameTree(
                tree.id,
                transform_id,
                evidence,
                run,
                tuple(sorted({s.id for s in tree.streams})),
            )
        )
        refs = {}
        for name in sorted(tree.frames):
            ref = work.ref(tree, name)
            if ref is not None:
                refs[name] = ref
                union.add(ref)
        tree_frames[tree.id] = sorted(refs.values(), key=_key)
        for stream in tree.streams:
            for name in header_counts.get(stream.id, ({}, 0, 0))[0]:
                if name in refs:
                    work.evidence[refs[name]].append(stream.provenance.evidence)
        _tree_edges(work, tree, refs, union, parents, dynamic, edge_lines)
        for ref in refs.values():
            if not work.evidence[ref]:  # named only by transforms that set no edge
                work.evidence[ref].append(evidence[0])
        _tree_findings(work, tree, refs, evidence)

    # Declared graphs: transforms, frames and stated bindings.
    declared: dict[FrameRef, None] = {}
    for transform in transforms:
        for ref in (transform.parent, transform.child):
            declared.setdefault(ref)
            work.evidence[ref].append(transform.provenance.evidence)
        union.union(transform.parent, transform.child)
        parents[transform.child].add(transform.parent)
    for frame in frames:
        declared.setdefault(frame.ref)
        union.add(frame.ref)
        work.evidence[frame.ref].append(frame.provenance.evidence)
    by_transform = {t.id: t for t in transforms}
    identities = _Union()
    for binding in bindings:
        bound = by_transform.get(binding.transform)
        for ref in (binding.parent, binding.child):
            union.add(ref)
            work.evidence[ref].append(binding.provenance.evidence)
        if bound is None:
            continue
        for mine, theirs in ((binding.parent, bound.parent), (binding.child, bound.child)):
            union.union(mine, theirs)
            identities.union(mine, theirs)
    references = _references(work, header_streams, header_counts, trees, artifacts, union)
    for reference in references:
        for count in reference.frames:
            if count.frame not in work.evidence:
                work.evidence[count.frame].extend(reference.evidence)
    links = _links(work, sorted(declared, key=_key), trees, tree_frames)
    groups = _groups(work, union, parents, identities, dynamic)
    _disconnected(work, trees, tree_frames, union)
    return FrameAlignment(
        work.transform,
        tuple(sorted(tree_lines, key=lambda r: r.id)),
        tuple(sorted(edge_lines, key=lambda r: r.id)),
        tuple(sorted(links, key=lambda r: r.id)),
        tuple(sorted(groups, key=lambda r: r.id)),
        tuple(sorted(references, key=lambda r: r.id)),
        tuple(sorted(work.found, key=lambda f: f.id)),
    )


def _tree_edges(
    work: _Pass,
    tree: _Tree,
    refs: Mapping[str, FrameRef],
    union: _Union,
    parents: dict[FrameRef, set[FrameRef]],
    dynamic: set[FrameRef],
    out: list[FrameEdge],
) -> None:
    transform_id = work.transform.id
    loops: list[list[str]] = []
    joined: set[tuple[str, str]] = set()
    changed: list[FrameEdge] = []
    for key in sorted(tree.edges):
        edge = tree.edges[key]
        parent, child = refs.get(edge.parent), refs.get(edge.child)
        if parent is None or child is None or edge.first is None or edge.last is None:
            continue
        clock = edge.stream.clocks[0]
        static = is_static_topic(_topic(edge.stream))
        line = FrameEdge(
            record_id(
                EDGE_KIND,
                {
                    "child": edge.child,
                    "parent": edge.parent,
                    "stream": edge.stream.id,
                    "transform": transform_id,
                },
            ),
            transform_id,
            (edge.stream.provenance.evidence,),
            tree.id,
            edge.stream.id,
            parent,
            child,
            Persistence.STATIC if static else Persistence.DYNAMIC,
            Known(TransformDirection.CHILD_TO_PARENT),
            Unknown(),
            Unknown(),
            edge.samples,
            Timestamp(edge.first, clock),
            Timestamp(edge.last, clock),
        )
        out.append(line)
        work.evidence[parent].append(edge.stream.provenance.evidence)
        work.evidence[child].append(edge.stream.provenance.evidence)
        parents[child].add(parent)
        if not static:
            dynamic.update((parent, child))
        if (edge.child, edge.parent) in joined:
            loops.append([edge.parent, edge.child])  # stated both ways: a loop of two
        elif (edge.parent, edge.child) not in joined:
            joined.add((edge.parent, edge.child))
            if not union.union(parent, child):
                loops.append([edge.parent, edge.child])
        if edge.changed:
            changed.append(line)
    subject = tree.streams[0].provenance.evidence
    many = {
        child.frame_id: sorted(p.frame_id for p in found)
        for child, found in parents.items()
        if child.frame_graph_id == tree.id and len(found) > 1
    }
    if many:
        work.finding(
            "multiple_parents",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            subject,
            f"{len(many)} frame(s) of a run's tree have more than one parent; a tf tree gives a"
            " frame one parent, so a lookup through them depends on which transform is read",
            {"frames": {k: many[k] for k in sorted(many)[:_LISTED]}, "count": len(many)},
            [tree.id, *(s.id for s in tree.streams)],
        )
    if loops:
        work.finding(
            "loop",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            subject,
            f"{len(loops)} transform(s) close a loop in a run's tree: two chains join the same"
            " frames, which a tf tree does not allow",
            {"pairs": loops[:_LISTED], "count": len(loops)},
            [tree.id, *(s.id for s in tree.streams)],
        )
    for line in changed:
        work.finding(
            "static_changed",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            line.evidence[0],
            f"the static transform {line.parent.frame_id!r} -> {line.child.frame_id!r} is stated"
            " again with other values; a static transform holds until restated, so which one"
            " applies depends on time",
            {"child": line.child.frame_id, "edge": line.id, "parent": line.parent.frame_id},
            [line.stream, tree.id],
        )


def _tree_findings(
    work: _Pass, tree: _Tree, refs: Mapping[str, FrameRef], evidence: tuple[EvidenceRef, ...]
) -> None:
    subject = evidence[0]
    records = [tree.id, *(s.id for s in tree.streams)]
    if tree.unset:
        work.finding(
            "frame_unset",
            FindingCategory.MISSING,
            Severity.INFO,
            subject,
            f"{sum(tree.unset.values())} row(s) or transform(s) of a run name no frame (an empty"
            " frame id); their values are in no known frame",
            {"streams": {k: tree.unset[k] for k in sorted(tree.unset)}},
            [*records, *tree.unset],
        )
    if tree.untimed:
        work.finding(
            "untimed_transforms",
            FindingCategory.MISSING,
            Severity.INFO,
            subject,
            f"{tree.untimed} transform(s) are in rows whose clock-0 time is unknown; with no"
            " instant to place them at, they are no edge's samples",
            {"count": tree.untimed},
            records,
        )
    if tree.unrepresentable:
        shown = sorted(tree.unrepresentable)
        work.finding(
            "frame_unrepresentable",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            subject,
            f"{len(shown)} frame id(s) are longer than a frame reference may be; they are left out",
            {"frames": shown[:_LISTED], "count": len(shown)},
            records,
        )
    variants = sorted(
        (refs[name[1:]].frame_id, refs[name].frame_id)
        for name in refs
        if name.startswith("/") and name[1:] in refs
    )
    if variants:
        work.finding(
            "name_variants",
            FindingCategory.AMBIGUOUS,
            Severity.WARNING,
            subject,
            f"{len(variants)} frame name(s) of a run appear with and without a leading '/' (a"
            " ROS 1 habit); they are two frames, joined only by an inferred link",
            {"pairs": [list(pair) for pair in variants[:_LISTED]], "count": len(variants)},
            records,
        )


def _references(
    work: _Pass,
    header_streams: list[Stream],
    counts: Mapping[RecordId, tuple[dict[str, int], int, int]],
    trees: Mapping[RecordId, _Tree],
    artifacts: list[SpatialArtifact | Site | Asset],
    union: _Union,
) -> list[SpatialReference]:
    transform_id = work.transform.id
    found: list[SpatialReference] = []
    for stream in header_streams:
        tree = trees[stream.run]
        named, unset, read = counts[stream.id]
        if not read:
            continue  # no row: no value to place
        frames = []
        for name in sorted(named):
            try:
                ref = FrameRef(name, tree.id)
            except ValueError:
                continue
            union.add(ref)
            frames.append(FrameCount(ref, named[name]))
        type_name = _type_name(stream)
        found.append(
            SpatialReference(
                record_id(REFERENCE_KIND, {"subject": stream.id, "transform": transform_id}),
                transform_id,
                (stream.provenance.evidence,),
                stream.id,
                tuple(sorted(frames, key=lambda c: _key(c.frame))),
                unset,
                NotApplicable(),
                type_name if type_name in GEODETIC_TYPES else None,
            )
        )
    for record in artifacts:
        frames = []
        if isinstance(record, SpatialArtifact):
            crs = _crs(record.crs)
            if isinstance(record.frame, Known):
                frames.append(FrameCount(record.frame.value))
                union.add(record.frame.value)
        else:
            location = record.location
            assert isinstance(location, Known)
            crs = _crs(location.value.crs)
        found.append(
            SpatialReference(
                record_id(REFERENCE_KIND, {"subject": record.id, "transform": transform_id}),
                transform_id,
                (record.provenance.evidence,),
                record.id,
                tuple(frames),
                0,
                crs,
                None,
            )
        )
    for reference in found:
        if reference.has_origin:
            continue
        work.finding(
            "origin_unknown",
            FindingCategory.MISSING,
            Severity.INFO,
            reference.evidence[0],
            "a subject's spatial values name no frame and state no CRS: nothing says where their"
            " origin is, so they compare with nothing",
            {"crs": str(reference.crs.state), "rows_unset": reference.unset},
            [reference.subject],
        )
    return found


def _links(
    work: _Pass,
    declared: list[FrameRef],
    trees: Mapping[RecordId, _Tree],
    tree_frames: Mapping[RecordId, list[FrameRef]],
) -> list[FrameLink]:
    transform_id = work.transform.id
    found: dict[RecordId, FrameLink] = {}

    def link(a: FrameRef, b: FrameRef, rule: LinkRule) -> None:
        left, right = sorted((a, b), key=_key)
        line_id = record_id(
            LINK_KIND,
            {
                "left": left.to_json(),
                "right": right.to_json(),
                "rule": str(rule),
                "transform": transform_id,
            },
        )
        evidence = _sorted_refs([*work.evidence[left][:1], *work.evidence[right][:1]])
        found[line_id] = FrameLink(line_id, transform_id, evidence, left, right, rule)

    for refs in tree_frames.values():
        names = {ref.frame_id: ref for ref in refs}
        for name, ref in names.items():
            if name.startswith("/") and name[1:] in names:
                link(names[name[1:]], ref, LinkRule.LEADING_SLASH)
    for ref in declared:
        for _, refs in sorted(tree_frames.items()):
            names = {r.frame_id: r for r in refs}
            if ref.frame_id in names:
                link(ref, names[ref.frame_id], LinkRule.SAME_NAME)
                continue
            other = ref.frame_id[1:] if ref.frame_id.startswith("/") else "/" + ref.frame_id
            if other in names:
                link(ref, names[other], LinkRule.LEADING_SLASH)
    return list(found.values())


def _groups(
    work: _Pass,
    union: _Union,
    parents: Mapping[FrameRef, set[FrameRef]],
    identities: _Union,
    dynamic: set[FrameRef],
) -> list[FrameGroup]:
    transform_id = work.transform.id
    members: dict[FrameRef, list[FrameRef]] = defaultdict(list)
    for node in list(union.parent):
        members[union.find(node)].append(node)
    found: list[FrameGroup] = []
    for root in sorted(members, key=_key):
        group = sorted(members[root], key=_key)
        # Frames one stated binding makes one are one class; a root class has no parent outside.
        classes: dict[FrameRef, list[FrameRef]] = defaultdict(list)
        for ref in group:
            classes[identities.find(ref) if ref in identities.parent else ref].append(ref)
        roots = []
        for head, refs in sorted(classes.items(), key=lambda item: _key(item[0])):
            outside = {
                identities.find(p) if p in identities.parent else p
                for ref in refs
                for p in parents.get(ref, ())
            } - {head}
            if not outside:
                roots.append(min(refs, key=_key))
        origin: Knowledge[FrameRef]
        if len(roots) == 1:
            origin = Known(roots[0])
        elif roots:
            origin = Ambiguous(tuple(Candidate(r) for r in roots))
        else:
            origin = Unknown()
        evidence = _sorted_refs(ref for member in group for ref in work.evidence.get(member, ()))
        found.append(
            FrameGroup(
                record_id(
                    GROUP_KIND,
                    {"first": [root.frame_graph_id, root.frame_id], "transform": transform_id},
                ),
                transform_id,
                evidence,
                tuple(group),
                origin,
                Unknown(),
                any(member in dynamic for member in group),
            )
        )
    return found


def _disconnected(
    work: _Pass,
    trees: Mapping[RecordId, _Tree],
    tree_frames: Mapping[RecordId, list[FrameRef]],
    union: _Union,
) -> None:
    for run in sorted(trees):
        tree = trees[run]
        refs = tree_frames[tree.id]
        groups: dict[FrameRef, list[str]] = defaultdict(list)
        for ref in refs:
            groups[union.find(ref)].append(ref.frame_id)
        if len(groups) < 2:
            continue
        listed = sorted(sorted(names) for names in groups.values())
        work.finding(
            "disconnected",
            FindingCategory.MISSING,
            Severity.INFO,
            tree.streams[0].provenance.evidence,
            f"a run's frames form {len(listed)} groups that no transform joins; values in"
            " different groups cannot be compared",
            {"count": len(listed), "groups": listed[:_LISTED]},
            [tree.id, *(s.id for s in tree.streams)],
        )


__all__ = [
    "FRAMES_ID",
    "FrameAlignment",
    "RowReader",
    "align_frames",
    "frame_records",
    "is_static_topic",
]
