"""What every fleet-ops source shares: documents as objects, records over them, findings (ADR 0010).

A fleet-ops source does not hand the compiler raw bytes of a vendor's storage. What it reads is
API responses (Formant) or rows of a log database (Open-RMF), and what it keeps is what it read:

- ``walk()`` yields one object per non-empty **part** (devices, events, tasks, ...), each a
  document in the fixed byte form of ``documents.build_document``. ``open()`` serves its bytes. The
  compiler stores them, so every record below cites bytes that exist in the package.
- ``catalog()`` returns the ``stated`` records built over those documents, with provenance to the
  document, an item and a key.
- ``findings()`` returns what could not be read, stopped at a limit or could not be stored.

The source reads once, lazily, and keeps the result: two ``walk()`` calls and ``catalog()`` see one
read. Its output depends on what was read, not on page size, page order, row order or the clock.
"""

import io
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar, Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, FindingSubject, IngestFinding, Severity
from neptune.model.ids import ExternalObjectRef, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.source import SourceRevision
from neptune_deploy.sources.fleet_ops.documents import (
    Catalog,
    DeclaredClock,
    Document,
    StatedTable,
    build_document,
    clock_domain,
    stated_table,
)

CONNECTOR_VERSION: Final = "0.1.0"

CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "part_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a request for a part (devices, events, tasks, ...) failed or looped; the records read"
        " before that are kept and the rest of the part is not covered",
    ),
    "part_limit": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "a part stopped at its record, byte or page limit; later records are not covered",
    ),
    "part_invalid": (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "a response, page or file is not the strict JSON or table the part is documented to be;"
        " what was read before it is kept and the rest of the part is not covered",
    ),
    "value_unrepresentable": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "values that cannot be stored as cell text (a lone surrogate, or too large) are Unknown",
    ),
    "value_unreadable": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "a stated value is not in a form its record field reads (a time that no declared format"
        " reads); the field is Unknown and the value stays in the table",
    ),
    "part_empty": (
        FindingCategory.MISSING,
        Severity.INFO,
        "a part was read to its end and holds no records; no document or table is made for it",
    ),
    "record_skipped": (
        FindingCategory.MISSING,
        Severity.WARNING,
        "an item states no usable id, so no record is built from it; it stays in the table",
    ),
}


@dataclass(frozen=True, slots=True)
class DocumentEntry:
    """One object to read: a document and its size. Shaped like the compiler's ``SourceEntry``
    (``location``, ``size``)."""

    location: ExternalObjectRef
    size: int
    part: str

    @property
    def name(self) -> str:
        """An adapter's ``ProbeHints.name``: advisory, as every name is."""
        return f"{self.part}.json"


class DocumentReadError(OSError):
    """Opening an object that is not in this source (or whose revision moved on); ``code`` is the
    finding code without the connector prefix. A plain exception, as ADR 0006's ``ObjectReadError``
    is: it can be copied, pickled and re-raised across a boundary."""

    def __init__(self, code: str, location: ExternalObjectRef) -> None:
        super().__init__(f"{location.connector_id}: {code}")
        self.code = code
        self.location = location

    def __reduce__(self) -> tuple[type, tuple[str, ExternalObjectRef]]:
        return type(self), (self.code, self.location)


@dataclass(frozen=True)
class Discovery:
    """Entries sorted against a ledger: ``new`` (no head, or an absent head), ``changed`` (another
    revision token) and ``unchanged`` (the same token; never fetched)."""

    new: tuple[DocumentEntry, ...]
    changed: tuple[DocumentEntry, ...]
    unchanged: tuple[DocumentEntry, ...]

    @property
    def to_read(self) -> tuple[DocumentEntry, ...]:
        return tuple(sorted((*self.new, *self.changed), key=lambda e: e.location.object_id))


@dataclass
class Part:
    """One part's items as read, with the clock fields to declare and how it ended."""

    name: str
    items: list[JsonValue] = field(default_factory=list)
    clock_fields: tuple[str, ...] = ()
    clock: DeclaredClock = field(default_factory=DeclaredClock)
    stopped: str | None = None  # why it is incomplete: a cause, never error text
    status: int | None = None


