"""A rosbag2 bag's ``metadata.yaml``: what the bag says about itself, as stated evidence.

One chunk per file. The file is small, and everything in it is the recorder's own statement
(``stated``): the bag's start and duration, its storage and compression, the parts it lists, and
for each topic its type, serialisation and offered QoS profiles. It becomes

- one ``Run`` citing the ``rosbag2_bagfile_information`` mapping, its ``first`` the declared
  start and its ``last`` that start plus the declared duration (cited as one value: the bytes of
  both), on one ``TimestampDomain`` that names the key the ticks are read from;
- one ``StructuredTable`` per list the file holds (``topics_with_message_count``, ``files``,
  ``relative_file_paths``) and one named after the mapping for its scalar entries, each row
  cited by the bytes of its entry and each cell by its own;
- findings for everything the bag says that is wrong, doubtful or unreadable here.

The storage files the bag lists are other sources. This adapter reads nothing of them: the
reconciliation of what is listed with what is present is a cross-source check, so the findings
here are about the bag's own statements (``docs/adapter-contract.md``, law 9).
"""

import re
from collections import Counter
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    AdapterConfig,
    Chunk,
    ChunkOutput,
    InspectResult,
    Plan,
    SourceReader,
    make_chunk,
)
from neptune.adapters.rosbag2 import _yaml
from neptune.adapters.rosbag2._cite import Cite, clip
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotApplicable, Unknown
from neptune.model.provenance import ByteRange, Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run
from neptune.model.time import INT64_MAX, NANOSECOND, Timestamp
from neptune.model.world import StructuredRecord, StructuredTable

ROOT_KEY: Final = "rosbag2_bagfile_information"
LISTS: Final = ("topics_with_message_count", "files", "relative_file_paths")
KNOWN_STORAGE: Final = {"sqlite3": ".db3", "mcap": ".mcap"}
KNOWN_VERSIONS: Final = range(1, 10)
MAX_ROWS: Final = 10_000
LISTED: Final = 16  # entries a finding lists before it counts the rest
PART: Final = re.compile(r"(?P<stem>.*)_(?P<index>[0-9]+)(?P<ext>\.[^./]+)(\.[a-z0-9]+)?")
TOPIC_COLUMNS: Final = (
    "name",
    "type",
    "serialization_format",
    "offered_qos_profiles",
    "type_description_hash",
    "message_count",
)
FILE_COLUMNS: Final = ("path", "starting_time.nanoseconds_since_epoch", "duration.nanoseconds")
Cell = Knowledge[str | int | bool | float]

if TYPE_CHECKING:
    from neptune.identity.provenance import EvidenceRecord


def inspect_metadata(source: SourceReader) -> InspectResult:
    """The file's identity and size, without parsing the whole of it."""
    head = source.read(0, min(source.size, 4096))
    return InspectResult(
        {
            "part": "metadata",
            "root_key": ROOT_KEY,
            "size": source.size,
            "starts_with_root_key": ROOT_KEY.encode() in head,
        }
    )


def plan_metadata(source: SourceReader, config: AdapterConfig) -> Plan:
    return Plan((make_chunk(source, config, {"part": "metadata"}, source.size),))


def _integer(scalar: _yaml.Scalar | None) -> int | None:
    """A plain decimal integer scalar, else ``None``: YAML typing, without YAML's guesses."""
    if scalar is None or scalar.quoted or not re.fullmatch(r"[0-9]{1,19}", scalar.text):
        return None
    return int(scalar.text)


def _scalar(node: _yaml.Node | None) -> _yaml.Scalar | None:
    return node if isinstance(node, _yaml.Scalar) else None


def _mapping(node: _yaml.Node | None) -> _yaml.Mapping | None:
    return node if isinstance(node, _yaml.Mapping) else None


def _entry(mapping: _yaml.Mapping | None, key: str) -> _yaml.Entry | None:
    if mapping is not None:
        for entry in mapping.entries:
            if entry.key.text == key:
                return entry
    return None


def _path(mapping: _yaml.Mapping | None, *keys: str) -> _yaml.Node | None:
    node: _yaml.Node | None = mapping
    for key in keys:
        entry = _entry(_mapping(node), key)
        node = entry.value if entry else None
    return node


