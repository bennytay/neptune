"""Identity policy (ADR 0003 §1) on the four compiler worked platforms plus the shared-URDF case.

Worked platforms are the compiler's examples (``docs/canonical-data-model.md``): drone (PX4 ULog,
``sys_uuid``), quadruped (ROS 2 bag + URDF), manipulator (MCAP + hand-eye YAML) and mobile robot
(ROS 1 bag + site register CSV). Each exercises one ``same_as`` ground or the candidate path.
"""

from collections.abc import Mapping, Sequence

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.time import Timestamp
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
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import Claim
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeType

Record = dict[str, object]
TX = ledger_tx(3)
CLOCK = record_id("test.clock", {"name": "site-utc"})
MACHINE, CONFIG = NodeType.MACHINE, NodeType.CONFIGURATION


def _rid(kind: str, n: object) -> RecordId:
    return record_id(kind, {"n": str(n)})


def _cite(source: ContentId, length: int = 64) -> dict[str, object]:
    return EvidenceRef(source, (ByteRange(0, length),)).to_json()  # type: ignore[return-value]


def _when(ticks: int = 100) -> dict[str, object]:
    return Timestamp(ticks, CLOCK).to_json()  # type: ignore[return-value]


def _thread(node: LogicalId, *sources: ContentId, node_type: NodeType = MACHINE) -> Record:
    return {
        "kind": "ledger_thread",
        "id": _rid("ledger_thread", (node.namespace, node.value)),
        "logical_id": node.to_json(),
        "node_type": str(node_type),
        "valid_from": _when(),
        "evidence": [_cite(s) for s in sources],
    }


def _link(kind: str, name: str, left: LogicalId, right: LogicalId, **extra: object) -> Record:
    sides = {
        "identity_link": ("left", "right"),
        "configuration_lineage": ("predecessor", "successor"),
        "operator_assertion": ("subject", "object"),
    }[kind]
    return {
        "kind": kind,
        "id": _rid(kind, name),
        sides[0]: left.to_json(),
        sides[1]: right.to_json(),
        "valid_from": _when(200),
        "evidence": [_cite(content_id(name.encode()))],
        **extra,
    }


def _ledger(packages: Mapping[str, Sequence[Record]]) -> StubLedger:
    return StubLedger({pid: (1, list(records)) for pid, records in packages.items()})


def _run(packages: Mapping[str, Sequence[Record]]) -> Consolidation:
    return run_consolidator(
        IdentityConsolidator(),
        _ledger(packages),
        (),
        {},
        recorded_at=TX,
        registry=IDENTITY_PREDICATES,
    )


def _of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


# --- drone (aerial): declared identifier, PX4 sys_uuid ----------------------------------------

ULOG = content_id(b"px4 ulog flight 0042")
FLEET_CSV = content_id(b"asset_tag,sys_uuid\nD-07,0x3f2a\n")
DRONE_LOG = LogicalId("px4.sys_uuid", "0x3f2a")
DRONE_ASSET = LogicalId("fleet.asset_tag", "D-07")
SYS_UUID = {"namespace": "px4.sys_uuid", "value": "0x3f2a"}


def _drone() -> dict[str, list[Record]]:
    link = _link("identity_link", "drone", DRONE_ASSET, DRONE_LOG, identifier=SYS_UUID)
    return {
        "pkg-flight": [_thread(DRONE_LOG, ULOG)],
        "pkg-fleet": [_thread(DRONE_ASSET, FLEET_CSV), link],
    }


def test_drone_declared_identifier_is_same_as() -> None:
    result = _run(_drone())
    (claim,) = _of(result, SAME_AS)
    assert claim.subject == node_ref(MACHINE, DRONE_ASSET)  # lower logical id in canonical order
    assert claim.object == node_ref(MACHINE, DRONE_LOG)
    assert claim.assertion_kind is AssertionKind.OBSERVED
    assert claim.provenance.records == (_rid("identity_link", "drone"),)
    assert claim.provenance.consolidator_id == "memory.identity"
    assert not _of(result, SAME_AS_CANDIDATE) and not result.findings
    assert len(nodes(_ledger(_drone()))) == 2  # linked, never merged


# --- quadruped (legged): operator assertion ---------------------------------------------------

BAG2 = content_id(b"ros2 bag spot1 walk")
SPOT_URDF = content_id(b"<robot name='spot'/>")
SPOT_BAG = LogicalId("ros2.namespace", "/spot1")
SPOT_SERIAL = LogicalId("serial", "SPOT-1234")


