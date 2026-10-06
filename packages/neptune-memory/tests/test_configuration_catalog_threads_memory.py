"""Configuration lineage over the Ledger catalog's thread membership, with no stand-ins (ADR 0018).

The catalog holds lifecycle records in no thread (Ledger ADR 0003 §2), so the ids they declare
name nodes of their own once the Ledger holds the record; a record the Ledger does not hold places
nothing. Runs and snapshots name the nodes of the threads the catalog says they open. Two
embodiments: the compiler's warehouse AMR and manipulator-cell worked examples.
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import TYPE_CHECKING

from memory_catalog_threads import anchored, catalog, package_id, thread_id
from memory_configuration_records import (
    binding,
    change,
    envelope,
    hardware,
    maintenance,
    run,
    worked_example,
)
from memory_identity_records import civil_domain
from neptune.model.ids import LogicalId
from neptune.model.run import run_from_json
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.configuration import ConfigurationLineageConsolidator
from neptune_memory.consolidate.runs import run_node
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune_memory.schema.claim import Claim

Record = dict[str, object]
TX = ledger_tx(3)
SECONDS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
SITE_CLOCK, SITE_CLOCK_ID = civil_domain("site forms")
AMR = LogicalId("fleet", "AMR-07")
AMR_NODE = NodeRef(NodeType.MACHINE, "fleet:AMR-07")
R3, R4, R5 = (LogicalId("siteops.configuration", f"CFG-AMR07-r{n}") for n in (3, 4, 5))
ARM = LogicalId("robot.serial", "20415")
ARM_NODE = NodeRef(NodeType.MACHINE, "robot.serial:20415")


def posix(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


def stated(text: str) -> Timestamp:
    return Timestamp(posix(text), SITE_CLOCK_ID)


def civil(text: str) -> Timestamp:
    return SECONDS.at(posix(text))


def consolidate(ledger: StubLedger) -> Consolidation:
    result = run_consolidator(ConfigurationLineageConsolidator(), ledger, (), {}, recorded_at=TX)
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    return result


def packages(*records: Sequence[Record]) -> dict[str, list[Record]]:
    return {package_id(f"package-{i}"): list(r) for i, r in enumerate(records)}


def of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def chain(result: Consolidation, machine: NodeRef) -> list[tuple[str, Timestamp, object]]:
    spans = sorted(
        (c for c in of(result, "has_configuration") if c.subject == machine),
        key=lambda c: c.valid_from.ticks,
    )
    return [(c.object.node_id, c.valid_from, c.valid_to) for c in spans]  # type: ignore[union-attr]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def warehouse() -> list[Record]:
    """The warehouse worked example and a firmware change: lifecycle records only, no threads."""
    return [
        *worked_example("warehouse_amr"),
        SITE_CLOCK,
        change("CHG-0040 firmware V01.04.00", [AMR], R5, stated("2026-10-01T06:00:00+10:00")),
    ]


def test_lifecycle_records_the_ledger_holds_in_no_thread_still_make_the_chain() -> None:
    result = consolidate(catalog(packages(warehouse())))
    commissioned = civil("2026-09-21T09:00:00+10:00")
    map_change = civil("2026-09-27T06:00:00+10:00")
    firmware = civil("2026-10-01T06:00:00+10:00")
    assert chain(result, AMR_NODE) == [
        ("siteops.configuration:CFG-AMR07-r3", commissioned, map_change),
        ("siteops.configuration:CFG-AMR07-r4", map_change, firmware),
        ("siteops.configuration:CFG-AMR07-r5", firmware, OPEN),
    ]
    assert {(c.subject.node_id, c.object.node_id) for c in of(result, "succeeds")} == {  # type: ignore[union-attr]
        ("siteops.configuration:CFG-AMR07-r4", "siteops.configuration:CFG-AMR07-r3"),
        ("siteops.configuration:CFG-AMR07-r5", "siteops.configuration:CFG-AMR07-r4"),
    }
    (authorised,) = of(result, "authorised_configuration")
    assert authorised.subject == NodeRef(NodeType.SITE, "siteops.site:S-007")
    assert all(c.assertion_kind == "stated" for c in result.claims)
    assert "configuration.unthreaded_id" not in codes(result)


def test_the_stand_in_path_is_unchanged_when_the_ledger_answers_no_thread_queries() -> None:
    plain = StubLedger({pid: (1, r) for pid, r in packages(warehouse()).items()})
    result = consolidate(plain)
    assert of(result, "has_configuration") == []
    assert "configuration.unthreaded_id" in codes(result)


def test_a_record_the_ledger_does_not_hold_places_nothing_and_says_so() -> None:
    records = warehouse()
    latest = records[-1]
    result = consolidate(catalog(packages(records), without=[str(latest["id"])]))
    spans = chain(result, AMR_NODE)
    assert [node for node, _, _ in spans] == [
        "siteops.configuration:CFG-AMR07-r3",
        "siteops.configuration:CFG-AMR07-r4",
    ]
    assert spans[-1][2] == OPEN  # r5 is not known to the Ledger, so r4 is never closed by it
    uncatalogued = [f for f in result.findings if f.code == "configuration.uncatalogued_record"]
    assert uncatalogued and all(f.records == (latest["id"],) for f in uncatalogued)


def test_a_declared_id_in_a_reserved_namespace_names_no_node() -> None:
    forged = LogicalId("thread", "sha256:" + "0" * 64)
    record = maintenance("WO forged", [forged], R3, stated("2026-09-30T08:00:00+10:00"))
    result = consolidate(catalog(packages([SITE_CLOCK, record])))
    assert result.claims == ()
    assert codes(result) == ["configuration.unthreaded_id"]


def test_manipulator_cell_chain_from_the_catalog() -> None:
    """The arm cell's commissioning, joint repair and requalification, no stand-in threads."""
    result = consolidate(catalog(packages(worked_example("manipulator_cell"))))
    assert [node for node, _, _ in chain(result, ARM_NODE)] == [
        "plant.configuration:CELL3-CFG-A",
        "plant.configuration:CELL3-CFG-A.1",
    ]
    (succession,) = of(result, "succeeds")
    assert succession.object == NodeRef(NodeType.CONFIGURATION, "plant.configuration:CELL3-CFG-A")


