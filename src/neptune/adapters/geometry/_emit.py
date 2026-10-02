"""The adapter's output as it is built: one artifact, two tables and findings, each citing bytes.

A source yields one ``SpatialArtifact`` citing the whole file (the geometry stays in those bytes: a
lazy handle, nothing copied), a ``geometry properties`` table whose rows each cite the exact bytes
they come from, and a ``geometry dependencies`` table of the files it names (ADR 0052 §3). Ids
derive from the record's evidence and the config's transform; a record or finding already made is
not made twice.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from neptune.adapters.contract import AdapterConfig, SourceReader
from neptune.adapters.geometry import _refs
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    Locator,
    Provenance,
    adapter_locator,
)
from neptune.model.scalars import real
from neptune.model.units import Unit
from neptune.model.world import (
    CellValue,
    SpatialArtifact,
    SpatialCategory,
    StructuredRecord,
    StructuredTable,
)

UNREADABLE: Final = "geometry.unreadable"
TRUNCATED: Final = "geometry.truncated"
MALFORMED: Final = "geometry.malformed"
COUNT_MISMATCH: Final = "geometry.count_mismatch"
NON_FINITE: Final = "geometry.non_finite"
LIMIT_EXCEEDED: Final = "geometry.limit_exceeded"
NOT_COVERED: Final = "geometry.not_covered"
UNIT_UNMAPPED: Final = "geometry.unit_unmapped"
OUTSIDE_ROOT: Final = "geometry.reference_outside_root"
LEAVES_DIRECTORY: Final = "geometry.reference_leaves_directory"
REFERENCE_UNSAFE: Final = "geometry.reference_unsafe"

FINDINGS: Final[Mapping[str, tuple[FindingCategory, Severity, str]]] = {
    COUNT_MISMATCH: (
        FindingCategory.INCONSISTENT,
        Severity.WARNING,
        "a count or size the header declares is not what the file holds (a PLY element count, an"
        " STL facet count, a GLB length); the declared value and the measured one are both kept",
    ),
    LEAVES_DIRECTORY: (
        FindingCategory.SKIPPED,
        Severity.INFO,
        "a reference climbs out of its file's directory with ..; it stays in the source root only"
        " if the file sits deep enough, which only a check across sources knows; never opened",
    ),
    LIMIT_EXCEEDED: (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "a configured limit stopped a parse or skipped a value; a scan cut short gives no bounds"
        " or counts (they are NotCovered) and what was read before it is kept",
    ),
    MALFORMED: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "statements or structures break the format's rules and are skipped; one finding per"
        " reason, citing the first and counting the rest",
    ),
    NON_FINITE: (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "vertices with NaN or infinite coordinates are left out of the bounds; the finding cites"
        " the first and counts the rest",
    ),
    NOT_COVERED: (
        FindingCategory.UNSUPPORTED,
        Severity.INFO,
        "well-formed bytes this adapter does not decode (a binary USD crate, a GLB buffer chunk,"
        " USD prims, OBJ free-form statements); the file is identified and the value is NotCovered",
    ),
    OUTSIDE_ROOT: (
        FindingCategory.SKIPPED,
        Severity.WARNING,
        "a reference is absolute, a file: or other URI: it names nothing inside the source root"
        " and is never opened or fetched",
    ),
    REFERENCE_UNSAFE: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "a reference is empty, holds control characters or is not valid text; it has no row",
    ),
    TRUNCATED: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "the file ends inside a structure or before the data its header declares; what is there"
        " is read",
    ),
    UNIT_UNMAPPED: (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "a declared scale to metres is not one of the standard lengths; the artifact's unit is"
        " Unknown and the declared number stays in the properties table",
    ),
    UNREADABLE: (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "the bytes are not a readable file of the format they start as; no record is made",
    ),
}

PROPERTY_STEP: Final = "geometry:property"
TABLE_STEP: Final = "geometry:table"
PROPERTIES_HEADER: Final = ("property", "v0", "v1", "v2")
DEPENDENCIES_HEADER: Final = ("kind", "target", "scope")


@dataclass(frozen=True)
class Prop:
    """One property of the geometry: its name, its values in order and the bytes they come from.

    ``state`` is ``known`` (``values`` hold them), or the explicit missingness of a value the
    file could have given (``unknown``), has no place for (``not_covered``) or cannot have
    (``not_applicable``: the bounds of no vertices).
    """

    name: str
    values: tuple[CellValue, ...]
    where: tuple[Locator, ...]
    kind: AssertionKind
    state: str = "known"


def known(
    name: str, values: Iterable[CellValue], where: Sequence[Locator], kind: AssertionKind
) -> Prop:
    return Prop(name, tuple(values), tuple(where), kind)


def missing(name: str, state: str, where: Sequence[Locator], kind: AssertionKind) -> Prop:
    return Prop(name, (), tuple(where), kind, state)


@dataclass(frozen=True)
class Dep:
    """A file the geometry names: ``kind`` (material_library, texture, buffer, sublayer), its
    ``target`` text as written, and the exact bytes that write it."""

    kind: str
    target: str
    where: tuple[Locator, ...]
    percent_encoded: bool = False


@dataclass(frozen=True)
class Geometry:
    """What one reader found: the artifact's declared fields, its properties and dependencies."""

    format: str
    category: SpatialCategory
    name: Knowledge[str]
    unit: Knowledge[Unit]
    props: tuple[Prop, ...]
    deps: tuple[Dep, ...] = ()
    notes: tuple[str, ...] = field(default=())


