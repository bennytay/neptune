"""A run's pinned configuration snapshot is its configuration (ADR 0022, root ADR 0072).

The Ledger threads no ``configuration_snapshot`` (Ledger ADR 0003 §2), so before ADR 0022 every
run-sheet pin to a parameter document was ``configuration_unknown``. The packages are the
compiler's manifest goldens (``tests/golden/manifest/``): three embodiments ingested under a
``neptune.yaml`` with run-sheet pins, so every ``snapshot_binding`` here is the compiler's own.
Calibration and hardware pins use the compiler's record classes. Nothing is hand-written JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from memory_calibration_records import calibration
from memory_catalog_threads import anchored, answers, catalog, package_id, subject, thread_id
from memory_configuration_records import binding, hardware, run
from memory_identity_records import civil_domain
from neptune.model.ids import LogicalId
from neptune.model.run import run_from_json
from neptune.model.time import Timestamp
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.configuration import ConfigurationLineageConsolidator
from neptune_memory.consolidate.runs import run_node
from neptune_memory.ledger import StubLedger, ThreadsOf
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune_memory.schema.claim import Claim

Record = dict[str, object]
GOLDEN: Final = Path(__file__).resolve().parents[3] / "tests" / "golden" / "manifest"
EMBODIMENTS: Final = ("aerial_survey", "amr_fleet", "manipulator_cell")
TX = ledger_tx(3)


def package(name: str) -> list[Record]:
    """A compiler manifest golden's record lines, in file-name then line order."""
    return [
        json.loads(line)
        for path in sorted((GOLDEN / name / "records").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def kind(records: Sequence[Record], name: str) -> list[Record]:
    return [r for r in records if r["kind"] == name]


def consolidate(ledger: StubLedger) -> Consolidation:
    result = run_consolidator(ConfigurationLineageConsolidator(), ledger, (), {}, recorded_at=TX)
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    return result


def of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def configuration_of(snapshot: Record) -> NodeRef:
    """The anchored configuration thread the snapshot's own evidence keys, derived here from the
    Ledger's published rule independently of the code under test."""
    return NodeRef(
        NodeType.CONFIGURATION, f"thread:{thread_id(anchored('configuration', snapshot))}"
    )


def by_id(records: Sequence[Record]) -> dict[object, Record]:
    return {r["id"]: r for r in records if "id" in r}


# --- Compiler pins to a configuration snapshot ---------------------------------------------------


@pytest.mark.parametrize("name", EMBODIMENTS)
def test_every_pinned_parameter_document_is_its_runs_configuration(name: str) -> None:
    records = package(name)
    pins = kind(records, "snapshot_binding")
    assert pins and {p["snapshot_kind"] for p in pins} == {"configuration_snapshot"}
    result = consolidate(catalog({package_id(name): records}))
    held = by_id(records)
    active = of(result, "configuration_active_during")
    assert len(active) == len(pins)
    for pin in pins:
        (claim,) = [c for c in active if pin["id"] in c.provenance.records]
        run_record = run_from_json(held[pin["run"]])  # type: ignore[arg-type]
        assert claim.subject == run_node(run_record)
        assert claim.object == configuration_of(held[pin["snapshot"]])
        assert claim.assertion_kind == "stated"  # the run sheet states it
        assert {pin["run"], pin["snapshot"]} <= set(claim.provenance.records)
    assert of(result, "configuration_unknown") == []
    assert of(result, "configuration_candidate") == []
    assert "configuration.unthreaded_id" not in codes(result)


def test_runs_pinning_one_document_share_its_configuration() -> None:
    """Two AMR-fleet runs pin the same parameter file: one configuration node, so a later run on
    other bytes reads as a change and these two do not."""
    result = consolidate(catalog({package_id("amr_fleet"): package("amr_fleet")}))
    first, second = of(result, "configuration_active_during")
    assert first.subject != second.subject
    assert first.object == second.object


def test_the_node_is_the_one_a_ledger_threading_snapshots_would_answer() -> None:
    """If the Ledger's table gains a ``configuration_snapshot`` row, the catalog answers the
    snapshot's anchored thread, and every claim is byte-for-byte the same: no lineage break."""
    records = package("manipulator_cell")
    pid = package_id("manipulator_cell")
    held = answers({pid: records})
    for snapshot in kind(records, "configuration_snapshot"):
        rid = str(snapshot["id"])
        member = subject(anchored("configuration", snapshot), pid)
        held[rid] = ThreadsOf(rid, "found", (member,), ())
    threaded = StubLedger({pid: (1, records)}, "1.7.0", held)
    assert consolidate(threaded).claims == consolidate(catalog({pid: records})).claims


# --- Every other pin kind, unchanged -------------------------------------------------------------


def test_calibration_and_hardware_pins_resolve_through_their_ledger_threads() -> None:
    """A manipulator cell's run pins a hand-eye calibration and a hardware configuration beside its
    parameter document: three kinds, three configurations, each through its own thread."""
    controller, controller_id = civil_domain("cell controller clock")
    shift = run(
        "cell-shift.bag",
        LogicalId("cell.run", "C3/2026-09-30"),
        Timestamp(1_790_000_000, controller_id),
        Timestamp(1_790_003_599, controller_id),
    )
    arm = LogicalId("robot.serial", "20415")
    hand_eye = calibration("hand_eye.yaml", machine=arm, subject="wrist_camera")
    urdf = hardware("cell.urdf", arm)
    _, _, document = _manipulator()
    pins = [binding(f"pin-{i}", shift, s) for i, s in enumerate((hand_eye, urdf, document))]
    records = [controller, shift, hand_eye, urdf, document, *pins]
    result = consolidate(catalog({package_id("cell"): records}))
    active = of(result, "configuration_active_during")
    assert {c.object for c in active} == {
        configuration_of(hand_eye),
        configuration_of(urdf),
        configuration_of(document),
    }
    assert {c.subject for c in active} == {NodeRef(NodeType.RUN, "cell.run:C3/2026-09-30")}
    assert of(result, "configuration_candidate") == []


# --- What cannot be resolved stays unknown ------------------------------------------------------


def _manipulator() -> tuple[list[Record], Record, Record]:
    records = package("manipulator_cell")
    (pin,) = kind(records, "snapshot_binding")
    return records, pin, by_id(records)[pin["snapshot"]]


def _aerial() -> tuple[list[Record], Record, Record]:
    records = package("aerial_survey")
    (pin,) = kind(records, "snapshot_binding")
    return records, pin, by_id(records)[pin["snapshot"]]


def _unknown_citing(result: Consolidation, pin: Record) -> Claim:
    assert of(result, "configuration_active_during") == []
    assert of(result, "configuration_candidate") == []
    (unknown,) = of(result, "configuration_unknown")
    assert unknown.object == LedgerRecordRef(pin["id"])  # type: ignore[arg-type]
    assert pin["id"] in unknown.provenance.records
    return unknown


def test_a_pinned_snapshot_the_ledger_does_not_hold_stays_unknown() -> None:
    records, pin, snapshot = _manipulator()
    kept = [r for r in records if r is not snapshot]
    result = consolidate(catalog({package_id("manipulator_cell"): kept}))
    _unknown_citing(result, pin)
    (finding,) = [f for f in result.findings if f.code == "configuration.dangling_binding"]
    assert finding.records == (pin["id"],)
    assert finding.details["snapshot"] == snapshot["id"]


def test_a_pinned_snapshot_the_catalog_answers_unknown_record_stays_unknown() -> None:
    records, pin, snapshot = _manipulator()
    ledger = catalog({package_id("manipulator_cell"): records}, without=[str(snapshot["id"])])
    result = consolidate(ledger)
    _unknown_citing(result, pin)
    (finding,) = [f for f in result.findings if f.code == "configuration.uncatalogued_record"]
    assert set(finding.records) == {pin["id"], snapshot["id"]}
    assert "configuration.unthreaded_id" not in codes(result)


def test_a_pin_naming_a_record_of_another_kind_stays_unknown() -> None:
    """The binding says ``configuration_snapshot``; the Ledger holds that id as something else."""
    records, pin, snapshot = _manipulator()
    impostor = {**hardware("cell.urdf"), "id": snapshot["id"]}
    swapped = [impostor if r is snapshot else r for r in records]
    result = consolidate(catalog({package_id("manipulator_cell"): swapped}))
    _unknown_citing(result, pin)
    (finding,) = [f for f in result.findings if f.code == "configuration.dangling_binding"]
    assert finding.details["held_as"] == "hardware_configuration"


def test_two_pins_naming_different_documents_for_one_run_are_candidates() -> None:
    """The run sheet pins two parameter documents to one run over the whole run: which one it ran
    with is ``Ambiguous``, so each is a candidate and neither is active."""
    records, pin, snapshot = _manipulator()
    _, _, other = _aerial()
    shift = by_id(records)[pin["run"]]
    second = binding("second-pin", shift, other)
    result = consolidate(catalog({package_id("manipulator_cell"): [*records, other, second]}))
    assert of(result, "configuration_active_during") == []
    candidates = of(result, "configuration_candidate")
    assert {c.object for c in candidates} == {configuration_of(snapshot), configuration_of(other)}
    (overlap,) = [f for f in result.findings if f.code == "configuration.binding_overlap"]
    assert set(overlap.records) == {pin["id"], second["id"]}


# --- Determinism ----------------------------------------------------------------------------------


def _all() -> dict[str, list[Record]]:
    return {package_id(name): package(name) for name in EMBODIMENTS}


def _reversed(packages: Mapping[str, Sequence[Record]]) -> dict[str, list[Record]]:
    return {pid: list(reversed(packages[pid])) for pid in reversed(list(packages))}


def test_the_same_pins_give_the_same_claims_whatever_the_order() -> None:
    first = consolidate(catalog(_all()))
    again = consolidate(catalog(_all()))
    shuffled = consolidate(catalog(_reversed(_all())))
    assert first.claims == again.claims == shuffled.claims
    assert codes(first) == codes(shuffled)
    assert len(of(first, "configuration_active_during")) == 4
