"""Alignment records: what evidence says relates records of other families (ADR 0050).

Every record here is an evidence record (ADR 0017) of the ``alignment`` family. Each relates
records, ids or clocks that other records declare, and each says so only as far as cited evidence
says it: a fleet register naming one drone by two ids, a rosbag2 ``metadata.yaml`` naming its
storage files, a calibration naming the edge its transform gives. Its ``provenance`` cites that
declaration. A relation that a procedure estimates (a fitted clock drift, a session grouped by
folder, two frames matched by name) is inferred and lives in ``derived/``, in the same shape
(ADR 0050 §2). None of these records merges, rewrites or re-times anything: they are edges that
consumers may traverse.

- ``IdentityLink``: two logical ids name one thing. Never a merge (ADR 0003, AGENTS.md).
- ``ClockMapping``: an affine map from one clock's ticks to another's, with a residual bound.
- ``FrameBinding``: which ``FrameTransform`` record gives one edge of a frame graph its value.
- ``RunAssembly``: which source files form one run, each with the evidence for its membership.
- ``SnapshotBinding``: which machine-context snapshot a run ran with.

Each carries ``validity``: the half-open world-time window ``[start, end)`` on one named clock in
which the relation holds, as the evidence states it.
"""

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any, ClassVar, Final

from neptune.model._fields import (
    check_type,
    enum_decoder,
    exact_object,
    json_array,
    json_str,
    values_of,
)
from neptune.model.frames import FrameRef, frame_ref_from_json
from neptune.model.ids import (
    LogicalId,
    RecordId,
    check_token,
    logical_id_from_json,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Knowledge,
    Known,
    NotApplicable,
    from_json,
    to_json,
)
from neptune.model.provenance import (
    EvidenceRef,
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    evidence_ref_from_json,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.time import (
    Duration,
    Timestamp,
    duration_from_json,
    resolution_from_json,
    resolution_to_json,
    timestamp_from_json,
)

# The schema version that added these kinds (ADR 0050 §9, ADR 0037 §1).
ALIGNMENT_SINCE: Final = 3


def _record_id(data: JsonValue) -> RecordId:
    return parse_record_id(json_str(data, "record id"))


def _record_id_json(value: RecordId) -> JsonValue:
    return value


# --- Validity windows ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidityWindow:
    """When a relation holds: ``[start, end)`` on the clock ``clock`` names (ADR 0050 §3).

    ``clock`` is a ``TimestampDomain`` record id, and every instant the bounds state is on it.
    ``start`` is inclusive and ``end`` exclusive, so consecutive windows share no instant. A
    declared inclusive last instant is written as the next tick, exactly. Each bound is ``Known``
    where the evidence states it, ``KnownAbsent`` where it states the window is open on that side,
    ``Unknown`` where it could and does not, and ``NotCovered`` where it has no place to.
    """

    clock: RecordId
    start: Knowledge[Timestamp]
    end: Knowledge[Timestamp]

    def __post_init__(self) -> None:
        parse_record_id(self.clock)
        for name in ("start", "end"):
            bound: Knowledge[Timestamp] = getattr(self, name)
            check_type(name, bound, Timestamp)
            for stamp in values_of(bound):
                if stamp.domain_id != self.clock:
                    raise ValueError(f"the window's {name} is not on its clock {self.clock}")
        start, end = self.start, self.end
        if isinstance(start, Known) and isinstance(end, Known) and not start.value < end.value:
            raise ValueError("a window's start must be before its end; [start, end) is empty")

    def to_json(self) -> JsonObject:
        return {
            "clock": self.clock,
            "end": to_json(self.end, Timestamp.to_json),
            "start": to_json(self.start, Timestamp.to_json),
        }


def validity_window_from_json(data: JsonValue) -> ValidityWindow:
    obj = exact_object(data, "validity window", {"clock", "end", "start"})
    return ValidityWindow(
        clock=_record_id(obj["clock"]),
        start=from_json(obj["start"], timestamp_from_json, provenance_from_json),
        end=from_json(obj["end"], timestamp_from_json, provenance_from_json),
    )


def _check_validity(validity: Knowledge[ValidityWindow], clock: RecordId | None = None) -> None:
    check_type("validity", validity, ValidityWindow)
    for window in values_of(validity):
        if clock is not None and window.clock != clock:
            raise ValueError(f"validity must be on {clock}, got a window on {window.clock}")


def _validity_json(validity: Knowledge[ValidityWindow]) -> JsonObject:
    return to_json(validity, ValidityWindow.to_json)


def _validity(data: JsonValue) -> Knowledge[ValidityWindow]:
    return from_json(data, validity_window_from_json, provenance_from_json)


def _stated(field: str, knowledge: Knowledge[Any]) -> None:
    """A relation's other end is stated: ``Known``, or ``Ambiguous`` among candidates."""
    if not isinstance(knowledge, Known | Ambiguous):
        raise ValueError(
            f"{field} is what the evidence states, Known or Ambiguous; got {knowledge!r}"
        )


# --- Identity links -----------------------------------------------------------------------------


class LinkBasis(StrEnum):
    """Why the evidence says two logical ids name one thing (ADR 0050 §4)."""

    # One declaration names the thing by both ids: a fleet register row giving an asset tag and
    # the flight controller's sys_uuid. ``identifier`` is NotApplicable.
    CO_DECLARED = "co_declared"
    # Two declarations each name their thing by ``identifier``, verbatim and in one namespace.
    # ``provenance`` cites the left side's declaration and ``evidence`` the right side's.
    SHARED_IDENTIFIER = "shared_identifier"


@dataclass(frozen=True)
class IdentityLink:
    """Evidence that ``left`` and ``right`` name one real-world thing. Never a merge.

    Records, threads and nodes keyed by either id stay apart; a consumer may traverse the link
    and must keep both ids (ADR 0003, ADR 0050 §4). ``right`` is ``Known``, or ``Ambiguous`` with
    every id the evidence could mean (a register row naming a robot whose tag two machines carry):
    the link never picks one. ``identifier`` is the id both declarations give for a
    ``shared_identifier`` link and ``NotApplicable`` for a ``co_declared`` one. ``evidence`` lists
    every other declaration the link rests on, in the order they were read; ``provenance``'s own
    evidence is not repeated.
    """

    kind: ClassVar[str] = "identity_link"
    family: ClassVar[Family] = Family.ALIGNMENT
    since: ClassVar[int] = ALIGNMENT_SINCE
    id: RecordId
    provenance: Provenance
    left: LogicalId
    right: Knowledge[LogicalId]
    basis: LinkBasis
    identifier: Knowledge[LogicalId]
    evidence: tuple[EvidenceRef, ...]
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        if not isinstance(self.left, LogicalId):
            raise TypeError(f"left must be a LogicalId, got {type(self.left).__name__}")
        _stated("right", self.right)
        check_type("right", self.right, LogicalId)
        if self.left in values_of(self.right):
            raise ValueError(f"an identity link relates two ids; {self.left} is on both sides")
        if not isinstance(self.basis, LinkBasis):
            raise TypeError(f"basis must be a LinkBasis, got {self.basis!r}")
        check_type("identifier", self.identifier, LogicalId)
        if not isinstance(self.evidence, tuple):
            raise TypeError(f"evidence must be a tuple, got {type(self.evidence).__name__}")
        for ref in self.evidence:
            if not isinstance(ref, EvidenceRef):
                raise TypeError(f"evidence must be EvidenceRefs, got {ref!r}")
        refs = (self.provenance.evidence, *self.evidence)
        if len(set(refs)) != len(refs):
            raise ValueError("evidence repeats a citation, or repeats the record's own")
        if self.basis is LinkBasis.CO_DECLARED:
            if not isinstance(self.identifier, NotApplicable):
                raise ValueError("a co_declared link rests on no shared identifier")
        else:
            _stated("identifier", self.identifier)
            if not self.evidence:
                raise ValueError("a shared_identifier link cites the right side's declaration")
        _check_validity(self.validity)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "basis": str(self.basis),
                "evidence": [ref.to_json() for ref in self.evidence],
                "identifier": to_json(self.identifier, LogicalId.to_json),
                "left": self.left.to_json(),
                "right": to_json(self.right, LogicalId.to_json),
                "validity": _validity_json(self.validity),
            },
            self.since,
        )