# --- Runs and bindings --------------------------------------------------------------------------


def _runs() -> tuple[list[Record], dict[str, Record]]:
    controller, controller_id = civil_domain("AMR-07 controller clock")

    def at(text: str) -> Timestamp:
        return Timestamp(posix(text), controller_id)

    shifts = {
        "declared": run(
            "declared.bag",
            LogicalId("fleet.run", "AMR-07/2026-09-28"),
            at("2026-09-28T10:00:00+10:00"),
            at("2026-09-28T10:59:59+10:00"),
        ),
        "anchored": run(
            "anchored.bag", None, at("2026-09-29T10:00:00+10:00"), at("2026-09-29T10:59:59+10:00")
        ),
        "untimed": run("untimed.bag"),
    }
    snapshot = hardware("CFG-AMR07-r4.yaml", AMR)
    bound = binding(
        "09-28", shifts["declared"], snapshot, start="open", end="open", clock=controller_id
    )
    return [*warehouse(), controller, *shifts.values(), snapshot, bound], {
        **shifts,
        "snapshot": snapshot,
        "binding": bound,
    }


def test_runs_name_the_node_of_the_thread_the_catalog_says_they_open() -> None:
    records, named = _runs()
    result = consolidate(catalog(packages(records)))
    (unknown,) = of(result, "configuration_unknown")
    anchored_run = run_from_json(named["anchored"])  # type: ignore[arg-type]
    assert unknown.subject == run_node(anchored_run)  # the runs consolidator's node
    assert unknown.subject.node_id == f"record:{named['anchored']['id']}"
    assert unknown.object == LedgerRecordRef(named["anchored"]["id"])  # type: ignore[arg-type]
    (active,) = of(result, "configuration_active_during")
    assert active.subject == NodeRef(NodeType.RUN, "fleet.run:AMR-07/2026-09-28")
    snapshot_thread = thread_id(anchored("configuration", named["snapshot"]))
    assert active.object == NodeRef(NodeType.CONFIGURATION, f"thread:{snapshot_thread}")
    # A run that states no first instant has no stand-in start to fall back on: not placed.
    untimed = [f for f in result.findings if named["untimed"]["id"] in f.records]
    assert [f.code for f in untimed] == ["configuration.untimeable_window"]


