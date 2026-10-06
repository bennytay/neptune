"""Writing an ingest package as a stream, in memory bounded independently of its records (ADR 0065).

``PackageWriter`` takes records one at a time and writes the package ``package_contents`` would,
byte for byte. Each record is encoded as it arrives and handed to a ``Sorter`` per table
(``neptune.store.spill``), which holds a bounded batch and spills sorted runs to a scratch
directory. ``finish`` then reads each table back once, merged in id order, and for each record:
drops a repeated id (or refuses it), reads the line back as the package reader would (the round
trip, lineage, series and blob checks of ``read_files``), adds what the receipt needs, and appends
the line to the table's file while hashing it. The receipt's sections that grow with the records
(runs, streams, entities, findings, ambiguous fields) go through sorters of their own; its id is
hashed, and ``receipt.json`` and ``receipt.md`` are written, from those sections as streams.

What stays in memory, whatever the record count: the spill budget, one read buffer per merged run,
the manifest, and the receipt's per-source sections (sources, absences, transforms, clocks, and a
stream count per run). ``package_contents`` is this writer without a scratch directory: nothing
spills and every file is bytes.

Tables are read back in a fixed order (transforms first, then the source ledger, clocks and
streams, then every other kind by name), so each record is checked against transforms already
read, and each run counts streams already read. The order changes no byte: each table is its own
file, and the manifest lists files by path.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, BinaryIO, Final, Self

from neptune.identity import canonical_json
from neptune.identity.ids import RECORD_ID_SCHEME
from neptune.identity.provenance import check_transform_record
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import RECORD_KINDS, kinds_at, package_version, record_key, record_version
from neptune.model.package import (
    IngestReceipt,
    PackageFile,
    PackageManifest,
    ReceiptClock,
    ReceiptEntity,
    ReceiptFinding,
    ReceiptRun,
    ReceiptStream,
    ReceiptTransform,
    SourceHandle,
    Storage,
    ambiguous_field_from_json,
    finding_order,
    receipt_entity_from_json,
    receipt_finding_from_json,
    receipt_run_from_json,
    receipt_stream_from_json,
)
from neptune.model.record import envelope
from neptune.store.package import (
    _DERIVED,
    MANIFEST,
    RECEIPT,
    RECEIPT_TEXT,
    Content,
    PackageError,
    _derived_line,
    _digest,
    _document,
    _series_settings,
    blob_path,
    check_record_lineage,
    derived_path,
    lay_down,
    package_id,
    replacing,
    series_path,
    table_path,
)
from neptune.store.receipt import (
    _LEDGER,
    NON_READERS,
    RECEIPT_KIND,
    _stated_ids,
    cite,
    ledger_sections,
    render_lines,
)
from neptune.store.series import SeriesError, check_series
from neptune.store.spill import SPILL_BUDGET, Key, Sorter, SpillSpace

if TYPE_CHECKING:
    from neptune.model.provenance import TransformRecord

# Tables read back before the rest: what later records are checked or counted against.
_FIRST: Final = (
    "transform_record",
    "source_artifact",
    "source_revision",
    "source_absence",
    "timestamp_domain",
    "stream",
)
_ENTITIES: Final = frozenset({"machine", "site", "asset"})
_REPEATABLE: Final = b"L"  # a record a later one of the same id replaces (``add(last_wins=True)``)
_SINGLE: Final = b"R"  # a record whose id must be the only one of its table
_FLUSH: Final = 1024 * 1024


class _Sink:
    """One file of the package as it is written: sized and hashed as it goes, kept as bytes in
    memory or written to ``path``, through a buffer of about a megabyte. With ``keep=False`` and
    no path it is only sized and hashed: what the reader compares with the manifest."""

    def __init__(self, path: Path | None, *, keep: bool = True) -> None:
        self._hash = hashlib.sha256()
        self._size = 0
        self._buffer = bytearray()
        self._parts: list[bytes] = []
        self._keep = keep  # without a path: hold the bytes, or only hash them (the reader's check)
        self._path = path
        self._file: BinaryIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("xb")

    def write(self, data: bytes) -> None:
        self._buffer += data
        if len(self._buffer) >= _FLUSH:
            self._flush()

    def _flush(self) -> None:
        block = bytes(self._buffer)
        self._buffer.clear()
        self._hash.update(block)
        self._size += len(block)
        if self._file is not None:
            self._file.write(block)
        elif self._keep:
            self._parts.append(block)

    def close(self) -> tuple[Content, tuple[int, ContentId]]:
        """The file's content (its bytes, or its path) and its size and sha256."""
        self._flush()
        digest = (self._size, ContentId("sha256:" + self._hash.hexdigest()))
        if self._file is not None:
            self._file.close()
            assert self._path is not None
            return self._path, digest
        return b"".join(self._parts), digest

    def abandon(self) -> None:
        if self._file is not None and not self._file.closed:
            self._file.close()


