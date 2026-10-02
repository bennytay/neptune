"""ROS 1 bags (http://wiki.ros.org/Bags/Format/2.0, format version 2.0): the second run adapter.

What it emits for a bag (ADR 0046), in the same shape the MCAP adapter gives a recording:

- one ``Run`` citing the Bag Header record (the magic when there is none), its first and last
  instants as the Chunk Infos state them, on the bag's record time;
- one ``TimestampDomain`` for the record time (``time``, the recorder's clock, one per bag):
  nanosecond ticks from ``sec`` and ``nsec`` as stored, on an epoch and timescale the bag does not
  state;
- one ``Stream`` per connection, citing its Connection record, with the topic it is filed under,
  the type (schema name), the message definition (the exact bytes of its field), every other
  connection header field (md5sum, callerid, latching) as metadata and its stated message count,
  all of it ``stated`` by the publisher;
- one series row per message: ``seq``, the record time and a locator to its exact Message Data
  record inside its Chunk's uncompressed data. Payloads are not decoded (MVL-21); a finding per
  stream says so;
- findings for every problem met.

Planning reads the Bag Header and the index; when the index holds together, the data section
itself is not read to plan it. A bag without a usable index (one never closed, or cut short) is
planned by scanning it once. Corruption is local: a chunk that will not decompress, a record cut
short, an index that lies, each costs what it touches and is a finding; what can still be read is
read.
"""

import sys
from importlib.metadata import version
from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    SIGNATURE,
    VERIFIED,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
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
from neptune.adapters.rosbag1.data import Data
from neptune.adapters.rosbag1.ingest import Declarations
from neptune.adapters.rosbag1.planning import make_plan
from neptune.adapters.rosbag1.records import (
    FORMAT_VERSION,
    MAGIC,
    MAX_CHUNK_BYTES_CEILING,
    FieldError,
    Op,
    parse_bag_header,
    parse_record,
)
from neptune.adapters.rosbag1.summary import summarize

DEFAULT_CHUNK_BYTES: Final = 64 * 1024 * 1024
DEFAULT_MAX_ROWS: Final = 100_000
DEFAULT_MAX_CHUNK_BYTES: Final = 64 * 1024 * 1024
DEFAULT_MAX_HEADER_BYTES: Final = 1024 * 1024
# The decompressors, as installed: how far each gets into a chunk cut short becomes rows, so their
# versions are part of the transform and of every cache key (ADR 0046 §1, ADR 0031). bz2 is the
# standard library's, so its version is the interpreter's.
LIBRARIES: Final = (
    ("bz2", f"cpython-{sys.version_info.major}.{sys.version_info.minor}"),
    ("lz4", version("lz4")),
)


def _code(name: str, description: str) -> Documented:
    return Documented(f"rosbag1.{name}", description)


