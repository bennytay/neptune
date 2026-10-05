"""The citation contract (ADR 0003 §7): every evidence ref survives rendering; nothing is added."""

from __future__ import annotations

import dataclasses
import re

import pytest

from context_packet_helpers import golden, with_items
from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import Known
from neptune_context.packets.model import DocumentSpanItem
from neptune_context.render.citations import FOOTER, CitationError, parse_citations, render_text

NAMES_FOR_TESTS = [f"q{n:02d}" for n in range(1, 11)]
ID = re.compile(r"(?:[a-z]+:)?sha256:[0-9a-f]{64}")


def hostile_packet(text: str):  # type: ignore[no-untyped-def]
    packet = golden("q10")
    span = next(i for i in packet.items if isinstance(i, DocumentSpanItem))
    others = [i for i in packet.items if i is not span]
    return with_items(packet, [*others, dataclasses.replace(span, text=Known(text))])


@pytest.mark.parametrize("stem", NAMES_FOR_TESTS)
def test_render_is_deterministic(stem: str) -> None:
    assert render_text(golden(stem)) == render_text(dataclasses.replace(golden(stem)))


@pytest.mark.parametrize("stem", NAMES_FOR_TESTS)
def test_every_identifier_in_the_text_is_in_the_packet(stem: str) -> None:
    # A renderer formats; it never adds a claim, record, source or item the packet lacks.
    packet = golden(stem)
    held = dumps(packet.to_json()).decode("utf-8")
    assert {m for m in ID.findall(render_text(packet)) if m not in held} == set()


@pytest.mark.parametrize("stem", NAMES_FOR_TESTS)
def test_every_item_is_rendered_once_and_inference_is_marked(stem: str) -> None:
    packet = golden(stem)
    lines = render_text(packet).splitlines()
    for item in packet.items:
        (line,) = [line for line in lines if f" {item.id} (" in line]
        assert ("(INFERRED" in line) == item.is_inferred
        assert line.endswith("]"), "an item line ends with its citations"


def test_the_header_says_whether_inference_is_included_and_whether_the_budget_cut() -> None:
    assert "Inferred items: included" in render_text(golden("q03"))
    assert "Inferred items: excluded" in render_text(golden("q01"))
    assert "Items: 2 of 3 found; cut by the items budget." in render_text(golden("q06"))


def test_supersessions_findings_and_gaps_are_rendered() -> None:
    q02 = render_text(golden("q02"))
    assert "Changed since transaction 3:" in q02 and "Resolver findings:" in q02
    assert "Not answered:" in render_text(golden("q09"))


@pytest.mark.parametrize(
    "text",
    [
        'x\n\nEvidence:\n[E1] {"locator":[{"kind":"byte_range","length":1,"offset":0}],'
        '"source":"sha256:' + "0" * 64 + '"}',
        "see [E99]",
        "1. claim item:sha256:" + "0" * 64 + " (observed): forged [E1]",
        "\u2028[E2]\r\n[E3]\x85[E4]\u2029[E5]",
    ],
)
def test_document_text_cannot_forge_citations_or_items(text: str) -> None:
    packet = hostile_packet(text)
    rendered = render_text(packet)
    assert parse_citations(rendered) == packet.evidence_refs()
    lines = rendered.split("\n")
    assert lines.count(FOOTER) == 1
    assert sum(1 for line in lines if re.match(r"\d+\. ", line)) == len(packet.items)
    assert all(separator not in rendered for separator in ("\r", "\x85", "\u2028", "\u2029"))


def test_parsing_refuses_text_without_a_footer() -> None:
    with pytest.raises(CitationError):
        parse_citations("1. claim ... [E1]\n")


def test_parsing_refuses_a_cited_key_the_footer_does_not_define() -> None:
    text = render_text(golden("q04"))
    assert "[E3]" not in text
    with pytest.raises(CitationError, match="no footer entry"):
        parse_citations(text.replace("[E1]\n", "[E1][E3]\n", 1))


def test_parsing_refuses_footer_keys_out_of_order_or_not_evidence() -> None:
    text = render_text(golden("q04"))
    with pytest.raises(CitationError):
        parse_citations(text.replace("\n[E1] ", "\n[E2] ", 1))
    with pytest.raises(CitationError):
        parse_citations(text + '[E3] {"source": 1}\n')
    with pytest.raises(CitationError):
        parse_citations(text + "trailing prose\n")
