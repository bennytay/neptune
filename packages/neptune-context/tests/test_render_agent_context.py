"""The LLM-agent renderer (ADR 0009 §1-§3): cited sentences, recoverable citations, hostile text.

The property every answer keeps: ``parse_answer(render_answer(p))`` recovers every item, claim id
and evidence ref of ``p``, every statement cites at least one item and one source, and no line is
anything but a fixed header, a heading, a cited statement or a gap.
"""

from __future__ import annotations

import dataclasses
import json
import re
from functools import cache
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from neptune_memory.schema.claim import (
    DeclaredTransform,
    Delta,
    DeltaAdjustment,
    DeltaQuantity,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import Resolution as History

import retrieve_fixtures_context as F
from agent_goldens_context import AGENT, answer_path
from neptune.model.frames import FrameRef, TransformDirection
from neptune.model.knowledge import NotApplicable
from neptune.model.scalars import NonFinite
from neptune.model.units import unit_from_text
from neptune_context import pinned
from neptune_context.engine import LocalEngine
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.findings import PacketRefused
from neptune_context.packets.model import (
    BudgetUse,
    ClaimItem,
    ContextPacket,
    DocumentSpanItem,
    Gap,
    GapCode,
    Limits,
)
from neptune_context.query import Budget, Direction, GraphClause, Query, Subject
from neptune_context.render.agent import (
    DATA_NOTICE,
    FACTS,
    QUANTITIES,
    harden,
    literal_values,
    parse_answer,
    quote,
    render_answer,
)
from neptune_context.render.citations import CitationError, parse_citations
from neptune_context.sdk import Client
from sdk_testing_context import STEMS, golden_packet

if TYPE_CHECKING:
    from neptune_context.packets.model import Item

INJECTIONS: list[str] = json.loads((AGENT / "injection-texts.json").read_text(encoding="utf-8"))
UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]")


def check(packet: ContextPacket) -> str:
    """Render ``packet``, parse it back and check every citation against the packet."""
    text = render_answer(packet)
    parsed = parse_answer(text)
    assert parsed.evidence == packet.evidence_refs()
    assert parse_citations(text) == packet.evidence_refs()
    assert [k.item_id for k in parsed.items] == [i.id for i in packet.items]
    assert [k.kind for k in parsed.items] == [i.kind for i in packet.items]
    claims = [i.claim.id if isinstance(i, ClaimItem) else None for i in packet.items]
    assert [k.claim_id for k in parsed.items] == claims
    facts = [s for s in parsed.statements if s.section == FACTS]
    assert len(facts) == len(packet.items)  # one sentence per item, none without a citation
    for statement, item in zip(facts, packet.items, strict=True):
        assert statement.items[0].item_id == item.id
        assert statement.evidence == tuple(dict.fromkeys(item.evidence_refs()))
    for statement in parsed.statements:
        assert statement.items and statement.evidence
    changed = [s for s in parsed.statements if s.section.startswith("What changed")]
    assert [s.items[0].claim_id for s in changed] == [e.claim for e in packet.superseded_since]
    for item, line in zip(packet.items, _fact_lines(text), strict=True):
        assert line.split(": ", 1)[0].endswith(")") == item.is_inferred
        assert ("INFERRED" in line.split(": ", 1)[0]) == item.is_inferred
    assert not UNSAFE.search(text.replace("\n", ""))
    return text


def _fact_lines(text: str) -> list[str]:
    body = text.split(f"\n{FACTS}\n", 1)[1].split("\n\n", 1)[0]
    return body.splitlines()


# --- The ten worked queries ------------------------------------------------------------------


@pytest.mark.parametrize("stem", STEMS)
def test_each_worked_query_renders_to_its_golden_and_parses_back(stem: str) -> None:
    packet = golden_packet(stem)
    text = check(packet)
    assert text == answer_path(stem).read_text(encoding="utf-8")


