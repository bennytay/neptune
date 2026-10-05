"""Identity policy (ADR 0003 §1, ADR 0008) on four worked platforms of different embodiments.

Each platform exercises one ground or the candidate path, with the record shapes the compiler
writes (``memory_identity_records`` builds them with the compiler's own types):

- aerial: a fleet register row co-declaring a drone's asset tag and its PX4 ``sys_uuid``
  (``IdentityLink``, ``co_declared``), and a log and a register that each declare one serial
  (``shared_identifier``);
- legged: an operator's ``same_identity`` assertion joining a quadruped's bag namespace to its
  serial (root ADR 0062);
- manipulator: configuration lineage between two hand-eye calibrations of one arm cell;
- mobile: an AMR whose bag and site-register row cite one register file: only a candidate.
"""

from collections.abc import Mapping, Sequence

import pytest

from memory_identity_records import (
    OBSERVED,
    STATED,
    Record,
    assertion,
    at,
    cite,
    ledger,
    lineage,
    link,
    source,
    thread,
    window,
)
from neptune.identity import canonical_json
from neptune.model.assertion import AssertionType
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind
from neptune_memory.consolidate.base import Consolidation, rebuild, run_consolidator
from neptune_memory.consolidate.identity import (
    IDENTITY_PREDICATES,
    SAME_AS,
    SAME_AS_CANDIDATE,
    IdentityConsolidator,
    node_ref,
    nodes,
    same_as_candidates,
)
from neptune_memory.schema.claim import Claim
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

TX = ledger_tx(3)
MACHINE, CONFIG = NodeType.MACHINE, NodeType.CONFIGURATION


def _run(
    packages: Mapping[str, Sequence[Record]], config: Mapping[str, object] = {}
) -> Consolidation:
    return run_consolidator(
        IdentityConsolidator(),
        ledger(packages),
        (),
        dict(config),  # type: ignore[arg-type]
        recorded_at=TX,
        registry=IDENTITY_PREDICATES,
    )


def _of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def _codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


# --- aerial: the fleet register names the drone by asset tag and sys_uuid ---------------------

DRONE_LOG = LogicalId("px4.sys_uuid", "000200000000343233345117003a0027")
DRONE_TAG = LogicalId("asset-tag", "UAV-0042")
REGISTER_ROW = "fleet.csv row 1"
DRONE_LINK = link(REGISTER_ROW, DRONE_TAG, DRONE_LOG)


def _drone() -> dict[str, list[Record]]:
    return {
        "pkg-flight": [thread(DRONE_LOG, "flight.ulg")],
        "pkg-fleet": [thread(DRONE_TAG, "fleet.csv"), DRONE_LINK],
    }


def test_drone_co_declared_identity_link_is_same_as_citing_the_register_row() -> None:
    result = _run(_drone())
    (claim,) = _of(result, SAME_AS)
    assert claim.subject == node_ref(MACHINE, DRONE_TAG)  # lower logical id in canonical order
    assert claim.object == node_ref(MACHINE, DRONE_LOG)
    assert claim.assertion_kind is STATED  # a register row is what someone stated
    assert claim.provenance.records == (DRONE_LINK["id"],)
    # co_declared: the link's own evidence is empty; the row and the right side's cell cite it.
    assert {ref.source for ref in claim.provenance.evidence} == {source(REGISTER_ROW)}
    assert len(claim.provenance.evidence) == 2
    assert claim.provenance.consolidator_id == "memory.identity"
    assert claim.provenance.consolidator_version == "2"
    assert (claim.valid_from, claim.valid_to) == (at(100), OPEN)  # no window: subject's thread
    assert not _of(result, SAME_AS_CANDIDATE) and not result.findings
    assert len(nodes(ledger(_drone()))) == 2  # linked, never merged