def test_coverage_of_a_configuration_known_only_by_its_anchor_is_undecided() -> None:
    """The envelope names CFG-AMR07-r3 by its declared id; the bound snapshot is keyed by its
    evidence. Whether they are one configuration is not stated, so nothing is claimed."""
    records, named = _runs()
    result = consolidate(catalog(packages(records)))
    assert of(result, "not_covered_by_authorisation") == []
    undecided = [f for f in result.findings if f.code == "configuration.authorisation_undecided"]
    assert len(undecided) == 1 and named["binding"]["id"] in undecided[0].records
    assert "evidence anchor" in undecided[0].message


def test_with_no_envelope_at_all_an_anchored_configuration_is_surely_uncovered() -> None:
    records, named = _runs()
    kept = [r for r in records if r.get("kind") != "authorisation_envelope"]
    (uncovered,) = of(consolidate(catalog(packages(kept))), "not_covered_by_authorisation")
    assert uncovered.subject == NodeRef(NodeType.RUN, "fleet.run:AMR-07/2026-09-28")
    assert uncovered.assertion_kind == "observed"
    assert named["binding"]["id"] in uncovered.provenance.records


def test_an_envelope_naming_a_declared_configuration_still_places_on_its_site() -> None:
    record = envelope(
        "ENV-AMR07-r5",
        LogicalId("siteops.site", "S-007"),
        R5,
        stated("2026-10-01T00:00:00+10:00"),
        "open",
    )
    result = consolidate(catalog(packages([SITE_CLOCK, record])))
    (authorised,) = of(result, "authorised_configuration")
    assert authorised.object == NodeRef(
        NodeType.CONFIGURATION, "siteops.configuration:CFG-AMR07-r5"
    )
    assert authorised.valid_to == OPEN


# --- Determinism --------------------------------------------------------------------------------


def test_the_same_catalog_gives_the_same_claims_whatever_the_package_order() -> None:
    records, _ = _runs()
    half = len(records) // 2
    first = consolidate(catalog(packages(records[:half], records[half:])))
    second = consolidate(catalog(dict(reversed(packages(records[:half], records[half:]).items()))))
    again = consolidate(catalog(packages(records[:half], records[half:])))
    assert first.claims == second.claims == again.claims
    assert first.findings == second.findings == again.findings


def test_lineage_siblings_of_one_anchored_run_stay_two_run_nodes() -> None:
    """A parser upgrade gives the same bag a second Run record citing the same evidence; the
    catalog puts both in one anchored thread, the runs consolidator keeps two nodes, and so does
    this one, rather than calling the anchor ambiguous."""
    records, named = _runs()
    sibling = {**named["anchored"], "id": "rec:sha256:" + "a" * 64}
    result = consolidate(catalog(packages(records, [sibling])))
    unknown = {c.subject.node_id for c in of(result, "configuration_unknown")}
    assert unknown == {f"record:{named['anchored']['id']}", f"record:{sibling['id']}"}
    assert "configuration.ambiguous_anchor" not in codes(result)


def test_an_envelope_that_places_nothing_still_leaves_anchored_coverage_undecided() -> None:
    """The only envelope is one the Ledger does not hold: it names no node, yet it may be the
    one that covers the run, so coverage is undecided rather than surely uncovered."""
    records, named = _runs()
    envelopes = [r for r in records if r.get("kind") == "authorisation_envelope"]
    result = consolidate(catalog(packages(records), without=[str(e["id"]) for e in envelopes]))
    assert of(result, "not_covered_by_authorisation") == []
    undecided = [f for f in result.findings if f.code == "configuration.authorisation_undecided"]
    assert [named["binding"]["id"] in f.records for f in undecided] == [True]
