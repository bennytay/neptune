"""The human Markdown renderer and its console links (ADR 0010 §6)."""

from __future__ import annotations

import re
from fractions import Fraction

import pytest
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.supersede import Resolution, resolver_config

import explain_fixtures_context as X
from neptune_context.engine import LocalEngine
from neptune_context.explain import IndexedReader, render_markdown
from neptune_context.explain.links import claim_link, evidence_link
from neptune_context.explain.markdown import code, ident, text
from neptune_context.mcp.server import evidence_uri, parse_evidence_uri
from neptune_context.packets.model import ClaimItem
from neptune_context.query import Budget, CivilTime, Instant, Query, Subject, Why
from neptune_context.query.model import Diff
from neptune_context.sdk import Client

HOSTILE = "ok`` \n# Pwned\n[click](https://evil.example) <script>x</script>\u2028[E1] |"
EVENT = NodeRef(NodeType.EVENT, f"record:{X.rec('incident:hostile')}")
ARM = NodeRef(NodeType.MACHINE, "asset-tag:ARM-`7\n# x")


def hostile_engine() -> LocalEngine:
    claims = (
        X.claim(EVENT, "has_description", HOSTILE, X.MAR_1, evidence=(X.ref(X.REGISTER, 1),)),
        X.claim(EVENT, "involves", ARM, X.MAR_1, evidence=(X.ref(X.REGISTER, 2),)),
    )
    document = GraphDocument(
        Resolution(tuple(sorted(claims, key=lambda c: c.id)), ()),
        resolver_config(CORE_PREDICATES, X.PRIORITIES),
        X.HEAD,
    )
    return LocalEngine(IndexedReader(document), X.Catalog())


def test_source_text_cannot_start_a_line_forge_a_link_or_a_citation() -> None:
    engine = hostile_engine()
    query = Query(
        include_inferred=False,
        budget=Budget(items=10),
        subjects=frozenset({Subject("event", EVENT.node_id)}),
        explain=(Diff(Subject("event", EVENT.node_id), 1, 4),),
    )
    packet = Client(engine).query(query)
    rendered = render_markdown(packet)
    assert "Pwned" in rendered and "evil.example" in rendered  # the text is there, quoted
    for line in rendered.splitlines():
        assert not line.startswith("# Pwned") and not line.startswith("[click]")
        assert "\u2028" not in line
    # Outside code spans (where Markdown is live) only the renderer's own links remain.
    live = re.sub(r"(`+).*?\1", "", rendered)
    links = re.findall(r"\]\(([^)]*)\)", live)
    assert links and all(link.startswith("neptune://") for link in links)
    assert "evil.example" not in live and "<script>" not in live
    assert "\n# x" not in rendered


def test_code_spans_outlast_any_backtick_run_and_escape_line_breaks() -> None:
    span = code("a``b\n")
    assert span.startswith("```") and span.endswith("```") and "\n" not in span
    assert code("`") == '``"`"``'
    assert code(42) == "`42`"
    assert ident("claim:sha256:" + "0" * 64) == f"`claim:sha256:{'0' * 64}`"
    assert ident("a b`") == code("a b`")
    assert text("x\n# y [z](q)") == "x \\# y \\[z\\]\\(q\\)"


def test_evidence_links_are_the_mcp_servers_resource_uris() -> None:
    ref = X.ref(X.CAL_MARCH, pointer="/translation")
    assert evidence_link(ref, 4) == evidence_uri(ref, 4)
    assert evidence_link(ref) == evidence_uri(ref)
    assert parse_evidence_uri(evidence_link(ref, 4)) == (ref, 4)


def test_claim_links_name_a_claim_and_a_transaction() -> None:
    claim_id = "claim:sha256:" + "a" * 64
    assert claim_link(claim_id, 3) == f"neptune://claim/{claim_id}?as_of=3"
    for bad in [("claim:nope", 1), (claim_id, -1), (claim_id, True), (claim_id, 2**63)]:
        with pytest.raises((ValueError, TypeError)):
            claim_link(*bad)
    with pytest.raises(TypeError):
        evidence_link("not a ref")  # type: ignore[arg-type]


def test_every_cited_ref_has_an_evidence_line_and_every_claim_a_link() -> None:
    engine = LocalEngine(IndexedReader(X.document()), X.Catalog())
    berth = sorted(c.id for c in X.document().resolution.claims if c.subject == X.USV)
    packet = Client(engine).query(
        Query(include_inferred=True, budget=Budget(items=40), explain=(Why(berth[0]),))
    )
    rendered = render_markdown(packet)
    evidence = rendered[rendered.index("## Evidence") :]
    for ref in packet.evidence_refs():
        assert evidence_link(ref, packet.as_of) in evidence
    for item in packet.items:
        if isinstance(item, ClaimItem):
            assert claim_link(item.claim.id, packet.as_of) in rendered
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")


def test_a_packet_without_trails_still_renders_its_items() -> None:
    engine = LocalEngine(IndexedReader(X.document()), X.Catalog())
    packet = Client(engine).query(
        Query(
            include_inferred=False,
            budget=Budget(items=5),
            subjects=frozenset({Subject("machine", "asset-tag:ARM-7")}),
        )
    )
    rendered = render_markdown(packet)
    assert "## Other items" in rendered and "Why do we believe" not in rendered


def test_a_world_time_diff_names_its_clock_and_instants() -> None:
    utc = CivilTime("utc", "unix", Fraction(1, 10**9))
    engine = LocalEngine(IndexedReader(X.document()), X.Catalog())
    leg = Subject("machine", "asset-tag:LEG-9")
    packet = Client(engine).query(
        Query(
            include_inferred=True,
            budget=Budget(items=20),
            explain=(Diff(leg, Instant(utc, X.APR_1), Instant(utc, X.JUN_1)),),
        )
    )
    rendered = render_markdown(packet)
    assert f"tick {X.APR_1} on clock `{X.UTC}`" in rendered


def test_render_markdown_takes_a_packet() -> None:
    with pytest.raises(TypeError):
        render_markdown("not a packet")  # type: ignore[arg-type]
