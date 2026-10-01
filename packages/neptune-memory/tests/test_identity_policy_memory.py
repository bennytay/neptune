"""Identity policy (ADR 0003 §1) on the four compiler worked platforms plus the shared-URDF case.

Worked platforms are the compiler's examples (``docs/canonical-data-model.md``): drone (PX4 ULog,
``sys_uuid``), quadruped (ROS 2 bag + URDF), manipulator (MCAP + hand-eye YAML) and mobile robot
(ROS 1 bag + site register CSV). Each exercises one ground or the candidate path.
"""

from collections.abc import Mapping, Sequence

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.knowledge import KnowledgeState
from neptune_memory.consolidate.base import Consolidation, ProposedClaim, rebuild, run_consolidator
from neptune_memory.consolidate.identity import (
    SAME_AS,
    SAME_AS_CANDIDATE,
    IdentityConsolidator,
    nodes,
)
from neptune_memory.ledger import StubLedger

Record = Mapping[str, object]


def _rid(kind: str, n: object) -> RecordId:
    return record_id(kind, {"n": str(n)})


def _thread(node: LogicalId, *sources: ContentId, n: int = 0) -> Record:
    return {
        "kind": "ledger_thread",
        "id": _rid("ledger_thread", (node.namespace, node.value, n)),
        "logical_id": node.to_json(),
        "sources": list(sources),
    }


def _run(packages: Mapping[str, Sequence[Record]]) -> Consolidation:
    ledger = StubLedger({pid: (1, list(records)) for pid, records in packages.items()})
    return run_consolidator(IdentityConsolidator(), ledger, (), {})


def _of(result: Consolidation, predicate: str) -> list[ProposedClaim]:
    return [c for c in result.claims if c.predicate == predicate]


# --- drone: declared identifier (PX4 sys_uuid) ------------------------------------------------

ULOG = content_id(b"px4 ulog flight 0042")
FLEET_CSV = content_id(b"asset_tag,sys_uuid\nD-07,0x3f2a\n")
DRONE_LOG = LogicalId("px4.sys_uuid", "0x3f2a")
DRONE_ASSET = LogicalId("fleet.asset_tag", "D-07")


def _drone() -> dict[str, list[Record]]:
    link = {
        "kind": "identity_link",
        "id": _rid("identity_link", "drone"),
        "left": DRONE_ASSET.to_json(),
        "right": DRONE_LOG.to_json(),
        "identifier": {"namespace": "px4.sys_uuid", "value": "0x3f2a"},
    }
    return {
        "pkg-flight": [_thread(DRONE_LOG, ULOG)],
        "pkg-fleet": [_thread(DRONE_ASSET, FLEET_CSV), link],
    }


def test_drone_declared_identifier_is_same_as() -> None:
    result = _run(_drone())
    (claim,) = _of(result, SAME_AS)
    assert claim.draft.subject == DRONE_ASSET  # lower logical id in canonical order
    assert claim.draft.object == {
        "ground": "declared_identifier",
        "identifier": {"namespace": "px4.sys_uuid", "value": "0x3f2a"},
        "node": DRONE_LOG.to_json(),
    }
    assert claim.draft.assertion_kind == "observed"
    assert claim.draft.inputs == (_rid("identity_link", "drone"),)
    assert not _of(result, SAME_AS_CANDIDATE) and not result.findings


# --- quadruped: operator assertion -----------------------------------------------------------

BAG2 = content_id(b"ros2 bag spot1 walk")
SPOT_URDF = content_id(b"<robot name='spot'/>")
SPOT_BAG = LogicalId("ros2.namespace", "/spot1")
SPOT_SERIAL = LogicalId("serial", "SPOT-1234")


def test_quadruped_operator_assertion_is_a_stated_same_as() -> None:
    assertion = {
        "kind": "operator_assertion",
        "id": _rid("operator_assertion", "spot"),
        "predicate": "same_as",
        "subject": SPOT_BAG.to_json(),
        "object": SPOT_SERIAL.to_json(),
        "operator": "field-ops/ana",
    }
    result = _run(
        {
            "pkg-walk": [_thread(SPOT_BAG, BAG2, SPOT_URDF)],
            "pkg-assets": [_thread(SPOT_SERIAL), assertion],
        }
    )
    (claim,) = _of(result, SAME_AS)
    assert claim.draft.assertion_kind == "stated"
    assert claim.draft.object == {
        "ground": "operator_assertion",
        "node": SPOT_SERIAL.to_json(),
        "operator": "field-ops/ana",
    }
    assert claim.draft.inputs == (_rid("operator_assertion", "spot"),)


