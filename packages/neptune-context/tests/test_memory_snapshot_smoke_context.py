"""Memory regenerates its snapshot as the corpus grows: Context still reads it (MVL-147, ADR 0013).

This is the only test that reads Memory's live ``acceptance_corpus.graph.json.gz``. Goldens read a
frozen copy, so this one compares no bytes and names no ids: it finds the arm and the incident in
the document, then checks the snapshot decodes with the pinned codec and the arm-cell question
comes back cited. A regeneration that keeps the contract passes; one that breaks it fails here and
nowhere else, and the fix is to refreeze (``scripts/freeze_demo_graph.py``) on purpose.
"""

from __future__ import annotations

import retrieve_fixtures_context as F
from neptune_context import pins
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine, read_graph_document
from neptune_context.explain import IndexedReader
from neptune_context.packets.model import ClaimItem
from neptune_context.query import Budget, Direction, GraphClause, Query, Subject
from neptune_context.render.agent import parse_answer, render_answer
from neptune_context.sdk import Client


def test_memorys_live_snapshot_decodes_and_answers_the_arm_cell_question_cited() -> None:
    assert F.MEMORY_SNAPSHOT.name.endswith(".json.gz")
    document = read_graph_document(F.MEMORY_SNAPSHOT)  # the pinned codec, as the server reads it
    assert (
        str(document.to_json()["graph_schema"]).split(".")[0]
        == pins.GRAPH_SCHEMA_VERSION.split(".")[0]
    )
    claims = document.resolution.claims
    arms = {
        c.subject.node_id
        for c in claims
        if c.subject.node_type == "machine" and c.subject.node_id.endswith("ARM-3A")
    }
    incidents = {
        c.subject.node_id
        for c in claims
        if c.predicate == "event_kind" and getattr(c.object, "value", None) == "incident"
    }
    assert arms and incidents  # the arm, and at least one incident, by what the claims say
    query = Query(
        include_inferred=True,
        budget=Budget(items=60, tokens=24_000),
        subjects=frozenset(
            [Subject("machine", i) for i in sorted(arms)]
            + [Subject("event", i) for i in sorted(incidents)]
        ),
        graph=GraphClause(None, 2, Direction.BOTH),
    )
    packet = Client(LocalEngine(IndexedReader(document))).query(query)
    assert answer_problems(query, packet) == ()
    items = [i for i in packet.items if isinstance(i, ClaimItem)]
    assert any(i.claim.subject.node_id in incidents for i in items)
    assert any(i.claim.subject.node_id in arms for i in items)
    text = render_answer(packet)
    parsed = parse_answer(text)
    assert parsed.evidence  # every fact carries its citations, recoverable from the text
    assert {i.claim.id for i in items if i.claim.provenance.evidence}  # claims cite evidence
