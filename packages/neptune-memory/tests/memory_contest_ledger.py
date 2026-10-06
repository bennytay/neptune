"""A Ledger whose facts are contested, for incremental-versus-rebuild checks (ADR 0016 §2.4, §5).

The archetype Ledger never contests a ``one`` fact, so it cannot show that incremental
consolidation and a rebuild settle a contest the same way. Here a small deterministic test
consolidator, ``test.locations``, reads ``test_location`` records (a machine is at a site from an
instant, observed or stated) and ``test_retraction`` records (a record no longer stands; a
retraction can itself be retracted), and emits ``located_at`` claims for the locations that stand.

Scenarios, by registration transaction:

- AMR (mobile): tx 1 states ``dock`` from 0 and ``pier`` from 5 (a contest: pier cuts dock at 5);
  tx 2 retracts ``pier`` (a withdrawn winner: dock holds from 0 again); tx 3 retracts that
  retraction (``pier`` is emitted again and restated).
- Humanoid: tx 1 states ``cell-a`` from 0, tx 2 ``cell-b`` from 0, both stated: a full tie.
- Quadruped (legged): ``yard`` from 0 (tx 1), ``kennel`` from 3 (tx 2), ``yard`` from 6
  (tx 3): the later yard claim beats kennel, which beat the earlier yard claim.
- ROV (marine): tx 4 states ``berth`` from 2 (observed) and ``quay`` from 2 (stated) in one
  package: observed and stated rank alike, so a full tie again.

Every claim is on civil seconds. Nothing reads the clock or randomness.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.time import Epoch, Timescale
from neptune_memory.consolidate.base import ClaimDraft, ConsolidatorOutput, ModelRef
from neptune_memory.consolidate.snapshot import Registration
from neptune_memory.schema.interval import CivilClock
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import is_closure

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.supersede import Resolution

Record = dict[str, object]
SECONDS: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))


def location(name: str, machine: str, site: str, start: int, kind: str = "observed") -> Record:
    return {
        "assertion": kind,
        "from": start,
        "id": name,
        "kind": "test_location",
        "machine": machine,
        "site": site,
    }


def retraction(name: str, retracts: str) -> Record:
    return {"id": name, "kind": "test_retraction", "retracts": retracts}


CONTESTED: Final[dict[int, dict[str, list[Record]]]] = {
    1: {
        "amr/day-1": [
            location("amr-dock", "amr-7", "dock", 0),
            location("amr-pier", "amr-7", "pier", 5),
        ],
        "humanoid/shift-1": [location("hum-a", "humanoid-2", "cell-a", 0, "stated")],
        "quadruped/walk-1": [location("spot-yard-0", "spot-1", "yard", 0)],
    },
    2: {
        "amr/ops-1": [retraction("ops-1", "amr-pier")],
        "humanoid/shift-2": [location("hum-b", "humanoid-2", "cell-b", 0, "stated")],
        "quadruped/walk-2": [location("spot-kennel-3", "spot-1", "kennel", 3)],
    },
    3: {
        "amr/ops-2": [retraction("ops-2", "ops-1")],
        "quadruped/walk-3": [location("spot-yard-6", "spot-1", "yard", 6)],
    },
    4: {
        "rov/dive-1": [
            location("rov-berth", "rov-11", "berth", 2),
            location("rov-quay", "rov-11", "quay", 2, "stated"),
        ],
    },
}
HEAD: Final = max(CONTESTED)


def packages_at(snapshot: int) -> dict[str, list[Record]]:
    return {
        pid: records
        for tx, packages in sorted(CONTESTED.items())
        if tx <= snapshot
        for pid, records in packages.items()
    }


def _standing(retractions: Mapping[str, str], name: str) -> bool:
    """A record stands unless a standing retraction names it (retractions chain)."""
    seen: set[str] = set()
    for by, target in sorted(retractions.items()):
        if target == name and by not in seen:
            seen.add(by)
            if _standing(retractions, by):
                return False
    return True


@dataclass(frozen=True)
class Locations:
    """``located_at`` from ``test_location`` records that no standing retraction names."""

    consolidator_id: str = "test.locations"
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self, ledger: LedgerReader, previous: Sequence[Claim], config: Mapping[str, JsonValue]
    ) -> ConsolidatorOutput:
        locations: list[Mapping[str, object]] = []
        retractions: dict[str, str] = {}
        for package in ledger.list_packages():
            locations.extend(ledger.read_records(package.package_id, "test_location") or ())
            for record in ledger.read_records(package.package_id, "test_retraction") or ():
                retractions[str(record["id"])] = str(record["retracts"])
        drafts = []
        for record in sorted(locations, key=lambda r: str(r["id"])):
            name = str(record["id"])
            if not _standing(retractions, name):
                continue
            start = record["from"]
            assert isinstance(start, int)
            drafts.append(
                ClaimDraft(
                    subject=NodeRef(NodeType.MACHINE, f"asset-tag:{record['machine']}"),
                    predicate="located_at",
                    object=NodeRef(NodeType.SITE, f"site:{record['site']}"),
                    valid_from=SECONDS.at(start),
                    assertion_kind=AssertionKind(str(record["assertion"])),
                    evidence=(EvidenceRef(content_id(name.encode()), (ByteRange(0, 64),)),),
                    records=(record_id("test_location", {"name": name}),),
                )
            )
        return ConsolidatorOutput(tuple(drafts))


def registrations() -> tuple[Registration, ...]:
    return (Registration(Locations()),)


Placement = tuple[str, bytes, bytes]


def head_projection(resolution: Resolution) -> list[Placement]:
    """What Memory's first guarantee compares at the head (``docs/guarantees.md``).

    For each current version: the assertion it is a version of (following ``supersedes`` past
    resolver versions), its valid interval and its object, sorted. Version ids, resolver
    provenance and evidence order are not compared: they record how a graph got there.
    """
    history = {c.id: c for c in resolution.claims}
    out: list[Placement] = []
    for version in resolution.claims:
        if not version.is_current:
            continue
        root = version
        while is_closure(root):
            root = history[root.supersedes[0]]
        out.append(
            (
                root.id,
                canonical_json.dumps(version.valid.to_json()),
                canonical_json.dumps(version.object.to_json()),
            )
        )
    return sorted(out)
