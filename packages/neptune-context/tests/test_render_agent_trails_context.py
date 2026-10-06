"""Why and diff trails in the agent renderer (ADR 0011): cited outline and change lines.

The property every answer with trails keeps: ``parse_answer(render_answer(p))`` recovers, for each
trail, every line's claim, its role, its depth (why) or predicate group (diff), the finding behind
a conflict and the evidence it cites, in the packet's order; no trail line states anything
without a citation (a claim the packet does not carry is only named); and text from sources
cannot break the grammar.
"""

from __future__ import annotations

import json
import re
from fractions import Fraction
from functools import cache
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import Resolution, is_closure, resolver_config

import explain_fixtures_context as X
from agent_goldens_context import AGENT
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine
from neptune_context.explain import Caps, IndexedReader
from neptune_context.mcp.server import packet_content
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.model import ClaimItem, ContextPacket
from neptune_context.packets.trails import Change, DiffTrail, WhyTrail
from neptune_context.query import Budget, CivilTime, Instant, Query, Subject, Why
from neptune_context.query.model import Diff
from neptune_context.render.agent import (
    answer_evidence_refs,
    literal_values,
    parse_answer,
    render_answer,
)
from neptune_context.render.citations import CitationError, parse_citations
from neptune_context.sdk import Client

UTC_NS = CivilTime("utc", "unix", Fraction(1, 10**9))
INJECTIONS: list[str] = json.loads((AGENT / "injection-texts.json").read_text(encoding="utf-8"))
UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]")
LEG = Subject("machine", "asset-tag:LEG-9")
AMR = Subject("machine", "asset-tag:AMR-9")


@cache
def reader() -> IndexedReader:
    return IndexedReader(X.document())


def ask(query: Query, **kwargs: Any) -> ContextPacket:
    packet = Client(LocalEngine(reader(), X.Catalog(), **kwargs)).query(query)
    assert answer_problems(query, packet) == ()
    return packet


def why(claim_id: str, *, inferred: bool = True, **kwargs: Any) -> ContextPacket:
    return ask(
        Query(include_inferred=inferred, budget=Budget(items=50), explain=(Why(claim_id),)),
        **kwargs,
    )


def diff(subject: Subject, before: Any, after: Any, *, items: int = 60, **kwargs: Any) -> Any:
    query = Query(
        include_inferred=True, budget=Budget(items=items), explain=(Diff(subject, before, after),)
    )
    return ask(query, **kwargs)


def instant(ticks: int) -> Instant:
    return Instant(UTC_NS, ticks)


def berth() -> list[str]:
    return sorted(c.id for c in X.document().resolution.claims if c.subject == X.USV)


def cases() -> dict[str, ContextPacket]:
    drift = X.find(X.WCAM, "drift")
    yard = X.find(X.TRUCK, "located_at", X.YARD)
    field = X.find(X.UAV, "located_at", X.FIELD)
    candidate = X.find(X.UAV, "same_as_candidate")
    humc = X.find(X.HUM_RUN, "configuration_candidate", X.HUM_C1)
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    return {
        "why-berth-cycle": why(berth()[0]),
        "why-berth-capped": why(berth()[0], explain_caps=Caps(depth=0)),
        "why-berth-tiny-budget": ask(
            Query(include_inferred=True, budget=Budget(items=2), explain=(Why(berth()[0]),))
        ),
        "why-clock-conflict": why(yard.id),
        "why-overridden-inference": why(field.id),
        "why-inferred-root": why(candidate.id),
        "why-candidates": why(humc.id),
        "why-beyond-the-pin": why(drift.id),
        "why-calibration": why(april.id, inferred=False),
        "why-beyond-the-pin-no-catalog": Client(LocalEngine(reader())).query(
            Query(include_inferred=True, budget=Budget(items=50), explain=(Why(drift.id),))
        ),
        "why-two-clauses": ask(
            Query(
                include_inferred=True,
                budget=Budget(items=40),
                explain=(Why(yard.id), Why(april.id)),
            )
        ),
        "diff-world-superseded": diff(LEG, instant(X.APR_1), instant(X.JUN_1)),
        "diff-tx-closed": diff(LEG, 1, 3),
        "diff-tx-superseded": diff(AMR, 1, 2),
        "diff-tx-empty": diff(AMR, 2, 4),
        "diff-world-between": diff(AMR, instant(X.JAN_1), instant(X.APR_1)),
        "diff-tx-between": diff(Subject("machine", "asset-tag:HUM-1"), 1, 3),
        "diff-tx-between-walked": diff(
            Subject("machine", "asset-tag:X9"), 1, 3, explain_caps=Caps(scan=0)
        ),
        "diff-identities": diff(
            Subject("machine", "asset-tag:UAV-8", same_as_depth=1),
            instant(X.FEB_1),
            instant(X.MAR_1),
        ),
        "diff-budget-cut": diff(AMR, 1, 2, items=1),
        "diff-without-history": Client(
            LocalEngine(ReferenceReader(X.document()), explain_caps=Caps(scan=0))
        ).query(
            Query(
                include_inferred=True,
                budget=Budget(items=60),
                explain=(Diff(Subject("machine", "asset-tag:X9"), 1, 3),),
            )
        ),
    }