def test_shared_identifier_link_cites_both_declarations_and_its_window() -> None:
    serial = LogicalId("serial", "AERO-7731")
    shared = link(
        "flight.ulg header",
        DRONE_LOG,
        DRONE_TAG,
        identifier=serial,
        validity=window(at(150), at(900)),
        kind=OBSERVED,
    )
    result = _run({**_drone(), "pkg-fleet": [thread(DRONE_TAG, "fleet.csv"), shared]})
    (claim,) = _of(result, SAME_AS)
    assert claim.assertion_kind is OBSERVED
    assert {ref.source for ref in claim.provenance.evidence} == {
        source("flight.ulg header"),
        source("flight.ulg header (other)"),
    }
    assert (claim.valid_from, claim.valid_to) == (at(150), at(900))


# --- legged: an operator's same_identity assertion ---------------------------------------------

SPOT_BAG = LogicalId("ros2.namespace", "/spot1")
SPOT_SERIAL = LogicalId("serial", "SPOT-1234")


def test_quadruped_same_identity_assertion_is_a_stated_same_as() -> None:
    said = assertion(
        "ASR-1", AssertionType.SAME_IDENTITY, (SPOT_SERIAL, SPOT_BAG), authored_at=at(300)
    )
    result = _run(
        {
            "pkg-walk": [thread(SPOT_BAG, "walk_0.mcap", "robot.urdf")],
            "pkg-assets": [thread(SPOT_SERIAL, "asset register")],
            "pkg-ops": [said],
        }
    )
    (claim,) = _of(result, SAME_AS)
    assert claim.assertion_kind is STATED
    assert {claim.subject, claim.object} == {
        node_ref(MACHINE, SPOT_BAG),
        node_ref(MACHINE, SPOT_SERIAL),
    }
    assert claim.provenance.records == (said["id"],)
    assert claim.valid_from == at(300)  # from when the operator said so
    assert not result.findings


def test_other_assertion_types_are_not_identity() -> None:
    noted = assertion("ASR-2", AssertionType.ANNOTATE, (SPOT_BAG, SPOT_SERIAL))
    result = _run({"pkg": [thread(SPOT_BAG, "bag"), thread(SPOT_SERIAL, "urdf"), noted]})
    assert not result.claims and not result.findings


# --- manipulator: configuration lineage --------------------------------------------------------

CELL_V1 = LogicalId("cell.config", "left-arm/v1")
CELL_V2 = LogicalId("cell.config", "left-arm/v2")


def test_manipulator_configuration_lineage_is_same_as() -> None:
    joined = lineage("cell", CELL_V1, CELL_V2)
    result = _run(
        {
            "pkg-v1": [thread(CELL_V1, "session.mcap", "hand_eye v1", node_type=CONFIG)],
            "pkg-v2": [thread(CELL_V2, "session.mcap", "hand_eye v2", node_type=CONFIG), joined],
        }
    )
    (claim,) = _of(result, SAME_AS)
    assert claim.subject == node_ref(CONFIG, CELL_V1)
    assert claim.assertion_kind is OBSERVED
    assert claim.provenance.records == (joined["id"],)
    # Both cite one MCAP, but they are already joined by same_as: no redundant candidate.
    assert not _of(result, SAME_AS_CANDIDATE)


# --- mobile: shared evidence only -> candidate -------------------------------------------------

AMR_BAG = LogicalId("ros1.hostname", "amr-12")
AMR_ROW = LogicalId("site.register_row", "W3/AMR-12")


def test_mobile_robot_shared_register_is_only_a_candidate() -> None:
    result = _run(
        {
            "pkg-bag": [thread(AMR_BAG, "drive.bag", "sites.csv")],
            "pkg-site": [thread(AMR_ROW, "sites.csv")],
        }
    )
    assert not _of(result, SAME_AS)
    bag, row = node_ref(MACHINE, AMR_BAG), node_ref(MACHINE, AMR_ROW)
    candidates = _of(result, SAME_AS_CANDIDATE)
    assert {(c.subject, c.object) for c in candidates} == {(bag, row), (row, bag)}
    for claim in candidates:
        assert claim.assertion_kind is OBSERVED
        assert {ref.source for ref in claim.provenance.evidence} == {source("sites.csv")}
        assert len(claim.provenance.records) == 2  # the evidence for each candidate
    assert same_as_candidates(result.claims, bag) == (bag, row)  # two readings