class _Members:
    """A receipt section read back from its sorter: each member's canonical JSON, or the member,
    in order, as often as asked; ``unique`` names the section if its keys must not repeat."""

    def __init__(
        self, sorter: Sorter, parse: Callable[[JsonValue], Any], unique: str | None = None
    ) -> None:
        self._sorter = sorter
        self._parse = parse
        self._unique = unique

    def __len__(self) -> int:
        return len(self._sorter)

    def encoded(self) -> Iterator[bytes]:
        previous: Key | None = None
        for key, payload in self._sorter:
            if self._unique is not None and previous is not None and key <= previous:
                raise ValueError(f"{self._unique} must be unique and sorted: {key!r} repeats")
            previous = key
            yield payload

    def __iter__(self) -> Iterator[Any]:
        for _, payload in self._sorter:
            yield self._parse(json.loads(payload))


def _encode(value: Any) -> Iterator[bytes]:
    """``canonical_json.dumps(value)`` in pieces, where ``value`` may hold ``_Members``: each is
    an array of its members' canonical JSON, read from disk as it is written."""
    if isinstance(value, _Members):
        yield b"["
        for index, member in enumerate(value.encoded()):
            if index:
                yield b","
            yield member
        yield b"]"
    elif isinstance(value, Mapping) and any(
        isinstance(item, (_Members, Mapping)) for item in value.values()
    ):
        yield b"{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b","
            yield canonical_json.dumps(key) + b":"
            yield from _encode(value[key])
        yield b"}"
    else:
        yield canonical_json.dumps(value)


class _ReceiptView:
    """What ``render_lines`` reads of a receipt: in-memory sections and ``_Members``."""

    def __init__(self, receipt_id: RecordId, sections: Mapping[str, Any]) -> None:
        self.id = receipt_id
        for name, value in sections.items():
            setattr(self, name, value)


