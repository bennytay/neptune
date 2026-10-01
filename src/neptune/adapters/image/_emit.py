"""The adapter's output as it is built: records and findings, each once, citing exact bytes.

Every parser writes through one ``Emitter``. It derives every id from the record's evidence and
the config's transform, refuses a second record with an id already taken (two structures a
hostile file points at the same bytes) with an ``image.overlap`` finding, and keeps identical
findings once, so the contract's "no record or finding twice" holds whatever the file says.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from neptune.adapters.contract import AdapterConfig, SourceReader
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, Locator, Provenance, Row
from neptune.model.scalars import NonFinite, real
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

# Finding codes: (code, category, severity, what it means). The descriptor documents each.
UNREADABLE: Final = "image.unreadable"
TRUNCATED: Final = "image.truncated"
MALFORMED: Final = "image.malformed"
CRC_MISMATCH: Final = "image.crc_mismatch"
BAD_OFFSET: Final = "image.bad_offset"
IFD_LOOP: Final = "image.ifd_loop"
OVERLAP: Final = "image.overlap"
REPEATED: Final = "image.repeated"
VALUE_UNREADABLE: Final = "image.value_unreadable"
VALUE_NOT_COPIED: Final = "image.value_not_copied"
NOT_MODELLED: Final = "image.not_modelled"
LIMIT_EXCEEDED: Final = "image.limit_exceeded"
PIXEL_LIMIT: Final = "image.pixel_limit"
RASTER_TRUNCATED: Final = "image.raster_truncated"
XMP_UNREADABLE: Final = "image.xmp_unreadable"
ICC_UNREADABLE: Final = "image.icc_unreadable"

FINDINGS: Final[Mapping[str, tuple[FindingCategory, Severity, str]]] = {
    UNREADABLE: (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "the bytes are not a readable image of the format they start as; no image record",
    ),
    TRUNCATED: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "the file ends inside a structure or before the end its format requires",
    ),
    MALFORMED: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "a structure breaks its format's rules; it is read as far as it holds, or skipped",
    ),
    CRC_MISMATCH: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "a PNG chunk's CRC does not match its type and data; the chunk is still read",
    ),
    BAD_OFFSET: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "an offset or a declared size points outside the bytes that hold it; nothing there is read",
    ),
    IFD_LOOP: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "an IFD is reached a second time (a loop or a shared pointer); it is not read again",
    ),
    OVERLAP: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "two structures claim the same bytes; the second is not read",
    ),
    REPEATED: (
        FindingCategory.AMBIGUOUS,
        Severity.WARNING,
        "a block or tag the format allows once appears again; the first is read for the image",
    ),
    VALUE_UNREADABLE: (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "a declared value does not parse as its format defines it; it is Unknown",
    ),
    VALUE_NOT_COPIED: (
        FindingCategory.SKIPPED,
        Severity.INFO,
        "an IFD entry's value is not copied into its row (larger than max_value_bytes, a"
        " MakerNote, an unknown type); the finding cites its bytes",
    ),
    NOT_MODELLED: (
        FindingCategory.UNSUPPORTED,
        Severity.INFO,
        "the file declares something no record holds (animation frames, further images,"
        " extended XMP, bytes after the image); the finding cites it",
    ),
    LIMIT_EXCEEDED: (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "a configured limit stopped parsing; what was read before it is kept",
    ),
    PIXEL_LIMIT: (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "the declared raster holds more than max_pixels pixels: a decoder must refuse or bound it",
    ),
    RASTER_TRUNCATED: (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "the declared raster needs more bytes than the file holds for it",
    ),
    XMP_UNREADABLE: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "an XMP packet is not well-formed RDF/XML, declares a DTD or nests too deep; no table",
    ),
    ICC_UNREADABLE: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "an ICC profile's header or tag table is malformed; it is read as far as it holds",
    ),
}

CellInput = CellValue | Knowledge[CellValue]
_STATES: Final = (Known, KnownAbsent, Unknown, NotCovered, NotApplicable, Ambiguous)


def cell(value: CellInput) -> Knowledge[CellValue]:
    """A cell as declared: blank or whitespace-only text is ``Unknown``, never ``""``."""
    if isinstance(value, _STATES):
        return value
    if isinstance(value, NonFinite):
        return Known(value)
    if isinstance(value, str):
        return Known(value) if value.strip() else Unknown()
    if isinstance(value, float):
        return Known(real(value))
    assert isinstance(value, int)  # bool is an int
    return Known(value)


class Emitter:
    """One source's records and findings under one config, in the order they were made."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config
        self._records: dict[RecordId, Any] = {}
        self._findings: dict[RecordId, IngestFinding] = {}
        self._tried: set[tuple[str, tuple[Locator, ...]]] = set()

    @property
    def records(self) -> tuple[Any, ...]:
        return tuple(self._records.values())

    @property
    def findings(self) -> tuple[IngestFinding, ...]:
        return tuple(self._findings.values())

    def evidence(self, locator: Sequence[Locator]) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, tuple(locator))

    def provenance(self, locator: Sequence[Locator]) -> Provenance:
        return Provenance(self.evidence(locator), self.config.transform.id, AssertionKind.OBSERVED)

    def record_id(self, kind: str, locator: Sequence[Locator]) -> RecordId:
        return evidence_record_id(kind, self.evidence(locator), self.config.transform)

    def first(self, what: str, locator: Sequence[Locator]) -> bool:
        """``True`` the first time ``what`` is read at ``locator``, ``False`` after.

        A hostile file can point many tags at the same big block; it is parsed once, however
        many point at it, and the repeats are ``image.overlap`` findings.
        """
        key = (what, tuple(locator))
        if key in self._tried:
            self.finding(OVERLAP, locator, f"the {what} at these bytes was already read")
            return False
        self._tried.add(key)
        return True

    def taken(self, record_id: RecordId) -> bool:
        return record_id in self._records

    def add(self, record: Any) -> None:
        """Keep ``record``; an identical one already kept is the same record."""
        kept = self._records.get(record.id)
        if kept is None:
            self._records[record.id] = record
        elif kept != record:  # pragma: no cover - callers check ``taken`` first
            raise ValueError(f"two different records share the id {record.id}")

    def table(
        self,
        locator: Sequence[Locator],
        name: Knowledge[str],
        header: tuple[str, ...],
        what: str,
    ) -> RecordId | None:
        """A ``StructuredTable`` citing ``locator``; ``None`` if those bytes are already a table."""
        table_id = self.record_id(StructuredTable.kind, locator)
        if self.taken(table_id):
            self.finding(
                OVERLAP,
                locator,
                f"{what} is at bytes another structure already holds; it is not read again",
            )
            return None
        header_state: Knowledge[tuple[str, ...]] = Known(header) if header else NotApplicable()
        self.add(StructuredTable(table_id, self.provenance(locator), name, header_state))
        return table_id

    def row(
        self,
        table: RecordId,
        table_locator: Sequence[Locator],
        row: int,
        cells: Iterable[CellInput],
    ) -> RecordId:
        """Row ``row`` of ``table``, cited ``[*table_locator, Row(row)]``: its cells inherit."""
        locator = (*table_locator, Row(row))
        record_id = self.record_id(StructuredRecord.kind, locator)
        record = StructuredRecord(
            record_id, self.provenance(locator), table, row, tuple(cell(c) for c in cells)
        )
        self.add(record)
        return record_id

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