# --- the issue's named cases -------------------------------------------------------------------

LEG_A = LogicalId("serial", "ANYMAL-001")
LEG_B = LogicalId("serial", "ANYMAL-002")


def _same_urdf() -> dict[str, list[Record]]:
    return {
        "pkg-a": [thread(LEG_A, "anymal.urdf", "bag a")],
        "pkg-b": [thread(LEG_B, "anymal.urdf", "bag b")],
    }


def test_two_robots_with_identical_urdfs_and_different_serials_never_link() -> None:
    a, b = node_ref(MACHINE, LEG_A), node_ref(MACHINE, LEG_B)
    assert nodes(ledger(_same_urdf())) == (a, b)
    result = _run(_same_urdf())
    # Two serials in one namespace are declared distinct: no same_as, not even a candidate.
    assert not result.claims and not result.findings


def test_the_same_robot_across_two_packages_with_a_declared_id_links() -> None:
    """Two packages declaring one serial are one thread, so one node; a third package naming the
    robot by its fleet tag links to it through the register's co-declaration."""
    tag = LogicalId("asset-tag", "LEG-01")
    packages = {
        "pkg-site-a": [thread(LEG_A, "bag at site a", name="a")],
        "pkg-site-b": [thread(LEG_A, "bag at site b", name="b")],
        "pkg-fleet": [thread(tag, "fleet.csv"), link("fleet.csv row 4", tag, LEG_A)],
    }
    assert nodes(ledger(packages)) == (node_ref(MACHINE, tag), node_ref(MACHINE, LEG_A))
    (claim,) = _of(_run(packages), SAME_AS)
    assert {claim.subject, claim.object} == {node_ref(MACHINE, tag), node_ref(MACHINE, LEG_A)}


LEG_B_BAG = LogicalId("ros2.namespace", "/anymal_b")


def _same_urdf_undeclared() -> dict[str, list[Record]]:
    return {
        "pkg-a": [thread(LEG_A, "anymal.urdf", "bag a")],
        "pkg-b": [thread(LEG_B_BAG, "anymal.urdf", "bag b")],
    }


def test_same_urdf_without_comparable_identifiers_is_at_most_a_candidate() -> None:
    a, b = node_ref(MACHINE, LEG_A), node_ref(MACHINE, LEG_B_BAG)
    assert nodes(ledger(_same_urdf_undeclared())) == (b, a)
    result = _run(_same_urdf_undeclared())
    assert not _of(result, SAME_AS)
    assert {(c.subject, c.object) for c in _of(result, SAME_AS_CANDIDATE)} == {(a, b), (b, a)}
    assert same_as_candidates(result.claims, a) == (a, b)


def test_different_parts_of_one_file_are_not_shared_evidence() -> None:
    row = {**thread(AMR_ROW), "evidence": [cite("sites.csv", 0, 20).to_json()]}
    other = {**thread(AMR_BAG), "evidence": [cite("sites.csv", 20, 20).to_json()]}
    assert not _run({"pkg": [row, other]}).claims


def test_shared_source_across_node_types_is_not_a_candidate() -> None:
    urdf_config = LogicalId("urdf.revision", "anymal@3")
    packages = {
        "pkg-a": [thread(LEG_A, "anymal.urdf")],
        "pkg-c": [thread(urdf_config, "anymal.urdf", node_type=CONFIG)],
    }
    assert not _run(packages).claims


def test_nodes_joined_by_same_as_are_not_candidates_for_each_other() -> None:
    spare = LogicalId("serial", "PX4-SPARE")
    result = _run(
        {
            "pkg-flight": [thread(DRONE_LOG, "flight.ulg", "x500.urdf")],
            "pkg-fleet": [thread(DRONE_TAG, "fleet.csv", "x500.urdf"), DRONE_LINK],
            "pkg-spare": [thread(spare, "x500.urdf")],
        }
    )
    assert len(_of(result, SAME_AS)) == 1
    pairs = {(c.subject.node_id, c.object.node_id) for c in _of(result, SAME_AS_CANDIDATE)}  # type: ignore[union-attr]
    log, tag = f"px4.sys_uuid:{DRONE_LOG.value}", "asset-tag:UAV-0042"
    assert (log, tag) not in pairs and (tag, log) not in pairs
    assert (log, "serial:PX4-SPARE") in pairs and ("serial:PX4-SPARE", tag) in pairs