class _Receipt:
    """The receipt core, accumulated one record at a time (``build_receipt``, as a stream)."""

    def __init__(self, sorter: Callable[[str], Sorter]) -> None:
        self.judges: set[str] = set()
        self.read_by: dict[str, set[str]] = defaultdict(set)
        self.transforms: list[ReceiptTransform] = []
        self.clocks: list[ReceiptClock] = []
        self.revisions: list[Any] = []
        self.absences: list[Any] = []
        self.streams_of: dict[str, int] = defaultdict(int)
        self.runs = sorter("receipt-runs")
        self.streams = sorter("receipt-streams")
        self.entities = sorter("receipt-entities")
        self.findings = sorter("receipt-findings")
        self.ambiguous = sorter("receipt-ambiguous")

    def add(self, record: Any, data: JsonValue) -> None:
        kind = record.kind
        if kind == "transform_record":
            self.transforms.append(
                ReceiptTransform(
                    record.id,
                    record.adapter_id,
                    record.adapter_version,
                    record.config_hash,
                    record.libraries,
                    record.upstream,
                )
            )
            if record.adapter_id in NON_READERS:
                self.judges.add(record.id)
            return
        if kind == "source_revision":
            self.revisions.append(record)
        elif kind == "source_absence":
            self.absences.append(record)
        if kind in _LEDGER:
            return
        for field in cite(record, data, self.judges, self.read_by):
            self.ambiguous.add((field.record, field.pointer), canonical_json.dumps(field.to_json()))
        if kind == "timestamp_domain":
            self.clocks.append(ReceiptClock(record.id, record.field, record.scope))
        elif kind == "stream":
            self.streams_of[record.run] += 1
            stream = ReceiptStream(
                id=record.id,
                run=record.run,
                topic=record.topic,
                clocks=record.clocks,
                message_count=record.message_count,
                first=record.first,
                last=record.last,
            )
            self.streams.add((record.id,), canonical_json.dumps(stream.to_json()))
        elif kind == "run":
            run = ReceiptRun(
                id=record.id,
                logical_id=record.logical_id,
                machine=record.machine,
                first=record.first,
                last=record.last,
                streams=self.streams_of.get(record.id, 0),
            )
            self.runs.add((record.id,), canonical_json.dumps(run.to_json()))
        elif kind in _ENTITIES:
            entity = ReceiptEntity(record.id, record.kind, _stated_ids(record.identifiers))
            self.entities.add((record.id,), canonical_json.dumps(entity.to_json()))
        elif kind == "ingest_finding":
            finding = ReceiptFinding(
                record.id, record.code, record.category, record.severity, record.message
            )
            self.findings.add(finding_order(finding), canonical_json.dumps(finding.to_json()))

    def close(self) -> None:
        """Remove the runs of the sections that went through sorters."""
        for sorter in (self.runs, self.streams, self.entities, self.findings, self.ambiguous):
            sorter.close()

    def sections(
        self, artifacts: Mapping[ContentId, int], counts: Mapping[str, int], version: int
    ) -> dict[str, Any]:
        """Every section of the core, checked as ``IngestReceipt`` checks its own."""
        sources, absent = ledger_sections(self.revisions, self.absences, artifacts, self.read_by)
        small: dict[str, Any] = {
            "sources": sources,
            "absent": absent,
            "transforms": tuple(self.transforms),
            "records": tuple(sorted(counts.items())),
            "clocks": tuple(self.clocks),
        }
        # The sections held in memory are checked by the receipt itself; the streamed ones are
        # sorted by their sorters and checked for repeats as they are read (``_Members``).
        IngestReceipt(
            id=RecordId("rec:sha256:" + "0" * 64),
            runs=(),
            streams=(),
            entities=(),
            findings=(),
            ambiguous=(),
            version=version,
            **small,
        )
        return {
            **small,
            "runs": _Members(self.runs, receipt_run_from_json),
            "streams": _Members(self.streams, receipt_stream_from_json),
            "entities": _Members(self.entities, receipt_entity_from_json, "entities ids"),
            "findings": _Members(self.findings, receipt_finding_from_json),
            "ambiguous": _Members(self.ambiguous, ambiguous_field_from_json, "ambiguous fields"),
        }


def _content_json(sections: Mapping[str, Any]) -> dict[str, Any]:
    """``IngestReceipt.content_json`` over ``sections``, the streamed ones left as ``_Members``."""

    def listed(name: str) -> Any:
        value = sections[name]
        return value if isinstance(value, _Members) else [item.to_json() for item in value]

    return {
        "absent": listed("absent"),
        "ambiguous": listed("ambiguous"),
        "clocks": listed("clocks"),
        "entities": listed("entities"),
        "findings": listed("findings"),
        "records": dict(sections["records"]),
        "runs": listed("runs"),
        "sources": listed("sources"),
        "streams": listed("streams"),
        "transforms": listed("transforms"),
    }


def table_order(kinds: Iterable[str]) -> list[str]:
    """The order a package's tables are read in: ``_FIRST``, then every other kind by name."""
    present = set(kinds)
    return [kind for kind in _FIRST if kind in present] + sorted(present - set(_FIRST))