class Emitter:
    """One source's records and findings under one config, in the order they were made."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config
        self._records: dict[RecordId, Any] = {}
        self._findings: dict[RecordId, IngestFinding] = {}

    @property
    def records(self) -> tuple[Any, ...]:
        return tuple(self._records.values())

    @property
    def findings(self) -> tuple[IngestFinding, ...]:
        return tuple(self._findings.values())

    def evidence(self, locator: Sequence[Locator]) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, tuple(locator))

    def provenance(
        self, locator: Sequence[Locator], kind: AssertionKind = AssertionKind.OBSERVED
    ) -> Provenance:
        """``OBSERVED`` is what this adapter decoded from the bytes (a count, a bound); ``STATED``
        is what the file or its format declares (a unit, an up axis, a reference)."""
        return Provenance(self.evidence(locator), self.config.transform.id, kind)

    def record_id(self, kind: str, locator: Sequence[Locator]) -> RecordId:
        return evidence_record_id(kind, self.evidence(locator), self.config.transform)

    def add(self, record: Any) -> None:
        self._records.setdefault(record.id, record)

    def finding(
        self,
        code: str,
        locator: Sequence[Locator],
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        records: Iterable[RecordId] = (),
    ) -> None:
        category, severity, _ = FINDINGS[code]
        finding = ingest_finding(
            code=code,
            category=category,
            severity=severity,
            subject=self.evidence(locator),
            transform=self.config.transform,
            message=message,
            details=details,
            records=records,
        )
        self._findings.setdefault(finding.id, finding)


def _step(kind: str, name: str) -> AdapterLocator:
    return adapter_locator(kind, {"name": name})


def _cell(value: CellValue | float, prov: Provenance) -> Knowledge[CellValue]:
    return Known(real(value) if isinstance(value, float) else value, prov)


def _state(state: str, prov: Provenance) -> Knowledge[CellValue]:
    match state:
        case "unknown":
            return Unknown(prov)
        case "not_covered":
            return NotCovered(prov)
        case _:
            return NotApplicable()


def emit(out: Emitter, geometry: Geometry, size: int, max_value_bytes: int) -> RecordId:
    """The artifact, its tables and the findings about its references. Returns the artifact's id."""
    whole = (ByteRange(0, size),)
    artifact_id = out.record_id(SpatialArtifact.kind, whole)
    out.add(
        SpatialArtifact(
            id=artifact_id,
            provenance=out.provenance(whole),
            category=geometry.category,
            name=geometry.name,
            unit=geometry.unit,
            crs=NotCovered(),
            frame=NotCovered(),
        )
    )
    _properties(out, geometry, whole)
    _dependencies(out, geometry, whole, artifact_id, max_value_bytes)
    return artifact_id


