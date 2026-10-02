"""G1 scenario 1: robots with identical URDFs and no declared ids (humanoids, arms).

Expected (ADR 0003 §1): one node per Ledger thread, keyed by the thread's logical id, never by
content; no ``same_as`` without a declared ground; a shared description is at most a pairwise
``same_as_candidate`` across namespaces, never inside one; nothing is merged, and readers see
separate nodes. Verdict: HOLDS.
"""

from __future__ import annotations

import random

from memory_g1_harness import cite, ledger, reader, source, thread
from neptune.identity import canonical_json
from neptune.model.knowledge import Known, NotCovered
from neptune_memory.consolidate.base import Consolidation, rebuild, run_consolidator
from neptune_memory.consolidate.identity import (
    SAME_AS,
    SAME_AS_CANDIDATE,
    IdentityConsolidator,
    nodes,
    same_as_candidates,
)
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

# One humanoid description, byte for byte, shipped with every unit of a fleet.
URDF = cite(source("h1_description/urdf/h1.urdf"))
TX = ledger_tx(4)


def _humanoid_fleet() -> dict[str, list[dict[str, object]]]:
    """Three humanoids whose run logs declare no serial: the Ledger keys each thread by the log
    it came from, one namespace for all three. All three cite the identical URDF."""
    return {
        f"pkg-run-{n}": [
            thread("run-log", f"h1-run-{n}", "machine", URDF, cite(source(f"log {n}")))
        ]
        for n in (1, 2, 3)
    }


def _identity(packages: dict[str, list[dict[str, object]]]) -> Consolidation:
    return run_consolidator(IdentityConsolidator(), ledger(packages), (), {}, recorded_at=TX)


def test_identical_urdfs_in_one_namespace_stay_three_robots_with_no_identity_claim() -> None:
    packages = _humanoid_fleet()
    assert len(nodes(ledger(packages))) == 3  # one node per thread, never one per URDF
    out = _identity(packages)
    assert out.claims == () and out.findings == ()  # no same_as, and no candidate either:
    # values of one namespace are declared distinct, so a fleet sharing a URDF costs nothing.


def test_a_thread_in_another_namespace_is_ambiguous_between_all_three_never_merged() -> None:
    """The fleet's maintenance controller logs one unit by its controller slot, citing the same
    URDF: it could be any of the three. Each pair is a candidate both ways; nothing is same_as."""
    packages = _humanoid_fleet()
    packages["pkg-maint"] = [thread("controller", "slot-b", "machine", URDF)]
    out = _identity(packages)
    assert {c.predicate for c in out.claims} == {SAME_AS_CANDIDATE}
    assert len(out.claims) == 6  # three pairs, one claim each way
    assert all(c.provenance.evidence == (URDF,) for c in out.claims)  # the shared file, cited
    slot = NodeRef(NodeType.MACHINE, "controller:slot-b")
    readings = same_as_candidates(out.claims, slot)
    assert readings[0] == slot and len(readings) == 4  # itself (distinct) plus three candidates

    graph = reader(out.claims, {"memory.identity": 0}, head=4)
    for n in (1, 2, 3):
        view = graph.node(NodeRef(NodeType.MACHINE, f"run-log:h1-run-{n}"), TX)
        assert isinstance(view, Known)
        assert view.value.node.node_id == f"run-log:h1-run-{n}"  # still its own node
        assert all(
            c.predicate == SAME_AS_CANDIDATE for c in (*view.value.claims, *view.value.incoming)
        )
    # A traversal reaches the other units only through candidate edges, and says so.
    hops = graph.neighbours(slot, 1, TX)
    assert {n.node.node_id for n in hops.neighbours} == {f"run-log:h1-run-{n}" for n in (1, 2, 3)}
    assert {c.predicate for n in hops.neighbours for c in n.via} == {SAME_AS_CANDIDATE}
    assert not any(c.predicate == SAME_AS for c in out.claims)


def test_two_arms_with_one_description_and_nothing_else_are_not_covered_by_identity() -> None:
    """Two arm cells, the same UR5e description, two vendor namespaces: a candidate pair; the
    graph says nothing more about either, and a node it never names is NotCovered."""
    urdf = cite(source("ur5e.urdf"))
    packages = {
        "cell-a": [thread("ur-controller", "cell-a", "machine", urdf)],
        "cell-b": [thread("mes", "line-4/station-2", "machine", urdf)],
    }
    out = _identity(packages)
    assert sorted(c.subject.node_id for c in out.claims) == [
        "mes:line-4/station-2",
        "ur-controller:cell-a",
    ]
    graph = reader(out.claims, {"memory.identity": 0}, head=4)
    assert isinstance(graph.node(NodeRef(NodeType.MACHINE, "ur-controller:cell-c"), TX), NotCovered)


def test_identity_output_is_independent_of_package_order() -> None:
    packages = _humanoid_fleet()
    packages["pkg-maint"] = [thread("controller", "slot-b", "machine", URDF)]
    expected = canonical_json.dumps([c.to_json() for c in _identity(packages).claims])
    names = list(packages)
    for seed in range(5):
        random.Random(seed).shuffle(names)
        shuffled = {name: packages[name] for name in names}
        built = rebuild(ledger(shuffled), [(IdentityConsolidator(), {})], recorded_at=TX)
        assert canonical_json.dumps([c.to_json() for c in built[0].claims]) == expected