DESCRIPTOR: Final = AdapterDescriptor(
    id="rosbag1",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="ROS 1 bags: a run, a stream per connection with every message's record time and"
    " exact bytes, the message definitions and md5sums as the publishers stated them.",
    formats=(FormatSpec("ROS 1 bag", extensions=(".bag",), magic=(Magic(0, MAGIC),)),),
    record_kinds=("run", "stream", "timestamp_domain"),
    config=(
        ConfigOption(
            "max_chunk_bytes",
            DEFAULT_MAX_CHUNK_BYTES,
            "a chunk declaring more stored or uncompressed bytes is not read; at most 256 MiB,"
            " the memory the adapter declares",
        ),
        ConfigOption(
            "max_header_bytes",
            DEFAULT_MAX_HEADER_BYTES,
            "a record header, or a Connection record's connection header, declaring more bytes"
            " is not read; at most 64 MiB",
        ),
    ),
    libraries=LIBRARIES,
    finding_codes=(
        _code(
            "bad_magic", "the source does not start with the ROS 1 bag 2.0 magic; nothing is read"
        ),
        _code(
            "chunk_truncated",
            "the file ends inside a chunk, or its record runs past its unit: the bytes its stored"
            " prefix decodes to are read (corrupt, warning)",
        ),
        _code(
            "conflicting_declaration",
            "a Connection record repeats an id with other content: the first declaration is"
            " read (inconsistent, warning)",
        ),
        _code(
            "corrupt_record",
            "records that do not parse, overrun their range or lack fields the format requires;"
            " not read (corrupt; error, warning for a damaged or missing Bag Header)",
        ),
        _code(
            "decompression_failed",
            "a chunk's records do not decompress to their declared size; no rows (corrupt, error)",
        ),
        _code(
            "duplicate_key",
            "a connection header repeats a field; the field is left out of the metadata"
            " (ambiguous, warning)",
        ),
        _code(
            "header_too_large",
            "a record header or connection header declares more than max_header_bytes; not read"
            " (limit, error)",
        ),
        _code(
            "index_invalid",
            "the Bag Header's index is missing or does not hold together; the bag is planned by"
            " scanning it (inconsistent; info for a bag never closed, else warning)",
        ),
        _code(
            "index_mismatch",
            "the index disagrees with the data: an Index Data record, a chunk's times (warning),"
            " a chunk not where indexed, or chunks the index or plan does not list, whose messages"
            " have no rows (error)",
        ),
        _code(
            "invalid_utf8",
            "a connection header string is not UTF-8; its value is unknown or left out"
            " (unrepresentable, warning)",
        ),
        _code(
            "message_count_mismatch",
            "a chunk holds other message counts than its Chunk Info states; messages past the"
            " count get no rows (inconsistent; error when messages lose rows, else warning)",
        ),
        _code(
            "message_outside_layout",
            "a message outside any chunk; no rows (unsupported, error)",
        ),
        _code(
            "missing_field",
            "a Connection lacks (or leaves empty) its topic, type, md5sum or message definition;"
            " that value is unknown (missing, warning)",
        ),
        _code(
            "payload_not_decoded",
            "a stream's payloads are not decoded; each row cites its message (unsupported, info)",
        ),
        _code(
            "record_too_large",
            "a chunk declares more bytes than max_chunk_bytes; not read (limit, error)",
        ),
        _code(
            "too_many_connections",
            "the bag declares more connections than a source may (10,000); the rest are not"
            " declared (limit, error)",
        ),
        _code(
            "too_many_records",
            "a chunk holds more records than max_chunk_bytes / 46, more than a chunk of messages"
            " within the limit can, or a scanned bag holds over 1,000,000 chunks; the rest is not"
            " read or planned (limit, error)",
        ),
        _code(
            "topic_mismatch",
            "a connection header names another topic than the record it is filed under; the"
            " record's topic is the stream's (inconsistent, info)",
        ),
        _code(
            "truncated",
            "the file is cut short: a record cut (error, what is there is read), or no Bag"
            " Header after the magic (error)",
        ),
        _code(
            "unknown_compression",
            "a chunk's compression is not none, bz2 or lz4; no rows (unsupported, error)",
        ),
        _code(
            "unknown_connection",
            "messages name a connection no Connection record declares; no rows (corrupt, error)",
        ),
        _code(
            "unknown_record",
            "records of an op the place does not hold or the specification does not define;"
            " skipped (unsupported, info)",
        ),
    ),
    locator_steps=(
        Documented(
            "rosbag1:time_field",
            "the time field `name` (time) that the specification defines on every Message Data"
            " record, cited after what declares the clock: the magic",
        ),
    ),
    conventions=(
        Documented(
            "chunks",
            "planned chunk 0 holds the declarations; the others are byte ranges of whole units"
            " (a chunk and what follows it), or stretches of one large chunk's messages, with"
            " each connection's seq start",
        ),
        Documented(
            "clocks",
            "clock 0 is the record time (scope (), the recorder's): ticks are nanoseconds from"
            " sec and nsec as stored, never converted or normalised",
        ),
        Documented(
            "declarations",
            "a connection is cited at its Connection record in a usable index, else at its first"
            " record in the data section; a stream is stated, cited at that record",
        ),
        Documented(
            "encodings",
            "schema_encoding is ros1msg and message_encoding ros1, the MCAP registry's names for"
            " the bag's own formats, cited to the magic; schema_definition is the bytes of the"
            " connection header's message_definition field",
        ),
        Documented(
            "locators",
            "a row cites its whole Message Data record: the Chunk record in the file, then a"
            " byte_range in the chunk's uncompressed data, offsets counted as Index Data records"
            " count them",
        ),
        Documented(
            "metadata",
            "every connection header field except topic, type and message_definition, verbatim"
            " (md5sum, callerid, latching); topic only when it differs from the record's",
        ),
        Documented(
            "series",
            "seq, time/0 the record time, locator/0 and locator/1 length and offset; no value"
            " columns, the payload is not decoded",
        ),
    ),
    # A call holds one chunk's stored bytes and what they decode to, each at most
    # max_chunk_bytes, then the rows of one planned chunk. Records are walked, never listed.
    resources=Resources(max_memory=3 * MAX_CHUNK_BYTES_CEILING, streaming=True),
    security=(
        "Decompression output is bounded by the chunk's declared size, itself bounded by"
        " max_chunk_bytes, and written into one buffer, so a decompression bomb costs at most"
        " max_chunk_bytes besides its stored bytes.",
        "A chunk's records are walked over its bytes without an object per record, at most"
        " max_chunk_bytes / 46 of them, so a chunk of tiny records costs its bytes, not more.",
        "Every length is checked against its record before it is read; a header over"
        " max_header_bytes is skipped by its lengths, not read, and at most 1,024 fields of a"
        " header are parsed.",
        "The index is read only up to 64 MiB, and a count that no chunk could hold is not"
        " believed.",
        "lz4 frames are decoded by the lz4 library and bz2 streams by the standard library,"
        " inside the sandbox.",
    ),
)


