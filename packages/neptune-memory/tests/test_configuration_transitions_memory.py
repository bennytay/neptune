"""Configuration changes are a machine's own (ADR 0019 §2).

An AMR fleet at one site runs firmware 4.2.0: AMR-05, AMR-06 and AMR-07 share the configuration
node ``firmware:4.2.0`` (ADR 0010 §1 keys nodes by declared id). AMR-07 alone is upgraded to
4.3.1 by two work orders. A ``succeeds`` claim between the shared nodes would read as a fleet-wide
upgrade, so none is claimed: the change is AMR-07's two abutting ``has_configuration`` spans, and
``transitions`` reads it back for AMR-07 only. A legged robot and a manipulator on other firmware
show the same rule on other embodiments, and that nothing is read across a gap, a tie or two
clocks.
"""

from __future__ import annotations

import dataclasses
import random
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest

from memory_configuration_records import change, commissioning, configuration_thread, threads
from memory_identity_records import Record, civil_domain, ledger
from memory_run_records import domain
from neptune.identity import canonical_json
from neptune.model.ids import LogicalId
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.configuration import (
    CONFIGURATION_CONSOLIDATOR_ID,
    HAS_CONFIGURATION,
    ConfigurationLineageConsolidator,
    transitions,
)
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune_memory.schema.claim import Claim

TX: Final = ledger_tx(11)
DAYS: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(86400))
FORMS, FORMS_ID = civil_domain("cmms work orders", Fraction(86400))
BOOT, BOOT_ID = domain("AMR-05 controller boot", civil=False)  # no epoch: never civil
DAY: Final = 20_500

AMR05, AMR06, AMR07 = (LogicalId("fleet", f"AMR-0{n}") for n in (5, 6, 7))
LEG01, ARM3A = LogicalId("fleet", "LEG-01"), LogicalId("fleet", "ARM-3A")
FW420, FW431 = LogicalId("firmware", "4.2.0"), LogicalId("firmware", "4.3.1")
LEG314, LEG320 = LogicalId("firmware", "3.1.4"), LogicalId("firmware", "3.2.0")
CTRL56, CTRL57 = LogicalId("controller", "5.6.0"), LogicalId("controller", "5.7.0")


def node(machine: LogicalId) -> NodeRef:
    return NodeRef(NodeType.MACHINE, f"{machine.namespace}:{machine.value}")


def cfg(configuration: LogicalId) -> NodeRef:
    return NodeRef(NodeType.CONFIGURATION, f"{configuration.namespace}:{configuration.value}")


def day(n: int, clock: str = FORMS_ID) -> Timestamp:
    return Timestamp(DAY + n, clock)  # type: ignore[arg-type]


def fleet(*extra: Record) -> list[Record]:
    return [
        FORMS,
        *threads(NodeType.MACHINE, AMR05, AMR06, AMR07, LEG01, ARM3A),
        *(configuration_thread(c) for c in (FW420, FW431, LEG314, LEG320, CTRL56, CTRL57)),
        commissioning("COM-S007-AMR", [AMR05, AMR06, AMR07], FW420, day(0)),
        change("WO-26-0414 firmware", [AMR07], FW431, day(40)),
        change("WO-26-0415 firmware verify", [AMR07], FW431, day(41)),
        *extra,
    ]


def consolidate(records: Sequence[Record]) -> Consolidation:
    result = run_consolidator(
        ConfigurationLineageConsolidator(), ledger({"p": list(records)}), (), {}, recorded_at=TX
    )
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    return result


def changes(claims: Sequence[Claim], machine: LogicalId) -> list[tuple[str, str, Timestamp]]:
    return [(t.before.node_id, t.after.node_id, t.at) for t in transitions(claims, node(machine))]


def test_a_shared_configuration_node_carries_no_fleet_wide_succession() -> None:
    result = consolidate(fleet())
    assert result.findings == ()
    assert not [c for c in result.claims if c.predicate == "succeeds"]
    # All three machines name the one firmware:4.2.0 node; only AMR-07 leaves it.
    on_420 = {
        c.subject
        for c in result.claims
        if c.predicate == HAS_CONFIGURATION and c.object == cfg(FW420)
    }
    assert on_420 == {node(AMR05), node(AMR06), node(AMR07)}
    assert changes(result.claims, AMR07) == [
        ("firmware:4.2.0", "firmware:4.3.1", DAYS.at(DAY + 40))
    ]
    assert changes(result.claims, AMR05) == []
    assert changes(result.claims, AMR06) == []


def test_the_change_is_read_from_the_machines_own_spans_citing_each_side() -> None:
    records = fleet()
    result = consolidate(records)
    (upgrade,) = transitions(result.claims, node(AMR07))
    before, after = upgrade.claims
    assert before.subject == after.subject == node(AMR07)
    assert (before.object, after.object) == (cfg(FW420), cfg(FW431))
    assert before.valid_to == after.valid_from == upgrade.at
    assert after.valid_to == OPEN
    assert {c.assertion_kind for c in upgrade.claims} == {"stated"}
    work_orders = {str(r["id"]) for r in records[-2:]}
    assert set(after.provenance.records) == work_orders  # the verify order extends the span
    assert not work_orders & set(before.provenance.records)
    # AMR-05's span on the shared node is its own: open, citing only the commissioning.
    (amr05,) = [c for c in result.claims if c.subject == node(AMR05)]
    assert amr05.valid_to == OPEN
    assert set(amr05.provenance.records) == {str(records[-3]["id"])}