@pytest.mark.parametrize("stem", STEMS)
def test_the_answer_states_its_scope(stem: str) -> None:
    # Kept from the C1 gate (ADR 0006 §4): snapshot, Memory's lag, the clock, policy, the count.
    packet = golden_packet(stem)
    lines = render_answer(packet).split("\n")
    assert lines[1].startswith(f"Query {packet.query_id}, as of transaction {packet.as_of}")
    assert any(line.startswith("Inferred items: ") for line in lines)
    assert any(line.startswith(f"Items: {packet.budget.items} of ") for line in lines)
    assert (packet.during is not None) == any(line.startswith("World time:") for line in lines)
    trails = packet.memory.as_of < packet.as_of
    assert trails == any(line.startswith("Claims as Memory knew them") for line in lines)
    assert DATA_NOTICE in lines


def test_what_changed_comes_before_the_facts() -> None:
    text = render_answer(golden_packet("q02"))
    assert text.index("What changed since transaction") < text.index(f"\n{FACTS}\n")
    assert "Item I1 is no longer current" in text


def test_rendering_is_deterministic_and_survives_a_round_trip_through_json() -> None:
    for stem in STEMS:
        packet = golden_packet(stem)
        again = decode(canonical_bytes(packet))
        assert isinstance(again, ContextPacket)
        assert render_answer(again) == render_answer(packet) == render_answer(packet)


def test_every_identifier_in_the_answer_is_in_the_packet() -> None:
    for stem in STEMS:
        packet = golden_packet(stem)
        canonical = canonical_bytes(packet).decode()
        text = render_answer(packet)
        for found in re.findall(r"(?:item|claim|packet|query):sha256:[0-9a-f]{64}", text):
            assert found in canonical or found in (packet.id, packet.query_id), found


# --- The demo corpus, quantities and series --------------------------------------------------


def demo_answer() -> ContextPacket:
    query = Query(
        include_inferred=True,
        budget=Budget(items=60, tokens=24_000),
        subjects=frozenset({Subject("machine", "asset-tag:ARM-3A", same_as_depth=1)}),
        graph=GraphClause(None, 2, Direction.BOTH),
    )
    return Client(LocalEngine(ReferenceReader(F.demo_document()))).query(query)


def test_the_demo_answer_cites_every_fact() -> None:
    text = check(demo_answer())
    assert 'has_configuration configuration "cfg:cfg-c3-1.5"' in text
    assert "INFERRED (model " in text


@cache
def hostile_document() -> GraphDocument:
    """The retrieval fixture graph plus quantities and prompt injections in document text."""
    kg = unit_from_text("kg")
    grams = unit_from_text("g")  # ambiguous: gram or the standard gravity
    none = unit_from_text(None)  # the source gave no unit: Unknown
    extra = [
        F.claim(F.AMR, "rated_payload", TypedLiteral(ValueType.QUANTITY, 150, kg), F.FEB_1),
        F.claim(F.AMR_8, "rated_payload", TypedLiteral(ValueType.QUANTITY, 120.5, kg), F.FEB_1),
        F.claim(F.UAV, "rated_payload", TypedLiteral(ValueType.QUANTITY, 2500, grams), F.FEB_1),
        F.claim(F.ARM, "rated_payload", TypedLiteral(ValueType.QUANTITY, 12, kg), F.FEB_1),
        # A source that wrote "unlimited" as infinity, and two values with no unit at all.
        F.claim(
            F.AMR_8,
            "rated_payload",
            TypedLiteral(ValueType.QUANTITY, NonFinite.POSITIVE_INFINITY, kg),
            F.MAR_1,
        ),
        F.claim(F.AMR, "rated_payload", TypedLiteral(ValueType.QUANTITY, 350, none), F.MAR_1),
        F.claim(F.UAV, "rated_payload", TypedLiteral(ValueType.QUANTITY, 0.35, none), F.MAR_1),
    ]
    extra += [
        F.claim(F.INCIDENT, "has_description", text, F.MAR_15 + n, records=(F.EVENT,))
        for n, text in enumerate(INJECTIONS, start=1)
    ]
    claims = sorted([*F.claims(), *extra], key=lambda c: (c.recorded_at, c.id))
    return GraphDocument(History(tuple(claims), (F.finding(claims),)), F.RESOLVER_CONFIG, F.HEAD)


