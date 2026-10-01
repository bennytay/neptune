"""A test-only adapter for a toy time-series format, written entirely outside ``src/neptune``.

It proves the contract's first acceptance criterion (MVL-7): a new format plugs in by registering
an adapter, with no change to the model, store, registry or runtime. It also exercises the series
path of the contract, which the text adapter does not.

The format, ``tally``::

    TALLY1\\n                 the signature
    <time> <value>\\n         one sample per line: two ASCII decimal integers and one space

A line that is not two integers is a ``tally.bad_row`` finding and no series row. Every line after
the signature is a sample position, so ``seq`` is the line's index among them, bad lines included.
Chunk 0 emits the clock, the run and the stream; every other chunk holds ``rows_per_chunk`` lines.
"""

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

MAGIC: Final = b"TALLY1\n"

DESCRIPTOR: Final = AdapterDescriptor(
    id="tally",
    version="1.0.0",
    abi=ABI_VERSION,
    summary="A toy time-series format for tests: a signature, then one integer sample per line.",
    formats=(FormatSpec("Tally", extensions=(".tally",), magic=(Magic(0, MAGIC),)),),
    record_kinds=("run", "stream", "timestamp_domain"),
    config=(),
    libraries=(),
    finding_codes=(Documented("tally.bad_row", "a line is not two integers; it has no row"),),
    locator_steps=(),
    conventions=(
        Documented("series", "seq is the line's index after the signature; value/value its value"),
    ),
    resources=Resources(max_memory=1024 * 1024, streaming=False),
    security=("Test-only.",),
)


def _int(context: JsonObject, key: str) -> int:
    value = context[key]
    assert isinstance(value, int)
    return value


def _lines(data: bytes, start: int) -> list[tuple[int, bytes]]:
    """``(offset, line)`` for every LF-terminated line of ``data``, which begins at ``start``."""
    out, offset = [], start
    for line in data.split(b"\n")[:-1]:
        out.append((offset, line))
        offset += len(line) + 1
    return out


class TallyAdapter:
    descriptor = DESCRIPTOR

    def __init__(self, rows_per_chunk: int = 2) -> None:
        self._rows_per_chunk = rows_per_chunk

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(MAGIC):
            return ProbeResult(SIGNATURE, (ProbeReason("tally.magic", "starts TALLY1"),), "1")
        return ProbeResult(0.0, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return InspectResult({"size": source.size})

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        lines = _lines(source.read(len(MAGIC), source.size), len(MAGIC))
        chunks = [make_chunk(source, config, {"part": "header"}, len(MAGIC))]
        for first in range(0, len(lines), self._rows_per_chunk):
            batch = lines[first : first + self._rows_per_chunk]
            start, (last, line) = batch[0][0], batch[-1]
            context: JsonObject = {"end": last + len(line) + 1, "first": first, "start": start}
            chunks.append(make_chunk(source, config, context, last + len(line) + 1 - start))
        return Plan(tuple(chunks))

    def _header(self, source: SourceReader, config: AdapterConfig) -> tuple[RecordId, ...]:
        transform = config.transform
        header = EvidenceRef(source.content_id, (ByteRange(0, len(MAGIC)),))
        whole = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        return (
            evidence_record_id(TimestampDomain.kind, header, transform),
            evidence_record_id(Run.kind, whole, transform),
            evidence_record_id(Stream.kind, header, transform),
        )

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        transform = config.transform
        domain, run, stream = self._header(source, config)
        if chunk.context.get("part") == "header":
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
            return ChunkOutput(records=records)
        start, end, first = (_int(chunk.context, key) for key in ("start", "end", "first"))
        columns: dict[str, list[int]] = {
            name: []
            for name in ("locator/0/length", "locator/0/offset", "seq", "time/0", "value/value")
        }
        findings: list[IngestFinding] = []
        for index, (offset, line) in enumerate(_lines(source.read(start, end - start), start)):
            parts = line.split(b" ")
            if len(parts) != 2 or not all(part.lstrip(b"-").isdigit() for part in parts):
                findings.append(
                    ingest_finding(
                        code="tally.bad_row",
                        category=FindingCategory.CORRUPT,
                        severity=Severity.ERROR,
                        subject=EvidenceRef(source.content_id, (ByteRange(offset, len(line)),)),
                        transform=transform,
                        message=f"line {first + index} is not two integers",
                    )
                )
                continue
            for name, value in zip(
                ("locator/0/length", "locator/0/offset", "seq", "time/0", "value/value"),
                (len(line), offset, first + index, int(parts[0]), int(parts[1])),
                strict=True,
            ):
                columns[name].append(value)
        series: tuple[SeriesBatch, ...] = ()
        if columns["seq"]:
            batch = SeriesBatch(
                stream,
                tuple(
                    SeriesColumn(name, ColumnType.INT64, tuple(values))
                    for name, values in columns.items()
                ),
            )
            series = (batch,)
        return ChunkOutput(series=series, findings=tuple(findings))