def test_quadruped_operator_assertion_is_a_stated_same_as() -> None:
    assertion = _link(
        "operator_assertion",
        "spot",
        SPOT_BAG,
        SPOT_SERIAL,
        predicate="same_as",
        operator="field-ops/ana",
    )
    result = _run(
        {
            "pkg-walk": [_thread(SPOT_BAG, BAG2, SPOT_URDF)],
            "pkg-assets": [_thread(SPOT_SERIAL, content_id(b"asset register")), assertion],
        }
    )
    (claim,) = _of(result, SAME_AS)
    assert claim.assertion_kind is AssertionKind.STATED
    assert {claim.subject, claim.object} == {
        node_ref(MACHINE, SPOT_BAG),
        node_ref(MACHINE, SPOT_SERIAL),
    }
    assert claim.provenance.records == (_rid("operator_assertion", "spot"),)


def test_operator_assertion_about_another_predicate_is_not_identity() -> None:
    assertion = _link(
        "operator_assertion", "x", SPOT_BAG, SPOT_SERIAL, predicate="located_at", operator="ana"
    )
    result = _run({"pkg": [_thread(SPOT_BAG, BAG2), _thread(SPOT_SERIAL, SPOT_URDF), assertion]})
    assert not result.claims and not result.findings


# --- manipulator: configuration lineage -------------------------------------------------------

MCAP = content_id(b"mcap ur10e pick session")
HAND_EYE_V1 = content_id(b"hand_eye: v1")
HAND_EYE_V2 = content_id(b"hand_eye: v2")
CELL_V1 = LogicalId("cell.config", "left-arm/v1")
CELL_V2 = LogicalId("cell.config", "left-arm/v2")


def test_manipulator_configuration_lineage_is_same_as() -> None:
    lineage = _link("configuration_lineage", "cell", CELL_V1, CELL_V2)
    result = _run(
        {
            "pkg-v1": [_thread(CELL_V1, MCAP, HAND_EYE_V1, node_type=CONFIG)],
            "pkg-v2": [_thread(CELL_V2, MCAP, HAND_EYE_V2, node_type=CONFIG), lineage],
        }
    )
    (claim,) = _of(result, SAME_AS)
    assert claim.subject == node_ref(CONFIG, CELL_V1)
    assert claim.provenance.records == (_rid("configuration_lineage", "cell"),)
    # Both cite one MCAP, but they are already joined by same_as: no redundant candidate.
    assert not _of(result, SAME_AS_CANDIDATE)


# --- mobile robot: shared evidence only -> candidate -------------------------------------------

BAG1 = content_id(b"ros1 bag amr warehouse")
REGISTER = content_id(b"site,robot\nW3,AMR-12\n")
AMR_BAG = LogicalId("ros1.hostname", "amr-12")
AMR_ROW = LogicalId("site.register_row", "W3/AMR-12")


def test_mobile_robot_shared_register_is_only_a_candidate() -> None:
    result = _run(
        {"pkg-bag": [_thread(AMR_BAG, BAG1, REGISTER)], "pkg-site": [_thread(AMR_ROW, REGISTER)]}
    )
    assert not _of(result, SAME_AS)
    bag, row = node_ref(MACHINE, AMR_BAG), node_ref(MACHINE, AMR_ROW)
    candidates = _of(result, SAME_AS_CANDIDATE)
    assert {(c.subject, c.object) for c in candidates} == {(bag, row), (row, bag)}
    for claim in candidates:
        assert claim.assertion_kind is AssertionKind.OBSERVED
        assert {ref.source for ref in claim.provenance.evidence} == {REGISTER}
        assert len(claim.provenance.records) == 2  # the evidence for each candidate
    assert same_as_candidates(result.claims, bag) == (bag, row)  # Ambiguous: two readings


# --- two robots built from one URDF must not merge --------------------------------------------

SHARED_URDF = content_id(b"<robot name='anymal'/>")
LEG_A = LogicalId("serial", "ANYMAL-001")
LEG_B = LogicalId("serial", "ANYMAL-002")


def _same_urdf() -> dict[str, list[Record]]:
    return {
        "pkg-a": [_thread(LEG_A, SHARED_URDF, content_id(b"bag a"))],
        "pkg-b": [_thread(LEG_B, SHARED_URDF, content_id(b"bag b"))],
    }


