"""The Markdown adapter on real files, malformed input, boundaries, and generated Markdown.

The oracle for every span is the file decoded at once (after a BOM, ``errors="replace"``): a block's
text is exactly the code points its span cites. Roles come from what CommonMark parses, which the
expected lists spell out per fixture.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import PROBE_HEAD_SIZE, STRUCTURE, ProbeHints, configure
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.markdown import DESCRIPTOR, NAMED_DAMAGED, NAMED_TEXT, MarkdownAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.finding import IngestFinding, Severity
from neptune.model.knowledge import Known, KnownAbsent, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Span
from neptune.model.world import (
    BlockRole,
    DocumentBlock,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
)

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "markdown"
TEXT_FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "text"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(MarkdownAdapter(), BytesReader(data), config)


def decoded(data: bytes) -> str:
    return data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", errors="replace")


def state(knowledge: Any) -> Any:
    return knowledge.value if isinstance(knowledge, Known) else type(knowledge).__name__


def blocks(output: SourceOutput) -> list[DocumentBlock]:
    found = [r for r in output.records() if isinstance(r, DocumentBlock)]
    return sorted(found, key=lambda block: block.order)


def span(provenance: Any) -> tuple[int, int]:
    assert isinstance(provenance, Provenance)
    (step,) = provenance.evidence.locator
    assert isinstance(step, Span)
    return step.start, step.end


def summary(output: SourceOutput) -> list[tuple[Any, Any, Any]]:
    return [(state(b.role), state(b.level), state(b.text)) for b in blocks(output)]


def document(output: SourceOutput) -> DocumentRecord:
    (found,) = [r for r in output.records() if isinstance(r, DocumentRecord)]
    return found


def finding(output: SourceOutput, code: str) -> IngestFinding:
    (found,) = [f for f in output.findings() if f.code == code]
    return found


def check_citations(data: bytes, output: SourceOutput) -> None:
    """Every block's and cell's text is exactly what its span cites; blocks do not overlap."""
    text = decoded(data)
    end = 0
    for order, block in enumerate(blocks(output)):
        assert block.order == order
        start, stop = span(block.provenance)
        assert end <= start <= stop <= len(text)
        end = stop
        if isinstance(block.text, Known):
            assert text[start:stop] == block.text.value
        assert state(block.region) == "NotApplicable"
    for row in (r for r in output.records() if isinstance(r, StructuredRecord)):
        row_start, row_end = span(row.provenance)
        for cell in row.cells:
            assert isinstance(cell, Known | Unknown)
            start, stop = span(cell.provenance)
            assert row_start <= start <= stop <= row_end
            if isinstance(cell, Known):
                assert text[start:stop].replace("\\|", "|") == cell.value


def as_bytes(output: SourceOutput) -> bytes:
    rows = [record.to_json() for record in output.package_records()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


# --- Probe and selection -----------------------------------------------------------------------


def probe(data: bytes, name: str = "f") -> tuple[float, list[str]]:
    result = MarkdownAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


YAML: Final = b"# robot config\njoints:\n  - shoulder\n  - elbow\nrate: 50\n"


def test_markdown_syntax_is_structure_whatever_the_name() -> None:
    assert probe(fixture("pump_sop.md"), "pump_sop.md") == (
        STRUCTURE,
        ["markdown.name", "markdown.syntax"],
    )
    assert probe(fixture("runbook"), "runbook") == (STRUCTURE, ["markdown.syntax"])
    assert probe(fixture("datasheet_crlf.md"))[0] == STRUCTURE  # CRLF and a BOM


def test_the_name_decides_only_where_the_bytes_cannot() -> None:
    assert probe(fixture("notes.md"), "notes.md") == (
        NAMED_TEXT,
        ["markdown.name", "markdown.text"],
    )
    assert probe(fixture("notes.md"), "notes") == (0.0, ["markdown.no_syntax"])
    assert probe(fixture("truncated.md"), "truncated.md")[0] == NAMED_DAMAGED
    assert probe(fixture("corrupted.md"), "corrupted.txt")[0] == 0.0
    assert probe(b"", "empty.md")[0] == NAMED_DAMAGED and probe(b"", "empty")[0] == 0.0
    assert probe(b"\x89PNG\r\n\x1a\n", "x.md")[0] == 0.0
    assert probe(YAML, "robot.yaml") == (0.0, ["markdown.no_syntax"])  # comments and dashes


def test_selection_against_the_text_adapter_never_ties() -> None:
    registry = default_registry()

    def select(data: bytes, name: str) -> str | None:
        return registry.select(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data))).adapter

    assert select(fixture("pump_sop.md"), "pump_sop.md") == "markdown"
    assert select(fixture("runbook"), "runbook") == "markdown"
    assert select(fixture("notes.md"), "notes.md") == "markdown"
    assert select(fixture("empty.md"), "empty.md") == "markdown"
    assert select(fixture("truncated.md"), "truncated.md") == "markdown"
    assert select((TEXT_FIXTURES / "notes.txt").read_bytes(), "notes.txt") == "text"
    assert select(fixture("corrupted.md"), "corrupted.txt") == "text"
    assert select(YAML, "robot.yaml") == "text"


def test_inspect_summarises_from_the_head() -> None:
    data = fixture("pump_sop.md")
    result = MarkdownAdapter().inspect(BytesReader(data), configure(DESCRIPTOR))
    assert result.summary == {
        "bom": False,
        "front_matter": True,
        "head_lines": data.count(b"\n"),
        "size": len(data),
    }


# --- Real files --------------------------------------------------------------------------------


def test_commonmark_blocks_carry_the_roles_the_syntax_declares() -> None:
    data = fixture("pump_sop.md")
    output = run(data)
    assert summary(output) == [
        (BlockRole.HEADING, 1, "Pump room start-up"),
        (
            BlockRole.PARAGRAPH,
            "NotApplicable",
            "Close valve **V-12** before entering the pump room.\n"
            "Check that the pressure gauge reads below 2 bar.",
        ),
        (
            BlockRole.QUOTE,
            "NotApplicable",
            "Warning: hearing protection is required\nnear pump P-2.",
        ),
        (BlockRole.HEADING, 2, "Procedure"),
        (BlockRole.LIST_ITEM, 1, "Isolate the pump at the local panel."),
        (BlockRole.LIST_ITEM, 1, "Start pump P-2."),
        (BlockRole.LIST_ITEM, 2, "Confirm discharge pressure."),
        (
            BlockRole.LIST_ITEM,
            2,
            "Log the reading in the [site register](https://example.invalid/register).",
        ),
        (BlockRole.LIST_ITEM, 2, "Repeat after five minutes."),
        (BlockRole.LIST_ITEM, 1, "Open valve V-12 slowly."),
        (BlockRole.CODE, "NotApplicable", "ros2 run pump_monitor log --pump P-2"),
        (BlockRole.HEADING, 2, "Torque table"),
        (
            BlockRole.TABLE,
            "NotApplicable",
            "| Bolt | Torque | Unit |\n|------|-------:|------|\n| M8   | 25     | N·m  |\n"
            "| M10  |        | N·m  |\n| M12  | 85 \\| 90 | N·m |",
        ),
        (BlockRole.HEADING, 2, "Sign-off"),  # setext
        ("Unknown", "Unknown", "<!-- reviewed by operations -->"),  # raw HTML
        ("Unknown", "Unknown", "[register]: https://example.invalid/register"),  # link definition
    ]
    assert [f.code for f in output.findings()] == ["markdown.link_definitions"]
    check_citations(data, output)


def test_link_reference_definitions_are_kept_as_evidence() -> None:
    data = b"See [spec][s] and [a].\n\n[s]: https://example.invalid/spec\n  'Spec'\n[A]: </a b>\n"
    data += b"[s]: /duplicate\n> [q]: /quoted\n- [i]: /listed\n"
    output = run(data)
    kept = [(state(b.role), state(b.text)) for b in blocks(output)]
    assert kept == [
        (BlockRole.PARAGRAPH, "See [spec][s] and [a]."),
        ("Unknown", "[s]: https://example.invalid/spec\n  'Spec'"),
        ("Unknown", "[A]: </a b>"),
        ("Unknown", "[s]: /duplicate"),  # a later duplicate is evidence too
        ("Unknown", "[q]: /quoted"),
        ("Unknown", "[i]: /listed"),
    ]
    found = finding(output, "markdown.link_definitions")
    assert (found.severity, found.details) == (Severity.INFO, {"blocks": 5})
    assert len(found.records) == 5
    check_citations(data, output)


def test_a_malformed_definition_is_only_a_paragraph() -> None:
    for data in (
        b"[a]:\n",
        b"[a] : /u\n",
        b"[a]: /u trailing junk\n",
        b"    [a]: /u\n",
        b"[]: /u\n",
    ):
        output = run(data)
        assert not [f for f in output.findings() if f.code == "markdown.link_definitions"]
        assert all(state(b.role) != "Unknown" for b in blocks(output)), data
        check_citations(data, output)


def test_a_fence_or_heading_interrupts_a_paragraph_as_commonmark_says() -> None:
    output = run(b"intro\n```\ncode\n```\ntext\n# Head\n> quote\n")
    assert [(state(b.role), state(b.text)) for b in blocks(output)] == [
        (BlockRole.PARAGRAPH, "intro"),
        (BlockRole.CODE, "code"),
        (BlockRole.PARAGRAPH, "text"),
        (BlockRole.HEADING, "Head"),
        (BlockRole.QUOTE, "quote"),
    ]


def test_the_front_matter_title_cites_its_value() -> None:
    data = fixture("pump_sop.md")
    record = document(run(data))
    assert (record.format, record.pages) == ("markdown", ())
    assert record.provenance.evidence == EvidenceRef(
        BytesReader(data).content_id, (ByteRange(0, len(data)),)
    )
    title = record.title
    assert isinstance(title, Known) and title.value == "Pump room start-up SOP"
    start, end = span(title.provenance)
    assert decoded(data)[start:end] == "Pump room start-up SOP"


def test_a_gfm_table_is_a_structured_table_cell_by_cell() -> None:
    data = fixture("pump_sop.md")
    output = run(data)
    (table,) = [r for r in output.records() if isinstance(r, StructuredTable)]
    (block,) = [b for b in blocks(output) if state(b.role) is BlockRole.TABLE]
    assert table.provenance.evidence == block.provenance.evidence
    assert (state(table.name), state(table.header)) == ("NotCovered", ("Bolt", "Torque", "Unit"))
    rows = sorted(
        (r for r in output.records() if isinstance(r, StructuredRecord)), key=lambda r: r.row
    )
    assert [(row.row, [state(cell) for cell in row.cells]) for row in rows] == [
        (1, ["M8", "25", "N·m"]),
        (2, ["M10", "Unknown", "N·m"]),  # blank is Unknown, never ""
        (3, ["M12", "85 | 90", "N·m"]),  # the escaped pipe is a pipe in the value
    ]
    blank = rows[1].cells[1]
    assert isinstance(blank, Unknown)
    start, end = span(blank.provenance)
    text = decoded(data)
    assert (
        start == end
        and text[start] == "|"
        and text[text.rindex("| M10", 0, start) :][:7] == "| M10  "
    )


def test_a_register_table_keeps_a_blank_cell_unknown() -> None:
    data = fixture("site_manifest.md")
    output = run(data)
    rows = sorted(
        (r for r in output.records() if isinstance(r, StructuredRecord)), key=lambda r: r.row
    )
    assert [state(cell) for cell in rows[2].cells] == [
        "R-7",
        "Inspection robot",
        "quadruped",
        "Unknown",
    ]
    code = summary(output)[-1]
    assert code == (
        BlockRole.CODE,
        "NotApplicable",
        "    asset_count: 3\n    register_revision: 2026-09",
    )
    check_citations(data, output)


def test_crlf_and_a_bom_are_counted_as_the_text_adapter_counts_them() -> None:
    data = fixture("datasheet_crlf.md")
    output = run(data)
    first = blocks(output)[0]
    assert span(first.provenance) == (2, 24) and state(first.text) == "GX-2 gripper datasheet"
    assert "\r\n" in state(blocks(output)[1].text)
    check_citations(data, output)


# --- Malformed input and bounds ----------------------------------------------------------------


def test_invalid_utf8_makes_its_block_unknown_and_says_where() -> None:
    data = fixture("corrupted.md")
    output = run(data)
    assert [text for *_, text in summary(output)] == [
        "Inspection log",
        "Reading 1: 1.2 bar, nominal.",
        "Unknown",
        "Reading 3: 1.3 bar.",
    ]
    damaged = finding(output, "markdown.invalid_utf8")
    assert damaged.details == {"blocks": 1, "first_invalid_byte": data.index(b"\xff")}
    assert damaged.records == (blocks(output)[2].id,) and damaged.severity is Severity.WARNING
    check_citations(data, output)


def test_a_file_cut_inside_a_character_loses_only_what_it_touches() -> None:
    data = fixture("truncated.md")
    output = run(data)
    assert (
        state(blocks(output)[-1].text) == "Unknown"
        and state(blocks(output)[-1].role) is BlockRole.TABLE
    )
    (row,) = [r for r in output.records() if isinstance(r, StructuredRecord)]
    assert [state(cell) for cell in row.cells] == ["M8", "25", "Unknown"]
    assert finding(output, "markdown.invalid_utf8").details["blocks"] == 1
    check_citations(data, output)


def test_an_empty_file_is_a_document_with_no_blocks() -> None:
    output = run(b"")
    assert blocks(output) == [] and output.findings() == ()
    assert state(document(output).title) == "Unknown"


def test_nesting_past_the_limit_is_reported_and_the_rest_read() -> None:
    output = run(fixture("deep_nesting.md"))
    assert finding(output, "markdown.nesting_limit").details == {"first_line": 2, "last_line": 2}
    assert [text for *_, text in summary(output)] == ["Nested quotes", "After the nesting."]


def test_a_file_over_the_size_bound_keeps_its_document_and_no_blocks() -> None:
    data = fixture("pump_sop.md")
    output = run(data, max_document_bytes=100)
    assert blocks(output) == [] and state(document(output).title) == "Pump room start-up SOP"
    assert finding(output, "markdown.too_large").details == {
        "bytes": len(data),
        "max_document_bytes": 100,
    }


# --- Front matter ------------------------------------------------------------------------------


def title(front: str) -> Any:
    output = run(f"---\n{front}\n---\n\n# Body\n".encode())
    return document(output).title, output


def test_front_matter_titles_are_read_only_when_plainly_written() -> None:
    assert state(title("title: Pump room # owner: ops")[0]) == "Pump room"
    assert state(title('title: "Pump: room"')[0]) == "Pump: room"
    assert state(title("title: 'Operator''s guide'")[0]) == "Operator's guide"
    assert state(title("site: north\ntitle:")[0]) == "Unknown"
    assert state(title("  title: nested")[0]) == "Unknown"  # not a top-level key
    absent, _ = title("title: ~")
    assert isinstance(absent, KnownAbsent)
    for unreadable in ("title: |\n  Pump room", 'title: "Pump \\" room"', "title: [a, b]"):
        found, output = title(unreadable)
        assert state(found) == "Unknown"
        assert [f.code for f in output.findings()] == ["markdown.title_unreadable"]


def test_front_matter_is_never_a_block_and_unclosed_is_not_front_matter() -> None:
    _, output = title("title: Pump room\nsite: north")
    assert summary(output) == [(BlockRole.HEADING, 1, "Body")]
    unclosed = run(b"---\ntitle: Pump room\n\nBody\n")
    assert state(document(unclosed).title) == "Unknown"
    assert "title: Pump room" in [text for *_, text in summary(unclosed)]
    off = run(b"---\ntitle: Pump room\n---\n", front_matter=False)
    assert state(document(off).title) == "Unknown" and summary(off) != []


def test_both_chunks_see_front_matter_in_the_same_first_64_kib_of_bytes() -> None:
    # The closing line is within 64 K code points but past 64 KiB of bytes: not front matter.
    late = ("---\ntitle: Pump\nnote: " + "\u00e9" * 40_000 + "\n---\n\nBody\n").encode()
    output = run(late)
    assert state(document(output).title) == "Unknown"
    assert any("title: Pump" in str(text) for *_, text in summary(output))
    early = ("---\ntitle: Pump\n---\n\n" + "\u00e9" * 40_000 + "\n").encode()
    output = run(early)
    assert state(document(output).title) == "Pump"
    assert not any("title: Pump" in str(text) for *_, text in summary(output))


# --- Determinism, lineage and generated Markdown -----------------------------------------------

EVERY: Final = sorted(p.name for p in FIXTURES.iterdir() if p.is_file() and p.name != "README.md")


def test_output_is_byte_identical_and_every_citation_resolves() -> None:
    for name in EVERY:
        data = fixture(name)
        once = run(data)
        assert as_bytes(run(data)) == as_bytes(once), name
        check_citations(data, once)


def test_another_version_is_another_lineage() -> None:
    class Bumped(MarkdownAdapter):
        descriptor = replace(DESCRIPTOR, version="0.1.1")

    data = fixture("pump_sop.md")
    old = ingest_source(MarkdownAdapter(), BytesReader(data))
    new = ingest_source(Bumped(), BytesReader(data))
    assert {r.id for r in old.records()}.isdisjoint({r.id for r in new.records()})
    assert summary(old) == summary(new)


LINES: Final = [
    "# Heading",
    "Setext",
    "===",
    "a paragraph line",
    "  indented continuation",
    "> quoted",
    ">> deeper",
    "- item",
    "  - nested item",
    "1. first",
    "```",
    "~~~ python",
    "    code line",
    "| a | b |",
    "|---|:-:|",
    "| 1 | \\| 2 |",
    "| x |",
    "<div>",
    "---",
    "title: x",
    "[ref]: /url",
    "\t tabbed",
    "café • \U0001f916",
    "**bold** and `code`",
    "",
    "",
]


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.sampled_from(LINES), max_size=40),
    st.sampled_from(["\n", "\r\n", "\r"]),
    st.binary(max_size=3),
)
def test_generated_markdown_never_raises_and_always_cites_exactly(
    lines: list[str], ending: str, tail: bytes
) -> None:
    data = ending.join(lines).encode() + tail
    output = run(data)
    check_citations(data, output)
    assert as_bytes(run(data)) == as_bytes(output)
