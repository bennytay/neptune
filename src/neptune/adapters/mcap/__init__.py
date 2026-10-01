"""MCAP recordings (https://mcap.dev/spec, format version 0): Neptune's first run adapter.

What it emits for a file (ADR 0034):

- one ``Run`` citing the Header record (the magic when there is none), its first and last
  instants as the summary's Statistics state them on ``log_time``;
- one ``TimestampDomain`` for ``log_time`` (the recorder's clock, one per file) and one for each
  channel's ``publish_time`` (each publisher's clock), as the specification defines them:
  nanosecond ticks from an epoch the file does not state;
- one ``Stream`` per channel, citing its Channel record, with its schema's name, encoding and the
  exact bytes of its definition, its metadata verbatim and its declared message count;
- one series row per message: ``seq``, both clocks, the message's ``sequence`` and a locator to
  its exact Message record, in the file or inside its chunk's uncompressed records. Payloads are
  not decoded (MVL-21); a finding per stream says so;
- one ``StructuredTable`` per Metadata record and a ``StructuredRecord`` per entry (key, value);
- findings for attachments, which no record kind holds yet, and for every problem met.

Planning reads the head, the footer and the summary; when the summary indexes every chunk, the
data section itself is not read to plan it. A file without a usable index is planned by scanning
it once. Corruption is local: a chunk that fails its CRC, a record cut short, an index that lies,
each costs what it touches and is a finding; what can still be read is read.
"""

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
from neptune.adapters.mcap.data import Data
from neptune.adapters.mcap.ingest import Declarations
from neptune.adapters.mcap.planning import make_plan
from neptune.adapters.mcap.records import (
    FORMAT_VERSION,
    MAGIC,
    MAX_CHUNK_BYTES_CEILING,
    RECORD_HEADER,
    FieldError,
    Opcode,
    parse_header,
    record_header,
)
from neptune.adapters.mcap.report import LOG_TIME_MAX
from neptune.adapters.mcap.summary import summarize

DEFAULT_CHUNK_BYTES: Final = 64 * 1024 * 1024
DEFAULT_MAX_ROWS: Final = 100_000
DEFAULT_MAX_CHUNK_BYTES: Final = MAX_CHUNK_BYTES_CEILING
# The decompressors, as installed: how far each gets into a chunk cut short, and what it makes of
# a chunk without a CRC, become rows, so their versions are part of the transform and of every
# cache key (ADR 0034 §1, ADR 0031).
LIBRARIES: Final = tuple((name, version(name)) for name in ("lz4", "zstandard"))


def _code(name: str, description: str) -> Documented:
    return Documented(f"mcap.{name}", description)