def test_operator_assertion_about_another_predicate_is_not_identity() -> None:
    assertion = {
        "kind": "operator_assertion",
        "id": _rid("operator_assertion", "other"),
        "predicate": "located_at",
        "subject": SPOT_BAG.to_json(),
        "object": SPOT_SERIAL.to_json(),
        "operator": "field-ops/ana",
    }
    result = _run({"pkg": [_thread(SPOT_BAG), _thread(SPOT_SERIAL), assertion]})
    assert not result.claims and not result.findings


# --- manipulator: configuration lineage ------------------------------------------------------

MCAP = content_id(b"mcap ur10e pick session")
HAND_EYE_V1 = content_id(b"hand_eye: v1")
HAND_EYE_V2 = content_id(b"hand_eye: v2")
CELL_V1 = LogicalId("cell.config", "left-arm/v1")
CELL_V2 = LogicalId("cell.config", "left-arm/v2")


def test_manipulator_configuration_lineage_is_same_as() -> None:
    lineage = {
        "kind": "configuration_lineage",
        "id": _rid("configuration_lineage", "cell"),
        "predecessor": CELL_V1.to_json(),
        "successor": CELL_V2.to_json(),
    }
    result = _run(
        {
            "pkg-v1": [_thread(CELL_V1, MCAP, HAND_EYE_V1)],
            "pkg-v2": [_thread(CELL_V2, MCAP, HAND_EYE_V2), lineage],
        }
    )
    (claim,) = _of(result, SAME_AS)
    assert claim.draft.object["ground"] == "configuration_lineage"  # type: ignore[index,call-overload]
    assert claim.draft.object["predecessor"] == CELL_V1.to_json()  # type: ignore[index,call-overload]
    # Both cite one MCAP, but they are already joined by same_as: no redundant candidate.
    assert not _of(result, SAME_AS_CANDIDATE)


# --- mobile robot: shared evidence only -> Ambiguous candidate --------------------------------

BAG1 = content_id(b"ros1 bag amr warehouse")
REGISTER = content_id(b"site,robot\nW3,AMR-12\n")
AMR_BAG = LogicalId("ros1.hostname", "amr-12")
AMR_ROW = LogicalId("site.register_row", "W3/AMR-12")


def test_mobile_robot_shared_register_is_only_a_candidate() -> None:
    result = _run(
        {
            "pkg-bag": [_thread(AMR_BAG, BAG1, REGISTER)],
            "pkg-site": [_thread(AMR_ROW, REGISTER)],
        }
    )
    assert not _of(result, SAME_AS)
    candidates = _of(result, SAME_AS_CANDIDATE)
    assert {c.draft.subject for c in candidates} == {AMR_BAG, AMR_ROW}
    for claim in candidates:
        assert claim.draft.state is KnowledgeState.AMBIGUOUS
        assert claim.draft.assertion_kind == "observed"
        obj = claim.draft.object
        assert isinstance(obj, dict) and obj["source"] == REGISTER
        listed = obj["candidates"]
        assert isinstance(listed, list) and len(listed) == 2
        assert all(isinstance(c, dict) and c["evidence"] for c in listed)


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
    ledger = StubLedger({pid: (1, recs) for pid, recs in _same_urdf().items()})
    assert nodes(ledger) == (LEG_A, LEG_B)
    result = run_consolidator(IdentityConsolidator(), ledger, (), {})
    assert not _of(result, SAME_AS)
    candidates = _of(result, SAME_AS_CANDIDATE)
    assert len(candidates) == 2
    assert all(c.draft.state is KnowledgeState.AMBIGUOUS for c in candidates)
    obj = candidates[0].draft.object
    assert isinstance(obj, dict)
    listed = obj["candidates"]
    assert isinstance(listed, list)
    assert [c["node"] for c in listed if isinstance(c, dict)] == [LEG_A.to_json(), LEG_B.to_json()]