def test_other_embodiments_change_on_their_own_chains() -> None:
    result = consolidate(
        fleet(
            commissioning("COM-LEG01", [LEG01], LEG314, day(5)),
            change("WO-26-0913 legged firmware", [LEG01], LEG320, day(50)),
            commissioning("COM-ARM3A", [ARM3A], CTRL56, day(3)),
            change("CHG-ARM3A controller", [ARM3A], CTRL57, day(60)),
        )
    )
    assert changes(result.claims, LEG01) == [
        ("firmware:3.1.4", "firmware:3.2.0", DAYS.at(DAY + 50))
    ]
    assert changes(result.claims, ARM3A) == [
        ("controller:5.6.0", "controller:5.7.0", DAYS.at(DAY + 60))
    ]
    assert changes(result.claims, AMR06) == []


def test_no_change_is_read_across_a_gap_or_a_tie() -> None:
    unknown = fleet(change("WO-26-0420 unknown result", [AMR06], None, day(45)))
    unknown.append(change("WO-26-0421 firmware", [AMR06], FW431, day(46)))
    assert changes(consolidate(unknown).claims, AMR06) == []

    tied = fleet(
        change("WO-26-0430 A", [AMR05], FW431, day(45)),
        change("WO-26-0430 B", [AMR05], LEG320, day(45)),
        change("WO-26-0431 settle", [AMR05], FW431, day(46)),
    )
    assert changes(consolidate(tied).claims, AMR05) == []


def test_two_clocks_are_two_chains_and_no_change_between_them() -> None:
    result = consolidate(
        [
            *fleet(),
            BOOT,
            change("controller service log", [AMR05], FW431, Timestamp(70, BOOT_ID)),
        ]
    )
    assert [f.code for f in result.findings] == ["configuration.clock_split"]
    assert changes(result.claims, AMR05) == []


def test_only_this_consolidators_spans_are_read() -> None:
    result = consolidate(fleet())
    (amr05,) = [c for c in result.claims if c.subject == node(AMR05)]
    # A span another consolidator might state, abutting AMR-05's, is not a change of its chain.
    foreign = type(amr05)(
        subject=amr05.subject,
        predicate=HAS_CONFIGURATION,
        object=cfg(FW431),
        valid_from=DAYS.at(DAY + 90),
        valid_to=OPEN,
        recorded_at=amr05.recorded_at,
        assertion_kind=amr05.assertion_kind,
        confidence=amr05.confidence,
        provenance=type(amr05.provenance)(
            evidence=amr05.provenance.evidence,
            records=amr05.provenance.records,
            consolidator_id="test.other",
            consolidator_version="1",
            config_hash=amr05.provenance.config_hash,
        ),
    )
    closed = type(amr05)(
        subject=amr05.subject,
        predicate=HAS_CONFIGURATION,
        object=amr05.object,
        valid_from=amr05.valid_from,
        valid_to=DAYS.at(DAY + 90),
        recorded_at=amr05.recorded_at,
        assertion_kind=amr05.assertion_kind,
        confidence=amr05.confidence,
        provenance=amr05.provenance,
    )
    assert closed.provenance.consolidator_id == CONFIGURATION_CONSOLIDATOR_ID
    assert transitions([closed, foreign], node(AMR05)) == ()
    ours = dataclasses.replace(foreign, provenance=amr05.provenance)
    (read,) = transitions([closed, ours], node(AMR05))
    assert (read.before, read.after) == (cfg(FW420), cfg(FW431))


def test_a_machine_the_claims_never_name_has_no_changes() -> None:
    result = consolidate(fleet())
    assert transitions(result.claims, NodeRef(NodeType.MACHINE, "fleet:AMR-99")) == ()


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_transitions_are_deterministic_and_independent_of_claim_order(seed: int) -> None:
    result = consolidate(
        fleet(
            change("WO-26-0500 rollback", [AMR07], FW420, day(80)),
            change("WO-26-0501 again", [AMR07], FW431, day(90)),
        )
    )
    shuffled = list(result.claims)
    random.Random(seed).shuffle(shuffled)
    expected = changes(result.claims, AMR07)
    assert changes(shuffled, AMR07) == expected
    assert [(b, a) for b, a, _ in expected] == [
        ("firmware:4.2.0", "firmware:4.3.1"),
        ("firmware:4.3.1", "firmware:4.2.0"),
        ("firmware:4.2.0", "firmware:4.3.1"),
    ]
    again = consolidate(
        fleet(
            change("WO-26-0500 rollback", [AMR07], FW420, day(80)),
            change("WO-26-0501 again", [AMR07], FW431, day(90)),
        )
    )
    assert [canonical_json.dumps(c.to_json()) for c in again.claims] == [
        canonical_json.dumps(c.to_json()) for c in result.claims
    ]