DESCRIPTOR: Final = AdapterDescriptor(
    id="mcap",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="MCAP recordings: a run, a stream per channel with every message's clocks and exact"
    " bytes, metadata as tables.",
    formats=(FormatSpec("MCAP", extensions=(".mcap",), magic=(Magic(0, MAGIC),)),),
    record_kinds=("run", "stream", "structured_record", "structured_table", "timestamp_domain"),
    config=(
        ConfigOption(
            "log_time_end",
            LOG_TIME_MAX,
            "the last log_time (ns, inclusive) whose messages get rows; others are counted only",
        ),
        ConfigOption(
            "log_time_start",
            0,
            "the first log_time (ns, inclusive) whose messages get rows; others are counted only",
        ),
        ConfigOption(
            "max_chunk_bytes",
            DEFAULT_MAX_CHUNK_BYTES,
            "a chunk or record declaring more stored or uncompressed bytes is not read; at most"
            " 256 MiB, the memory the adapter declares",
        ),
        ConfigOption(
            "topic_pattern",
            "",
            "a regular expression a channel's whole topic must match for its messages to get"
            " rows; empty selects every channel",
        ),
    ),
    libraries=LIBRARIES,
    finding_codes=(
        _code(
            "attachment_not_extracted",
            "an Attachment: an embedded file no record kind holds yet, cited with its name,"
            " media type, times and data range (unsupported, info)",
        ),
        _code("bad_magic", "the source does not start with MCAP's magic; nothing is read"),
        _code(
            "chunk_truncated",
            "the file ends inside a chunk: the bytes its stored prefix decodes to, and where its"
            " whole records end; only those are read (corrupt, warning)",
        ),
        _code(
            "conflicting_declaration",
            "Schema or Channel records repeat an id with other content, counted per id; the"
            " first declaration is read (inconsistent, warning)",
        ),
        _code(
            "corrupt_record",
            "records that do not parse, overrun their range, or follow Data End; not read"
            " (corrupt; error, warning for a damaged Header or bytes after Data End)",
        ),
        _code(
            "crc_mismatch",
            "a chunk's or attachment's bytes fail their CRC; a chunk's messages get no rows"
            " (corrupt, error)",
        ),
        _code(
            "decompression_failed",
            "a chunk's records do not decompress to their declared size; no rows (corrupt, error)",
        ),
        _code(
            "duplicate_key",
            "a channel's metadata repeats a key; the key is left out (ambiguous, warning)",
        ),
        _code(
            "index_invalid",
            "the summary's chunk index does not hold; the file is planned by scanning it"
            " (inconsistent, warning)",
        ),
        _code(
            "index_mismatch",
            "an index disagrees with the data: a Message Index, a chunk index entry's fields or"
            " a chunk's times (warning), a chunk not where indexed or not indexed, its messages"
            " without rows (error)",
        ),
        _code(
            "invalid_utf8",
            "a string field is not UTF-8; its value is unknown or left out"
            " (unrepresentable, warning)",
        ),
        _code(
            "message_count_mismatch",
            "a range holds other message counts than planned from the index; messages past the"
            " count get no rows (inconsistent; error when messages lose rows, else warning)",
        ),
        _code(
            "message_outside_layout",
            "a top-level message in a chunked file, or a chunk in an unchunked one; no rows"
            " (unsupported, error)",
        ),
        _code(
            "not_selected",
            "the config's topic pattern or log_time window leaves a stream's messages without"
            " rows (skipped, info)",
        ),
        _code(
            "payload_not_decoded",
            "a stream's payloads are not decoded; each row cites its message (unsupported, info)",
        ),
        _code(
            "record_too_large",
            "a chunk or record declares more bytes than max_chunk_bytes; not read (limit, error)",
        ),
        _code(
            "skipped_by_index",
            "indexed chunks not read because the summary's chunk index puts them outside the"
            " log_time window or lists no selected channel in them; the skip relies on the"
            " index (skipped, info)",
        ),
        _code(
            "summary_unusable",
            "the footer points at a summary out of bounds, too large, failing its CRC or"
            " malformed; the file is planned by scanning it (corrupt, warning)",
        ),
        _code(
            "time_out_of_range",
            "a u64 time does not fit a signed 64-bit tick count; it is unknown"
            " (unrepresentable, warning)",
        ),
        _code(
            "too_many_records",
            "a chunk holds more records than max_chunk_bytes / 31, more than a chunk of"
            " messages within the limit can; the rest is not read (limit, error)",
        ),
        _code(
            "truncated",
            "the file is cut short: a record cut (error, what is there is read) or the summary"
            " and footer missing after whole records (warning)",
        ),
        _code(
            "unknown_channel",
            "messages name a channel no Channel record declares; no rows (corrupt, error)",
        ),
        _code(
            "unknown_compression",
            "a chunk's compression is not one the specification defines; no rows"
            " (unsupported, error)",
        ),
        _code(
            "unknown_encoding",
            "a message or schema encoding the specification does not register; recorded as"
            " declared (unsupported, info)",
        ),
        _code(
            "unknown_record",
            "records of a kind the place does not hold or the specification does not define;"
            " skipped (unsupported, info)",
        ),
        _code(
            "unknown_schema",
            "a channel names a schema no Schema record declares; its schema is unknown"
            " (corrupt, warning)",
        ),
    ),
    locator_steps=(
        Documented(
            "mcap:time_field",
            "the time field `name` (log_time, publish_time) that the specification defines on"
            " every Message, cited after what declares the clock: the magic or a Channel record",
        ),
    ),
    conventions=(
        Documented(
            "chunks",
            "planned chunk 0 holds the declarations; the others are byte ranges of the data"
            " section, or stretches of one large chunk's messages, with each channel's seq start",
        ),
        Documented(
            "clocks",
            "clock 0 is log_time (scope (), the recorder's), clock 1 the channel's publish_time"
            " (scope (topic,), or ('channel', id) without a topic); ticks as stored, never"
            " converted",
        ),
        Documented(
            "declarations",
            "a schema or channel is cited at its record in a usable summary, else at its first"
            " record in the data section",
        ),
        Documented(
            "locators",
            "a row cites its whole Message record: one byte_range in the file, or the Chunk"
            " record then a byte_range in its uncompressed records, as Message Index offsets count",
        ),
        Documented(
            "metadata",
            "a Metadata record is a table named by it, without header; entry r is Row(r) with"
            " cells key, value",
        ),
        Documented(
            "series",
            "seq, time/0 log_time, time/1 publish_time (state unknown past 2^63-1),"
            " value/sequence (uint32), locator/<i>/offset and length",
        ),
    ),
    # A call holds one chunk's stored bytes and what they decode to, each at most
    # max_chunk_bytes, then the decoded bytes and 16 bytes per message; zstd's window (128 MiB
    # at most) and the rows of one planned chunk come on top. Records are walked, never listed.
    resources=Resources(max_memory=3 * DEFAULT_MAX_CHUNK_BYTES, streaming=True),
    security=(
        "Decompression output is bounded by the chunk's declared size, itself bounded by"
        " max_chunk_bytes, and written into one buffer, so a decompression bomb costs at most"
        " max_chunk_bytes besides its stored bytes.",
        "A chunk's records are walked over its bytes without an object per record, at most"
        " max_chunk_bytes / 31 of them, so a chunk of tiny records costs its bytes, not more.",
        "Every length is checked against its record before it is read; nothing is allocated"
        " from a length the file states without that check.",
        "zstd and lz4 frames are decoded by the zstandard and lz4 libraries inside the sandbox.",
    ),
)


class McapAdapter:
    """The MCAP adapter. ``chunk_bytes`` and ``max_rows`` set planning granularity only."""

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
            reason = ProbeReason("mcap.no_magic", "the source does not start with MCAP's magic")
            return ProbeResult(0.0, (reason,))
        reasons = [ProbeReason("mcap.magic", "the source starts with MCAP's magic, version 0")]
        start = len(MAGIC)
        if len(head) >= start + RECORD_HEADER:
            opcode, length = record_header(head, start)
            end = start + RECORD_HEADER + length
            if opcode == Opcode.HEADER and end <= len(head):
                try:
                    parse_header(head[start + RECORD_HEADER : end])
                except FieldError:
                    pass
                else:
                    reasons.append(
                        ProbeReason("mcap.header", "a well-formed Header record follows the magic")
                    )
                    return ProbeResult(VERIFIED, tuple(reasons), FORMAT_VERSION)
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