def test_two_robots_same_urdf_stay_two_nodes() -> None:
    a, b = node_ref(MACHINE, LEG_A), node_ref(MACHINE, LEG_B)
    assert nodes(_ledger(_same_urdf())) == (a, b)
    result = _run(_same_urdf())
    # Two serials in one namespace are declared distinct: no same_as, not even a candidate.
    assert not result.claims and not result.findings


LEG_B_BAG = LogicalId("ros2.namespace", "/anymal_b")


def _same_urdf_undeclared() -> dict[str, list[Record]]:
    return {
        "pkg-a": [_thread(LEG_A, SHARED_URDF, content_id(b"bag a"))],
        "pkg-b": [_thread(LEG_B_BAG, SHARED_URDF, content_id(b"bag b"))],
    }


def test_same_urdf_without_comparable_identifiers_is_at_most_a_candidate() -> None:
    a, b = node_ref(MACHINE, LEG_A), node_ref(MACHINE, LEG_B_BAG)
    assert nodes(_ledger(_same_urdf_undeclared())) == (b, a)
    result = _run(_same_urdf_undeclared())
    assert not _of(result, SAME_AS)
    assert {(c.subject, c.object) for c in _of(result, SAME_AS_CANDIDATE)} == {(a, b), (b, a)}
    assert same_as_candidates(result.claims, a) == (a, b)


def test_different_parts_of_one_file_are_not_shared_evidence() -> None:
    row = {**_thread(AMR_ROW, BAG1), "evidence": [_cite(REGISTER, 20)]}
    other = {
        **_thread(AMR_BAG, BAG1),
        "evidence": [EvidenceRef(REGISTER, (ByteRange(20, 20),)).to_json()],
    }
    assert not _run({"pkg": [row, other]}).claims


def test_shared_source_across_node_types_is_not_a_candidate() -> None:
    urdf_config = LogicalId("urdf.revision", "anymal@3")
    packages = {
        "pkg-a": [_thread(LEG_A, SHARED_URDF)],
        "pkg-c": [_thread(urdf_config, SHARED_URDF, node_type=CONFIG)],
    }
    assert not _run(packages).claims


def test_nodes_joined_by_same_as_are_not_candidates_for_each_other() -> None:
    spare = LogicalId("serial", "PX4-SPARE")
    link = _link("identity_link", "drone", DRONE_ASSET, DRONE_LOG, identifier=SYS_UUID)
    result = _run(
        {
            "pkg-flight": [_thread(DRONE_LOG, ULOG, SHARED_URDF)],
            "pkg-fleet": [_thread(DRONE_ASSET, FLEET_CSV, SHARED_URDF), link],
            "pkg-spare": [_thread(spare, SHARED_URDF)],
        }
    )
    assert len(_of(result, SAME_AS)) == 1
    pairs = {(c.subject.node_id, c.object.node_id) for c in _of(result, SAME_AS_CANDIDATE)}  # type: ignore[union-attr]
    log, asset = "px4.sys_uuid:0x3f2a", "fleet.asset_tag:D-07"
    assert (log, asset) not in pairs and (asset, log) not in pairs
    assert (log, "serial:PX4-SPARE") in pairs and ("serial:PX4-SPARE", asset) in pairs


def test_identical_thread_records_in_two_packages_are_one_node() -> None:
    thread = _thread(LEG_A, SHARED_URDF)
    assert nodes(_ledger({"pkg-1": [thread], "pkg-2": [thread]})) == (node_ref(MACHINE, LEG_A),)
    assert not _run({"pkg-1": [thread], "pkg-2": [thread]}).claims


def test_two_thread_records_with_one_logical_id_are_one_node() -> None:
    first = _thread(LEG_A, SHARED_URDF)
    second = {**_thread(LEG_A, content_id(b"bag a2")), "id": _rid("ledger_thread", "a-again")}
    ledger = _ledger({"pkg-1": [first], "pkg-2": [second]})
    assert nodes(ledger) == (node_ref(MACHINE, LEG_A),)
    assert not _run({"pkg-1": [first], "pkg-2": [second]}).findings


# --- hostile input -----------------------------------------------------------------------------


def test_dangling_self_and_cross_type_links_are_findings_not_claims() -> None:
    ghost = LogicalId("serial", "ghost")
    config = LogicalId("cell.config", "x")
    result = _run(
        {
            "pkg": [
                _thread(DRONE_LOG, ULOG),
                _thread(config, FLEET_CSV, node_type=CONFIG),
                _link("identity_link", "dangling", DRONE_LOG, ghost, identifier=SYS_UUID),
                _link("identity_link", "self", DRONE_LOG, DRONE_LOG, identifier=SYS_UUID),
                _link("configuration_lineage", "cross", config, DRONE_LOG),
            ]
        }
    )
    assert not result.claims
    assert sorted(f.code for f in result.findings) == [
        "identity.dangling_link",
        "identity.self_link",
        "identity.type_mismatch",
    ]