def test_identical_thread_records_in_two_packages_are_one_node() -> None:
    record = thread(LEG_A, "anymal.urdf")
    assert nodes(ledger({"pkg-1": [record], "pkg-2": [record]})) == (node_ref(MACHINE, LEG_A),)
    assert not _run({"pkg-1": [record], "pkg-2": [record]}).claims


# --- hostile input -----------------------------------------------------------------------------


def test_dangling_self_and_cross_type_links_are_findings_not_claims() -> None:
    ghost = LogicalId("serial", "ghost")
    config = LogicalId("cell.config", "x")
    result = _run(
        {
            "pkg": [
                thread(DRONE_LOG, "flight.ulg"),
                thread(config, "fleet.csv", node_type=CONFIG),
                link("dangling", DRONE_LOG, ghost),
                lineage("self", DRONE_LOG, DRONE_LOG),
                lineage("cross", config, DRONE_LOG),
                # One id named twice in a scope is one id: nothing to join.
                assertion("ASR-3", AssertionType.SAME_IDENTITY, (DRONE_LOG, DRONE_LOG)),
            ]
        }
    )
    assert not result.claims
    assert _codes(result) == [
        "identity.assertion_scope",
        "identity.dangling_link",
        "identity.self_link",
        "identity.type_mismatch",
    ]


def test_one_record_id_with_two_contents_is_a_conflict_not_last_wins() -> None:
    forged = {**DRONE_LINK, "left": SPOT_SERIAL.to_json()}
    packages = _drone()
    packages["pkg-spot"] = [thread(SPOT_SERIAL, "bag"), forged]
    result = _run(packages)
    assert not _of(result, SAME_AS)
    assert _codes(result) == ["identity.record_conflict"]
    record = thread(LEG_A, "anymal.urdf")
    altered = {**record, "evidence": [cite("other").to_json()]}
    clash = _run({"pkg-1": [record], "pkg-2": [altered]})
    assert _codes(clash) == ["identity.record_conflict"]
    assert nodes(ledger({"pkg-1": [record], "pkg-2": [altered]})) == ()


def test_conflicting_node_types_for_one_logical_id_make_no_node() -> None:
    a = thread(LEG_A, "anymal.urdf")
    b = thread(LEG_A, "anymal.urdf", node_type=CONFIG, name="other")
    result = _run({"pkg": [a, b]})
    assert nodes(ledger({"pkg": [a, b]})) == ()
    assert _codes(result) == ["identity.node_type_conflict"]


def test_an_inferred_identity_link_is_never_a_ground() -> None:
    declared = DRONE_LINK["provenance"]
    assert isinstance(declared, dict)
    inferred = {**DRONE_LINK, "provenance": {**declared, "assertion_kind": "inferred"}}
    result = _run({**_drone(), "pkg-fleet": [thread(DRONE_TAG, "fleet.csv"), inferred]})
    assert not result.claims
    assert _codes(result) == ["identity.inferred_link"]


GOOD = thread(LEG_A, "anymal.urdf")
SAID = assertion("ASR-9", AssertionType.SAME_IDENTITY, (LEG_A, LEG_B))