CASES = cases()


# --- The round trip --------------------------------------------------------------------------


def expected_why(packet: ContextPacket, trail: WhyTrail) -> list[tuple[Any, ...]]:
    carried = {i.claim.id: i for i in packet.items if isinstance(i, ClaimItem)}
    labels = {
        "root": "Root claim",
        "corroborates": "Corroborated by",
        "conflicts": "Conflicts with",
        "alternative": "Alternative reading",
    }
    out = []
    for step in trail.steps:
        item = carried.get(step.claim)
        evidence = tuple(dict.fromkeys(item.evidence_refs())) if item else step.evidence
        out.append(
            (
                labels[str(step.relation)],
                step.depth,
                step.claim,
                step.finding,
                step.repeat,
                item is not None,
                evidence,
                None,
            )
        )
    return out


def expected_diff(packet: ContextPacket, trail: DiffTrail) -> list[tuple[Any, ...]]:
    carried: dict[str, ClaimItem] = {
        i.claim.id: i for i in packet.items if isinstance(i, ClaimItem)
    }

    def line(label: str, depth: int, claim: str, predicate: str) -> tuple[Any, ...]:
        item = carried.get(claim)
        evidence = tuple(dict.fromkeys(item.evidence_refs())) if item else ()
        return (label, depth, claim, None, False, item is not None, evidence, predicate)

    out = []
    for predicate in dict.fromkeys(c.predicate for c in trail.changes):
        group = [c for c in trail.changes if c.predicate == predicate]
        for kind, label in (
            (Change.OPENED, "Opened"),
            (Change.CLOSED, "Closed"),
            (Change.SUPERSEDED, "Superseded"),
            (Change.BETWEEN, "Between"),
        ):
            for change in (c for c in group if c.change is kind):
                if kind in (Change.OPENED, Change.BETWEEN):
                    out += [line(label, 0, claim, predicate) for claim in change.after]
                    continue
                out.append(line(label, 0, change.before[0], predicate))
                for claim in change.after:
                    item = carried.get(claim)
                    narrowed = kind is Change.CLOSED or (
                        item is not None and is_closure(item.claim)
                    )
                    out.append(
                        line("Narrowed to" if narrowed else "Replaced by", 1, claim, predicate)
                    )
    return out


def check_trails(packet: ContextPacket) -> str:
    """Render ``packet``, parse it back and compare every trail line with the packet's trails."""
    text = render_answer(packet)
    parsed = parse_answer(text)
    assert parsed.evidence == answer_evidence_refs(packet) == parse_citations(text)
    assert parsed.evidence[: len(packet.evidence_refs())] == packet.evidence_refs()
    assert [k.item_id for k in parsed.items] == [i.id for i in packet.items]
    expected: list[tuple[Any, ...]] = []
    for trail in packet.trails:
        clause = int(trail.at.rsplit("/", 1)[1])
        rows = (
            expected_why(packet, trail)
            if isinstance(trail, WhyTrail)
            else expected_diff(packet, trail)
        )
        expected += [(trail.kind, clause, *row) for row in rows]
    found = [
        (
            ln.trail,
            ln.clause,
            ln.label,
            ln.depth,
            ln.claim_id,
            ln.finding,
            ln.repeat,
            ln.carried,
            ln.evidence,
            ln.predicate,
        )
        for ln in parsed.trail_lines
    ]
    assert found == expected
    for ln in parsed.trail_lines:
        assert (len(ln.items) == 1) == ln.carried  # a carried claim cites its item, first
        if ln.carried:
            assert ln.items[0].claim_id == ln.claim_id and ln.evidence
        if ln.trail == "why":
            assert ln.evidence  # a why step always cites its bytes
    assert not UNSAFE.search(text.replace("\n", ""))
    return text


