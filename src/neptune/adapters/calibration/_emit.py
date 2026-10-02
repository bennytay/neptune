"""Calibrations, extrinsics and the file's frame graph, with the checks the file allows itself
(ADR 0055 §3 to §6).

One ``Calibration`` per calibrated subject, its parameters as declared. Kalibr's extrinsics are
``FrameTransform``s in one ``FrameGraph`` for the file; a transform whose frames or numbers the
file does not give stays a parameter and costs a finding. Checks here are about what the file says
of itself (a matrix with fewer numbers than it declares, a frame joined to the rest twice, a
camera with no extrinsic). Whether two transforms agree, or which calibration a run used, is
computed from several records and belongs to ``validate/`` and ``derived/``.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.calibration._formats import (
    KALIBR_IMU_FRAME,
    KALIBR_PREVIOUS_CAMERA,
    CalibrationFormat,
    Entry,
    Recognised,
)
from neptune.adapters.calibration._items import Item, Kind
from neptune.adapters.calibration._params import Flattener, Gaps, single_number
from neptune.adapters.contract import AdapterConfig
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.frames import (
    STATIC,
    FrameRef,
    HomogeneousMatrix,
    MatrixLayout,
    TransformDirection,
)
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.machine import Calibration
from neptune.model.provenance import ByteRange, EvidenceRef, Locator, Provenance
from neptune.model.reference import FrameGraph, FrameTransform

ADAPTER_ID: Final = "calibration"
_CAMERA: Final = re.compile(r"cam([0-9]+)")
OPENCV_EXTRINSICS: Final = ("CameraExtrinsicMat", "R", "T")
_LISTED: Final = 8  # names a finding lists before it says "and n more"
EvidenceOut = Calibration | FrameGraph | FrameTransform


def _code(name: str) -> str:
    return f"{ADAPTER_ID}.{name}"


@dataclass
class Output:
    records: list[EvidenceOut] = field(default_factory=list)
    findings: list[IngestFinding] = field(default_factory=list)


class Emitter:
    """Turns recognised documents into records and findings for one source."""

    def __init__(self, source: ContentId, size: int, config: AdapterConfig, max_array: int) -> None:
        self.source = source
        self.config = config
        self.transform = config.transform
        self.max_array = max_array
        self.whole = EvidenceRef(source, (ByteRange(0, size),))
        self.graph = evidence_record_id(FrameGraph.kind, self.whole, self.transform)
        self.out = Output()
        self.transforms: list[FrameTransform] = []
        self.calibrations: list[tuple[Calibration, Entry]] = []
        self._ids: set[RecordId] = set()

    # --- helpers ------------------------------------------------------------------------------

    def cite(self, where: Locator) -> Provenance:
        evidence = EvidenceRef(self.source, (where,))
        return Provenance(evidence, self.transform.id, AssertionKind.OBSERVED)

    def _id(self, kind: str, where: Locator) -> RecordId | None:
        """The tier-2 id of a record of ``kind`` at ``where``; ``None`` if one is already there."""
        record = evidence_record_id(kind, EvidenceRef(self.source, (where,)), self.transform)
        if record in self._ids:
            return None
        self._ids.add(record)
        return record

    def finding(
        self,
        name: str,
        category: FindingCategory,
        severity: Severity,
        where: Locator,
        message: str,
        details: dict[str, JsonValue] | None = None,
        records: list[RecordId] | None = None,
    ) -> None:
        self.out.findings.append(
            ingest_finding(
                code=_code(name),
                category=category,
                severity=severity,
                subject=EvidenceRef(self.source, (where,)),
                transform=self.transform,
                message=message,
                details=details,
                records=records or (),
            )
        )

    # --- documents ----------------------------------------------------------------------------

    def document(self, recognised: Recognised, root: Item) -> None:
        names = {entry.item.name for entry in recognised.entries}
        if recognised.unread:
            listed = sorted(recognised.unread)
            self.finding(
                "entries_not_read",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                root.where,
                f"{len(listed)} top-level keys belong to no calibration entry and are not read:"
                f" {_names(listed)}",
                {"keys": list(listed[:_LISTED]), "count": len(listed)},
            )
        for entry in recognised.entries:
            self._entry(recognised.format, entry, names, root)

    def _entry(self, fmt: CalibrationFormat, entry: Entry, names: set[str], root: Item) -> None:
        item = entry.item
        calibration_id = self._id(Calibration.kind, item.where)
        if calibration_id is None:
            return
        transforms, consumed = self._kalibr_extrinsics(entry, names, calibration_id)
        flat = Flattener(self.cite, self.max_array)
        parameters = flat.run(item, consumed)
        if fmt in (CalibrationFormat.OPENCV_YAML, CalibrationFormat.OPENCV_XML):
            self._opencv_extrinsics(item, calibration_id)
        self._gaps(flat.gaps, item, calibration_id)
        for transform in transforms:
            self.out.records.append(transform)
        self.transforms.extend(transforms)
        subject = (
            Known(entry.subject, self.cite(entry.subject_where))
            if entry.subject is not None and entry.subject_where is not None
            else Unknown()
        )
        if not parameters and not transforms:
            self.finding(
                "not_calibration",
                FindingCategory.MISSING,
                Severity.WARNING,
                item.where,
                "the entry states no parameter and no extrinsic; no calibration is emitted",
            )
            return
        calibration = Calibration(
            id=calibration_id,
            provenance=self.cite(item.where),
            machine=Unknown(),
            hardware_revision=Unknown(),
            subject=subject,
            performed=Unknown(),
            valid_from=Unknown(),
            valid_until=Unknown(),
            parameters=parameters,
            extrinsics=tuple(sorted(t.id for t in transforms)),
        )
        self.out.records.append(calibration)
        self.calibrations.append((calibration, entry))

    # --- Kalibr extrinsics --------------------------------------------------------------------

    def _kalibr_extrinsics(
        self, entry: Entry, names: set[str], calibration_id: RecordId
    ) -> tuple[list[FrameTransform], set[str]]:
        """``T_cam_imu`` and ``T_cn_cnm1`` of a camera entry as transforms of the file's graph.

        Kalibr documents ``T_a_b`` as mapping ``b``'s coordinates into ``a``'s. The entry is
        ``a`` (the parent), the frame the key names is ``b`` (the child): ``child_to_parent``.
        """
        found: list[FrameTransform] = []
        consumed: set[str] = set()
        if not entry.camera:
            return found, consumed
        item = entry.item
        for key in (KALIBR_IMU_FRAME, KALIBR_PREVIOUS_CAMERA):
            matrix = item.child(key)
            if matrix is None:
                continue
            child = self._other_frame(key, item.name, names)
            values, why = _matrix(matrix)
            problem = why if values is None else None
            if problem is None and child is None:
                problem = "frame"
            if problem == "frame":
                self.finding(
                    "frame_unresolved",
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    matrix.where,
                    f"{key} of {item.name} names a camera before it that the file does not"
                    " declare; the transform is not emitted and its rows stay parameters",
                    {"entry": item.name, "key": key},
                    [calibration_id],
                )
                continue
            if problem is not None or values is None or child is None:
                self.finding(
                    "extrinsic_not_read",
                    FindingCategory.UNREPRESENTABLE,
                    Severity.WARNING,
                    matrix.where,
                    f"{key} of {item.name} is not a 4x4 matrix of finite numbers ({problem});"
                    " its rows stay parameters",
                    {"entry": item.name, "key": key},
                    [calibration_id],
                )
                continue
            record = self._id(FrameTransform.kind, matrix.where)
            if record is None:
                continue
            cited = self.cite(matrix.where)
            found.append(
                FrameTransform(
                    id=record,
                    provenance=cited,
                    parent=FrameRef(item.name, self.graph),
                    child=FrameRef(child, self.graph),
                    direction=Known(TransformDirection.CHILD_TO_PARENT, cited),
                    value=HomogeneousMatrix(
                        values, Known(MatrixLayout.ROW_MAJOR, cited), Unknown()
                    ),
                    validity=STATIC,
                )
            )
            consumed.add(key)
        return found, consumed

    @staticmethod
    def _other_frame(key: str, entry: str, names: set[str]) -> str | None:
        if key == KALIBR_IMU_FRAME:
            return "imu"
        match = _CAMERA.fullmatch(entry)
        if match is None or int(match.group(1)) == 0:
            return None
        previous = f"cam{int(match.group(1)) - 1}"
        return previous if previous in names else None

    def _opencv_extrinsics(self, item: Item, calibration_id: RecordId) -> None:
        present = [key for key in OPENCV_EXTRINSICS if item.child(key) is not None]
        if present:
            self.finding(
                "frame_unresolved",
                FindingCategory.MISSING,
                Severity.INFO,
                item.where,
                f"{_names(present)} state an extrinsic transform but name no frame: no"
                " FrameTransform is emitted, the values stay parameters",
                {"keys": list(present)},
                [calibration_id],
            )

    # --- findings from flattening -------------------------------------------------------------

    def _gaps(self, gaps: Gaps, item: Item, calibration_id: RecordId) -> None:
        def report(
            name: str,
            category: FindingCategory,
            severity: Severity,
            names: list[str],
            message: str,
        ) -> None:
            if names:
                self.finding(
                    name,
                    category,
                    severity,
                    item.where,
                    f"{len(names)} {message} (first: {names[0]!r})",
                    {"count": len(names), "first": names[0]},
                    [calibration_id],
                )

        report(
            "value_not_read",
            FindingCategory.UNSUPPORTED,
            Severity.WARNING,
            [name for name, _ in gaps.unread],
            "values are not read (an alias, a tag the application reads, a number beyond"
            " binary64 or a value no record holds); each parameter is Unknown",
        )
        report(
            "array_too_large",
            FindingCategory.LIMIT,
            Severity.WARNING,
            [name for name, _ in gaps.too_large],
            "arrays hold more numbers than max_array_values; each parameter is Unknown",
        )
        report(
            "non_finite_value",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            gaps.non_finite,
            "parameters hold NaN or an infinity, kept as declared",
        )
        report(
            "ambiguous_value",
            FindingCategory.AMBIGUOUS,
            Severity.WARNING,
            gaps.ambiguous,
            "values read differently under YAML 1.1 and 1.2; each parameter is Ambiguous",
        )
        report(
            "duplicate_key",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            gaps.repeated,
            "keys repeat in their mapping; each is kept, named by its position",
        )
        shapes = gaps.shapes
        report(
            "shape_mismatch",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            [f"{name}: declares {d} numbers, holds {f}" for name, d, f in shapes],
            "matrices hold other than rows x cols x channels numbers",
        )

    # --- the file ----------------------------------------------------------------------------

    def finish(self) -> Output:
        """Checks that need every entry: subjects, missing extrinsics and the frame graph."""
        self._subjects()
        self._missing_extrinsics()
        if self.transforms:
            graph = FrameGraph(id=self.graph, provenance=self._whole(), scope=())
            self.out.records.insert(0, graph)
            self._graph()
        return self.out

    def _whole(self) -> Provenance:
        return Provenance(self.whole, self.transform.id, AssertionKind.OBSERVED)

    def _subjects(self) -> None:
        by_subject: dict[str, list[Calibration]] = defaultdict(list)
        for calibration, entry in self.calibrations:
            if entry.subject is not None:
                by_subject[entry.subject].append(calibration)
        for subject, calibrations in sorted(by_subject.items()):
            if len(calibrations) > 1:
                self.finding(
                    "duplicate_subject",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    calibrations[1].provenance.evidence.locator[0],
                    f"{len(calibrations)} calibrations in one file state the subject"
                    f" {subject!r}; each is kept, none replaces another",
                    {"count": len(calibrations), "subject": subject},
                    [c.id for c in calibrations],
                )

    def _missing_extrinsics(self) -> None:
        cameras = [(c, e) for c, e in self.calibrations if e.camera]
        for key in (KALIBR_IMU_FRAME, KALIBR_PREVIOUS_CAMERA):
            if not any(e.item.child(key) is not None for _, e in cameras):
                continue
            for calibration, entry in cameras:
                if entry.item.child(key) is not None:
                    continue
                match = _CAMERA.fullmatch(entry.item.name)
                if key == KALIBR_PREVIOUS_CAMERA and (match is None or int(match.group(1)) == 0):
                    continue  # the first camera of a chain has no camera before it
                self.finding(
                    "extrinsic_missing",
                    FindingCategory.MISSING,
                    Severity.INFO,
                    entry.item.where,
                    f"{entry.item.name} declares no {key}, which other cameras of the file do",
                    {"entry": entry.item.name, "key": key},
                    [calibration.id],
                )

    def _graph(self) -> None:
        by_pair: dict[frozenset[str], list[FrameTransform]] = defaultdict(list)
        for transform in self.transforms:
            by_pair[frozenset((transform.parent.frame_id, transform.child.frame_id))].append(
                transform
            )
        parent: dict[str, str] = {}

        def find(name: str) -> str:
            parent.setdefault(name, name)
            while parent[name] != name:
                parent[name] = parent[parent[name]]
                name = parent[name]
            return name

        loops: list[FrameTransform] = []
        for pair, transforms in by_pair.items():
            if len(transforms) > 1:
                self.finding(
                    "frame_transform_repeated",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    transforms[1].provenance.evidence.locator[0],
                    f"{len(transforms)} transforms join {_names(sorted(pair))}; each is kept",
                    {"frames": sorted(pair)},
                    [t.id for t in transforms],
                )
            first = transforms[0]
            a, b = find(first.parent.frame_id), find(first.child.frame_id)
            if a == b:
                loops.append(first)
            else:
                parent[a] = b
        if loops:
            self.finding(
                "frame_loop",
                FindingCategory.INCONSISTENT,
                Severity.INFO,
                loops[0].provenance.evidence.locator[0],
                f"{len(loops)} transforms join frames the others already connect: the declared"
                " transforms have more than one path between frames, and may disagree",
                {"count": len(loops)},
                [t.id for t in loops],
            )
        frames = sorted({f for t in self.transforms for f in (t.parent.frame_id, t.child.frame_id)})
        roots = {find(frame) for frame in frames}
        if len(roots) > 1:
            self.finding(
                "frame_graph_disconnected",
                FindingCategory.MISSING,
                Severity.WARNING,
                self.transforms[0].provenance.evidence.locator[0],
                f"the file's transforms form {len(roots)} separate groups of frames: no"
                " transform joins them",
                {"frames": frames[: _LISTED * 4], "groups": len(roots)},
                [t.id for t in self.transforms],
            )


def _names(names: list[str]) -> str:
    shown = ", ".join(repr(name) for name in names[:_LISTED])
    return shown + (f" and {len(names) - _LISTED} more" if len(names) > _LISTED else "")


def _matrix(item: Item) -> tuple[tuple[float, ...] | None, str]:
    """A 4x4 matrix as sixteen floats in row order, or why not."""
    if item.kind is not Kind.SEQUENCE or len(item.children) != 4:
        return None, "it is not four rows"
    values: list[float] = []
    for row in item.children:
        if row.kind is not Kind.SEQUENCE or len(row.children) != 4:
            return None, "a row does not hold four numbers"
        for cell in row.children:
            number = single_number(cell)
            if not isinstance(number, float):
                return None, "an entry is not a finite number"
            values.append(number)
    return tuple(values), ""
