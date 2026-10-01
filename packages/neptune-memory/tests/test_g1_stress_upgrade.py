"""G1 scenario 5: a consolidator upgrade that changes a claim's object (marine ROV, AMR).

Expected (ADR 0003 §3, ADR 0005 §6, ADR 0006 §7): v2 is a new lineage recorded at a new
transaction. Its first claim retires every current version of v1, at that transaction, so v1's
object and v2's never contradict each other (no closure, no finding between them); ``as_of``
before the upgrade answers exactly as before; claim ids are siblings, never edited; the generation
is unchanged because priorities and vocabulary are. A rerun of v2 retires nothing and a return to
v1 is refused. Verdict: HOLDS. An upgrade that emits no claim at all retires nothing (ADR 0003's
known gap): ADR 0007 §5 closes it with build withdrawal, GAP owned by MVL-132 (strict ``xfail``).
"""

from __future__ import annotations

import pytest

from memory_g1_harness import (
    JUN_10_2026,
    MAR_02_2026,
    Fixed,
    cite,
    civil,
    draft,
    ledger,
    reader,
    rid,
    source,
)
from neptune.identity import canonical_json
from neptune_memory.consolidate.base import ClaimDraft, rebuild
from neptune_memory.schema.claim import Claim, TypedLiteral, ValueType
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.supersede import LineageError, is_closure, lineage_of, resolve

ROV = NodeRef(NodeType.MACHINE, "hull-number:ROV-SEAEYE-11")
INSPECTION = rid("inspection_report", "rov-11-may")
EVIDENCE = (cite(source("inspection-2026-05.pdf")),)
ID = "test.inspection"
PRIORITIES = {ID: 0}


def _state(text: str, *, start: int = MAR_02_2026) -> ClaimDraft:
    return draft(
        ROV,
        "maintenance_state",
        TypedLiteral(ValueType.TEXT, text),
        civil(start),
        records=(INSPECTION,),
        evidence=EVIDENCE,
    )


def _build(version: str, tx: int, *drafts: ClaimDraft) -> list[Claim]:
    (build,) = rebuild(
        ledger({"pkg": []}),
        [(Fixed(ID, tuple(drafts), version=version), {})],
        recorded_at=ledger_tx(tx),
    )
    return list(build.claims)


def test_an_upgrade_that_changes_the_object_replaces_the_old_lineage_at_its_transaction() -> None:
    v1 = _build("1", 1, _state("thruster fault"))
    v2 = _build("2", 2, _state("thruster 3 (port vertical) fault"))
    assert v1[0].id != v2[0].id  # siblings: the same assertion from a new lineage, a new id
    graph = reader(v1 + v2, PRIORITIES, head=2)
    (before,) = graph.claims(ROV, "maintenance_state", ledger_tx(1)).claims
    (after,) = graph.claims(ROV, "maintenance_state", ledger_tx(2)).claims
    assert before.object == TypedLiteral(ValueType.TEXT, "thruster fault")
    assert after.object == TypedLiteral(ValueType.TEXT, "thruster 3 (port vertical) fault")
    assert lineage_of(after)[1] == "2"
    history = resolve(v1 + v2, CORE_PREDICATES, PRIORITIES)  # nothing deleted, cut or closed
    old = next(c for c in history.claims if c.id == v1[0].id)
    assert old.superseded_at == 2 and old.valid == v1[0].valid
    assert not any(is_closure(c) for c in history.claims) and history.findings == ()
    assert after.supersedes == ()  # retired by lineage, not beaten on the overlap


def test_an_upgrade_that_drops_one_fact_still_retires_it_and_reruns_change_nothing() -> None:
    v1 = _build(
        "1",
        1,
        _state("thruster fault"),
        draft(
            ROV,
            "located_at",
            NodeRef(NodeType.SITE, "harbour:berth-3"),
            civil(MAR_02_2026),
            records=(INSPECTION,),
            evidence=EVIDENCE,
        ),
    )
    v2 = _build("2", 2, _state("thruster 3 fault"))  # v2 no longer derives the location
    rerun = _build("2", 3, _state("thruster 3 fault"))
    assert canonical_json.dumps([c.content_json() for c in rerun]) == canonical_json.dumps(
        [c.content_json() for c in v2]
    )
    graph = reader(v1 + v2 + rerun, PRIORITIES, head=3)
    assert graph.claims(ROV, "located_at", ledger_tx(1)).claims != ()
    assert graph.claims(ROV, "located_at", ledger_tx(2)).claims == ()  # retired with v1
    (current,) = graph.claims(ROV, "maintenance_state", ledger_tx(3)).claims
    assert current.recorded_at == 2  # the rerun kept the id and the first recording


def test_a_rollback_to_v1_is_a_new_version_never_a_reused_lineage() -> None:
    claims = _build("1", 1, _state("a")) + _build("2", 2, _state("b")) + _build("1", 3, _state("a"))
    with pytest.raises(LineageError) as caught:
        reader(claims, PRIORITIES, head=3)
    assert caught.value.code == "lineage_reuse"


@pytest.mark.xfail(strict=True, reason="GAP MVL-132: no build withdrawal yet (ADR 0007 §5)")
def test_an_upgrade_that_emits_nothing_still_retires_the_old_lineage() -> None:
    v1 = _build("1", 1, _state("thruster fault", start=JUN_10_2026))
    v2: list[Claim] = _build("2", 2)  # the new parser finds nothing to claim
    graph = reader(v1 + v2, PRIORITIES, head=2)
    assert graph.claims(ROV, "maintenance_state", ledger_tx(2)).claims == ()