@pytest.mark.parametrize("name", CASES)
def test_every_trail_citation_round_trips(name: str) -> None:
    packet = CASES[name]
    text = check_trails(packet)
    assert bool(packet.trails) == ("explain clause" in text)


@pytest.mark.parametrize("name", CASES)
def test_trail_text_is_deterministic_and_survives_json(name: str) -> None:
    packet = CASES[name]
    again = decode(canonical_bytes(packet))
    assert isinstance(again, ContextPacket)
    assert render_answer(again) == render_answer(packet) == render_answer(packet)


def test_a_fresh_engine_gives_the_same_trail_bytes() -> None:
    first = why(berth()[0])
    second = Client(LocalEngine(IndexedReader(X.document()), X.Catalog())).query(
        Query(include_inferred=True, budget=Budget(items=50), explain=(Why(berth()[0]),))
    )
    assert render_answer(first) == render_answer(second)


def test_packets_without_trails_render_exactly_as_before() -> None:
    from sdk_testing_context import STEMS, golden_packet

    for stem in STEMS:
        packet = golden_packet(stem)
        assert packet.trails == ()
        assert parse_answer(render_answer(packet)).trail_lines == ()
        assert answer_evidence_refs(packet) == packet.evidence_refs()


def test_the_golden_trail_packets_round_trip() -> None:
    from explain_goldens_context import TRAILS

    paths = sorted(TRAILS.glob("packet.*.json"))
    assert len(paths) == 4
    for path in paths:
        packet = decode(path.read_bytes())
        assert isinstance(packet, ContextPacket) and packet.trails
        check_trails(packet)


# --- What the sentences say ------------------------------------------------------------------


def lines_of(text: str, start: str) -> list[str]:
    """The lines of the trail section whose heading starts with ``start``."""
    body = text.split("\n")
    first = next(i for i, line in enumerate(body) if line.startswith(start))
    out = []
    for line in body[first + 1 :]:
        if line == "" or not (line.startswith(("- ", "  ", "Predicate "))):
            break
        out.append(line)
    return out


def test_a_why_tree_is_an_indented_outline_citing_claims_and_evidence() -> None:
    text = check_trails(CASES["why-berth-cycle"])
    outline = lines_of(text, "Why Memory holds ")
    assert outline[0].startswith("- Root claim: Stated: ")
    assert any(line.startswith("  - Corroborated by: ") for line in outline)
    assert any(f"is {'already shown above and not expanded again'}" in ln for ln in outline)
    for line in outline:
        assert re.search(r"(\[I[1-9][0-9]*\])+(\[E[1-9][0-9]*\])+$", line)
    assert re.search(r"4 claims, [1-9][0-9]* repeated, 0 gaps listed under Not answered", text)


def test_a_conflict_names_the_resolver_finding() -> None:
    packet = CASES["why-clock-conflict"]
    text = check_trails(packet)
    (finding,) = packet.findings
    (line,) = [ln for ln in text.split("\n") if ln.startswith("  - Conflicts with")]
    assert f"(resolver finding {finding.id})" in line
    assert "clock_mismatch between" in text  # the packet's own finding is still listed


def test_an_inferred_step_is_marked_and_an_uncarried_one_says_what_it_lacks() -> None:
    inferred = check_trails(CASES["why-inferred-root"])
    assert "- Root claim: INFERRED (model " in inferred
    overridden = check_trails(CASES["why-overridden-inference"])
    (line,) = [ln for ln in overridden.split("\n") if ln.startswith("  - Conflicts with")]
    assert "INFERRED (model and confidence are not in this packet): claim:sha256:" in line
    assert "whose content is not in this packet." in line and line.endswith("]")
    assert "[I" not in line  # no item to cite: only its evidence
    # The evidence an uncarried step cites is in the footer too.
    assert re.search(r"\[E[1-9][0-9]*\]$", line)