def identity_link_from_json(data: JsonValue) -> IdentityLink:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        IdentityLink.kind,
        {"basis", "evidence", "identifier", "left", "right", "validity"},
        IdentityLink.since,
    )
    return IdentityLink(
        id=record_id,
        provenance=provenance,
        left=logical_id_from_json(obj["left"]),
        right=from_json(obj["right"], logical_id_from_json, provenance_from_json),
        basis=LinkBasis(json_str(obj["basis"], "basis")),
        identifier=from_json(obj["identifier"], logical_id_from_json, provenance_from_json),
        evidence=tuple(evidence_ref_from_json(r) for r in json_array(obj["evidence"], "evidence")),
        validity=_validity(obj["validity"]),
    )


# --- Clock mappings -----------------------------------------------------------------------------


class MappingMethod(StrEnum):
    """How the evidence relates the two clocks (ADR 0050 §5)."""

    # The source states the relation: an offset and rate in a sync log, a manifest, or a format
    # specification that defines one clock by another (rosbag2's starting_time by its storage's
    # receive times).
    STATED = "stated"
    # The source says two fields of one sample are the same instant on two clocks (a PTP
    # follow-up's origin and receipt stamps). Two times merely written side by side are not.
    CO_SAMPLED = "co_sampled"


@dataclass(frozen=True)
class ClockAnchor:
    """One instant on two clocks: ``source`` and ``target`` are the same moment."""

    source: Timestamp
    target: Timestamp

    def __post_init__(self) -> None:
        for name in ("source", "target"):
            if not isinstance(getattr(self, name), Timestamp):
                raise TypeError(f"an anchor's {name} must be a Timestamp")

    def to_json(self) -> JsonObject:
        return {"source": self.source.to_json(), "target": self.target.to_json()}