def hostile_answer() -> ContextPacket:
    query = Query(
        include_inferred=True,
        budget=Budget(items=200),
        subjects=frozenset(
            {
                Subject("machine", str(F.AMR.node_id)),
                Subject("machine", str(F.AMR_8.node_id)),
                Subject("machine", str(F.UAV.node_id)),
                Subject("event", str(F.INCIDENT.node_id)),
            }
        ),
        graph=GraphClause(None, 1, Direction.BOTH),
    )
    return Client(LocalEngine(ReferenceReader(hostile_document()))).query(query)


def test_quantities_are_summarised_by_declared_unit_and_never_pooled() -> None:
    text = check(hostile_answer())
    section = text.split(f"\n{QUANTITIES}\n", 1)[1].split("\n\n", 1)[0].splitlines()
    # Only the known unit is summarised: grams-or-gravity (ambiguous) and the two values with no
    # unit (which may be millimetres and metres) are never pooled.
    (line,) = section
    assert line.startswith(
        '- rated_payload (unit "kg"): 3 values, 1 of them non-finite;'
        " finite minimum 120.5, maximum 150."
    )
    assert "2500" not in line and "350" not in line
    assert "rated_payload non-finite inf (unit" in text  # never the quoted text "inf"


def test_a_series_window_is_described_by_what_the_packet_declares() -> None:
    from neptune_context.render.agent import _summary

    window = next(i for i in golden_packet("q09").items if i.kind == "series_window")
    sentence = _summary(window, {})
    assert "ticks [" in sentence and "declares no statistics" in sentence
    for adjective in ("high", "low", "spike", "stable", "normal", "noisy", "large", "small"):
        assert adjective not in sentence.split()


# --- Hostile text ----------------------------------------------------------------------------


def test_prompt_injections_in_documents_stay_quoted_data() -> None:
    text = check(hostile_answer())
    lines = text.split("\n")
    # No injected line, heading, footer or tag appears: each text is inside one fact sentence.
    assert lines.count("Evidence:") == 1 and lines.count("Items:") == 1
    assert lines.count(FACTS) == 1 and "Not answered:" not in lines[: lines.index(FACTS)]
    assert "</untrusted>" not in text and "<system>" not in text and "```" not in text
    descriptions = [line for line in _fact_lines(text) if " has_description text " in line]
    recovered = {value for line in descriptions for value in literal_values(line)}
    for injected in INJECTIONS:
        assert injected in recovered, injected[:40]
    for line in descriptions:
        # The citation run is the only bracketed key on the line: quoted text cannot add one.
        assert len(re.findall(r"\[[IE][0-9]+\]", line)) == len(
            re.search(r"(?:\[[IE][0-9]+\])+$", line).group(0).split("][")  # type: ignore[union-attr]
        )