def test_a_claim_beyond_the_pin_is_named_with_the_evidence_it_cites() -> None:
    packet = CASES["why-beyond-the-pin"]
    (trail,) = packet.trails
    assert isinstance(trail, WhyTrail) and len(trail.steps[0].evidence) == 2
    text = check_trails(packet)
    (root,) = lines_of(text, "Why Memory holds ")
    assert root.startswith("- Root claim: Observed: claim:sha256:") and root.endswith("[E1][E2]")


def test_caps_and_gaps_are_counted_in_the_heading_and_listed() -> None:
    text = check_trails(CASES["why-berth-capped"])
    heading = next(ln for ln in text.split("\n") if ln.startswith("Why Memory holds "))
    assert re.search(r"[1-9][0-9]* gaps? listed under Not answered", heading)
    assert 'at "/explain/0"' in text.split("Not answered:", 1)[1]
    cut = check_trails(CASES["why-berth-tiny-budget"])
    assert "budget cut these claims" in cut
    parsed = parse_answer(cut)
    assert any(not ln.carried for ln in parsed.trail_lines)


def test_a_diff_is_grouped_by_predicate_and_between_is_said_aloud() -> None:
    text = check_trails(CASES["diff-world-between"])
    section = lines_of(text, "What changed about ")
    assert section[0] == "Predicate located_at:"
    group = section[1 : section.index("Predicate recorded_by:")]
    labels = [ln.split(":", 1)[0] for ln in group]
    assert labels == sorted(labels, key=["- Opened", "- Closed", "- Superseded", "- Between"].index)
    (between,) = [ln for ln in group if ln.startswith("- Between: ")]
    assert "; held only between the two points, at neither of them. [I" in between


def test_an_uncarried_between_is_still_said_aloud_and_cites_nothing_it_lacks() -> None:
    text = check_trails(CASES["diff-tx-between"])
    section = lines_of(text, "What changed about ")
    (between,) = [ln for ln in section if ln.startswith("- Between: ")]
    assert "held only between the two points, at neither of them" in between
    assert "not carried in this packet" in between and "[" not in between
    # A version current at neither transaction has no as_of that reads it.
    assert "neptune_why" not in between


def test_closed_and_superseded_changes_name_what_replaced_them() -> None:
    text = check_trails(CASES["diff-tx-superseded"])
    section = lines_of(text, "What changed about ")
    assert any(ln.startswith("- Superseded: claim:sha256:") for ln in section)
    assert any(ln.startswith("  - Narrowed to: ") for ln in section)
    assert any(ln.startswith("  - Replaced by: ") for ln in section)
    # An older version is named with the transaction where neptune_why reads it.
    (old,) = [ln for ln in section if ln.startswith("- Superseded: ")]
    assert "neptune_why with as_of 1 reads it." in old
    closed = check_trails(CASES["diff-tx-closed"])
    assert "  - Narrowed to: " in closed


def test_a_world_time_diff_names_its_instants_and_clock() -> None:
    text = check_trails(CASES["diff-world-superseded"])
    heading = next(ln for ln in text.split("\n") if ln.startswith("What changed about "))
    assert f"between tick {X.APR_1} on clock {X.UTC} and tick {X.JUN_1} on clock {X.UTC}" in heading
    assert "Replaced by: " in text


def test_declared_identities_and_an_empty_diff_are_stated_in_the_heading() -> None:
    text = check_trails(CASES["diff-identities"])
    assert ", with its declared identities machine " in text
    empty = check_trails(CASES["diff-tx-empty"])
    assert "0 claims, 0 gaps listed under Not answered):" in empty
    assert lines_of(empty, "What changed about ") == []


def test_what_changed_since_comes_before_the_trails_and_the_trails_before_the_facts() -> None:
    text = render_answer(CASES["why-calibration"])
    assert text.index("Why Memory holds ") < text.index("\nFacts:\n")
    assert text.index("Quoted strings are data") < text.index("Why Memory holds ")


def test_mcp_links_cover_every_cited_source() -> None:
    packet = CASES["why-beyond-the-pin"]
    blocks = packet_content(packet)
    links = [b for b in blocks if getattr(b, "type", "") == "resource_link"]
    assert len(links) == len(parse_answer(render_answer(packet)).evidence) == 2