def test_identical_thread_records_in_two_packages_are_one_node() -> None:
    thread = _thread(LEG_A, SHARED_URDF)
    ledger = StubLedger({"pkg-1": (1, [thread]), "pkg-2": (1, [thread])})
    assert nodes(ledger) == (LEG_A,)
    assert not run_consolidator(IdentityConsolidator(), ledger, (), {}).claims


# --- hostile input -----------------------------------------------------------------------------


def test_dangling_and_self_links_are_findings_not_claims() -> None:
    dangling = {
        "kind": "identity_link",
        "id": _rid("identity_link", "dangling"),
        "left": DRONE_LOG.to_json(),
        "right": LogicalId("serial", "ghost").to_json(),
        "identifier": {"namespace": "serial", "value": "ghost"},
    }
    self_link = {
        **dangling,
        "id": _rid("identity_link", "self"),
        "right": DRONE_LOG.to_json(),
    }
    result = _run({"pkg": [_thread(DRONE_LOG), dangling, self_link]})
    assert not result.claims
    assert sorted(f.code for f in result.findings) == [
        "identity.dangling_link",
        "identity.self_link",
    ]


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "ledger_thread", "id": "nope", "logical_id": LEG_A.to_json(), "sources": []},
        {
            "kind": "ledger_thread",
            "id": _rid("t", 1),
            "logical_id": {"namespace": "X"},
            "sources": [],
        },
        {
            "kind": "ledger_thread",
            "id": _rid("t", 2),
            "logical_id": LEG_A.to_json(),
            "sources": "x",
        },
        {
            "kind": "ledger_thread",
            "id": _rid("t", 3),
            "logical_id": LEG_A.to_json(),
            "sources": ["md5:1"],
        },
        {"kind": "identity_link", "id": _rid("l", 1), "left": LEG_A.to_json()},
        {"kind": "configuration_lineage", "id": _rid("c", 1), "predecessor": 7, "successor": 8},
        {"kind": "operator_assertion", "id": _rid("o", 1), "subject": LEG_A.to_json()},
        {
            "kind": "operator_assertion",
            "id": _rid("o", 2),
            "predicate": "same_as",
            "subject": LEG_A.to_json(),
            "object": LEG_B.to_json(),
            "operator": "",
        },
    ],
    ids=[
        "bad-id",
        "bad-logical-id",
        "sources-not-list",
        "bad-content-id",
        "link-missing-fields",
        "lineage-not-ids",
        "assertion-no-predicate",
        "assertion-empty-operator",
    ],
)
def test_malformed_records_are_findings(record: Record) -> None:
    result = _run({"pkg": [_thread(LEG_A), _thread(LEG_B), record]})
    assert not _of(result, SAME_AS)
    assert [f.code for f in result.findings] == ["identity.malformed_record"]


def test_unknown_config_is_a_warning() -> None:
    ledger = StubLedger({"pkg": (1, [_thread(LEG_A)])})
    result = run_consolidator(IdentityConsolidator(), ledger, (), {"merge": True})
    assert [f.code for f in result.findings] == ["identity.unknown_config"]


# --- determinism -------------------------------------------------------------------------------


def _everything() -> dict[str, list[Record]]:
    packages = {**_drone(), **_same_urdf()}
    packages["pkg-site"] = [_thread(AMR_ROW, REGISTER)]
    packages["pkg-bag"] = [_thread(AMR_BAG, BAG1, REGISTER)]
    return packages


def test_identity_rebuild_is_byte_identical_and_order_independent() -> None:
    packages = _everything()
    forward = StubLedger({pid: (1, recs) for pid, recs in packages.items()})
    backward = StubLedger(
        {pid: (1, list(reversed(recs))) for pid, recs in reversed(packages.items())}
    )

    def build(ledger: StubLedger) -> bytes:
        return canonical_json.dumps(
            [r.to_json() for r in rebuild(ledger, [(IdentityConsolidator(), {})])]
        )

    assert build(forward) == build(forward) == build(backward)