def clock_anchor_from_json(data: JsonValue) -> ClockAnchor:
    obj = exact_object(data, "clock anchor", {"source", "target"})
    return ClockAnchor(timestamp_from_json(obj["source"]), timestamp_from_json(obj["target"]))


@dataclass(frozen=True)
class ClockMapping:
    """An affine map from ``source`` ticks to ``target`` ticks, as the evidence states it.

    ``target(t) = anchor.target.ticks + rate * (t - anchor.source.ticks)`` in each clock's own
    ticks (ADR 0050 §5): ``anchor`` is the offset, as one instant on both clocks, so no tick is
    converted to make it; ``rate`` is target ticks per source tick, exact and positive, so drift
    and differing resolutions are one number. ``residual_bound`` is the largest distance, in target
    ticks, between the map and the truth that the evidence states; a bound finer than a tick is
    rounded up to whole ticks, so it stays a bound. ``validity`` is a window on the source clock.
    The two clocks are ``TimestampDomain`` record ids and differ. Applying the map is a consumer's
    (the Ledger's cross-clock merge); no record's ticks are ever rewritten through it.
    """

    kind: ClassVar[str] = "clock_mapping"
    family: ClassVar[Family] = Family.ALIGNMENT
    since: ClassVar[int] = ALIGNMENT_SINCE
    id: RecordId
    provenance: Provenance
    source: RecordId
    target: RecordId
    method: MappingMethod
    anchor: Knowledge[ClockAnchor]
    rate: Knowledge[Fraction]
    residual_bound: Knowledge[Duration]
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.source)
        parse_record_id(self.target)
        if self.source == self.target:
            raise ValueError(f"a clock mapping relates two clocks; {self.source} is both")
        if not isinstance(self.method, MappingMethod):
            raise TypeError(f"method must be a MappingMethod, got {self.method!r}")
        check_type("anchor", self.anchor, ClockAnchor)
        for anchor in values_of(self.anchor):
            if (anchor.source.domain_id, anchor.target.domain_id) != (self.source, self.target):
                raise ValueError("an anchor's instants must be on the source and target clocks")
        check_type("rate", self.rate, Fraction)
        if any(rate <= 0 for rate in values_of(self.rate)):
            raise ValueError(f"rate must be positive: a clock map is increasing, got {self.rate}")
        check_type("residual_bound", self.residual_bound, Duration)
        for bound in values_of(self.residual_bound):
            if bound.domain_id != self.target or bound.ticks < 0:
                raise ValueError("residual_bound is a non-negative duration on the target clock")
        _check_validity(self.validity, self.source)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "anchor": to_json(self.anchor, ClockAnchor.to_json),
                "method": str(self.method),
                "rate": to_json(self.rate, resolution_to_json),
                "residual_bound": to_json(self.residual_bound, Duration.to_json),
                "source": self.source,
                "target": self.target,
                "validity": _validity_json(self.validity),
            },
            self.since,
        )


def clock_mapping_from_json(data: JsonValue) -> ClockMapping:
    """Parse strictly; ``rate`` is a positive fraction in lowest terms."""
    obj, record_id, provenance = evidence_record_object(
        data,
        ClockMapping.kind,
        {"anchor", "method", "rate", "residual_bound", "source", "target", "validity"},
        ClockMapping.since,
    )
    return ClockMapping(
        id=record_id,
        provenance=provenance,
        source=_record_id(obj["source"]),
        target=_record_id(obj["target"]),
        method=MappingMethod(json_str(obj["method"], "method")),
        anchor=from_json(obj["anchor"], clock_anchor_from_json, provenance_from_json),
        rate=from_json(obj["rate"], resolution_from_json, provenance_from_json),
        residual_bound=from_json(obj["residual_bound"], duration_from_json, provenance_from_json),
        validity=_validity(obj["validity"]),
    )


