"""G1 scenario 4: a clock mapping revised after claims were made on it (quadruped).

Expected (ADR 0002 §3, ADR 0005 §2, ADR 0007 §4, ADR 0011): a claim keeps the clock its record
declares, and a boot-clock claim competing with a civil one is a ``clock_mismatch``, never a
comparison. Nothing is re-timed: a mapping is a claim about clocks with its own validity, and what
rests on it (a chain, a conversion) cites it. When the mapping is revised, the old one and
everything through it end at the revision.

Verdict: the ``clock_mismatch`` refusal HOLDS. The revision HOLDS for every build (MVL-130): the
time-domain registry closes the old mapping at the revision, citing both records, and a conversion
after the revision goes through the new one. Since MVL-132 (ADR 0016), build withdrawal ends the
version emitted open before the revision at the revision's build, so it never stays current
beside the closed one. HOLDS.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Final, cast

from memory_g1_harness import (
    MAR_02_2026,
    SECONDS,
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
from neptune.identity.provenance import transform_record
from neptune.model.alignment import ClockAnchor, ClockMapping, MappingMethod, ValidityWindow
from neptune.model.ids import parse_record_id
from neptune.model.knowledge import AssertionKind, Known, KnownAbsent
from neptune.model.provenance import Provenance, evidence_ref_from_json
from neptune.model.time import Duration, Timestamp, timestamp_from_json
from neptune_memory.consolidate.base import ConsolidatorOutput, ModelRef, rebuild
from neptune_memory.consolidate.time import TimeDomainConsolidator, clock_node
from neptune_memory.schema.claim import Claim, TypedLiteral, ValueType
from neptune_memory.schema.clocks import convert
from neptune_memory.schema.interval import OPEN, Interval, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import FindingCode

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidation, Consolidator
    from neptune_memory.ledger import LedgerReader

SPOT = NodeRef(NodeType.MACHINE, "spot-serial:BD-0731")
BOOT = own_clock("BD-0731 boot clock")
OVERTEMP = TypedLiteral(ValueType.TEXT, "motor 2 overtemp")
OPERATIONAL = TypedLiteral(ValueType.TEXT, "operational")


REVISION: Final = 4_000  # the boot tick of the re-sync
SYNC_LOG: Final = transform_record(adapter_id="g1.sync_log", adapter_version="1", config={})
V1, V2 = rid("clock_mapping", "v1"), rid("clock_mapping", "v2")


def _alignment(name: str, offset: int, start: int = 0) -> Record:
    """The compiler's ``ClockMapping`` from the boot clock onto civil seconds, valid from boot
    tick ``start`` and stated open after: civil = ``offset`` + boot ticks (root ADR 0050 §5)."""
    cited = cite(source(f"spot-0731-sync-{name}.log"))
    return dict(
        ClockMapping(
            id=rid("clock_mapping", name),
            provenance=Provenance(cited, SYNC_LOG.id, AssertionKind.STATED),
            source=BOOT,
            target=SECONDS.domain_id,
            method=MappingMethod.STATED,
            anchor=Known(ClockAnchor(Timestamp(start, BOOT), civil(offset + start))),
            rate=Known(Fraction(1)),
            residual_bound=Known(Duration(1, SECONDS.domain_id)),
            validity=Known(
                ValidityWindow(
                    BOOT,
                    Known(Timestamp(start, BOOT)),
                    KnownAbsent(Provenance(cited, SYNC_LOG.id, AssertionKind.STATED)),
                )
            ),
        ).to_json()
    )


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


# tx 1: observations only. tx 2: a mapping arrives. tx 3: a re-sync revises it from REVISION.
SNAPSHOTS: Final[dict[int, dict[str, list[Record]]]] = {
    1: {"obs": OBSERVATIONS},
    2: {"obs": OBSERVATIONS, "align-1": [_alignment("v1", 1_772_404_000)]},
    3: {
        "obs": OBSERVATIONS,
        "align-1": [_alignment("v1", 1_772_404_000)],
        "align-2": [_alignment("v2", 1_772_404_030, REVISION)],
    },
}


def _runs(consolidator: Consolidator) -> list[tuple[int, Consolidation]]:
    runs: list[tuple[int, Consolidation]] = []
    for tx, packages in SNAPSHOTS.items():
        (run,) = rebuild(ledger(packages), [(consolidator, {})], recorded_at=ledger_tx(tx))
        runs.append((tx, run))
    return runs


def _builds(consolidator: Consolidator | None = None) -> list[Claim]:
    return [c for _, run in _runs(consolidator or Diagnostics()) for c in run.claims]


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


def _mappings(claims: Sequence[Claim]) -> list[Claim]:
    return sorted(
        (c for c in claims if c.predicate == "clock_map" and c.subject == clock_node(BOOT)),
        key=lambda c: (c.recorded_at, c.valid_from.ticks),
    )


def test_a_revision_ends_the_old_mapping_at_the_revision_in_every_later_build() -> None:
    claims = _builds(TimeDomainConsolidator())
    at_2 = [c for c in _mappings(claims) if c.recorded_at == 2]
    at_3 = [c for c in _mappings(claims) if c.recorded_at == 3]
    assert [(c.valid_from.ticks, c.valid_to) for c in at_2] == [(0, OPEN)]
    assert [(c.valid_from.ticks, c.valid_to) for c in at_3] == [
        (0, Timestamp(REVISION, BOOT)),
        (REVISION, OPEN),
    ]
    closed, revised = at_3
    assert closed.provenance.records == tuple(sorted({V1, V2}))  # closed by the revision
    assert revised.provenance.records == (V2,)
    # Converting the diagnostic's instant uses the mapping that holds then, in each build.
    for tx, civil_ticks, cited in ((2, 1_772_409_000, V1), (3, 1_772_409_030, V2)):
        graph = reader([c for c in claims if c.recorded_at == tx], {"memory.time": 0}, head=tx)
        result = convert(graph, 5_000, BOOT, SECONDS.domain_id, ledger_tx(tx)).result
        assert isinstance(result, Known) and result.value.ticks == civil_ticks
        assert [c.provenance.records for c in result.value.path] == [(cited,)]


def test_after_a_revision_no_current_mapping_runs_open_through_the_old_one() -> None:
    """At tx 3 the build states v1 closed at the revision; the version it emitted open at tx 2
    stops being current there, withdrawn by that build (ADR 0016)."""
    runs = _runs(TimeDomainConsolidator())
    claims = [c for _, run in runs for c in run.claims]
    builds = [build(run, tx) for tx, run in runs]
    graph = reader(claims, {"memory.time": 0}, head=3, builds=builds)
    current = graph.claims(clock_node(BOOT), "clock_map", ledger_tx(3)).claims
    through_v1 = [c for c in current if V1 in c.provenance.records]
    assert [c.valid_to for c in through_v1] == [Timestamp(REVISION, BOOT)]
