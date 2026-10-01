"""ROS 2 rosbag2 bags (https://github.com/ros2/rosbag2): metadata and sqlite3 storage.

A bag is a directory: a ``metadata.yaml`` and one or more storage files, ``.db3`` (sqlite3) or
``.mcap``. Each file is a source of its own, so each is read by the adapter that claims it
(ADR 0045 §1):

- ``metadata.yaml`` and ``.db3`` files are this adapter's. The metadata gives a ``Run``, its clock
  and tables (topics with their QoS, the parts, the scalar entries), all ``stated``; a sqlite3
  file gives what an MCAP file gives (ADR 0034): a ``Run``, a clock, a ``Stream`` per topic and a
  series row per message citing its cell, so the two storage backends of one recording give
  equivalent runs and streams.
- ``.mcap`` files are the MCAP adapter's, unchanged: nothing of MCAP is read or copied here.

Timestamps are ticks of the bag's own clock as declared, never converted. The parts of a split bag
are separate sources and separate streams of their topic; which parts are listed and which are
present is reconciled across sources, so this adapter reports only what a source says of itself.
"""

from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    SIGNATURE,
    VERIFIED,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    Documented,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
)
from neptune.adapters.rosbag2 import metadata, storage
from neptune.adapters.rosbag2._sqlite import MAGIC, Database, SqliteError, Walk, read_schema

DEFAULT_MAX_ROWS: Final = 100_000
ROOT: Final = "rosbag2_bagfile_information"


def _code(name: str, description: str) -> Documented:
    return Documented(f"rosbag2.{name}", description)