class _Gathered:
    """What a package's tables give as they are read in ``table_order``, one record at a time:
    each record checked as the package reader checks it (lineage, series), and what the receipt
    and the manifest need. The writer and the reader (``neptune.store.reader``) share it, so what
    one writes is what the other accepts."""

    def __init__(
        self,
        sorter: Callable[[str], Sorter],
        series: Mapping[RecordId, Content],
        settings: JsonObject | None,
        version: int,
    ) -> None:
        self.receipt = _Receipt(sorter)
        self.version = version
        self.counts: dict[str, int] = {}
        self.transforms: dict[str, TransformRecord] = {}
        self.artifacts: dict[ContentId, int] = {}
        self.streams: set[RecordId] = set()
        self._series = series
        self._settings = settings

    def add(self, record: Any, data: JsonValue) -> None:
        """One record, read back from its line; ``data`` is its JSON."""
        kind = record.kind
        if kind == "transform_record":
            self.transforms[record.id] = _lineage(check_transform_record, record)
        else:
            check_record_lineage(record, self.transforms)
        if kind == "source_artifact":
            self.artifacts[record.content_id] = record.size
        elif kind == "stream":
            self.streams.add(record.id)
            if self._settings is not None and record.id in self._series:
                _check_series(record, self._series[record.id], self._settings)
        self.receipt.add(record, data)
        self.version = max(self.version, record_version(record))

    def done(self, kind: str, count: int) -> None:
        """A table read whole: ``count`` records. Transforms are checked against each other."""
        self.counts[kind] = count
        if kind == "transform_record":
            for transform in self.transforms.values():
                check_record_lineage(transform, self.transforms)

    def receipt_files(
        self, sink: Callable[[str], _Sink]
    ) -> tuple[RecordId, dict[str, tuple[Content, tuple[int, ContentId]]]]:
        """The receipt's id, and ``receipt.json`` and ``receipt.md`` written through ``sink`` as
        streams. ``counts`` must name every kind of the package's version by now."""
        sections = self.receipt.sections(self.artifacts, self.counts, self.version)
        core = _content_json(sections)
        hashed = hashlib.sha256()
        for piece in _encode({"inputs": core, "kind": RECEIPT_KIND, "scheme": RECORD_ID_SCHEME}):
            hashed.update(piece)
        receipt_id = RecordId("rec:sha256:" + hashed.hexdigest())
        document = _encode(envelope(RECEIPT_KIND, {**core, "id": receipt_id}, self.version))
        text = (line.encode("utf-8") for line in render_lines(_ReceiptView(receipt_id, sections)))
        return receipt_id, {
            RECEIPT: _write_all(sink(RECEIPT), document),
            RECEIPT_TEXT: _write_all(sink(RECEIPT_TEXT), text),
        }


def _write_all(sink: _Sink, pieces: Iterable[bytes]) -> tuple[Content, tuple[int, ContentId]]:
    try:
        for piece in pieces:
            sink.write(piece)
    except BaseException:
        sink.abandon()
        raise
    return sink.close()


