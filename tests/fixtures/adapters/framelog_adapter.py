"""A test-only adapter for a toy frame log, the shape of a camera or lidar recording.

The format, ``framelog``::

    FRAMELOG1\\n                                the signature
    <time: u64 LE> <length: u32 LE> <payload>    one frame, repeated to the end of the file

Payloads are cited, never decoded: each row is a frame's time and the byte range of its payload,
and one ``framelog.payload_not_decoded`` finding says so (ADR 0018 §4). So a recording of many
gigabytes ingests into a few kilobytes of records and series, which is what MVL-73's acceptance
test needs. Planning reads only the frame headers; a header or payload cut short by the end of the
file is a ``framelog.truncated`` finding. Chunk 0 emits the clock, run and stream; every other
chunk holds up to ``frames_per_chunk`` frames.
"""

import struct
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    ABI_VERSION,
    SIGNATURE,
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
    make_chunk,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import AssertionKind, Knowledge, NotApplicable, NotCovered, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import (
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    SeriesProvenance,
    step_template,
)

if TYPE_CHECKING:
    from neptune.model.time import Timestamp

MAGIC: Final = b"FRAMELOG1\n"
HEADER: Final = struct.Struct("<QI")
COLUMNS: Final = ("locator/0/length", "locator/0/offset", "seq", "time/0")

DESCRIPTOR: Final = AdapterDescriptor(
    id="framelog",
    version="1.0.0",
    abi=ABI_VERSION,
    summary="A toy frame log for tests: timed binary frames, cited and never decoded.",
    formats=(FormatSpec("Frame log", extensions=(".framelog",), magic=(Magic(0, MAGIC),)),),
    record_kinds=("run", "stream", "timestamp_domain"),
    config=(),
    libraries=(),
    finding_codes=(
        Documented("framelog.payload_not_decoded", "frames are cited by byte range, not decoded"),
        Documented("framelog.truncated", "the file ends inside a frame; the rest has no rows"),
    ),
    locator_steps=(),
    conventions=(Documented("series", "one row per frame: its time and its payload's bytes"),),
    resources=Resources(max_memory=1024 * 1024, streaming=True),
    security=("Test-only.",),
)


def frame_log(frames: list[tuple[int, bytes]]) -> bytes:
    """A frame log holding ``frames``, each ``(time, payload)``."""
    return MAGIC + b"".join(HEADER.pack(t, len(p)) + p for t, p in frames)


def _int(context: JsonObject, key: str) -> int:
    value = context[key]
    assert isinstance(value, int)
    return value


def _empty() -> dict[str, list[int]]:
    return {name: [] for name in COLUMNS}


def _batch(stream: RecordId, columns: dict[str, list[int]]) -> SeriesBatch:
    return SeriesBatch(
        stream,
        tuple(SeriesColumn(name, ColumnType.INT64, tuple(columns[name])) for name in COLUMNS),
    )


class FrameLogAdapter:
    descriptor = DESCRIPTOR

    def __init__(self, frames_per_chunk: int = 64) -> None:
        self._frames_per_chunk = frames_per_chunk

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(MAGIC):
            return ProbeResult(SIGNATURE, (ProbeReason("framelog.magic", "starts FRAMELOG1"),))
        return ProbeResult(0.0, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return InspectResult({"size": source.size})

    def _frames(self, source: SourceReader, start: int, end: int) -> list[tuple[int, int, int]]:
        """``(header offset, time, payload length)`` of each whole frame in ``[start, end)``."""
        frames, offset = [], start
        while offset + HEADER.size <= end:
            time, length = HEADER.unpack(source.read(offset, HEADER.size))
            if offset + HEADER.size + length > end:
                break
            frames.append((offset, time, length))
            offset += HEADER.size + length
        return frames

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        chunks = [make_chunk(source, config, {"part": "header"}, len(MAGIC))]
        frames = self._frames(source, len(MAGIC), source.size)
        for first in range(0, len(frames), self._frames_per_chunk):
            group = frames[first : first + self._frames_per_chunk]
            start, (last, _, length) = group[0][0], group[-1]
            end = last + HEADER.size + length
            context: JsonObject = {"end": end, "first": first, "start": start}
            chunks.append(make_chunk(source, config, context, len(group) * HEADER.size))
        tail = frames[-1][0] + HEADER.size + frames[-1][2] if frames else len(MAGIC)
        findings: tuple[IngestFinding, ...] = ()
        if tail < source.size:
            findings = (
                ingest_finding(
                    code="framelog.truncated",
                    category=FindingCategory.CORRUPT,
                    severity=Severity.ERROR,
                    subject=EvidenceRef(source.content_id, (ByteRange(tail, source.size - tail),)),
                    transform=config.transform,
                    message="the file ends inside a frame",
                ),
            )
        return Plan(tuple(chunks), findings)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        transform = config.transform
        header = Provenance(
            EvidenceRef(source.content_id, (ByteRange(0, len(MAGIC)),)),
            transform.id,
            AssertionKind.OBSERVED,
        )
        whole = Provenance(
            EvidenceRef(source.content_id, (ByteRange(0, source.size),)),
            transform.id,
            AssertionKind.OBSERVED,
        )
        domain = evidence_record_id(TimestampDomain.kind, header.evidence, transform)
        run = evidence_record_id(Run.kind, whole.evidence, transform)
        stream = evidence_record_id(Stream.kind, header.evidence, transform)
        if chunk.context.get("part") == "header":
            unknown_id: Knowledge[LogicalId] = Unknown()
            no_time: Knowledge[Timestamp] = NotCovered()
            records = (
                TimestampDomain(
                    domain,
                    header,
                    "time",
                    (),
                    Unknown(),
                    Unknown(),
                    Unknown(),
                    Unknown(),
                    Unknown(),
                ),
                Run(run, whole, unknown_id, NotCovered(), no_time, no_time),
                Stream(
                    id=stream,
                    provenance=header,
                    run=run,
                    topic=NotApplicable(),
                    schema_name=NotCovered(),
                    schema_encoding=NotCovered(),
                    schema_definition=NotCovered(),
                    message_encoding=NotCovered(),
                    metadata=(),
                    clocks=(domain,),
                    message_count=NotCovered(),
                    first=no_time,
                    last=no_time,
                    series=SeriesProvenance(
                        source.content_id,
                        (step_template("byte_range", per_row=("length", "offset")),),
                        AssertionKind.OBSERVED,
                    ),
                ),
            )
            not_decoded = ingest_finding(
                code="framelog.payload_not_decoded",
                category=FindingCategory.UNSUPPORTED,
                severity=Severity.INFO,
                subject=header.evidence,
                transform=transform,
                message="frame payloads are cited by byte range, not decoded",
                records=[stream],
            )
            return ChunkOutput(
                records=records, series=(_batch(stream, _empty()),), findings=(not_decoded,)
            )
        start, end, first = (_int(chunk.context, key) for key in ("start", "end", "first"))
        columns = _empty()
        for index, (offset, time, length) in enumerate(self._frames(source, start, end)):
            payload = offset + HEADER.size
            for name, value in zip(COLUMNS, (length, payload, first + index, time), strict=True):
                columns[name].append(value)
        return ChunkOutput(series=(_batch(stream, columns),))
