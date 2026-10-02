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
import shutil
import tempfile
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
from neptune.model.kinds import RECORD_KINDS, kinds_at, package_version, record_key
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
    copy_file,
    derived_path,
    package_id,
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
from neptune.store.spill import SPILL_BUDGET, Key, Sorter, SpillBudget

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
    memory or written to ``path``, through a buffer of about a megabyte."""

    def __init__(self, path: Path | None) -> None:
        self._hash = hashlib.sha256()
        self._size = 0
        self._buffer = bytearray()
        self._parts: list[bytes] = []
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
        else:
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


class PackageWriter:
    """Build a package from records given one at a time, in bounded memory (ADR 0065).

    ``spill`` is the scratch directory sorted runs are spilled to (never the system temp
    directory): a private directory is made in it and removed by ``close``. Without one nothing
    spills and the package is held in memory, as ``package_contents`` holds it. ``budget`` is what
    the writer's unspilled entries may hold, in bytes. Use it as a context manager, or ``close`` it.
    """

    def __init__(self, spill: Path | None = None, *, budget: int = SPILL_BUDGET) -> None:
        self._directory = (
            None if spill is None else Path(tempfile.mkdtemp(prefix="spill-", dir=spill))
        )
        self._budget = SpillBudget(budget)
        self._sorters: list[Sorter] = []
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
        for sorter in self._sorters:
            sorter.close()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    def _sorter(self, name: str) -> Sorter:
        sorter = Sorter(name, self._directory, self._budget)
        self._sorters.append(sorter)
        return sorter

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
            table = self._tables[kind] = self._sorter(f"table-{kind}")
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
        version = package_version(self._tables)
        kinds = kinds_at(version)
        order: list[str] = [kind for kind in _FIRST if kind in kinds]
        order += sorted(kind for kind in kinds if kind not in _FIRST)
        receipt = _Receipt(self._sorter)
        files: dict[str, Content] = {}
        digests: dict[str, tuple[int, ContentId]] = {}
        counts: dict[str, int] = {}
        transforms: dict[str, TransformRecord] = {}
        artifacts: dict[ContentId, int] = {}
        streams: set[RecordId] = set()

        def sink(path: str) -> _Sink:
            return _Sink(None if out is None else out / path)

        for kind in order:
            path = table_path(kind)
            table = sink(path)
            count = 0
            try:
                for record, data, encoded in self._records(kind, path):
                    if kind == "transform_record":
                        transforms[record.id] = _lineage(check_transform_record, record)
                    else:
                        check_record_lineage(record, transforms)
                    if kind == "source_artifact":
                        artifacts[record.content_id] = record.size
                    elif kind == "stream":
                        streams.add(record.id)
                        if settings is not None and record.id in series:
                            _check_series(record, series[record.id], settings)
                    receipt.add(record, data)
                    table.write(encoded)
                    count += 1
            except BaseException:
                table.abandon()
                raise
            files[path], digests[path] = table.close()
            counts[kind] = count
            if kind in self._tables:  # read once: its runs are not needed again
                self._tables[kind].close()
            if kind == "transform_record":
                for transform in transforms.values():
                    check_record_lineage(transform, transforms)

        for stream, file in sorted(series.items()):
            if stream not in streams:
                raise PackageError(f"series for {stream}, which is not a stream of this package")
            files[series_path(stream)] = file
            digests[series_path(stream)] = _digest(file)
        for artifact, file in sorted(blobs.items()):
            if artifact not in artifacts:
                raise PackageError(f"blob {artifact} is not a source artifact of this package")
            digest = _digest(file)
            if digest != (artifacts[artifact], artifact):
                raise PackageError(f"blob bytes do not hash to {artifact}")
            files[blob_path(artifact)] = file
            digests[blob_path(artifact)] = digest
        for name, lines in sorted((derived or {}).items()):
            path = derived_path(name)
            if not _DERIVED.fullmatch(path):
                raise PackageError(f"not a derived table kind: {name!r}")
            files[path], digests[path] = self._derived(name, lines, set(transforms), sink(path))

        sections = receipt.sections(artifacts, counts, version)
        core = _content_json(sections)
        hashed = hashlib.sha256()
        payload = {"inputs": core, "kind": RECEIPT_KIND, "scheme": RECORD_ID_SCHEME}
        for piece in _encode(payload):
            hashed.update(piece)
        receipt_id = RecordId("rec:sha256:" + hashed.hexdigest())
        document = sink(RECEIPT)
        try:
            for piece in _encode(envelope(RECEIPT_KIND, {**core, "id": receipt_id}, version)):
                document.write(piece)
        except BaseException:
            document.abandon()
            raise
        files[RECEIPT], digests[RECEIPT] = document.close()
        text = sink(RECEIPT_TEXT)
        try:
            for rendered in render_lines(_ReceiptView(receipt_id, sections)):
                text.write(rendered.encode("utf-8"))
        except BaseException:
            text.abandon()
            raise
        files[RECEIPT_TEXT], digests[RECEIPT_TEXT] = text.close()

        manifest = PackageManifest(
            receipt=receipt_id,
            tables=tuple((kind, counts[kind]) for kind in sorted(kinds)),
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
        """A derived table, sorted by id, each line checked as the reader checks it."""
        path = derived_path(kind)
        sorter = self._sorter(f"derived-{kind}")
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
    as streams, as ``write_package`` copies them.
    """
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise PackageError(f"{root} is not an empty directory")
    root.mkdir(parents=True, exist_ok=True)
    try:
        with PackageWriter(scratch, budget=budget) as writer:
            writer.extend(records)
            contents = writer.finish(root, series=series, blobs=blobs, store=store, derived=derived)
        for relative, data in sorted(contents.items()):
            target = root / relative
            if isinstance(data, bytes):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            elif data != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_file(data, target)
    except BaseException:
        for entry in list(root.iterdir()):  # it was empty: leave it so, not half a package
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        raise
    return package_id(contents)
