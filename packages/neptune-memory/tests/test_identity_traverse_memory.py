"""The archetype identity graph: four platforms of different embodiments, consolidated over two
Ledger transactions, resolved, and read through ``MemoryReader`` (ADR 0008 §5).

- aerial: a fleet register co-declares a drone's asset tag and PX4 ``sys_uuid`` (tx 1);
- legged: an operator joins a quadruped's bag namespace to its serial (tx 2);
- manipulator: an arm cell's calibration is re-commissioned twice, a lineage chain v1-v2-v3;
- mobile: an AMR's bag and a register row cite one register file: only candidates.

Beside identity, the drone's run is said to be recorded by the log (on its boot clock) and by the
asset tag (an operator, on civil time): a ``clock_mismatch``, so the suite's findings checks have
something to check.

The contract suite's ``node`` and ``neighbours`` checks hold over it, and ``same_as_closure``
follows ``same_as`` to a depth, never ``same_as_candidate`` unless asked, and never merges.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from memory_g1_harness import Fixed, cite, civil, draft, own_clock, rid, source
from memory_identity_records import Record, assertion, at, ledger, lineage, link, thread
from neptune.identity import canonical_json
from neptune.model.assertion import AssertionType
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind, Known
from neptune_memory.consolidate.base import rebuild
from neptune_memory.consolidate.identity import IdentityConsolidator, node_ref
from neptune_memory.contract.suite import check_neighbours, check_nodes
from neptune_memory.schema.codec import GraphDocument, graph_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, SAME_AS, SAME_AS_CANDIDATE
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import resolve, resolver_config
from neptune_memory.schema.traverse import DEFAULT_SAME_AS_DEPTH, same_as_closure

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidator
    from neptune_memory.schema.claim import Claim

MACHINE, CONFIG = NodeType.MACHINE, NodeType.CONFIGURATION
DRONE_TAG = LogicalId("asset-tag", "UAV-0042")
DRONE_LOG = LogicalId("px4.sys_uuid", "000200000000343233345117003a0027")
SPOT_BAG = LogicalId("ros2.namespace", "/spot1")
SPOT_SERIAL = LogicalId("serial", "SPOT-1234")
CELL = [LogicalId("cell.config", f"left-arm/v{n}") for n in (1, 2, 3)]
AMR_BAG = LogicalId("ros1.hostname", "amr-12")
AMR_ROW = LogicalId("site.register_row", "W3/AMR-12")

LEDGER: dict[int, dict[str, list[Record]]] = {
    1: {
        "drone": [thread(DRONE_LOG, "flight.ulg")],
        "fleet": [thread(DRONE_TAG, "fleet.csv"), link("fleet.csv row 1", DRONE_TAG, DRONE_LOG)],
        "cell": [
            *(thread(c, "session.mcap", f"hand_eye {c.value}", node_type=CONFIG) for c in CELL),
            lineage("recommissioned v2", CELL[0], CELL[1]),
            lineage("recommissioned v3", CELL[1], CELL[2]),
        ],
        "amr": [thread(AMR_BAG, "drive.bag", "sites.csv"), thread(AMR_ROW, "sites.csv")],
    },
    2: {
        "quadruped": [thread(SPOT_BAG, "walk_0.mcap"), thread(SPOT_SERIAL, "asset register")],
        "ops": [
            assertion(
                "ASR-1", AssertionType.SAME_IDENTITY, (SPOT_BAG, SPOT_SERIAL), authored_at=at(7)
            )
        ],
    },
}


DRONE_RUN = NodeRef(NodeType.RUN, "record:flight.ulg")
BOOT = own_clock("drone boot")


def _recorders(tx: int) -> list[Fixed]:
    """The drone run's recorder: the log names its sys_uuid (tx 1), an operator its tag (tx 2)."""
    log = Fixed(
        "test.log",
        (
            draft(
                DRONE_RUN,
                "recorded_by",
                node_ref(MACHINE, DRONE_LOG),
                at(12_000_000, BOOT),
                records=(rid("run", "flight.ulg"),),
                evidence=(cite(source("flight.ulg")),),
            ),
        ),
    )
    operator = Fixed(
        "test.operator",
        (
            draft(
                DRONE_RUN,
                "recorded_by",
                node_ref(MACHINE, DRONE_TAG),
                civil(1_790_762_400),
                kind=AssertionKind.STATED,
                records=(rid("operator_log", "line 2"),),
                evidence=(cite(source("operator log")),),
            ),
        ),
    )
    return [log] if tx == 1 else [log, operator]