@pytest.mark.parametrize(
    "record",
    [
        {**GOOD, "id": "nope"},
        {**GOOD, "logical_id": {"namespace": "X"}},
        {**GOOD, "node_type": "spaceship"},
        {**GOOD, "valid_from": {"ticks": "soon"}},
        {**GOOD, "evidence": []},
        {**GOOD, "evidence": [{"source": "md5:1", "locator": []}]},
        {k: v for k, v in GOOD.items() if k != "evidence"},
        {"kind": "identity_link", "id": DRONE_LINK["id"], "left": LEG_A.to_json()},
        {**DRONE_LINK, "basis": "same_paint_job"},
        {**DRONE_LINK, "right": DRONE_LINK["left"]},  # a link relates two ids
        {**DRONE_LINK, "schema_version": 2},  # identity_link is since package-schema 3
        {**DRONE_LINK, "extra": 1},
        {**lineage("c", LEG_A, LEG_B), "predecessor": 7},
        {**SAID, "assertion_type": {"knowledge": "known", "value": "merge"}},
        {**SAID, "provenance": {**SAID["provenance"], "assertion_kind": "observed"}},  # type: ignore[dict-item]
        {**GOOD, "logical_id": {"namespace": "serial", "value": "   "}},
        {**GOOD, "logical_id": {"namespace": "serial", "value": " SPOT-1"}},
        link("pad", LEG_A, LogicalId("serial", "B ")),
        assertion("ASR-pad", AssertionType.SAME_IDENTITY, (LEG_A, LogicalId("serial", "B "))),
    ],
    ids=[
        "bad-id",
        "bad-logical-id",
        "bad-node-type",
        "bad-valid-from",
        "no-evidence",
        "bad-evidence",
        "missing-evidence",
        "link-missing-fields",
        "link-bad-basis",
        "link-self",
        "link-older-schema",
        "link-unknown-key",
        "lineage-not-ids",
        "assertion-bad-type",
        "assertion-not-stated",
        "whitespace-only-logical-id",
        "padded-logical-id",
        "padded-link-side",
        "padded-scope-id",
    ],
)
def test_malformed_records_are_findings_and_the_build_survives(record: Record) -> None:
    packages = _same_urdf_undeclared()
    packages["pkg-x"] = [record]
    result = _run(packages)
    assert _codes(result) == ["identity.malformed_record"]
    assert not _of(result, SAME_AS)
    assert len(_of(result, SAME_AS_CANDIDATE)) == 2


def test_unknown_config_is_a_warning() -> None:
    assert _codes(_run(_drone(), {"merge": True})) == ["identity.unknown_config"]


def test_identity_predicates_are_core_since_graph_schema_v1() -> None:
    result = run_consolidator(IdentityConsolidator(), ledger(_drone()), (), {}, recorded_at=TX)
    assert IDENTITY_PREDICATES is CORE_PREDICATES
    assert _of(result, SAME_AS)
    assert not result.findings


# --- determinism -------------------------------------------------------------------------------


def _everything() -> dict[str, list[Record]]:
    packages = {**_drone(), **_same_urdf()}
    packages["pkg-site"] = [thread(AMR_ROW, "sites.csv")]
    packages["pkg-bag"] = [thread(AMR_BAG, "drive.bag", "sites.csv")]
    packages["pkg-ops"] = [
        thread(SPOT_BAG, "walk"),
        thread(SPOT_SERIAL, "assets"),
        assertion("ASR-1", AssertionType.SAME_IDENTITY, (SPOT_SERIAL, SPOT_BAG)),
        link("register row 9", AMR_ROW, (AMR_BAG, SPOT_BAG)),
    ]
    return packages


def test_identity_rebuild_is_byte_identical_and_order_independent() -> None:
    packages = _everything()
    forward = ledger(packages)
    backward = ledger({pid: list(reversed(recs)) for pid, recs in reversed(packages.items())})

    def build(reader: object) -> bytes:
        built = rebuild(
            reader,  # type: ignore[arg-type]
            [(IdentityConsolidator(), {})],
            recorded_at=TX,
            registry=IDENTITY_PREDICATES,
        )
        return canonical_json.dumps([r.to_json() for r in built])

    assert build(forward) == build(forward) == build(backward)
    assert _run(packages).claims and not _run(packages).findings


def test_claim_ids_depend_on_content_and_lineage_only() -> None:
    first = _run(_drone())
    later = run_consolidator(
        IdentityConsolidator(), ledger(_drone()), (), {}, recorded_at=ledger_tx(9)
    )
    assert [c.id for c in first.claims] == [c.id for c in later.claims]  # tx is bookkeeping
    assert isinstance(first.claims[0].assertion_kind, AssertionKind)