class PackageWriter:
    """Build a package from records given one at a time, in bounded memory (ADR 0065).

    ``spill`` is the scratch directory sorted runs are spilled to (never the system temp
    directory): a private directory is made in it and removed by ``close``. Without one nothing
    spills and the package is held in memory, as ``package_contents`` holds it. ``budget`` is what
    the writer's unspilled entries may hold, in bytes. Use it as a context manager, or ``close`` it.
    """

    def __init__(self, spill: Path | None = None, *, budget: int = SPILL_BUDGET) -> None:
        self._space = SpillSpace(spill, budget)
        self._tables: dict[str, Sorter] = {}
        self._finished = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Remove every spilled run and the writer's scratch directory."""
        self._space.close()

    def add(self, record: Any, *, last_wins: bool = False) -> None:
        """Add one record. A record added with ``last_wins`` is replaced by a later record of the
        same kind and id added the same way; any other repeated id is refused at ``finish``."""
        if self._finished:
            raise PackageError("the package is already written")
        kind = getattr(record, "kind", None)
        if not isinstance(kind, str) or kind not in RECORD_KINDS:
            raise PackageError(f"not a record of a known kind: {record!r}")
        table = self._tables.get(kind)
        if table is None:
            table = self._tables[kind] = self._space.sorter(f"table-{kind}")
        flag = _REPEATABLE if last_wins else _SINGLE
        table.add((record_key(record),), flag + _document(record) + b"\n")

    def extend(self, records: Iterable[Any], *, last_wins: bool = False) -> None:
        for record in records:
            self.add(record, last_wins=last_wins)

    def finish(
        self,
        out: Path | None = None,
        *,
        series: Mapping[RecordId, Content] | None = None,
        blobs: Mapping[ContentId, Content] | None = None,
        store: JsonObject | None = None,
        derived: Mapping[str, Iterable[JsonObject]] | None = None,
    ) -> dict[str, Content]:
        """Every file of the package, by path, as ``package_contents`` gives them.

        With ``out``, the tables, derived tables and receipt files are written under it and given
        as paths; without, as bytes. The manifest is always bytes, and is not written: it names
        the package (``package_id``). ``series`` and ``blobs`` are given back as they came.
        """
        if self._finished:
            raise PackageError("the package is already written")
        self._finished = True
        series, blobs, store = dict(series or {}), dict(blobs or {}), dict(store or {})
        settings = _series_settings(store) if series else None
        # The lowest schema version that holds the records that survive replacement: their kinds'
        # versions, raised to any record's own later version (ADR 0037 §1, ADR 0061 §6). Lines
        # carry their own versions, so the tables are written before it is known; it decides only
        # which empty tables the package has, and the receipt's and manifest's version.
        gathered = _Gathered(self._space.sorter, series, settings, package_version(self._tables))
        files: dict[str, Content] = {}
        digests: dict[str, tuple[int, ContentId]] = {}

        def sink(path: str) -> _Sink:
            return _Sink(None if out is None else out / path)

        for kind in table_order(self._tables):
            path = table_path(kind)
            table = sink(path)
            count = 0
            try:
                for record, data, encoded in self._records(kind, path):
                    gathered.add(record, data)
                    table.write(encoded)
                    count += 1
            except BaseException:
                table.abandon()
                raise
            files[path], digests[path] = table.close()
            self._tables[kind].close()  # read once: its runs are not needed again
            gathered.done(kind, count)
        version = gathered.version
        kinds = kinds_at(version)
        for kind in sorted(set(kinds) - set(gathered.counts)):  # a kind of this version, no records
            path = table_path(kind)
            files[path], digests[path] = sink(path).close()
            gathered.done(kind, 0)

        for stream, file in sorted(series.items()):
            if stream not in gathered.streams:
                raise PackageError(f"series for {stream}, which is not a stream of this package")
            files[series_path(stream)] = file
            digests[series_path(stream)] = _digest(file)
        artifacts = gathered.artifacts
        for artifact, file in sorted(blobs.items()):
            if artifact not in artifacts:
                raise PackageError(f"blob {artifact} is not a source artifact of this package")
            digest = _digest(file)
            if digest != (artifacts[artifact], artifact):
                raise PackageError(f"blob bytes do not hash to {artifact}")
            files[blob_path(artifact)] = file
            digests[blob_path(artifact)] = digest
        transforms = set(gathered.transforms)
        for name, lines in sorted((derived or {}).items()):
            path = derived_path(name)
            if not _DERIVED.fullmatch(path):
                raise PackageError(f"not a derived table kind: {name!r}")
            files[path], digests[path] = self._derived(name, lines, transforms, sink(path))

        receipt_id, written = gathered.receipt_files(sink)
        gathered.receipt.close()  # its sections are written: their runs are not needed again
        for path, (content, digest) in written.items():
            files[path], digests[path] = content, digest

        manifest = PackageManifest(
            receipt=receipt_id,
            tables=tuple((kind, gathered.counts[kind]) for kind in sorted(kinds)),
            sources=tuple(
                SourceHandle(
                    content,
                    size,
                    Storage.MATERIALISED if content in blobs else Storage.REFERENCED,
                )
                for content, size in sorted(artifacts.items())
            ),
            files=tuple(PackageFile(path, *digests[path]) for path in sorted(digests)),
            store=store,
            version=version,
        )
        files[MANIFEST] = _document(manifest)
        return files

    def _records(self, kind: str, path: str) -> Iterator[tuple[Any, JsonValue, bytes]]:
        """The records of one table in id order, one per id, each read back from its line as the
        package reader reads it: ``(record, its JSON, its line)``."""
        table = self._tables.get(kind)
        if table is None:
            return
        read = RECORD_KINDS[kind][1]
        held: tuple[str | int, bytes] | None = None
        replaceable = True
        for (key,), payload in table:
            if held is not None and held[0] == key:
                replaceable = replaceable and payload[:1] == _REPEATABLE
                if not replaceable:
                    raise PackageError(f"two {kind} records share an id")
                held = (key, payload)
                continue
            if held is not None:
                yield _read_back(read, held[1][1:], path)
            held, replaceable = (key, payload), payload[:1] == _REPEATABLE
        if held is not None:
            yield _read_back(read, held[1][1:], path)

    def _derived(
        self, kind: str, lines: Iterable[JsonObject], transforms: set[str], sink: _Sink
    ) -> tuple[Content, tuple[int, ContentId]]:
        """A derived table, sorted by id, each line checked as the reader checks it. Its runs are
        removed as soon as it is written, not when the writer closes."""
        path = derived_path(kind)
        sorter = self._space.sorter(f"derived-{kind}")
        previous: Key | None = None
        try:
            for line in lines:
                ident = _derived_line(kind, line, transforms)
                sorter.add((ident,), canonical_json.dumps(line) + b"\n")
            for key, encoded in sorter:
                if previous is not None and key <= previous:
                    raise PackageError(f"{path} must name each id once")
                previous = key
                sink.write(encoded)
        except BaseException:
            sink.abandon()
            raise
        finally:
            sorter.close()
        return sink.close()


