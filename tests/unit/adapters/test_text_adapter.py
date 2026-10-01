"""The plain-text reference adapter on real files, malformed input, boundaries and determinism.

The oracle for every span is an independent reading: decode the whole file at once with
``errors="replace"``, split it into lines, and group them by the block rule. The adapter must
agree with it exactly, however its plan cuts the file into chunks.
"""

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.contract import (
    GENERIC,
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    Chunk,
    ChunkOutput,
    ProbeHints,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.text import BOM, DESCRIPTOR, TextAdapter, _lines
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.knowledge import Known, NotApplicable, NotCovered, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Span
from neptune.model.world import DocumentBlock, DocumentRecord

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "text"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, chunk_bytes: int = 1024 * 1024, **config: Any) -> SourceOutput:
    return ingest_source(TextAdapter(chunk_bytes), BytesReader(data), config)


def blocks(output: SourceOutput) -> list[DocumentBlock]:
    found = [r for r in output.records() if isinstance(r, DocumentBlock)]
    return sorted(found, key=lambda block: block.order)


def span(block: DocumentBlock) -> tuple[int, int]:
    (step,) = block.provenance.evidence.locator
    assert isinstance(step, Span)
    return step.start, step.end


def oracle(data: bytes, rule: str = "paragraph") -> list[tuple[int, int]]:
    """Block spans from the whole text decoded at once: an independent reading of the rules."""
    text = data.removeprefix(BOM).decode("utf-8", errors="replace")
    pieces = text.split("\n")
    if pieces[-1] == "":
        pieces.pop()  # nothing after the last LF is not a line
    lines, offset = [], 0
    for index, piece in enumerate(pieces):
        ended = index < len(pieces) - 1 or text.endswith("\n")
        content = piece[:-1] if ended and piece.endswith("\r") else piece
        lines.append((offset, offset + len(content), content.strip(" \t\r") == ""))
        offset += len(piece) + 1
    spans: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for start, end, blank in lines:
        if blank:
            if current:
                spans.append(current)
            current = None
        elif rule == "line":
            spans.append((start, end))
        else:
            current = (current[0], end) if current else (start, end)
    if current:
        spans.append(current)
    return spans


