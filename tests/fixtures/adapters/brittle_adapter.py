"""A test-only adapter whose format tells it how to misbehave: the runtime's fault fixture.

The runtime (MVL-6) promises that an adapter's crash, a transient fault, a plan that cannot be
made and output that breaks the contract each become findings about one source while every other
source reaches the package. Those faults need a real adapter over real bytes to happen in; this
one reads a format whose lines ask for them, so a test writes the fault it wants into a file.

The format, ``brittle`` (ASCII only)::

    BRITTLE1\\n              the signature
    <line>\\n                one block per line

Chunk 0 emits the ``DocumentRecord``; every other chunk holds one line and emits its
``DocumentBlock``, citing the line's span (code points equal bytes: the format is ASCII). Lines
with these texts misbehave instead:

- ``crash``: ``ingest`` raises ``RuntimeError`` every time (a bug, or hostile input);
- ``flaky``: ``ingest`` raises ``OSError`` the first time this adapter instance sees the chunk
  and succeeds after (a transient fault; the counter lives on the instance, which a pure adapter
  would never do, and is why this adapter stays in the test fixtures);
- ``bad-output``: the block's id does not derive from its evidence (a contract violation the
  per-chunk check catches);
- ``dup``: the block cites the first line's span, so its id collides with that line's block (a
  cross-chunk violation only the whole source's output shows);
- a first line ``plan-crash``: ``plan`` raises ``RuntimeError``.
"""

from typing import Final

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
from neptune.identity.provenance import evidence_record_id
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotApplicable, NotCovered
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Span
from neptune.model.world import DocumentBlock, DocumentRecord

MAGIC: Final = b"BRITTLE1\n"

DESCRIPTOR: Final = AdapterDescriptor(
    id="brittle",
    version="1.0.0",
    abi=ABI_VERSION,
    summary="A toy text format for runtime tests whose lines ask the adapter to misbehave.",
    formats=(FormatSpec("Brittle", extensions=(".brittle",), magic=(Magic(0, MAGIC),)),),
    record_kinds=("document_block", "document_record"),
    config=(),
    libraries=(),
    finding_codes=(),
    locator_steps=(),
    conventions=(
        Documented("blocks", "one block per line after the signature, citing the line's span"),
        Documented("faults", "crash, flaky, bad-output, dup and a first line plan-crash misbehave"),
    ),
    resources=Resources(max_memory=1024 * 1024, streaming=False),
    security=("Test-only.",),
)


def brittle(*lines: str) -> bytes:
    """A brittle file holding ``lines``."""
    return MAGIC + b"".join(line.encode("ascii") + b"\n" for line in lines)


def _int(context: JsonObject, key: str) -> int:
    value = context[key]
    assert isinstance(value, int)
    return value


def _lines(source: SourceReader) -> list[tuple[int, bytes]]:
    """``(offset, line)`` for every LF-terminated line after the signature."""
    data = source.read(len(MAGIC), source.size)
    found, offset = [], len(MAGIC)
    for line in data.split(b"\n")[:-1]:
        found.append((offset, line))
        offset += len(line) + 1
    return found


class BrittleAdapter:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self._flaked: set[str] = set()

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(MAGIC):
            return ProbeResult(SIGNATURE, (ProbeReason("brittle.magic", "starts BRITTLE1"),), "1")
        return ProbeResult(0.0, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return InspectResult({"size": source.size})

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        lines = _lines(source)
        if lines and lines[0][1] == b"plan-crash":
            raise RuntimeError("the plan was asked to crash")
        chunks = [make_chunk(source, config, {"part": "document"}, source.size)]
        for order, (offset, line) in enumerate(lines):
            context: JsonObject = {"end": offset + len(line), "order": order, "start": offset}
            chunks.append(make_chunk(source, config, context, len(line)))
        return Plan(tuple(chunks))

    def _document(self, source: SourceReader, config: AdapterConfig) -> tuple[RecordId, Provenance]:
        evidence = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        return (
            evidence_record_id(DocumentRecord.kind, evidence, config.transform),
            Provenance(evidence, config.transform.id, AssertionKind.OBSERVED),
        )

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        document, provenance = self._document(source, config)
        if chunk.context.get("part") == "document":
            title: Knowledge[str] = NotCovered()
            return ChunkOutput(records=(DocumentRecord(document, provenance, "text", title, ()),))
        start, end, order = (_int(chunk.context, key) for key in ("start", "end", "order"))
        text = source.read(start, end - start).decode("ascii")
        if text == "crash":
            raise RuntimeError("the chunk was asked to crash")
        if text == "flaky" and chunk.id not in self._flaked:
            self._flaked.add(chunk.id)
            raise OSError("a transient fault, once")
        if text == "dup":  # the first line's span: another chunk's block
            first = _lines(source)[0]
            start, end = first[0], first[0] + len(first[1])
        evidence = EvidenceRef(source.content_id, (Span(start, end),))
        block_id = evidence_record_id(DocumentBlock.kind, evidence, config.transform)
        if text == "bad-output":
            block_id = RecordId("rec:sha256:" + "0" * 64)
        block = DocumentBlock(
            id=block_id,
            provenance=Provenance(evidence, config.transform.id, AssertionKind.OBSERVED),
            document=document,
            order=order,
            role=NotCovered(),
            level=NotCovered(),
            text=Known(text),
            region=NotApplicable(),
        )
        return ChunkOutput(records=(block,))