def archetype_graph() -> GraphDocument:
    """Each transaction rebuilds the plan over the cumulative Ledger; then one resolve."""
    landed: dict[str, list[Record]] = {}
    claims: list[Claim] = []
    for tx, packages in sorted(LEDGER.items()):
        landed.update(packages)
        consolidators: list[Consolidator] = [*_recorders(tx), IdentityConsolidator()]
        plan: list[tuple[Consolidator, Mapping[str, JsonValue]]] = [(c, {}) for c in consolidators]
        for result in rebuild(ledger(landed), plan, recorded_at=ledger_tx(tx)):
            assert result.findings == ()
            claims.extend(result.claims)
    priorities = {"test.log": 0, "test.operator": 1, "memory.identity": 2}
    resolution = resolve(claims, CORE_PREDICATES, priorities)
    return GraphDocument(resolution, resolver_config(CORE_PREDICATES, priorities), ledger_tx(2))


GRAPH = archetype_graph()
READER = ReferenceReader(GRAPH)
HEAD = ledger_tx(2)


@pytest.mark.parametrize("check", [check_nodes, check_neighbours], ids=lambda c: c.__name__)
def test_node_and_neighbours_contract_checks_hold_over_the_archetype_graph(check: object) -> None:
    check(ReferenceReader, GRAPH)  # type: ignore[operator]


def test_the_archetype_graph_rebuilds_byte_for_byte_and_round_trips() -> None:
    assert canonical_json.dumps(archetype_graph().to_json()) == canonical_json.dumps(
        GRAPH.to_json()
    )
    assert graph_from_json(GRAPH.to_json()) == GRAPH


def test_the_graph_holds_the_finding_the_suite_checks() -> None:
    assert [f.code for f in GRAPH.resolution.findings] == ["clock_mismatch"]


def test_every_platform_keeps_its_own_nodes_never_merged() -> None:
    for node in (DRONE_TAG, DRONE_LOG, SPOT_BAG, SPOT_SERIAL, AMR_BAG, AMR_ROW):
        view = READER.node(node_ref(MACHINE, node), HEAD)
        assert isinstance(view, Known) and view.value.node == node_ref(MACHINE, node)


def _ids(closure: tuple[object, ...]) -> list[tuple[str, int]]:
    return [(n.node.node_id, n.depth) for n in closure]  # type: ignore[attr-defined]


def test_closure_follows_same_as_both_ways_with_its_evidence() -> None:
    tag, log = node_ref(MACHINE, DRONE_TAG), node_ref(MACHINE, DRONE_LOG)
    (from_tag,) = same_as_closure(READER, tag, HEAD)
    (from_log,) = same_as_closure(READER, log, HEAD)  # the edge is stored once, walked both ways
    assert (from_tag.node, from_log.node) == (log, tag)
    assert from_tag.via == from_log.via and from_tag.via[0].predicate == SAME_AS


def test_closure_depth_bounds_a_lineage_chain() -> None:
    v1 = node_ref(CONFIG, CELL[0])
    assert _ids(same_as_closure(READER, v1, HEAD, depth=1)) == [("cell.config:left-arm/v2", 1)]
    full = same_as_closure(READER, v1, HEAD)
    assert _ids(full) == [("cell.config:left-arm/v2", 1), ("cell.config:left-arm/v3", 2)]
    assert len(full[1].via) == 2 and full[1].via[0] == full[0].via[0]
    assert same_as_closure(READER, v1, HEAD, depth=0) == ()
    assert DEFAULT_SAME_AS_DEPTH >= 2


def test_closure_never_follows_candidates_unless_asked() -> None:
    bag = node_ref(MACHINE, AMR_BAG)
    assert same_as_closure(READER, bag, HEAD) == ()
    (candidate,) = same_as_closure(READER, bag, HEAD, include_candidates=True)
    assert candidate.node == node_ref(MACHINE, AMR_ROW)
    assert candidate.via[0].predicate == SAME_AS_CANDIDATE  # says it is only a candidate


def test_closure_is_a_snapshot_at_as_of() -> None:
    spot = node_ref(MACHINE, SPOT_BAG)
    assert same_as_closure(READER, spot, ledger_tx(1)) == ()  # the operator spoke at tx 2
    assert _ids(same_as_closure(READER, spot, HEAD)) == [("serial:SPOT-1234", 1)]


def test_closure_of_an_unnamed_node_is_empty_and_bad_depths_are_refused() -> None:
    assert same_as_closure(READER, NodeRef(MACHINE, "serial:nobody"), HEAD) == ()
    for depth in (-1, True, 1.5):
        with pytest.raises(ValueError, match="depth"):
            same_as_closure(READER, node_ref(MACHINE, DRONE_TAG), HEAD, depth=depth)  # type: ignore[arg-type]