# --- Frame bindings -----------------------------------------------------------------------------


class FrameBindingBasis(StrEnum):
    """What declares the transform that gives the edge its value (ADR 0050 §6)."""

    ROBOT_DESCRIPTION = "robot_description"  # a URDF, SDF or MJCF joint's origin
    CALIBRATION = "calibration"  # a calibration's extrinsic; ``calibration`` names it
    TRANSFORM_MESSAGE = "transform_message"  # a tf / tf_static message in a recording


@dataclass(frozen=True)
class FrameBinding:
    """The ``FrameTransform`` record that gives the edge ``parent`` → ``child`` its value.

    ``parent`` and ``child`` are frames of one graph and name the edge as the binding's evidence
    names it; the transform may sit in another graph (a calibration file's own), which is how a
    calibration says which description edge it measures. ``calibration`` is the ``Calibration``
    record that declares the transform, ``NotApplicable`` for any other basis. ``validity`` is
    when the transform gives the edge its value: a calibration's stated window, a tf message's
    stamp to the next one. Composing transforms is a consumer's (MVL-37); nothing here does.
    """

    kind: ClassVar[str] = "frame_binding"
    family: ClassVar[Family] = Family.ALIGNMENT
    since: ClassVar[int] = ALIGNMENT_SINCE
    id: RecordId
    provenance: Provenance
    parent: FrameRef
    child: FrameRef
    transform: RecordId
    basis: FrameBindingBasis
    calibration: Knowledge[RecordId]
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        for name in ("parent", "child"):
            if not isinstance(getattr(self, name), FrameRef):
                raise TypeError(f"{name} must be a FrameRef")
        if self.parent.frame_graph_id != self.child.frame_graph_id:
            raise ValueError("an edge's frames are in one graph")
        if self.parent == self.child:
            raise ValueError(f"an edge joins two frames; {self.parent.frame_id!r} is both")
        parse_record_id(self.transform)
        if not isinstance(self.basis, FrameBindingBasis):
            raise TypeError(f"basis must be a FrameBindingBasis, got {self.basis!r}")
        check_type("calibration", self.calibration, str)
        for calibration in values_of(self.calibration):
            parse_record_id(calibration)
        if self.basis is FrameBindingBasis.CALIBRATION:
            _stated("calibration", self.calibration)
        elif not isinstance(self.calibration, NotApplicable):
            raise ValueError(f"a {self.basis} binding names no calibration")
        _check_validity(self.validity)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "basis": str(self.basis),
                "calibration": to_json(self.calibration, _record_id_json),
                "child": self.child.to_json(),
                "parent": self.parent.to_json(),
                "transform": self.transform,
                "validity": _validity_json(self.validity),
            },
            self.since,
        )


def frame_binding_from_json(data: JsonValue) -> FrameBinding:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        FrameBinding.kind,
        {"basis", "calibration", "child", "parent", "transform", "validity"},
        FrameBinding.since,
    )
    return FrameBinding(
        id=record_id,
        provenance=provenance,
        parent=frame_ref_from_json(obj["parent"]),
        child=frame_ref_from_json(obj["child"]),
        transform=_record_id(obj["transform"]),
        basis=FrameBindingBasis(json_str(obj["basis"], "basis")),
        calibration=from_json(obj["calibration"], _record_id, provenance_from_json),
        validity=_validity(obj["validity"]),
    )


# --- Run assembly -------------------------------------------------------------------------------


class MemberRole(StrEnum):
    """What a file is to its run, as the membership's evidence says (ADR 0050 §7)."""

    RECORDING = "recording"  # holds the run's samples: a ULog, an MCAP, a bag or one split of it
    DESCRIPTION = "description"  # declares the run itself: a rosbag2 metadata.yaml, a manifest
    CONTEXT = "context"  # anything else the evidence places in the run: a config, a calibration


@dataclass(frozen=True)
class RunMember:
    """One file of a run: its ``SourceRevision`` record id, its role, and what declares it."""

    revision: RecordId
    role: MemberRole
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        parse_record_id(self.revision)
        if not isinstance(self.role, MemberRole):
            raise TypeError(f"role must be a MemberRole, got {self.role!r}")
        if not isinstance(self.evidence, EvidenceRef):
            raise TypeError(f"evidence must be an EvidenceRef, got {self.evidence!r}")

    def to_json(self) -> JsonObject:
        return {
            "evidence": self.evidence.to_json(),
            "revision": self.revision,
            "role": str(self.role),
        }


