"""G1 scenario 4: a clock mapping revised after claims were made on it (quadruped).

Expected (ADR 0002 §3, ADR 0005 §2, ADR 0007 §4): a claim keeps the clock its record declares,
and a boot-clock claim competing with a civil one is a ``clock_mismatch``, never a comparison. A
claim re-timed through a mapping is a derivative that cites the mapping, and is withdrawn when the
mapping is revised.

Verdict: the ``clock_mismatch`` refusal HOLDS. The revised-mapping handling is a GAP owned by
MVL-130 + MVL-132. No consolidator reads ``clock_alignment`` records today, so nothing in the code
can make a claim through a mapping. The ``Diagnostics`` consolidator below ignores the mapping as
well, so its byte-identical rebuilds show only that a claim's id and valid time come from its own
record, not that a revision is handled. The hostile case is the strict ``xfail`` at the end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from memory_g1_harness import (
    MAR_02_2026,
    Build,
    Record,
    build,
    cite,
    civil,
    draft,
    ledger,
    own_clock,
    reader,
    rid,
    source,
)
from neptune.identity import canonical_json
from neptune.model.ids import parse_record_id
from neptune.model.provenance import evidence_ref_from_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune_memory.consolidate.base import ConsolidatorOutput, ModelRef, rebuild
from neptune_memory.schema.claim import Claim, TypedLiteral, ValueType
from neptune_memory.schema.interval import OPEN, Interval, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import FindingCode

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader

SPOT = NodeRef(NodeType.MACHINE, "spot-serial:BD-0731")
BOOT = own_clock("BD-0731 boot clock")
OVERTEMP = TypedLiteral(ValueType.TEXT, "motor 2 overtemp")
OPERATIONAL = TypedLiteral(ValueType.TEXT, "operational")


def _alignment(name: str, offset: int) -> Record:
    """A boot-clock-to-civil mapping as the compiler's alignment records carry one (MVL-36)."""
    return {
        "kind": "clock_alignment",
        "id": rid("clock_alignment", name),
        "source": BOOT,
        "target": "posix",
        "offset_seconds": offset,
    }


OBSERVATIONS: list[Record] = [
    {
        "kind": "observation",
        "id": rid("diagnostic", "overtemp"),
        "at": Timestamp(5_000, BOOT).to_json(),
        "value": "motor 2 overtemp",
        "evidence": cite(source("spot-0731.bag")).to_json(),
    },
    {
        "kind": "observation",
        "id": rid("maintenance", "ok"),
        "at": civil(MAR_02_2026).to_json(),
        "value": "operational",
        "evidence": cite(source("work-order-88.pdf")).to_json(),
    },
]


@dataclass(frozen=True)
class Diagnostics:
    """Reads observation records and keeps the clock each declares (ADR 0002 §3)."""

    consolidator_id: str = "test.diagnostics"
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self, ledger: LedgerReader, previous: Sequence[Claim], config: Mapping[str, JsonValue]
    ) -> ConsolidatorOutput:
        drafts = []
        for ref in ledger.list_packages():
            for record in ledger.read_records(ref.package_id, "observation") or ():
                drafts.append(
                    draft(
                        SPOT,
                        "maintenance_state",
                        TypedLiteral(ValueType.TEXT, str(record["value"])),
                        timestamp_from_json(cast("JsonValue", record["at"])),
                        records=(parse_record_id(str(record["id"])),),
                        evidence=(evidence_ref_from_json(cast("JsonValue", record["evidence"])),),
                    )
                )
        return ConsolidatorOutput(tuple(drafts))


def _builds() -> list[Claim]:
    """tx 1: observations only. tx 2: a mapping arrives. tx 3: the mapping is revised."""
    snapshots: dict[int, dict[str, list[Record]]] = {
        1: {"obs": OBSERVATIONS},
        2: {"obs": OBSERVATIONS, "align-1": [_alignment("v1", 1_772_404_000)]},
        3: {
            "obs": OBSERVATIONS,
            "align-1": [_alignment("v1", 1_772_404_000)],
            "align-2": [_alignment("v2", 1_772_404_030)],
        },
    }
    claims: list[Claim] = []
    for tx, packages in snapshots.items():
        (build,) = rebuild(ledger(packages), [(Diagnostics(), {})], recorded_at=ledger_tx(tx))
        claims.extend(build.claims)
    return claims


def test_a_claim_keeps_its_declared_clock_and_id_across_rebuilds() -> None:
    """Not evidence about mappings (``Diagnostics`` never reads them): only that a claim's valid
    time is its record's, on its record's clock, and its id is stable across rebuilds."""
    claims = _builds()
    by_tx = {
        tx: sorted(canonical_json.dumps(c.content_json()) for c in claims if c.recorded_at == tx)
        for tx in (1, 2, 3)
    }
    assert by_tx[1] == by_tx[2] == by_tx[3]  # same ids and valid times at every rebuild
    graph = reader(claims, {"test.diagnostics": 0}, head=3)
    snapshots = [graph.claims(SPOT, "maintenance_state", ledger_tx(tx)) for tx in (1, 2, 3)]
    assert snapshots[0].claims == snapshots[1].claims == snapshots[2].claims
    boot = next(c for c in snapshots[2].claims if c.object == OVERTEMP)
    assert boot.valid_from == Timestamp(5_000, BOOT)  # on the boot clock, as declared


def test_the_boot_clock_fact_competes_as_a_mismatch_never_as_a_comparison() -> None:
    graph = reader(_builds(), {"test.diagnostics": 0}, head=3)
    result = graph.claims(SPOT, "maintenance_state", ledger_tx(3), during=Interval(civil(0), OPEN))
    assert [c.object for c in result.claims] == [OPERATIONAL]
    assert [c.object for c in result.other_clocks] == [OVERTEMP]
    (finding,) = result.findings
    assert finding.code is FindingCode.CLOCK_MISMATCH and finding.recorded_at == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="GAP MVL-130 + MVL-132: no mapping derivatives or build withdrawal yet (ADR 0007 §4-§5)",
)
def test_a_derivative_through_a_revised_mapping_is_withdrawn() -> None:
    """A re-timing consolidator cites the mapping it used. When the mapping is revised, its claim
    through the old mapping must stop being current; today it stays current beside the new one."""

    def retimed(tx: int, offset: int, name: str) -> tuple[list[Claim], Build]:
        (run,) = rebuild(
            ledger({"obs": OBSERVATIONS}),
            [(_Retimer(offset, name), {})],
            recorded_at=ledger_tx(tx),
        )
        return list(run.claims), build(run, tx)

    (old, old_build), (new, new_build) = (
        retimed(2, 1_772_404_000, "v1"),
        retimed(3, 1_772_404_030, "v2"),
    )
    graph = reader(old + new, {"test.retime": 0}, head=3, builds=[old_build, new_build])
    current = graph.claims(SPOT, "maintenance_state", ledger_tx(3)).claims
    assert all(rid("clock_alignment", "v1") not in c.provenance.records for c in current)


@dataclass(frozen=True)
class _Retimer:
    offset: int
    alignment: str
    consolidator_id: str = "test.retime"
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self, ledger: LedgerReader, previous: Sequence[Claim], config: Mapping[str, JsonValue]
    ) -> ConsolidatorOutput:
        return ConsolidatorOutput(
            (
                draft(
                    SPOT,
                    "maintenance_state",
                    OVERTEMP,
                    civil(self.offset + 5_000),
                    records=(rid("diagnostic", "overtemp"), rid("clock_alignment", self.alignment)),
                    evidence=(cite(source("spot-0731.bag")),),
                ),
            )
        )