def as_bytes(output: SourceOutput) -> bytes:
    rows = [record.to_json() for record in output.package_records()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


# --- Probe -------------------------------------------------------------------------------------


def probe(data: bytes, name: str = "f") -> tuple[float, list[str]]:
    head = data[:PROBE_HEAD_SIZE]
    result = TextAdapter().probe(head, ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


def test_utf8_text_is_generic_text_whatever_its_name() -> None:
    assert probe(fixture("notes.txt"), "notes.txt") == (GENERIC, ["text.utf8"])
    assert probe(fixture("notes.txt"), "notes.mcap") == (GENERIC, ["text.utf8"])
    assert probe(fixture("operator_log")) == (GENERIC, ["text.utf8", "text.bom"])


def test_damaged_text_is_claimed_weakly_so_it_is_still_read() -> None:
    assert probe(fixture("truncated.txt")) == (NAME_ONLY, ["text.not_utf8"])
    assert probe(fixture("corrupted.txt")) == (NAME_ONLY, ["text.not_utf8"])
    assert probe(b"\xef\xbb\xbf\xff") == (NAME_ONLY, ["text.bom", "text.not_utf8"])


def test_binary_is_not_text() -> None:
    assert probe(b"PK\x03\x04\x00\x00") == (0.0, ["text.nul"])
    assert probe(b"\x89MCAP0\r\n\x01") == (0.0, ["text.control"])
    assert probe(b"text then \x7f is fine")[0] == GENERIC


def test_an_empty_source_is_claimed_weakly() -> None:
    assert probe(b"") == (NAME_ONLY, ["text.empty"])


def test_a_head_cut_inside_a_character_is_still_text() -> None:
    data = b"a" * (PROBE_HEAD_SIZE - 1) + "é".encode() + b"tail"
    assert probe(data)[0] == GENERIC
    assert probe(data[:PROBE_HEAD_SIZE])[0] == NAME_ONLY  # the same bytes as a whole source


# --- Inspect -----------------------------------------------------------------------------------


def test_inspect_summarises_from_the_head_only() -> None:
    class HeadOnly(BytesReader):
        def read(self, offset: int, length: int) -> bytes:
            assert offset + length <= PROBE_HEAD_SIZE
            return super().read(offset, length)

    source = HeadOnly(fixture("operator_log") + b"x" * (2 * PROBE_HEAD_SIZE))
    result = TextAdapter().inspect(source, configure(DESCRIPTOR))
    assert result.summary == {"bom": True, "head_lines": 5, "size": source.size}
    assert result.findings == ()


# --- Real files --------------------------------------------------------------------------------


def test_a_document_cites_every_byte_and_declares_no_title_or_pages() -> None:
    data = fixture("notes.txt")
    output = run(data)
    (document,) = (r for r in output.records() if isinstance(r, DocumentRecord))
    assert document.provenance.evidence == EvidenceRef(
        BytesReader(data).content_id, (ByteRange(0, len(data)),)
    )
    assert (document.format, document.title, document.pages) == ("text", NotCovered(), ())
    assert all(block.document == document.id for block in blocks(output))


def test_paragraphs_are_blocks_with_exact_text() -> None:
    output = run(fixture("notes.txt"))
    assert [block.text for block in blocks(output)] == [
        Known(
            "Site visit — dock 4\nRobot spot-02 walked the north aisle twice.\n"
            "Battery swap at 14:05."
        ),
        Known(
            "Issues\n  - lidar dropout near the cold store door\n"
            "  - stair edge marker missing (日本語 label)"
        ),
        Known("Next: re-run with the 2026-09 map 🗺️"),
    ]
    assert [block.order for block in blocks(output)] == [0, 1, 2]
    for block in blocks(output):
        assert (block.role, block.level, block.region) == (
            NotCovered(),
            NotCovered(),
            NotApplicable(),
        )
    assert output.findings() == ()


def test_the_line_rule_makes_each_nonblank_line_a_block() -> None:
    output = run(fixture("notes.txt"), block_rule="line")
    assert len(blocks(output)) == 7
    assert blocks(output)[4].text == Known("  - lidar dropout near the cold store door")


def test_spans_count_code_points_after_the_bom_and_keep_crlf_inside_blocks() -> None:
    data = fixture("operator_log")
    output = run(data)
    text = data.removeprefix(BOM).decode()
    assert [span(block) for block in blocks(output)] == oracle(data)
    for block in blocks(output):
        start, end = span(block)
        assert block.text == Known(text[start:end])
    assert blocks(output)[0].text == Known("OPERATOR LOG\r\nshift: night")


def test_a_truncated_file_keeps_its_text_up_to_the_damage() -> None:
    data = fixture("truncated.txt")
    output = run(data)
    (block,) = blocks(output)
    assert block.text == Unknown()
    (finding,) = output.findings()
    assert finding.code == "text.invalid_utf8"
    assert finding.records == (block.id,)
    assert finding.details == {"block": 0, "first_invalid_byte": len(data) - 1}
    assert finding.subject == EvidenceRef(BytesReader(data).content_id, (ByteRange(0, len(data)),))


def test_a_corrupted_paragraph_is_unknown_and_the_rest_decode() -> None:
    data = fixture("corrupted.txt")
    output = run(data)
    texts = [block.text for block in blocks(output)]
    assert texts == [Known("Inspection summary"), Unknown(), Known("signed: R. Ito")]
    (finding,) = output.findings()
    assert finding.details == {"block": 1, "first_invalid_byte": data.index(b"\xff")}
    assert [span(block) for block in blocks(output)] == oracle(data)


def test_an_empty_or_blank_file_is_a_document_without_blocks() -> None:
    for data in (fixture("empty.txt"), b" \t\r\n\n  \n", BOM, BOM + b"\n"):
        output = run(data)
        assert [r.kind for r in output.records()] == ["document_record"]
        assert len(output.plan.chunks) == 1


# --- Limits ------------------------------------------------------------------------------------


def test_a_block_over_the_limit_is_reported_and_its_order_left_empty() -> None:
    data = b"short\n\n" + b"x" * 40 + b"\n\nafter\n"
    output = run(data, max_block_bytes=16)
    assert [(block.order, block.text) for block in blocks(output)] == [
        (0, Known("short")),
        (2, Known("after")),
    ]
    (finding,) = output.findings()
    assert finding.code == "text.block_too_large"
    assert finding.subject == EvidenceRef(BytesReader(data).content_id, (ByteRange(7, 40),))
    assert finding.details == {"block": 1, "bytes": 40, "max_block_bytes": 16}
    for chunk in output.plan.chunks[1:]:
        assert not (chunk.context["start"] <= 7 < chunk.context["end"])  # type: ignore[operator]


class Reads(BytesReader):
    """A reader that remembers every range it served."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.ranges: list[tuple[int, int]] = []

    def read(self, offset: int, length: int) -> bytes:
        self.ranges.append((offset, length))
        return super().read(offset, length)


@given(
    st.lists(st.sampled_from([b"word ", b"\n", b"\n\n", b" \n"]), max_size=60),
    st.integers(1, 64),
    st.sampled_from([8, 32, 1 << 20]),
)
def test_ingest_reads_only_its_chunk_and_a_chunk_is_bounded(
    parts: list[bytes], chunk_bytes: int, limit: int
) -> None:
    data = b"".join(parts)
    adapter, config = TextAdapter(chunk_bytes), configure(DESCRIPTOR, {"max_block_bytes": limit})
    plan = adapter.plan(BytesReader(data), config)
    for chunk in plan.chunks:
        source = Reads(data)
        adapter.ingest(source, chunk, config)
        if chunk.context["part"] == "document":
            assert source.ranges == []
            continue
        start, end = chunk.context["start"], chunk.context["end"]
        assert source.ranges == [(start, end - start)]  # type: ignore[operator]
        assert chunk.cost == end - start <= max(chunk_bytes, limit)  # type: ignore[operator]


# --- Determinism, chunking and lineage ---------------------------------------------------------

TEXTY = st.lists(
    st.sampled_from(
        [
            b"a",
            b"bc",
            b" ",
            b"\t",
            b"\r",
            b"\n",
            b"\r\n",
            b"\xc3\xa9",
            b"\xf0\x9f\x97\xba",
            b"\xff",
            b"\xc3",
            b"\x80",
        ]
    ),
    max_size=80,
).map(b"".join)


@settings(max_examples=300)
@given(TEXTY, st.booleans(), st.sampled_from(["paragraph", "line"]), st.integers(1, 40))
def test_output_never_depends_on_where_chunks_are_cut(
    body: bytes, bom: bool, rule: str, chunk_bytes: int
) -> None:
    data = (BOM if bom else b"") + body
    whole = run(data, block_rule=rule)
    cut = run(data, chunk_bytes, block_rule=rule)
    assert as_bytes(cut) == as_bytes(whole)
    assert [span(block) for block in blocks(whole)] == oracle(data, rule)
    text = data.removeprefix(BOM).decode("utf-8", errors="replace")
    for block in blocks(whole):
        start, end = span(block)
        if isinstance(block.text, Known):
            assert block.text.value == text[start:end]
        else:
            assert "�" in text[start:end]


@given(TEXTY, st.lists(st.integers(1, 9), min_size=1, max_size=20))
def test_lines_stream_across_any_piece_boundaries(data: bytes, sizes: list[int]) -> None:
    pieces: list[bytes] = []
    position = 0
    while position < len(data):
        size = sizes[len(pieces) % len(sizes)]
        pieces.append(data[position : position + size])
        position += size
    assert list(_lines(pieces, 5, 7)) == list(_lines([data], 5, 7))


def test_ingesting_twice_gives_identical_bytes() -> None:
    for name in ("notes.txt", "operator_log", "corrupted.txt", "truncated.txt", "empty.txt"):
        assert as_bytes(run(fixture(name), 16)) == as_bytes(run(fixture(name), 16))


def test_another_config_is_another_lineage() -> None:
    paragraphs, lines = run(fixture("notes.txt")), run(fixture("notes.txt"), block_rule="line")
    assert paragraphs.config.transform.id != lines.config.transform.id
    assert not {r.id for r in paragraphs.records()} & {r.id for r in lines.records()}


def test_a_new_adapter_version_is_a_new_lineage_and_leaves_the_old_untouched() -> None:
    class Upgraded(TextAdapter):
        descriptor = replace(DESCRIPTOR, version="0.2.0")

    data = fixture("notes.txt")
    old = run(data)
    before = as_bytes(old)
    new = ingest_source(Upgraded(), BytesReader(data))
    assert new.config.transform.adapter_version == "0.2.0"
    assert not {r.id for r in old.records()} & {r.id for r in new.records()}
    assert [b.text for b in blocks(new)] == [b.text for b in blocks(old)]
    assert as_bytes(old) == before


def test_planning_twice_gives_the_same_chunks_in_the_same_order() -> None:
    data = fixture("notes.txt") * 50
    config = configure(DESCRIPTOR)
    first = TextAdapter(256).plan(BytesReader(data), config)
    assert first == TextAdapter(256).plan(BytesReader(data), config)
    assert len(first.chunks) > 10
    assert all(re.fullmatch(r"chunk:sha256:[0-9a-f]{64}", chunk.id) for chunk in first.chunks)


def test_a_chunk_ingests_the_same_alone_as_in_a_run() -> None:
    data = fixture("notes.txt") * 3
    adapter, config = TextAdapter(64), configure(DESCRIPTOR)
    source = BytesReader(data)
    plan = adapter.plan(source, config)
    alone: list[ChunkOutput] = [
        adapter.ingest(source, chunk, config) for chunk in reversed(plan.chunks)
    ]
    together = ingest_source(adapter, source)
    assert list(reversed(alone)) == list(together.outputs)


def test_chunks_name_their_source_and_transform() -> None:
    output = run(fixture("notes.txt"), 64)
    chunk: Chunk
    for chunk in output.plan.chunks:
        assert chunk.source == BytesReader(fixture("notes.txt")).content_id
        assert chunk.transform == output.config.transform.id