def test_one_record_id_with_two_contents_is_a_conflict_not_last_wins() -> None:
    link = _link("identity_link", "drone", DRONE_ASSET, DRONE_LOG, identifier=SYS_UUID)
    forged = {**link, "right": SPOT_SERIAL.to_json()}
    packages = _drone()
    packages["pkg-spot"] = [_thread(SPOT_SERIAL, BAG2), forged]
    result = _run(packages)
    assert not _of(result, SAME_AS)
    assert [f.code for f in result.findings] == ["identity.record_conflict"]
    thread = _thread(LEG_A, SHARED_URDF)
    altered = {**thread, "evidence": [_cite(content_id(b"other"))]}
    clash = _run({"pkg-1": [thread], "pkg-2": [altered]})
    assert [f.code for f in clash.findings] == ["identity.record_conflict"]
    assert nodes(_ledger({"pkg-1": [thread], "pkg-2": [altered]})) == ()


def test_conflicting_node_types_for_one_logical_id_make_no_node() -> None:
    a = _thread(LEG_A, SHARED_URDF)
    b = {**_thread(LEG_A, SHARED_URDF, node_type=CONFIG), "id": _rid("ledger_thread", "other")}
    result = _run({"pkg": [a, b]})
    assert nodes(_ledger({"pkg": [a, b]})) == ()
    assert [f.code for f in result.findings] == ["identity.node_type_conflict"]


GOOD = _thread(LEG_A, SHARED_URDF)


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
        {"kind": "identity_link", "id": _rid("l", 1), "left": LEG_A.to_json()},
        {**_link("identity_link", "noid", LEG_A, LEG_B), "identifier": 7},
        {**_link("configuration_lineage", "c", LEG_A, LEG_B), "predecessor": 7},
        {**_link("operator_assertion", "o1", LEG_A, LEG_B, operator="ana")},
        _link("operator_assertion", "o2", LEG_A, LEG_B, predicate="same_as", operator=""),
        _link("operator_assertion", "o3", LEG_A, LEG_B, predicate="same_as", operator="\ud800"),
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
        "link-bad-identifier",
        "lineage-not-ids",
        "assertion-no-predicate",
        "assertion-empty-operator",
        "assertion-lone-surrogate",
    ],
)
def test_malformed_records_are_findings_and_the_build_survives(record: Record) -> None:
    packages = _same_urdf_undeclared()
    packages["pkg-x"] = [record]
    result = _run(packages)
    assert [f.code for f in result.findings] == ["identity.malformed_record"]
    assert not _of(result, SAME_AS)
    assert len(_of(result, SAME_AS_CANDIDATE)) == 2


def test_unknown_config_is_a_warning() -> None:
    result = run_consolidator(
        IdentityConsolidator(),
        _ledger(_drone()),
        (),
        {"merge": True},
        recorded_at=TX,
        registry=IDENTITY_PREDICATES,
    )
    assert [f.code for f in result.findings] == ["identity.unknown_config"]


def test_identity_claims_need_the_identity_vocabulary() -> None:
    result = run_consolidator(IdentityConsolidator(), _ledger(_drone()), (), {}, recorded_at=TX)
    assert not result.claims
    assert {f.code for f in result.findings} == {"consolidate.schema_violation"}


# --- determinism -------------------------------------------------------------------------------


def _everything() -> dict[str, list[Record]]:
    packages = {**_drone(), **_same_urdf()}
    packages["pkg-site"] = [_thread(AMR_ROW, REGISTER)]
    packages["pkg-bag"] = [_thread(AMR_BAG, BAG1, REGISTER)]
    return packages


def test_identity_rebuild_is_byte_identical_and_order_independent() -> None:
    packages = _everything()
    forward = _ledger(packages)
    backward = StubLedger(
        {pid: (1, list(reversed(recs))) for pid, recs in reversed(packages.items())}
    )

    def build(ledger: StubLedger) -> bytes:
        built = rebuild(
            ledger, [(IdentityConsolidator(), {})], recorded_at=TX, registry=IDENTITY_PREDICATES
        )
        return canonical_json.dumps([r.to_json() for r in built])

    assert build(forward) == build(forward) == build(backward)