def run_member_from_json(data: JsonValue) -> RunMember:
    obj = exact_object(data, "run member", {"evidence", "revision", "role"})
    return RunMember(
        revision=_record_id(obj["revision"]),
        role=MemberRole(json_str(obj["role"], "role")),
        evidence=evidence_ref_from_json(obj["evidence"]),
    )


@dataclass(frozen=True)
class RunAssembly:
    """Which files form the run ``run``, and the evidence for each (ADR 0050 §7).

    ``members`` are sorted by revision id, each once, at least one. ``rule`` names the declared
    rule the producer applied (``rosbag2.metadata``: the files ``relative_file_paths`` lists;
    ``recording``: a recording is its own run), and the producer's transform pins its version and
    config. ``validity`` is when the membership holds, which a split recording's manifest may
    bound; a file's membership of a run is usually timeless, so usually ``NotApplicable``.
    """

    kind: ClassVar[str] = "run_assembly"
    family: ClassVar[Family] = Family.ALIGNMENT
    since: ClassVar[int] = ALIGNMENT_SINCE
    id: RecordId
    provenance: Provenance
    run: RecordId
    rule: str
    members: tuple[RunMember, ...]
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.run)
        check_token("rule", self.rule)
        if not isinstance(self.members, tuple) or not self.members:
            raise ValueError("a run assembly has at least one member")
        for member in self.members:
            if not isinstance(member, RunMember):
                raise TypeError(f"members must be RunMembers, got {member!r}")
        revisions = [member.revision for member in self.members]
        if revisions != sorted(set(revisions)):
            raise ValueError("members must be sorted by revision id, each once")
        _check_validity(self.validity)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "members": [member.to_json() for member in self.members],
                "rule": self.rule,
                "run": self.run,
                "validity": _validity_json(self.validity),
            },
            self.since,
        )


def run_assembly_from_json(data: JsonValue) -> RunAssembly:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, RunAssembly.kind, {"members", "rule", "run", "validity"}, RunAssembly.since
    )
    return RunAssembly(
        id=record_id,
        provenance=provenance,
        run=_record_id(obj["run"]),
        rule=json_str(obj["rule"], "rule"),
        members=tuple(run_member_from_json(m) for m in json_array(obj["members"], "members")),
        validity=_validity(obj["validity"]),
    )


# --- Snapshot bindings --------------------------------------------------------------------------


class SnapshotKind(StrEnum):
    """The record kinds a run can be bound to: machine context as one declaration states it."""

    HARDWARE_CONFIGURATION = "hardware_configuration"
    SOFTWARE_CONFIGURATION = "software_configuration"
    CALIBRATION = "calibration"


@dataclass(frozen=True)
class SnapshotBinding:
    """The run ``run`` ran with the snapshot ``snapshot``, a record of kind ``snapshot_kind``.

    The shape MVL-38 binds to (ADR 0050 §8). ``provenance`` cites the evidence that the run used
    it: a flight log's own version and parameter messages, a manifest's entry. ``validity`` is
    the part of the run it applied to, on one of the run's clocks; a parameter changed mid-run
    ends one binding's window and starts another's. A snapshot is never edited: a change is a new
    snapshot and a new binding.
    """

    kind: ClassVar[str] = "snapshot_binding"
    family: ClassVar[Family] = Family.ALIGNMENT
    since: ClassVar[int] = ALIGNMENT_SINCE
    id: RecordId
    provenance: Provenance
    run: RecordId
    snapshot: RecordId
    snapshot_kind: SnapshotKind
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.run)
        parse_record_id(self.snapshot)
        if not isinstance(self.snapshot_kind, SnapshotKind):
            raise TypeError(f"snapshot_kind must be a SnapshotKind, got {self.snapshot_kind!r}")
        _check_validity(self.validity)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "run": self.run,
                "snapshot": self.snapshot,
                "snapshot_kind": str(self.snapshot_kind),
                "validity": _validity_json(self.validity),
            },
            self.since,
        )


def snapshot_binding_from_json(data: JsonValue) -> SnapshotBinding:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        SnapshotBinding.kind,
        {"run", "snapshot", "snapshot_kind", "validity"},
        SnapshotBinding.since,
    )
    return SnapshotBinding(
        id=record_id,
        provenance=provenance,
        run=_record_id(obj["run"]),
        snapshot=_record_id(obj["snapshot"]),
        snapshot_kind=enum_decoder(SnapshotKind)(obj["snapshot_kind"]),
        validity=_validity(obj["validity"]),
    )