def _unsafe(path: str) -> str | None:
    """Why a listed part path could escape its bag's directory, or ``None``."""
    if not path:
        return "empty"
    if "\0" in path:
        return "nul"
    if path.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", path):
        return "absolute"
    if "\\" in path:
        return "backslash"
    if ".." in path.split("/"):
        return "parent"
    return None


@dataclass
class _Part:
    path: _yaml.Scalar
    entry: _yaml.Node
    start: _yaml.Scalar | None = None
    duration: _yaml.Scalar | None = None
    count: _yaml.Scalar | None = None


class Metadata:
    """Reads one ``metadata.yaml`` source into records and findings."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.cite = Cite(source, config)
        self.records: list[EvidenceRecord] = []
        self.findings: list[IngestFinding] = []

    # --- plumbing ------------------------------------------------------------------------------

    def range(self, node: _yaml.Node | _yaml.Scalar) -> ByteRange:
        start, end = _yaml.span(node)
        return clip(self.source, start, end - start)

    def say(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        where: ByteRange,
        message: str,
        details: dict[str, JsonValue] | None = None,
        related: SequenceABC[ByteRange] = (),
    ) -> None:
        self.findings.append(
            self.cite.finding(
                code,
                category,
                severity,
                (where,),
                message,
                details,
                related=[(r,) for r in related],
            )
        )

    def stated(self, where: ByteRange) -> Provenance:
        return self.cite.provenance(where, kind=AssertionKind.STATED)

    def cell(self, scalar: _yaml.Scalar | None, fallback: ByteRange) -> Cell:
        """A cell as the file types it: a plain integer is a number, everything else its text."""
        if scalar is None:
            return Unknown(self.stated(fallback))
        where = self.range(scalar) if scalar.length else fallback
        if not scalar.text or (not scalar.quoted and scalar.text in ("~", "null")):
            return Unknown(self.stated(where))
        number = _integer(scalar)
        return Known(number if number is not None else scalar.text, self.stated(where))

    def raw_cell(self, node: _yaml.Node | None, fallback: ByteRange) -> Cell:
        """A cell for a field that should be text: a scalar, or a nested value as its own YAML."""
        if node is None or isinstance(node, _yaml.Scalar):
            return self.cell(node, fallback)
        where = self.range(node)
        text = self.source.read(where.offset, where.length).decode("utf-8", "replace").strip()
        return Known(text, self.stated(where)) if text else Unknown(self.stated(where))

    # --- the file ------------------------------------------------------------------------------

    def run(self) -> ChunkOutput:
        source = self.source
        if source.size > _yaml.MAX_BYTES:
            document = _yaml.Document(None, [_yaml.YamlLimitError(f"{source.size} bytes", 1)])
        else:
            document = _yaml.parse(source.read(0, source.size))
        top = clip(source, 0, min(source.size, 64))
        for error in document.errors[:LISTED]:
            line = error.line - 1
            where = clip(source, *document.lines[line]) if line < len(document.lines) else top
            self.say(
                "unsupported_yaml",
                FindingCategory.UNSUPPORTED,
                Severity.WARNING,
                where,
                f"{error.message} (line {error.line}); what it belongs to is not read",
                {"line": error.line, "reason": error.message},
            )
        if len(document.errors) > LISTED:
            self.say(
                "unsupported_yaml",
                FindingCategory.UNSUPPORTED,
                Severity.WARNING,
                top,
                f"{len(document.errors) - LISTED} more lines are not read",
                {"omitted": len(document.errors) - LISTED},
            )
        root = _mapping(document.root)
        info_entry = _entry(root, ROOT_KEY)
        info = _mapping(info_entry.value) if info_entry else None
        if info_entry is None or info is None:
            if document.root is not None or not document.errors:
                self.say(
                    "not_bag_metadata",
                    FindingCategory.UNSUPPORTED,
                    Severity.ERROR,
                    top,
                    f"the file has no `{ROOT_KEY}` mapping, so it is not read as a bag's metadata",
                    {"root_key": ROOT_KEY},
                )
            return self.output()
        self.duplicates(info)
        self.bag(info_entry, info)
        return self.output()

    def output(self) -> ChunkOutput:
        return ChunkOutput(records=tuple(self.records), findings=tuple(self.findings))

    def duplicates(self, info: _yaml.Mapping) -> None:
        if info.duplicates:
            self.say(
                "duplicate_key",
                FindingCategory.AMBIGUOUS,
                Severity.WARNING,
                self.range(info),
                f"{len(set(info.duplicates))} key(s) repeat in the bag's mapping; the first of"
                " each is read",
                {"keys": sorted(set(info.duplicates))[:LISTED]},
            )

    # --- the bag -------------------------------------------------------------------------------

    def bag(self, info_entry: _yaml.Entry, info: _yaml.Mapping) -> None:
        cite = self.cite
        place = self.range(info)
        self.run_record(info, place)
        self.check_storage(info, place)
        paths = self.listed_paths(info)
        parts = self.listed_files(info)
        self.check_parts(info, paths, parts)
        total = _integer(_scalar(_path(info, "message_count")))
        self.check_counts(info, total)
        # Tables: the scalar entries, then each list.
        bag_table = StructuredTable(
            id=cite.record_id(StructuredTable.kind, place),
            provenance=self.stated(place),
            name=Known(ROOT_KEY, self.stated(self.range(info_entry.key))),
            header=NotApplicable(),
        )
        self.records.append(bag_table)
        rows = list(_flatten(info))
        if len(rows) > MAX_ROWS:
            self.say(
                "too_many_entries",
                FindingCategory.LIMIT,
                Severity.WARNING,
                place,
                f"the bag lists {len(rows)} scalar entries; the first {MAX_ROWS} are tabled",
                {"entries": len(rows), "limit": MAX_ROWS},
            )
        for row, (key, key_node, value) in enumerate(rows[:MAX_ROWS]):
            where = clip(self.source, key_node.start, value.end - key_node.start)
            self.add_row(
                bag_table,
                row,
                where,
                [
                    self.cell(
                        _yaml.Scalar(key, key_node.start, key_node.length, key_node.quoted), where
                    ),
                    self.cell(value, where),
                ],
            )
        for name in LISTS:
            entry = _entry(info, name)
            if entry is not None and isinstance(entry.value, _yaml.Sequence):
                self.list_table(name, entry)

    def add_row(
        self, table: StructuredTable, row: int, where: ByteRange, cells: list[Cell]
    ) -> None:
        self.records.append(
            StructuredRecord(
                id=self.cite.record_id(StructuredRecord.kind, where),
                provenance=self.stated(where),
                table=table.id,
                row=row,
                cells=tuple(cells),
            )
        )

    def list_table(self, name: str, entry: _yaml.Entry) -> None:
        sequence = entry.value
        assert isinstance(sequence, _yaml.Sequence)
        where = self.range(sequence)
        table = StructuredTable(
            id=self.cite.record_id(StructuredTable.kind, where),
            provenance=self.stated(where),
            name=Known(name, self.stated(self.range(entry.key))),
            header=NotApplicable(),
        )
        self.records.append(table)
        items = sequence.items[:MAX_ROWS]
        if len(sequence.items) > MAX_ROWS:
            self.say(
                "too_many_entries",
                FindingCategory.LIMIT,
                Severity.WARNING,
                where,
                f"`{name}` lists {len(sequence.items)} entries; the first {MAX_ROWS} are tabled",
                {"entries": len(sequence.items), "limit": MAX_ROWS, "list": name},
            )
        for row, item in enumerate(items):
            here = self.range(item)
            if name == "topics_with_message_count":
                cells = self.topic_cells(item, here)
            elif name == "files":
                cells = self.file_cells(item, here)
            else:
                cells = [self.cell(_scalar(item), here)]
            self.add_row(table, row, here, cells)

    def topic_cells(self, item: _yaml.Node, here: ByteRange) -> list[Cell]:
        mapping = _mapping(item)
        meta = _mapping(_path(mapping, "topic_metadata"))
        cells = [self.cell(_scalar(_path(meta, key)), here) for key in TOPIC_COLUMNS[:3]]
        cells.append(self.raw_cell(_path(meta, "offered_qos_profiles"), here))
        cells.append(self.cell(_scalar(_path(meta, "type_description_hash")), here))
        cells.append(self.cell(_scalar(_path(mapping, "message_count")), here))
        return cells

    def file_cells(self, item: _yaml.Node, here: ByteRange) -> list[Cell]:
        if isinstance(item, _yaml.Scalar):
            return [self.cell(item, here)]
        mapping = _mapping(item)
        return [
            self.cell(_scalar(_path(mapping, "path")), here),
            self.cell(_scalar(_path(mapping, "starting_time", "nanoseconds_since_epoch")), here),
            self.cell(_scalar(_path(mapping, "duration", "nanoseconds")), here),
            self.cell(_scalar(_path(mapping, "message_count")), here),
        ]

    # --- the run -------------------------------------------------------------------------------

    def run_record(self, info: _yaml.Mapping, place: ByteRange) -> None:
        cite = self.cite
        start_entry = _entry(_mapping(_path(info, "starting_time")), "nanoseconds_since_epoch")
        duration_entry = _entry(_mapping(_path(info, "duration")), "nanoseconds")
        start = _scalar(start_entry.value) if start_entry else None
        duration = _scalar(duration_entry.value) if duration_entry else None
        first: Knowledge[Timestamp] = Unknown()
        last: Knowledge[Timestamp] = Unknown()
        ticks, length = _integer(start), _integer(duration)
        for what, scalar, number in (
            ("starting_time", start, ticks),
            ("duration", duration, length),
        ):
            if scalar is not None and number is None:
                self.say(
                    "bad_field",
                    FindingCategory.UNREPRESENTABLE,
                    Severity.WARNING,
                    self.range(scalar),
                    f"`{what}` is not a non-negative integer of nanoseconds; it is unknown",
                    {"field": what, "text": scalar.text[:64]},
                )
        if start_entry is not None and start is not None and ticks is not None:
            if ticks > INT64_MAX:
                self.time_range("starting_time", start)
            else:
                key = self.range(start_entry.key)
                domain = TimestampDomain(
                    id=cite.record_id(TimestampDomain.kind, key),
                    provenance=self.stated(key),
                    field="starting_time.nanoseconds_since_epoch",
                    scope=(),
                    role=Unknown(),
                    resolution=Known(NANOSECOND, self.stated(key)),
                    epoch=Unknown(),
                    timescale=Unknown(),
                    declared_monotonic=Unknown(),
                )
                self.records.append(domain)
                first = Known(Timestamp(ticks, domain.id), self.stated(self.range(start)))
                if length is not None and duration is not None:
                    if ticks + length > INT64_MAX:
                        self.time_range("starting_time + duration", duration)
                    else:
                        low = min(start.start, duration.start)
                        high = max(start.end, duration.end)
                        both = self.stated(clip(self.source, low, high - low))
                        last = Known(Timestamp(ticks + length, domain.id), both)
        self.records.append(
            Run(
                id=cite.record_id(Run.kind, place),
                provenance=self.stated(place),
                logical_id=Unknown(),
                machine=Unknown(),
                first=first,
                last=last,
            )
        )

    def time_range(self, what: str, scalar: _yaml.Scalar) -> None:
        self.say(
            "time_out_of_range",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            self.range(scalar),
            f"{what} does not fit a signed 64-bit tick count; it is unknown",
            {"field": what},
        )

    # --- checks of the bag's own statements ------------------------------------------------------

    def check_storage(self, info: _yaml.Mapping, place: ByteRange) -> None:
        version = _scalar(_path(info, "version"))
        number = _integer(version)
        if version is not None and (number is None or number not in KNOWN_VERSIONS):
            self.say(
                "unknown_version",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                self.range(version),
                f"metadata version {version.text[:32]!r} is not one this adapter was written"
                " against (1 to 9); it is read as the nearest layout",
                {"version": version.text[:32]},
            )
        storage = _scalar(_path(info, "storage_identifier"))
        if storage is not None and storage.text not in KNOWN_STORAGE:
            self.say(
                "unknown_storage",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                self.range(storage),
                f"storage `{storage.text[:64]}` is not sqlite3 or mcap; the parts are read by"
                " whatever adapter claims them",
                {"storage_identifier": storage.text[:64]},
            )
        formats = [
            (key, node)
            for key in ("compression_format", "compression_mode")
            if (node := _scalar(_path(info, key))) is not None and node.text
        ]
        if formats:
            self.say(
                "compressed_storage",
                FindingCategory.UNSUPPORTED,
                Severity.WARNING,
                self.range(formats[0][1]),
                "the bag declares compression; its parts are not plain sqlite3 or MCAP files and"
                " no adapter reads them",
                {key: node.text[:32] for key, node in formats},
            )

    def listed_paths(self, info: _yaml.Mapping) -> list[_yaml.Scalar]:
        node = _path(info, "relative_file_paths")
        if not isinstance(node, _yaml.Sequence):
            return []
        return [item for item in node.items if isinstance(item, _yaml.Scalar)]

    def listed_files(self, info: _yaml.Mapping) -> list[_Part]:
        node = _path(info, "files")
        parts: list[_Part] = []
        if not isinstance(node, _yaml.Sequence):
            return parts
        for item in node.items:
            if isinstance(item, _yaml.Scalar):
                parts.append(_Part(item, item))
            elif isinstance(item, _yaml.Mapping) and (path := _scalar(_path(item, "path"))):
                parts.append(
                    _Part(
                        path,
                        item,
                        _scalar(_path(item, "starting_time", "nanoseconds_since_epoch")),
                        _scalar(_path(item, "duration", "nanoseconds")),
                        _scalar(_path(item, "message_count")),
                    )
                )
        return parts

    def check_parts(
        self, info: _yaml.Mapping, paths: list[_yaml.Scalar], parts: list[_Part]
    ) -> None:
        declared = [(scalar, scalar.text) for scalar in paths]
        declared += [(part.path, part.path.text) for part in parts]
        unsafe: dict[str, tuple[_yaml.Scalar, str]] = {}
        for scalar, text in declared:
            why = _unsafe(text)
            if why is not None and text not in unsafe:
                unsafe[text] = (scalar, why)
        if unsafe:
            first, _ = next(iter(unsafe.values()))
            self.say(
                "unsafe_part_path",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                self.range(first),
                f"{len(unsafe)} listed part path(s) are empty, absolute or leave the bag's"
                " directory: they are kept as declared and must not be opened relative to the bag",
                {
                    "paths": {text[:256]: why for text, (_, why) in list(unsafe.items())[:LISTED]},
                    "total": len(unsafe),
                },
            )
        for label, group in (
            ("relative_file_paths", paths),
            ("files", [p.path for p in parts]),
        ):
            counts = Counter(scalar.text for scalar in group)
            repeated = sorted(text for text, n in counts.items() if n > 1)
            if repeated:
                first = next(scalar for scalar in group if scalar.text == repeated[0])
                self.say(
                    "duplicate_part",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    self.range(first),
                    f"`{label}` lists {len(repeated)} part(s) more than once",
                    {"list": label, "parts": repeated[:LISTED], "total": len(repeated)},
                )
        in_paths = {s.text for s in paths}
        in_files = {p.path.text for p in parts}
        if paths and parts and in_paths != in_files:
            only_paths, only_files = sorted(in_paths - in_files), sorted(in_files - in_paths)
            candidates = [s for s in paths if s.text not in in_files]
            candidates += [p.path for p in parts if p.path.text not in in_paths]
            anchor = candidates[0]
            self.say(
                "parts_disagree",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                self.range(anchor),
                "`relative_file_paths` and `files` list different parts: the parts only one of"
                " them lists are missing from, or extra to, the other",
                {
                    "only_in_files": only_files[:LISTED],
                    "only_in_relative_file_paths": only_paths[:LISTED],
                },
            )
        self.check_sequence(sorted(in_paths | in_files), paths, parts)
        self.check_order(parts)
        storage = _scalar(_path(info, "storage_identifier"))
        suffix = KNOWN_STORAGE.get(storage.text) if storage else None
        if suffix is not None:
            wrong = sorted(p for p in in_paths | in_files if not p.endswith(suffix))
            if wrong:
                self.say(
                    "storage_mismatch",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    self.range(storage) if storage else self.range(info),
                    f"the bag declares `{storage.text if storage else ''}` storage but lists"
                    f" parts that do not end in `{suffix}`",
                    {"parts": wrong[:LISTED], "suffix": suffix},
                )

    def check_sequence(
        self, names: list[str], paths: list[_yaml.Scalar], parts: list[_Part]
    ) -> None:
        """rosbag2 names the parts of a split bag ``<name>_<n>.<ext>``, counting from 0."""
        found = [m for n in names if (m := PART.fullmatch(n.rsplit("/", 1)[-1]))]
        if not found or len(found) != len(names):
            return
        if len({m["stem"] + m["ext"] for m in found}) != 1:
            return
        indices = {int(m["index"]) for m in found}
        last = max(indices)
        missing_count = last + 1 - len(indices)  # numbering counts from 0
        if missing_count:
            missing: list[int] = []
            for index in range(last + 1):
                if index not in indices:
                    missing.append(index)
                    if len(missing) >= 64:
                        break
            anchor = paths[0] if paths else parts[0].path
            self.say(
                "part_gap",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                self.range(anchor),
                "the listed parts of a split bag are not numbered from 0 without gaps: parts the"
                " numbering implies are missing",
                {
                    "first_index": min(indices),
                    "missing": missing,
                    "missing_count": missing_count,
                    "parts": len(indices),
                },
            )

    def check_order(self, parts: list[_Part]) -> None:
        starts = [(p, n) for p in parts if (n := _integer(p.start)) is not None]
        for (_, left), (later, right) in pairwise(starts):
            if right < left:
                assert later.start is not None
                self.say(
                    "part_order",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    self.range(later.start),
                    "`files` does not list the parts in order of their start times; the table"
                    " keeps the listed order and consumers must sort by start time",
                    {"path": later.path.text[:256]},
                )
                return

    def check_counts(self, info: _yaml.Mapping, total: int | None) -> None:
        if total is None:
            return
        sums: dict[str, tuple[int, ByteRange]] = {}
        topics = _path(info, "topics_with_message_count")
        files = _path(info, "files")
        for label, node, route in (
            ("topics_with_message_count", topics, ("message_count",)),
            ("files", files, ("message_count",)),
        ):
            if not isinstance(node, _yaml.Sequence) or not node.items:
                continue
            counts = [_integer(_scalar(_path(_mapping(item), *route))) for item in node.items]
            if all(c is not None for c in counts):
                sums[label] = (sum(c for c in counts if c is not None), self.range(node))
        for label, (counted, where) in sums.items():
            if counted != total:
                self.say(
                    "count_mismatch",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    where,
                    f"the messages `{label}` count add up to {counted}, not the bag's"
                    f" message_count {total}",
                    {"list": label, "message_count": total, "sum": counted},
                )


def _flatten(
    node: _yaml.Node, prefix: str = "", skip: bool = True
) -> list[tuple[str, _yaml.Scalar, _yaml.Scalar]]:
    """The scalar leaves of a mapping as ``(dotted key, key scalar, value scalar)``, in order.

    The three lists have tables of their own; a list elsewhere is flattened with ``[i]``."""
    found: list[tuple[str, _yaml.Scalar, _yaml.Scalar]] = []
    if not isinstance(node, _yaml.Mapping):
        return found
    for entry in node.entries:
        name = prefix + entry.key.text
        value = entry.value
        if skip and not prefix and entry.key.text in LISTS and isinstance(value, _yaml.Sequence):
            continue  # a list with a table of its own
        if isinstance(value, _yaml.Scalar):
            found.append((name, entry.key, value))
        elif isinstance(value, _yaml.Mapping):
            found += _flatten(value, name + ".", skip)
        else:
            for index, item in enumerate(value.items):
                if isinstance(item, _yaml.Scalar):
                    found.append((f"{name}[{index}]", entry.key, item))
    return found


def ingest_metadata(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
    return Metadata(source, config).run()


__all__ = ["ingest_metadata", "inspect_metadata", "plan_metadata"]