DESCRIPTOR: Final = AdapterDescriptor(
    id="rosbag2",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="ROS 2 bags: metadata.yaml as stated tables and a run, sqlite3 storage as a run and a"
    " stream per topic with every message's cell.",
    formats=(
        FormatSpec("rosbag2 metadata", extensions=(".yaml",)),
        FormatSpec("rosbag2 sqlite3 storage", extensions=(".db3",), magic=(Magic(0, MAGIC),)),
    ),
    record_kinds=("run", "stream", "structured_record", "structured_table", "timestamp_domain"),
    config=(),
    libraries=(),
    finding_codes=(
        _code("bad_database", "the source is not a SQLite database this reader opens (corrupt)"),
        _code(
            "bad_field",
            "a start or duration that is not a non-negative integer; it is unknown"
            " (unrepresentable, warning)",
        ),
        _code(
            "bad_row",
            "message rows whose record does not parse or whose topic or timestamp is not an"
            " integer, counted per reason; they get no rows (corrupt, warning)",
        ),
        _code(
            "compressed_storage",
            "the bag declares file or message compression; no adapter reads such parts"
            " (unsupported, warning)",
        ),
        _code(
            "count_mismatch",
            "the per-topic or per-file message counts do not add up to the bag's message_count"
            " (inconsistent, warning)",
        ),
        _code(
            "damaged_page",
            "b-tree pages that do not hold, per reason; the rows under them are not read"
            " (corrupt, error)",
        ),
        _code(
            "definition_not_local",
            "a type's definition spills into overflow pages, so its bytes are not one range"
            " (limit, info)",
        ),
        _code("duplicate_key", "a key repeats in the bag's mapping; the first is read (ambiguous)"),
        _code(
            "duplicate_part",
            "a part listed more than once in one list (inconsistent, warning)",
        ),
        _code(
            "invalid_utf8",
            "a topics column that is not UTF-8 text held in its page; it is unknown"
            " (unrepresentable, warning)",
        ),
        _code(
            "not_bag_metadata",
            "a YAML file without the rosbag2_bagfile_information mapping (unsupported, error)",
        ),
        _code(
            "not_rosbag2_storage",
            "a database without rosbag2's topics and messages tables (unsupported, error)",
        ),
        _code(
            "part_gap",
            "the listed parts of a split bag are not numbered from 0 without gaps: parts the"
            " numbering implies are missing (inconsistent, warning)",
        ),
        _code(
            "part_order",
            "the files list is not in order of start time (inconsistent, warning)",
        ),
        _code(
            "part_unnumbered",
            "listed part names ending in more than 18 digits are not read as numbered parts"
            " (unsupported, info)",
        ),
        _code(
            "parts_disagree",
            "relative_file_paths and files list different parts: missing from or extra to each"
            " other (inconsistent, warning)",
        ),
        _code(
            "payload_not_decoded",
            "a stream's payloads are not decoded; each row cites its message (unsupported, info)",
        ),
        _code(
            "storage_mismatch",
            "listed parts do not carry the extension the declared storage uses"
            " (inconsistent, warning)",
        ),
        _code(
            "time_out_of_range",
            "a declared time does not fit a signed 64-bit tick count; it is unknown"
            " (unrepresentable, warning)",
        ),
        _code(
            "too_many_entries",
            "more entries than are tabled (limit, warning)",
        ),
        _code(
            "too_many_topics",
            "more topics rows than are read (limit, error)",
        ),
        _code(
            "truncated",
            "the file is shorter than its header declares or not a whole number of pages;"
            " the missing pages are not read (corrupt, warning)",
        ),
        _code(
            "unknown_storage",
            "a storage_identifier other than sqlite3 and mcap (unsupported, info)",
        ),
        _code(
            "unknown_topic",
            "messages naming a topic id no topics row declares; no rows (corrupt, error)",
        ),
        _code(
            "unknown_version",
            "a metadata version outside 1 to 9; read as the nearest layout (unsupported, info)",
        ),
        _code(
            "unsafe_part_path",
            "a listed part path that is absolute, empty or leaves the bag's directory; kept as"
            " declared, never to be opened relative to the bag (corrupt, warning)",
        ),
        _code(
            "unsupported_yaml",
            "YAML outside the subset rosbag2 writes, or malformed; what it belongs to is not"
            " read (unsupported, warning)",
        ),
        _code(
            "wal_not_read",
            "the database is in WAL mode; a -wal file is another source and is not read"
            " (limit, info)",
        ),
    ),
    locator_steps=(
        Documented(
            "rosbag2:time_field",
            "the time column `name` (timestamp) of a sqlite3 bag's messages table, cited after"
            " the bytes that establish the format",
        ),
    ),
    conventions=(
        Documented(
            "chunks",
            "a metadata file is one chunk; a sqlite3 file is a declarations chunk (clock, run,"
            " streams with their counts and the run's extent) and data chunks, each a rowid range"
            " [lo, hi] with the seq each topic starts from",
        ),
        Documented(
            "clocks",
            "clock 0 is the messages table's timestamp column (scope (), role receive), ticks as"
            " stored; the metadata's start and duration are on a clock of their own (field"
            " starting_time.nanoseconds_since_epoch) with unknown role and epoch",
        ),
        Documented(
            "locators",
            "a sqlite3 row cites its message's cell in the file: the payload size, rowid and the"
            " record's local part, then an overflow page number when the payload spills",
        ),
        Documented(
            "run",
            "metadata: first is the declared start, last start plus duration, both stated;"
            " sqlite3: the smallest and largest timestamp, observed, each citing its bytes",
        ),
        Documented(
            "series",
            "seq (rank among the topic's messages by rowid), time/0, value/message_id (rowid),"
            " value/data_bytes (payload size), locator/0/length and locator/0/offset",
        ),
        Documented(
            "streams",
            "one per topics row; metadata holds offered_qos_profiles and type_description_hash"
            " when not blank; the definition is cited from message_definitions when present;"
            " message_count is the rows counted by planning",
        ),
        Documented(
            "tables",
            "metadata tables hold no header: the bag table is (key path, value) for every scalar"
            " entry, keys of nested mappings joined by `.`; topics_with_message_count is (name,"
            " type, serialization_format, offered_qos_profiles, type_description_hash,"
            " message_count); files is (path, starting_time_ns, duration_ns, message_count);"
            " relative_file_paths is (path). A plain decimal integer is a number, any other"
            " scalar its text",
        ),
    ),
    # A data chunk holds up to DEFAULT_MAX_ROWS rows of six columns; a metadata file is at most
    # 4 MiB of YAML and 200,000 nodes. Neither holds a payload.
    resources=Resources(max_memory=256 * 1024 * 1024, streaming=True),
    security=(
        "The database is never handed to an SQL engine: its b-trees are walked from the source's"
        " bytes, every length and page number checked against the page and the file, each walk"
        " bounded by the file's page count and a depth limit, so a cycle or a huge row count costs"
        " a finding and a bounded walk. No journal, -wal or -shm file is opened or written.",
        "The YAML subset parser refuses anchors, aliases, tags and multi-line scalars, and bounds"
        " size, depth and node count, so an alias bomb or deep nesting cannot expand.",
        "Part paths the metadata lists are kept as text and never opened; unsafe ones are"
        " findings.",
    ),
)