def _properties(out: Emitter, geometry: Geometry, whole: tuple[Locator, ...]) -> None:
    table_locator = (*whole, _step(TABLE_STEP, "properties"))
    table_id = out.record_id(StructuredTable.kind, table_locator)
    table = StructuredTable(
        table_id,
        out.provenance(table_locator),
        Known("geometry properties"),
        Known(PROPERTIES_HEADER),
    )
    out.add(table)
    for row, prop in enumerate(geometry.props):
        locator = (*prop.where, _step(PROPERTY_STEP, prop.name))
        prov = out.provenance(locator, prop.kind)
        cells: list[Knowledge[CellValue]] = [Known(prop.name, prov)]
        if prop.state == "known":
            cells.extend(_cell(value, prov) for value in prop.values)
        else:
            cells.append(_state(prop.state, prov))
        out.add(
            StructuredRecord(
                out.record_id(StructuredRecord.kind, locator), prov, table_id, row, tuple(cells)
            )
        )


def _dependencies(
    out: Emitter,
    geometry: Geometry,
    whole: tuple[Locator, ...],
    artifact_id: RecordId,
    max_value_bytes: int,
) -> None:
    if not geometry.deps:
        return
    table_locator = (*whole, _step(TABLE_STEP, "dependencies"))
    table_id = out.record_id(StructuredTable.kind, table_locator)
    out.add(
        StructuredTable(
            table_id,
            out.provenance(table_locator),
            Known("geometry dependencies"),
            Known(DEPENDENCIES_HEADER),
        )
    )
    row = 0
    for dep in geometry.deps:
        scope = _refs.scope_of(dep.target, percent_encoded=dep.percent_encoded)
        details: dict[str, JsonValue] = {"kind": dep.kind, "scope": scope}
        if scope == _refs.UNSAFE:
            out.finding(
                REFERENCE_UNSAFE,
                dep.where,
                f"a {dep.kind} reference is empty or holds control characters",
                details,
                (artifact_id,),
            )
            continue
        if scope in (_refs.ABSOLUTE, _refs.URI):
            out.finding(
                OUTSIDE_ROOT,
                dep.where,
                f"a {dep.kind} reference is {scope}: it names nothing inside the source root",
                details,
                (artifact_id,),
            )
        elif scope == _refs.PARENT:
            out.finding(
                LEAVES_DIRECTORY,
                dep.where,
                f"a {dep.kind} reference climbs out of its file's directory",
                details,
                (artifact_id,),
            )
        stated = out.provenance(dep.where, AssertionKind.STATED)
        observed = out.provenance(dep.where, AssertionKind.OBSERVED)
        target: Knowledge[CellValue]
        if len(dep.target.encode()) > max_value_bytes:
            target = NotCovered(stated)
            out.finding(
                LIMIT_EXCEEDED,
                dep.where,
                f"a {dep.kind} reference is longer than max_value_bytes ({max_value_bytes});"
                " its cell is NotCovered and the bytes hold it",
                {"limit": max_value_bytes, "option": "max_value_bytes"},
                (artifact_id,),
            )
        else:
            target = Known(dep.target, stated)
        out.add(
            StructuredRecord(
                out.record_id(StructuredRecord.kind, dep.where),
                stated,
                table_id,
                row,
                (Known(dep.kind, stated), target, Known(scope, observed)),
            )
        )
        row += 1