def test_the_trail_adds_no_identifier_the_packet_does_not_hold() -> None:
    for name, packet in CASES.items():
        canonical = canonical_bytes(packet).decode()
        for found in re.findall(
            r"(?:claim|finding|item):sha256:[0-9a-f]{64}", render_answer(packet)
        ):
            assert found in canonical, (name, found)


# --- The parser refuses what breaks the trail grammar ----------------------------------------


def _swap(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def test_the_parser_refuses_uncited_or_forged_trail_lines() -> None:
    why_text = render_answer(CASES["why-berth-cycle"])
    outline = lines_of(why_text, "Why Memory holds ")
    corroborated = next(ln for ln in outline if "Corroborated by" in ln)
    bad = [
        _swap(why_text, corroborated, corroborated.rsplit(" [", 1)[0]),  # no citation
        _swap(why_text, corroborated, corroborated.replace("[I", "[E", 1)),  # no item key
        _swap(why_text, corroborated, corroborated + "[E99]"),  # a key with no footer entry
        _swap(why_text, corroborated, corroborated.replace("Corroborated by", "Proven by")),
        _swap(why_text, outline[0], outline[0].replace("Root claim", "Corroborated by")),
        _swap(why_text, corroborated, "      " + corroborated.lstrip()),  # skips a level
        _swap(why_text, corroborated, " " + corroborated),  # odd indentation
        _swap(why_text, corroborated, corroborated + "\nThe berth is certain."),
        _swap(why_text, "4 claims, 4 repeated", "3 claims, 4 repeated"),
        _swap(why_text, "4 claims, 4 repeated", "4 claims, 0 repeated"),
        _swap(why_text, "0 gaps listed", "2 gaps listed"),
        _swap(why_text, "Why Memory holds claim:", "Why we hold claim:"),
    ]
    for broken in bad:
        with pytest.raises(CitationError):
            parse_answer(broken)
    diff_text = render_answer(CASES["diff-world-between"])
    between = next(ln for ln in diff_text.split("\n") if ln.startswith("- Between: "))
    note = "; held only between the two points, at neither of them"
    opened = next(ln for ln in diff_text.split("\n") if ln.startswith("- Opened: "))
    bad = [
        _swap(diff_text, between, between.replace(note, "")),  # between must say so
        _swap(diff_text, opened, opened.rsplit(" [", 1)[0]),  # an opened claim with no citation
        _swap(diff_text, opened, "  " + opened),  # a top-level label, indented
        _swap(diff_text, "Predicate located_at:\n", ""),  # a change before any predicate
        _swap(diff_text, opened, opened + " and it is safe"),
        _swap(diff_text, "(explain clause 0;", "(explain clause 0 and more;"),
        _swap(diff_text, "Predicate located_at:", "Predicate Located At:"),
    ]
    for broken in bad:
        with pytest.raises(CitationError):
            parse_answer(broken)


def _refuses(text: str, old: str, new: str) -> None:
    assert old in text, old
    with pytest.raises(CitationError):
        parse_answer(text.replace(old, new, 1))


def test_a_refinement_line_cannot_cross_predicate_groups_or_follow_the_wrong_parent() -> None:
    text = render_answer(CASES["diff-tx-superseded"])
    lines = text.split("\n")
    child = next(ln for ln in lines if ln.startswith("  - Replaced by: "))
    # Moved under the next Predicate heading, its parent is in another group.
    heads = [i for i, ln in enumerate(lines) if ln.startswith("Predicate ")]
    assert len(heads) == 2
    moved = [ln for ln in lines if ln != child]
    moved.insert(moved.index(lines[heads[1]]) + 1, child)  # directly under the second heading
    with pytest.raises(CitationError):
        parse_answer("\n".join(moved))
    closed = render_answer(CASES["diff-tx-closed"])
    narrowed = next(ln for ln in closed.split("\n") if ln.startswith("  - Narrowed to: "))
    _refuses(closed, narrowed, narrowed.replace("Narrowed to", "Replaced by"))  # under Closed
    assert parse_answer(text).trail_lines  # Superseded may hold either, as rendered


def test_a_note_belongs_to_its_label_and_axis_and_quoted_text_cannot_supply_it() -> None:
    gone = "Memory no longer holds it at the later transaction"
    note = "held only between the two points, at neither of them"
    world = render_answer(CASES["diff-world-between"])
    between = next(ln for ln in world.split("\n") if ln.startswith("- Between: "))
    _refuses(world, between, between.replace("; " + note, ""))
    _refuses(world, between, between.replace(note, gone))
    opened = next(ln for ln in world.split("\n") if ln.startswith("- Opened: "))
    _refuses(world, opened, opened.replace(". [I", "; " + note + ". [I", 1))
    tx = render_answer(CASES["diff-tx-superseded"])
    superseded = next(ln for ln in tx.split("\n") if ln.startswith("- Superseded: "))
    _refuses(tx, superseded, superseded.replace("; neptune_why", "; " + note + "; neptune_why"))
    # "no longer holds" on a Superseded line that has replacements below it, and the wrong axis.
    _refuses(tx, superseded, superseded.replace("; neptune_why", "; " + gone + "; neptune_why"))
    wrong = gone.replace("transaction", "instant").replace(
        "Memory no longer holds it at the later", "it no longer holds at the later"
    )
    assert wrong != gone
    # The tx-diff Between names no as_of (ADR 0011 section 3); the world diff names the snapshot.
    walked = render_answer(CASES["diff-tx-between"])
    named = next(ln for ln in walked.split("\n") if ln.startswith("- Between: "))
    _refuses(
        walked, named, named.replace(note + ".", note + "; neptune_why with as_of 2 reads it.")
    )
    _refuses(tx, "neptune_why with as_of 1 reads it", "neptune_why with as_of 2 reads it")
    # A note written inside a quoted source string is data: it cannot stand in for the real one.
    forged = between.replace("; " + note, "").replace('"asset-tag', '"; ' + note + " asset-tag", 1)
    assert note in forged
    _refuses(world, between, forged)


def test_why_repeats_name_a_claim_shown_earlier_and_have_nothing_beneath() -> None:
    text = render_answer(CASES["why-berth-cycle"])
    lines = text.split("\n")
    repeat = next(ln for ln in lines if "is already shown above" in ln)
    key = re.search(r"item I([1-9][0-9]*) is", repeat).group(1)  # type: ignore[union-attr]
    other = "2" if key != "2" else "4"
    _refuses(text, repeat, repeat.replace(f"item I{key} is", f"item I{other} is"))
    # A repeat that is expanded: a child line under it.
    child = repeat.replace("- ", "  - ", 1)
    padded = repeat + "\n" + child.replace("  - ", "    - ", 1)
    with pytest.raises(CitationError):
        parse_answer(text.replace(repeat, padded, 1))
    # The first appearance of a claim posing as a repeat, and a full line repeating a shown claim.
    full = next(ln for ln in lines if ln.startswith("  - Corroborated by: Stated"))
    number = re.search(r"\[I([0-9]+)\]", full).group(1)  # type: ignore[union-attr]
    claim = f"item I{number} is already shown above and not expanded again. "
    swapped = re.sub(r"Corroborated by: .*?\. (?=\[I)", "Corroborated by: " + claim, full, count=1)
    assert swapped != full
    _refuses(text, full, swapped)
    root = next(ln for ln in lines if ln.startswith("- Root claim: "))
    again = "  - Corroborated by: " + root.split(": ", 1)[1]
    _refuses(text, full, again)


def test_a_diff_heading_states_no_change_count() -> None:
    text = render_answer(CASES["diff-tx-superseded"])
    heading = next(ln for ln in text.split("\n") if ln.startswith("What changed about "))
    assert "changes" not in heading and re.search(r"\d+ claims, \d+ gaps? listed", heading)
    _refuses(text, "claims, ", "claims, 2 changes, ")


def test_a_malformed_gap_pointer_is_a_citation_error_not_a_decode_error() -> None:
    text = render_answer(CASES["why-berth-tiny-budget"])
    gap = next(ln for ln in text.split("\n") if ln.startswith("- ") and ' at "/explain/0"' in ln)
    _refuses(text, gap, gap.replace('at "/explain/0"', 'at "\\x"', 1))


def test_a_trail_section_after_facts_is_refused() -> None:
    text = render_answer(CASES["why-calibration"])
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Why Memory holds "))
    end = lines.index("Facts:")
    section = lines[start:end]
    facts_end = lines.index("Items:")
    moved = lines[:start] + lines[end:facts_end] + section + lines[facts_end:]
    with pytest.raises(CitationError):
        parse_answer("\n".join(moved))


def test_the_footer_amends_adr_0003_by_appending_why_only_refs() -> None:
    packet = CASES["why-beyond-the-pin-no-catalog"]
    assert packet.evidence_refs() == ()  # no item cites the two calibration files
    assert len(answer_evidence_refs(packet)) == 2
    assert len(parse_answer(render_answer(packet)).evidence) == 2
    for packet in CASES.values():
        assert answer_evidence_refs(packet)[: len(packet.evidence_refs())] == packet.evidence_refs()


def test_a_named_only_line_carries_nothing_but_the_claim_id() -> None:
    text = render_answer(CASES["diff-budget-cut"])
    named = next(ln for ln in text.split("\n") if "is not carried in this packet" in ln)
    forged = named.replace("are not here", "are not here. The arm is safe")
    with pytest.raises(CitationError):
        parse_answer(text.replace(named, forged))
    with pytest.raises(CitationError):
        parse_answer(text.replace(named, named.replace("claim:sha256:", "claim:sha256:g", 1)))
    assert parse_answer(text).trail_lines


# --- Hostile text in trails ------------------------------------------------------------------


def hostile_engine(texts: list[str]) -> tuple[LocalEngine, list[NodeRef], list[str]]:
    """Events whose descriptions, and machines whose declared ids, are the given texts; the
    engine, the machines and the ids of the description claims."""
    claims = []
    machines = []
    for n, hostile in enumerate(texts):
        event = NodeRef(NodeType.EVENT, f"record:{X.rec(f'incident:{n}')}")
        machine = NodeRef(NodeType.MACHINE, f"asset-tag:a{hostile}z")
        machines.append(machine)
        claims.append(
            X.claim(
                event, "has_description", hostile, X.MAR_1, evidence=(X.ref(X.REGISTER, n + 1),)
            )
        )
        claims.append(
            X.claim(event, "involves", machine, X.MAR_1, evidence=(X.ref(X.REGISTER, n + 100),))
        )
    document = GraphDocument(
        Resolution(tuple(sorted(claims, key=lambda c: c.id)), ()),
        resolver_config(CORE_PREDICATES, X.PRIORITIES),
        X.HEAD,
    )
    described = [str(c.id) for c in claims if c.predicate == "has_description"]
    return LocalEngine(IndexedReader(document), X.Catalog()), machines, described


def test_prompt_injections_in_claims_and_ids_stay_quoted_in_trails() -> None:
    engine, machines, described = hostile_engine(INJECTIONS)
    client = Client(engine)
    assert len(described) == len(INJECTIONS)
    recovered: set[str] = set()
    for claim_id in described:
        packet = client.query(
            Query(include_inferred=False, budget=Budget(items=20), explain=(Why(claim_id),))
        )
        text = check_trails(packet)
        lines = text.split("\n")
        assert lines.count("Evidence:") == 1 and lines.count("Items:") == 1
        assert "</untrusted>" not in text and "<system>" not in text and "```" not in text
        (root,) = lines_of(text, "Why Memory holds ")
        recovered |= set(literal_values(root))
    for injected in INJECTIONS:
        assert injected in recovered, injected[:40]
    for machine in machines:
        packet = client.query(
            Query(
                include_inferred=False,
                budget=Budget(items=20),
                explain=(Diff(Subject("machine", machine.node_id), 1, 4),),
            )
        )
        text = check_trails(packet)
        heading = next(ln for ln in text.split("\n") if ln.startswith("What changed about "))
        assert machine.node_id in literal_values(heading)


@settings(max_examples=60, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=["Cs"]), min_size=1, max_size=120))
def test_any_text_in_a_trail_round_trips_and_never_breaks_the_grammar(value: str) -> None:
    engine, (machine,), (claim_id,) = hostile_engine([value])
    client = Client(engine)
    packet = client.query(
        Query(include_inferred=False, budget=Budget(items=20), explain=(Why(claim_id),))
    )
    text = check_trails(packet)
    (root,) = lines_of(text, "Why Memory holds ")
    assert value in literal_values(root)
    changes = client.query(
        Query(
            include_inferred=False,
            budget=Budget(items=20),
            explain=(Diff(Subject("machine", machine.node_id), 1, 4),),
        )
    )
    heading = next(
        ln for ln in check_trails(changes).split("\n") if ln.startswith("What changed about ")
    )
    assert machine.node_id in literal_values(heading)
