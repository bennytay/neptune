"""G1 scenario 6: an operator assertion later retracted (AMR, humanoid).

Expected (ADR 0002 §4, ADR 0005 §1, ADR 0007 §5): nothing is deleted. A retraction is a new Ledger
record, so a new claim or a closure at its transaction; ``as_of`` before it still shows what the
operator said, and the history keeps both.

- A ``one`` fact corrected by the operator (the AMR was never in bay 4): the correction is a new
  stated claim over the same interval, which wins on arrival and supersedes the mistake. HOLDS.
- A retraction with no replacement, and any retraction of a ``many`` fact such as ``same_as``,
  has no mechanism: ``many`` claims never contradict, and a consolidator that stops emitting a
  claim does not withdraw it. ADR 0003 §1.4 said "undoing an identity is superseding a claim";
  the resolver cannot do that. GAP: ADR 0007 §5 defines build withdrawal (MVL-132) and the
  ``operator_retraction`` record (MVL-126). Pinned by the strict ``xfail`` below.

Verdict: GAP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from memory_g1_harness import (
    JUN_10_2026,
    MAR_02_2026,
    Fixed,
    build,
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
from neptune.model.knowledge import AssertionKind, NotCovered
from neptune_memory.consolidate.base import Consolidation, rebuild, run_consolidator
from neptune_memory.consolidate.identity import SAME_AS, IdentityConsolidator
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

AMR = NodeRef(NodeType.MACHINE, "serial:MiR-250-0117")
BAY_4 = NodeRef(NodeType.ZONE, "zone-register:bay-4")
BAY_2 = NodeRef(NodeType.ZONE, "zone-register:bay-2")
STATED = AssertionKind.STATED


def _operator(tx: int, zone: NodeRef, note: str) -> list[Claim]:
    claim = draft(
        AMR,
        "located_at",
        zone,
        civil(JUN_10_2026),
        kind=STATED,
        records=(rid("operator_log", note),),
        evidence=(cite(source(note)),),
    )
    (build,) = rebuild(
        ledger({"ops": []}),
        [(Fixed("test.operator", (claim,)), {})],
        recorded_at=ledger_tx(tx),
    )
    return list(build.claims)


def test_a_corrected_one_fact_is_superseded_by_the_correction_and_kept_in_history() -> None:
    mistake = _operator(1, BAY_4, "shift log 10 Jun: AMR 117 parked in bay 4")
    correction = _operator(2, BAY_2, "shift log 11 Jun: correction, AMR 117 was in bay 2")
    graph = reader(mistake + correction, {"test.operator": 0}, head=2)
    (said,) = graph.claims(AMR, "located_at", ledger_tx(1)).claims
    (now,) = graph.claims(AMR, "located_at", ledger_tx(2)).claims
    assert said.object == BAY_4 and now.object == BAY_2  # as known then, and as known now
    assert now.supersedes == (mistake[0].id,)  # the correction names what it replaced
    assert isinstance(graph.node(BAY_4, ledger_tx(2)), NotCovered)  # no longer claimed


HUMANOID_A = LogicalId("fleet-register", "apollo-03")
HUMANOID_B = LogicalId("vendor-log", "unit-7f2c")


def _identity_packages(retracted: bool) -> dict[str, list[dict[str, object]]]:
    assertion = link(
        "operator_assertion",
        "apollo-03 is unit 7f2c",
        HUMANOID_A,
        HUMANOID_B,
        MAR_02_2026,
        predicate=SAME_AS,
        operator="badge:4411",
    )
    packages = {
        "register": [
            thread("fleet-register", "apollo-03", "machine", cite(source("register.csv")))
        ],
        "vendor": [thread("vendor-log", "unit-7f2c", "machine", cite(source("vendor.log")))],
        "ops-1": [assertion],
    }
    if retracted:  # the operator withdraws it: a new record, the old one untouched (ADR 0007 §5)
        packages["ops-2"] = [
            {
                "kind": "operator_retraction",
                "id": rid("operator_retraction", "x"),
                "retracts": assertion["id"],
                "operator": "badge:4411",
                "evidence": [cite(source("ops-2.log")).to_json()],
            }
        ]
    return packages


def _run(tx: int, retracted: bool) -> Consolidation:
    return run_consolidator(
        IdentityConsolidator(),
        ledger(_identity_packages(retracted)),
        (),
        {},
        recorded_at=ledger_tx(tx),
    )


def _identity(tx: int, retracted: bool) -> list[Claim]:
    return list(_run(tx, retracted).claims)


def test_an_operator_same_as_is_a_stated_edge_with_its_record() -> None:
    (same,) = _identity(1, retracted=False)
    assert same.predicate == SAME_AS and same.assertion_kind is STATED
    assert len(same.provenance.records) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="GAP MVL-126 + MVL-132: no retraction or withdrawal yet (ADR 0007 §5)",
)
def test_a_retracted_same_as_stops_being_current_and_stays_in_history() -> None:
    claims = _identity(1, retracted=False) + _identity(2, retracted=True)
    builds = [build(_run(1, retracted=False), 1), build(_run(2, retracted=True), 2)]
    graph = reader(claims, {"memory.identity": 0}, head=2, builds=builds)
    a = NodeRef(NodeType.MACHINE, "fleet-register:apollo-03")
    assert graph.claims(a, SAME_AS, ledger_tx(1)).claims != ()
    assert graph.claims(a, SAME_AS, ledger_tx(2)).claims == ()
