"""G1 scenario 2: a site renamed (AMR warehouse, marine harbour).

Expected (ADR 0003 §1, ADR 0002 §2-§4, ADR 0007 §2): a site's node is its declared logical id, so
a rename that keeps the identifier keeps the node and every claim id that names it. The name is
a ``one`` claim with a valid interval: the new name supersedes from the rename's date, the old
one keeps its interval as a closure, and ``as_of`` before the rename still shows the old name.
When the identifier *is* the name, the rename is a new thread, so a new node: it joins the old
one only through a declared ground (here a lineage record), never by resemblance.

Verdict: HOLDS. ``has_name`` joined the core vocabulary with MVL-126 in ADR 0007 §2's shape.
"""

from __future__ import annotations

from dataclasses import replace

from memory_g1_harness import (
    JUN_01_2025,
    MAR_02_2026,
    Fixed,
    cite,
    civil,
    draft,
    ledger,
    link,
    reader,
    rid,
    source,
    thread,
)
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind
from neptune_memory.consolidate.base import ClaimDraft, rebuild, run_consolidator
from neptune_memory.consolidate.identity import SAME_AS, IdentityConsolidator, nodes
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.interval import OPEN, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, Cardinality, PredicateSpec
from neptune_memory.schema.supersede import is_closure

# ADR 0007 §2: the shape MVL-126 added to the core vocabulary (a minor graph-schema release).
HAS_NAME = PredicateSpec(
    "has_name",
    1,
    frozenset(NodeType),
    frozenset({ValueType.TEXT}),
    Cardinality.ONE,
    "a declared display name, verbatim; never an identifier",
)
VOCABULARY = CORE_PREDICATES
SITE = NodeRef(NodeType.SITE, "site-register:WH-07")
AMR = NodeRef(NodeType.MACHINE, "serial:AMR-0042")
REGISTER_2025 = cite(source("site-register-2025.csv"))
REGISTER_2026 = cite(source("site-register-2026.csv"))
TX1, TX2 = ledger_tx(1), ledger_tx(2)


def _name(text: str, when: int, register: str) -> ClaimDraft:
    return draft(
        SITE,
        "has_name",
        TypedLiteral(ValueType.TEXT, text),
        civil(when),
        kind=AssertionKind.STATED,
        records=(rid("site_register_row", register),),
        evidence=(cite(source(register)),),
    )


def test_has_name_is_core_in_adr_0007_s_shape() -> None:
    """The GAP pin flipped (MVL-126): the core vocabulary holds ``has_name`` as fixed, at version
    3 since every node type grew ``clock`` (ADR 0011 §1) and ``event`` (ADR 0013 §6), which only
    widens it."""
    spec = CORE_PREDICATES.spec("has_name")
    assert spec == replace(HAS_NAME, version=3)
    assert spec.widens(HAS_NAME)


def test_a_rename_that_keeps_the_identifier_keeps_the_node_and_supersedes_only_the_name() -> None:
    packages = {
        "register-2025": [thread("site-register", "WH-07", "site", REGISTER_2025, record="r25")],
        "register-2026": [thread("site-register", "WH-07", "site", REGISTER_2026, record="r26")],
    }
    assert nodes(ledger(packages)) == (SITE,)  # two register rows, one thread, one node

    located = draft(
        AMR,
        "located_at",
        SITE,
        civil(JUN_01_2025),
        records=(rid("fleet_log", "amr-0042"),),
        evidence=(cite(source("amr-0042.bag")),),
    )
    first = rebuild(
        ledger({"register-2025": packages["register-2025"]}),
        [
            (Fixed("test.fleet", (located,)), {}),
            (Fixed("test.register", (_name("Warehouse 7", JUN_01_2025, "r25"),)), {}),
        ],
        recorded_at=TX1,
        registry=VOCABULARY,
    )
    second = run_consolidator(
        Fixed("test.register", (_name("Northgate Fulfilment", MAR_02_2026, "r26"),)),
        ledger(packages),
        (),
        {},
        recorded_at=TX2,
        registry=VOCABULARY,
    )
    claims = [c for build in first for c in build.claims] + list(second.claims)
    graph = reader(claims, {"test.fleet": 0, "test.register": 1}, registry=VOCABULARY, head=2)

    def names(tx: int) -> list[tuple[str, int, object]]:
        got = graph.claims(SITE, "has_name", ledger_tx(tx)).claims
        return sorted(
            (
                str(c.object.value) if isinstance(c.object, TypedLiteral) else "",
                c.valid_from.ticks,
                c.valid_to if isinstance(c.valid_to, Open) else c.valid_to.ticks,
            )
            for c in got
        )

    assert names(1) == [("Warehouse 7", JUN_01_2025, OPEN)]  # before the rename, as known then
    assert names(2) == [
        ("Northgate Fulfilment", MAR_02_2026, OPEN),
        ("Warehouse 7", JUN_01_2025, MAR_02_2026),  # kept as a closure: it was the name then
    ]
    closure = next(c for c in graph.claims(SITE, "has_name", TX2).claims if is_closure(c))
    assert REGISTER_2026 not in closure.provenance.evidence  # the old name rests on its register
    assert cite(source("r25")) in closure.provenance.evidence
    # The AMR's claim about the site is untouched by the rename: same id at every as_of.
    before = graph.claims(AMR, "located_at", TX1).claims
    after = graph.claims(AMR, "located_at", TX2).claims
    assert [c.id for c in before] == [c.id for c in after] and after[0].object == SITE


def test_a_rename_that_changes_the_identifier_is_a_new_node_joined_only_by_a_declared_ground() -> (
    None
):
    """A harbour register keyed by name: renaming the berth mints a new logical id."""
    old, new = LogicalId("harbour-register", "berth-east"), LogicalId("harbour-register", "berth-3")
    before = {
        "register-1": [thread(old.namespace, old.value, "site", cite(source("harbour-v1.csv")))]
    }
    after = {
        **before,
        "register-2": [thread(new.namespace, new.value, "site", cite(source("harbour-v2.csv")))],
    }
    identity = IdentityConsolidator()
    unlinked = run_consolidator(identity, ledger(after), (), {}, recorded_at=TX1)
    assert len(nodes(ledger(after))) == 2 and unlinked.claims == ()  # no guess from the name
    after["register-2"].append(
        link("configuration_lineage", "berth renamed", old, new, MAR_02_2026)
    )
    linked = run_consolidator(identity, ledger(after), (), {}, recorded_at=TX2)
    (same,) = linked.claims
    assert same.predicate == SAME_AS and same.assertion_kind is AssertionKind.OBSERVED
    assert isinstance(same.object, NodeRef)
    assert {same.subject.node_id, same.object.node_id} == {
        "harbour-register:berth-east",
        "harbour-register:berth-3",
    }
    assert same.valid_from == civil(MAR_02_2026)
