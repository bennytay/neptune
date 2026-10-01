"""G1 scenario 7: a claim whose evidence package expired under retention (aerial, marine).

Expected (ADR 0002 §2, ADR 0003 §4, ADR 0007 §6): a claim survives the expiry of the bytes it
cites. Memory never reads source bytes (only Ledger records), so expiry cannot change a claim,
its id, or any ``as_of`` answer, and a rebuild from a Ledger that keeps the expired package's
catalog records is byte-identical. The claim's ``EvidenceRef`` stays; what changes is whether its
bytes can still be fetched, which belongs beside the claim, not in it (a claim id must not change
when bytes expire).

Verdict: GAP. Survival HOLDS. Marking the ref unavailable has no signal: the Ledger catalog API
(``LedgerReader``, catalog-api v1) exposes no retention state, so readers cannot say "cited bytes
expired at tx". ADR 0007 §6 defines the shape: a ``Knowledge[EvidenceStatus]`` for every cited
source, joined at read time, which is ``NotCovered`` until the catalog emits a retention signal;
owner MVL-132.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_g1_harness import (
    JUN_01_2025,
    Fixed,
    Record,
    cite,
    civil,
    draft,
    ledger,
    reader,
    rid,
    source,
)
from neptune.identity import canonical_json
from neptune_memory.consolidate.base import Consolidator, rebuild
from neptune_memory.ledger import LedgerReader
from neptune_memory.schema.claim import Claim, LedgerRecordRef
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonValue

DRONE = NodeRef(NodeType.MACHINE, "px4-uuid:0006000000000000a1b2")
AUV = NodeRef(NodeType.MACHINE, "hull-number:AUV-REMUS-4")
FLIGHT_LOG = cite(source("flight-2025-06-01.ulg"), length=48_000_000)
DIVE_LOG = cite(source("dive-2025-06-01.bin"), length=900_000_000)
RECORDS: dict[str, Record] = {
    "flight": {"kind": "run_record", "id": rid("run_record", "flight-0601")},
    "dive": {"kind": "run_record", "id": rid("run_record", "dive-0601")},
}


def _consolidators() -> list[tuple[Consolidator, Mapping[str, JsonValue]]]:
    drafts = (
        draft(
            DRONE,
            "evidenced_by",
            LedgerRecordRef(rid("run_record", "flight-0601")),
            civil(JUN_01_2025),
            records=(rid("run_record", "flight-0601"),),
            evidence=(FLIGHT_LOG,),
        ),
        draft(
            AUV,
            "evidenced_by",
            LedgerRecordRef(rid("run_record", "dive-0601")),
            civil(JUN_01_2025),
            records=(rid("run_record", "dive-0601"),),
            evidence=(DIVE_LOG,),
        ),
    )
    return [(Fixed("test.runs", drafts), {})]


def _build(tx: int) -> list[Claim]:
    # The catalog keeps listing both packages' records after their bytes expire (catalog
    # metadata is not under the bytes' retention): same snapshot, same claims.
    packages = {"flight": [RECORDS["flight"]], "dive": [RECORDS["dive"]]}
    (build,) = rebuild(ledger(packages), _consolidators(), recorded_at=ledger_tx(tx))
    return list(build.claims)


def test_claims_survive_expiry_with_their_evidence_refs_and_ids() -> None:
    before = _build(1)  # tx 1: bytes present
    after = _build(5)  # tx 5: retention expired both packages' bytes at tx 4
    assert [c.content_json() for c in before] == [c.content_json() for c in after]
    graph = reader(before + after, {"test.runs": 0}, head=5)
    for node, ref in ((DRONE, FLIGHT_LOG), (AUV, DIVE_LOG)):
        (claim,) = graph.claims(node, "evidenced_by", ledger_tx(5)).claims
        assert claim.provenance.evidence == (ref,) and claim.recorded_at == 1
        assert graph.claims(node, "evidenced_by", ledger_tx(1)).claims == (claim,)
    first = canonical_json.dumps([c.to_json() for c in before])
    assert canonical_json.dumps([c.to_json() for c in _build(1)]) == first  # rebuild identical


def test_there_is_no_retention_signal_to_mark_a_ref_unavailable_yet() -> None:
    """GAP pin (MVL-132): flip when the Ledger reader exposes evidence status (ADR 0007 §6)."""
    surface = {name for name in dir(LedgerReader) if not name.startswith("_")}
    assert surface == {"catalog_api_version", "list_packages", "read_records"}