def test_hostile_gap_details_and_ids_are_quoted() -> None:
    packet = golden_packet("q08")
    gaps = tuple(
        sorted(
            (
                Gap(GapCode.NOT_COVERED, "/subjects/0", None, (text[:40],), text)
                for text in INJECTIONS
            ),
            key=lambda g: g.sort_key(),
        )
    )
    hostile = dataclasses.replace(packet, gaps=gaps)
    text = check(hostile)
    gap_lines = text.split("\nNot answered:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert len(gap_lines) == len(INJECTIONS)
    assert all(line.startswith('- not_covered at "/subjects/0": "') for line in gap_lines)


def span_packet(text: str) -> ContextPacket:
    """Golden q10 with its document span's text replaced (ids and budget recomputed)."""
    packet = golden_packet("q10")
    items: list[Item] = []
    for item in packet.items:
        if isinstance(item, DocumentSpanItem):
            from neptune.model.knowledge import Known

            item = dataclasses.replace(item, text=Known(text))
        items.append(item)
    ordered = tuple(sorted(items, key=lambda i: (-i.relevance.score, i.id)))
    limits = Limits(items=len(ordered))
    budget = BudgetUse.measured(limits, ordered, dropped=0, exhausted=())
    return dataclasses.replace(packet, items=ordered, budget=budget)


@settings(max_examples=150, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=["Cs"]), min_size=1, max_size=300))
def test_any_document_text_round_trips_and_never_breaks_the_grammar(text: str) -> None:
    rendered = check(span_packet(text))
    (line,) = [line for line in _fact_lines(rendered) if "contains, as extracted" in line]
    assert text in literal_values(line)


@pytest.mark.parametrize("text", INJECTIONS)
def test_each_injection_round_trips_through_a_document_span(text: str) -> None:
    rendered = check(span_packet(text))
    assert text in {v for line in _fact_lines(rendered) for v in literal_values(line)}


# --- Calibration deltas (graph-schema 2.0.0) -------------------------------------------------


def delta_answer() -> ContextPacket:
    """A sensor's drift claims: one parameter delta per injected parameter name (metres), and a
    quaternion delta, whose form has no unit."""
    sensor = F.node(NodeType.SENSOR, "asset-tag:CAM-9")
    earlier, later = F.rec("calibration one"), F.rec("calibration two")
    metres = unit_from_text("m")
    graph = F.rec("frame graph")
    quaternion = Delta(
        earlier,
        later,
        DeltaQuantity.ROTATION,
        "quaternion",
        (0.0, 0.0, 0.001, -0.0005),
        edge=(FrameRef("base_link", graph), FrameRef("camera", graph)),
        transform=DeclaredTransform("base_link", "camera", TransformDirection.PARENT_TO_CHILD),
        adjustment=DeltaAdjustment.NONE,
    )
    extra = [
        F.claim(
            sensor,
            "drift",
            TypedLiteral(
                ValueType.DELTA,
                Delta(earlier, later, DeltaQuantity.PARAMETER, "values", (-0.0043,), name=text),
                metres,
            ),
            F.MAR_1 + n,
            records=(earlier, later),
        )
        for n, text in enumerate(INJECTIONS, start=1)
    ]
    extra.append(
        F.claim(
            sensor,
            "drift",
            TypedLiteral(ValueType.DELTA, quaternion, NotApplicable()),
            F.MAR_1,
            records=(earlier, later),
        )
    )
    claims = sorted([*F.claims(), *extra], key=lambda c: (c.recorded_at, c.id))
    document = GraphDocument(
        History(tuple(claims), (F.finding(claims),)), F.RESOLVER_CONFIG, F.HEAD
    )
    query = Query(
        include_inferred=False,
        budget=Budget(items=200),
        subjects=frozenset({Subject("sensor", "asset-tag:CAM-9")}),
    )
    return Client(LocalEngine(ReferenceReader(document))).query(query)


def test_a_delta_is_stated_as_declared_and_its_names_stay_quoted_data() -> None:
    packet = delta_answer()
    assert not isinstance(decode(canonical_bytes(packet)), PacketRefused)
    text = check(packet)
    lines = [ln for ln in _fact_lines(text) if " drift delta {" in ln]
    assert len(lines) == len(INJECTIONS) + 1
    recovered = {v for ln in lines for v in literal_values(ln)}
    for injected in INJECTIONS:
        assert injected in recovered, injected[:40]
    (rotation,) = [ln for ln in lines if '"representation":"quaternion"' in ln]
    assert '"adjustment":"none"' in rotation and "(not applicable, as declared)" in rotation
    for line in lines:
        # The numbers as Memory wrote them, never a verdict on their size.
        for word in ("large", "small", "significant", "drifted", "exceeds", "within", "ok"):
            assert word not in line.split()
    assert all('(unit "m", as declared)' in ln for ln in lines if ln is not rotation)