class FleetOpsSource:
    """A read-only source whose objects are documents of what a fleet-ops system stated."""

    connector_id: ClassVar[str]
    scope: str  # the declared instance and scope an object id starts with; ends with ``/``

    def __init__(self, ledger: SourceLedger | None = None) -> None:
        self._ledger = ledger
        self._findings: dict[RecordId, IngestFinding] = {}

    # --- What a subclass says -----------------------------------------------------------------

    def config(self) -> dict[str, JsonValue]:
        """What decided which records were seen: declared options, never an endpoint, a path, a
        credential or a cursor."""
        raise NotImplementedError

    def collect(self) -> Sequence[Part]:
        """Read the parts, in a fixed order. A part that fails is a finding and is absent."""
        raise NotImplementedError

    def extend(
        self,
        part: Part,
        document: Document,
        table: StatedTable,
        clocks: Mapping[str, TimestampDomain],
    ) -> Sequence[Any]:
        """The records built over a part's document besides its table (runs, interventions);
        ``clocks`` are the clocks of its integer time fields, by field name."""
        return ()

    # --- Findings -----------------------------------------------------------------------------

    @cached_property
    def transform(self) -> TransformRecord:
        return transform_record(
            adapter_id=self.connector_id,
            adapter_version=CONNECTOR_VERSION,
            config=self.config(),
        )

    def report(
        self,
        code: str,
        subject: FindingSubject | ExternalObjectRef,
        details: dict[str, JsonValue],
    ) -> None:
        category, severity, message = _codes(self)[code]
        finding = ingest_finding(
            code=f"{self.connector_id}.{code}",
            category=category,
            severity=severity,
            subject=subject,
            transform=self.transform,
            message=message,
            details=details,
        )
        self._findings[finding.id] = finding

    def part_subject(self, part: str) -> ExternalObjectRef:
        """``<scope>:<part>``: the ``:`` keeps a part from sharing an id with anything else."""
        return ExternalObjectRef(self.connector_id, f"{self.scope}:{part}", "catalog")

    def _stopped(self, part: Part) -> None:
        cause = part.stopped or "unknown"
        details: dict[str, JsonValue] = {
            "cause": cause,
            "part": part.name,
            "records": len(part.items),
        }
        if part.status is not None:
            details["status"] = part.status
        if cause in {"record_limit", "byte_limit", "page_limit", "row_limit", "work_limit"}:
            self.report("part_limit", self.part_subject(part.name), details)
        elif cause in {"response_invalid", "file_invalid"}:
            self.report("part_invalid", self.part_subject(part.name), details)
        else:
            self.report("part_failed", self.part_subject(part.name), details)

    # --- Catalog ------------------------------------------------------------------------------

    @cached_property
    def _built(self) -> tuple[Catalog, tuple[tuple[Part, Document], ...]]:
        records: list[Any] = []
        documents: list[Document] = []
        built: list[tuple[Part, Document]] = []
        skipped: dict[tuple[str, str], int] = {}
        for part in self.collect():
            if part.stopped is not None:
                self._stopped(part)
            if not part.items:
                if part.stopped is None:
                    self.report("part_empty", self.part_subject(part.name), {"part": part.name})
                continue
            document = build_document(self.connector_id, f"{self.scope}{part.name}", part.items)
            if not document.items:
                continue
            clocks = {}
            for name in part.clock_fields:
                domain = clock_domain(document, name, (part.name,), self.transform, part.clock)
                if domain is not None:
                    clocks[name] = domain
            table = stated_table(
                document, f"{self.connector_id} {part.name}", self.transform, clocks=clocks
            )
            records.extend([*clocks.values(), table.table, *table.rows])
            records.extend(self.extend(part, document, table, clocks))
            documents.append(document)
            built.append((part, document))
            for _name, _, reason in table.skipped:
                skipped[(part.name, reason)] = skipped.get((part.name, reason), 0) + 1
        for (name, reason), count in sorted(skipped.items()):
            self.report(
                "value_unrepresentable",
                self.part_subject(name),
                {"count": count, "reason": reason, "part": name},
            )
        return Catalog(tuple(documents), tuple(records)), tuple(built)

    def catalog(self) -> Catalog:
        """Every document and ``stated`` record, built once and kept."""
        return self._built[0]

    def findings(self) -> tuple[IngestFinding, ...]:
        """What went wrong, sorted by id; complete only after ``catalog()`` (or ``walk``)."""
        _ = self._built
        return tuple(self._findings[key] for key in sorted(self._findings))

    # --- The compiler's Source shape ----------------------------------------------------------

    def listing(self) -> tuple[DocumentEntry, ...]:
        """One entry per non-empty part, by object id."""
        entries = [
            DocumentEntry(document.ref, len(document.data), part.name)
            for part, document in self._built[1]
        ]
        return tuple(sorted(entries, key=lambda entry: entry.location.object_id))

    def discover(self, ledger: SourceLedger) -> Discovery:
        new: list[DocumentEntry] = []
        changed: list[DocumentEntry] = []
        unchanged: list[DocumentEntry] = []
        for entry in self.listing():
            head = ledger.head(entry.location)
            if not isinstance(head, SourceRevision):
                new.append(entry)
            elif (
                isinstance(head.location, ExternalObjectRef)
                and head.location.revision_token == entry.location.revision_token
            ):
                unchanged.append(entry)
            else:
                changed.append(entry)
        return Discovery(tuple(new), tuple(changed), tuple(unchanged))

    def walk(self) -> Iterator[DocumentEntry]:
        """The documents to read: all of them, or with a ledger only the new and changed ones."""
        if self._ledger is None:
            yield from self.listing()
        else:
            yield from self.discover(self._ledger).to_read

    def entry(self, location: ExternalObjectRef) -> DocumentEntry:
        for entry in self.listing():
            if entry.location == location:
                return entry
        raise DocumentReadError("object_gone", location)

    def open(self, location: ExternalObjectRef) -> io.BytesIO:
        """The bytes of one document, exactly as listed: another revision is ``object_changed``."""
        for _, document in self._built[1]:
            if document.ref.object_id == location.object_id:
                if document.ref != location:
                    raise DocumentReadError("object_changed", location)
                return io.BytesIO(document.data)
        raise DocumentReadError("object_gone", location)


def _codes(source: FleetOpsSource) -> dict[str, tuple[FindingCategory, Severity, str]]:
    return {**CODES, **getattr(source, "EXTRA_CODES", {})}