def _read_back(read: Callable[[JsonValue], Any], line: bytes, path: str) -> tuple[Any, Any, bytes]:
    """A record read from the line written for it, which must encode back to the same line."""
    record = read(json.loads(line))
    data = record.to_json()
    if canonical_json.dumps(data) + b"\n" != line:
        raise PackageError(f"{path} is not one canonical line per record")
    return record, data, line


def _lineage(check: Callable[[Any], Any], record: Any) -> Any:
    try:
        return check(record)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc


def _check_series(stream: Any, data: Content, settings: JsonObject) -> None:
    try:
        check_series(stream, data, settings)
    except SeriesError as exc:
        raise PackageError(f"{series_path(stream.id)}: {exc}") from exc


def write_package_stream(
    root: Path,
    records: Iterable[Any],
    *,
    scratch: Path,
    series: Mapping[RecordId, Content] | None = None,
    blobs: Mapping[ContentId, Content] | None = None,
    store: JsonObject | None = None,
    derived: Mapping[str, Iterable[JsonObject]] | None = None,
    budget: int = SPILL_BUDGET,
) -> ContentId:
    """Write the package of ``records`` into ``root``, in bounded memory; return its id.

    The bytes are ``write_package(root, package_contents(records, ...))``'s. ``records`` may be
    lazy and are read once. ``root`` must not exist or be empty. Sorted runs spill under
    ``scratch``, a directory of the caller's (a workspace's scratch space, never the system temp
    directory), and are removed whether or not the write succeeds. Series and blobs are copied in
    as streams, as ``write_package`` copies them. Like ``write_package``, the package is written
    beside ``root`` and renamed into place (ADR 0070): ``root`` holds all of it or none of it.
    """
    with replacing(root) as partial:
        with PackageWriter(scratch, budget=budget) as writer:
            writer.extend(records)
            contents = writer.finish(
                partial, series=series, blobs=blobs, store=store, derived=derived
            )
        lay_down(partial, contents)
    return package_id(contents)