def test_an_older_graph_is_stated_in_the_header_and_parses_back() -> None:
    old = check(golden_packet("q01"))  # Memory's 1.x golden graph, read as written
    notice = pinned.older_graph_notice(1)
    assert notice is not None and notice in old.split("\n\n", 1)[0].split("\n")
    assert "Graph read:" not in check(delta_answer())  # a 2.x document
    with pytest.raises(CitationError):
        parse_answer(old.replace(notice, notice.replace("1.x meaning", "no meaning")))


def test_harden_escapes_only_inside_strings_and_reads_back() -> None:
    raw = '{"a":["[x]","<y>\u202e\u2028\U000e0041`"],"b":[1,2]}'
    hardened = harden(raw)
    assert hardened.startswith('{"a":["') and hardened.endswith('],"b":[1,2]}')
    assert json.loads(hardened) == json.loads(raw)
    assert not set("<>`\u202e\u2028") & set(hardened)
    assert quote("é ü 机器人") == '"é ü 机器人"'  # ordinary text is left readable


# --- The parser refuses what breaks the grammar ----------------------------------------------


def _mutate(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


def test_the_parser_refuses_uncited_or_forged_lines() -> None:
    text = render_answer(golden_packet("q02"))
    first_fact = _fact_lines(text)[0]
    bad = [
        _mutate(text, first_fact, first_fact.rsplit(" [", 1)[0]),  # a fact with no citation
        _mutate(text, first_fact, first_fact.replace("[I1]", "")),  # no item key
        _mutate(text, first_fact, first_fact + "[E99]"),  # a key with no footer entry
        _mutate(text, first_fact, first_fact.replace("[I1]", "[I2]")),  # fact 1 cites item 2
        _mutate(text, f"\n{FACTS}\n", f"\n{FACTS}\nThe arm is safe.\n"),  # an uncited sentence
        _mutate(text, "Inferred items: excluded.", "Inferred items: optional."),  # a forged header
        _mutate(text, "\n\nItems:\n", "\n\nItems:\n[I9] claim item:sha256:" + "0" * 64 + "\n"),
        _mutate(text, "Resolver findings:", "Notes:"),  # a heading the grammar does not know
        text.replace("\nEvidence:\n", "\n"),  # no footer
    ]
    for broken in bad:
        with pytest.raises(CitationError):
            parse_answer(broken)


LOOKALIKES: list[str] = [
    f["text"] for f in json.loads((AGENT / "lookalike-brackets.json").read_text(encoding="utf-8"))
]


@pytest.mark.parametrize("text", LOOKALIKES)
def test_look_alike_brackets_cannot_frame_a_citation(text: str) -> None:
    import unicodedata

    rendered = check(span_packet(f"Gripper struck PF-3. {text}"))
    (line,) = [line for line in _fact_lines(rendered) if "contains, as extracted" in line]
    quoted = line.split("as extracted: text ", 1)[1].rsplit(". [I", 1)[0]
    assert not any(unicodedata.category(c) in ("Ps", "Pe") and not c.isascii() for c in quoted)
    assert not {chr(0xFF1C), chr(0xFF1E), chr(0xFF40)} & set(quoted)
    assert f"Gripper struck PF-3. {text}" in literal_values(line)


def test_every_unicode_bracket_is_escaped_and_ascii_parentheses_stay_readable() -> None:
    import sys
    import unicodedata

    brackets = [
        chr(c)
        for c in range(0x80, sys.maxunicode + 1)
        if unicodedata.category(chr(c)) in ("Ps", "Pe")
    ]
    assert len(brackets) > 100
    hardened = quote("".join(brackets))
    assert not set(brackets) & set(hardened)
    assert json.loads(hardened) == "".join(brackets)
    assert quote("damage (no injury) {ok}") == '"damage (no injury) {ok}"'