class Rosbag1Adapter:
    """The ROS 1 bag adapter. ``chunk_bytes`` and ``max_rows`` set planning granularity only."""

    descriptor = DESCRIPTOR

    def __init__(
        self, chunk_bytes: int = DEFAULT_CHUNK_BYTES, max_rows: int = DEFAULT_MAX_ROWS
    ) -> None:
        if chunk_bytes <= 0 or max_rows <= 0:
            raise ValueError(
                f"chunk_bytes and max_rows must be positive: {chunk_bytes}, {max_rows}"
            )
        self._chunk_bytes = chunk_bytes
        self._max_rows = max_rows

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head.startswith(MAGIC):
            reason = ProbeReason(
                "rosbag1.no_magic", "the source does not start with the ROS 1 bag 2.0 magic"
            )
            return ProbeResult(0.0, (reason,))
        reasons = [
            ProbeReason("rosbag1.magic", "the source starts with the ROS 1 bag magic, version 2.0")
        ]
        try:
            record = parse_record(head, len(MAGIC))
            if record.op == Op.BAG_HEADER:
                parse_bag_header(record.fields)
                reasons.append(
                    ProbeReason(
                        "rosbag1.header", "a well-formed Bag Header record follows the magic"
                    )
                )
                return ProbeResult(VERIFIED, tuple(reasons), FORMAT_VERSION)
        except FieldError:
            pass
        return ProbeResult(SIGNATURE, tuple(reasons), FORMAT_VERSION)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return summarize(source, config)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return make_plan(source, config, self._chunk_bytes, self._max_rows)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        part = chunk.context["part"]
        if part == "declarations":
            return Declarations(source, chunk, config).run()
        if part == "data":
            return Data(source, chunk, config).run()
        return ChunkOutput()  # "unreadable": the plan's finding says why