def _is_database(source: SourceReader) -> bool:
    return source.size >= len(MAGIC) and source.read(0, len(MAGIC)) == MAGIC


class _Head:
    """The first bytes of a source as a reader, so the database code can probe a head."""

    def __init__(self, head: bytes, size: int) -> None:
        self._head, self.size = head, size

    def read(self, offset: int, length: int) -> bytes:
        return self._head[offset : offset + length]


class Rosbag2Adapter:
    """The rosbag2 adapter. ``max_rows`` sets planning granularity only."""

    descriptor = DESCRIPTOR

    def __init__(self, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        if max_rows <= 0:
            raise ValueError(f"max_rows must be positive: {max_rows}")
        self._max_rows = max_rows

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(MAGIC):
            return self._probe_database(head, hints)
        return self._probe_metadata(head)

    @staticmethod
    def _probe_database(head: bytes, hints: ProbeHints) -> ProbeResult:
        magic = ProbeReason("rosbag2.sqlite_magic", "the source starts with SQLite's magic")
        try:
            reader = _Head(head, max(hints.size, len(head)))
            schema = read_schema(Database(reader), Walk())  # type: ignore[arg-type]
        except SqliteError:
            # Bytes decide, never the name: a header whose schema does not read is not claimed.
            return ProbeResult(
                0.0, (magic, ProbeReason("rosbag2.schema_unreadable", "its schema is not readable"))
            )
        if storage.find_layout(schema) is None:
            return ProbeResult(
                0.0, (magic, ProbeReason("rosbag2.no_tables", "no rosbag2 topics and messages"))
            )
        return ProbeResult(
            VERIFIED,
            (
                magic,
                ProbeReason("rosbag2.tables", "topics and messages tables with rosbag2's columns"),
            ),
        )

    @staticmethod
    def _probe_metadata(head: bytes) -> ProbeResult:
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start < len(head) - 4:
                return ProbeResult(0.0, (ProbeReason("rosbag2.not_text", "the head is not UTF-8"),))
            text = head[: exc.start].decode("utf-8")
        lines = [line.rstrip() for line in text.splitlines()]
        if f"{ROOT}:" not in lines:
            return ProbeResult(
                0.0, (ProbeReason("rosbag2.no_root", f"no top-level `{ROOT}:` line in the head"),)
            )
        reasons = [ProbeReason("rosbag2.root", f"a top-level `{ROOT}:` mapping")]
        keys = {
            line.strip().split(":")[0] for line in lines if line.startswith("  ") and ":" in line
        }
        if {"version", "storage_identifier"} <= keys:
            reasons.append(
                ProbeReason("rosbag2.fields", "it holds `version` and `storage_identifier`")
            )
            return ProbeResult(VERIFIED, tuple(reasons))
        return ProbeResult(SIGNATURE, tuple(reasons))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        if _is_database(source):
            return storage.inspect_storage(source)
        return metadata.inspect_metadata(source)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        if _is_database(source):
            return storage.plan_storage(source, config, self._max_rows)
        return metadata.plan_metadata(source, config)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        if chunk.context["part"] == "metadata":
            return metadata.ingest_metadata(source, chunk, config)
        return storage.ingest_storage(source, chunk, config)
