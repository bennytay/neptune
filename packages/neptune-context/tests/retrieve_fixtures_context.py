"""Fixtures for the graph channel and the local engine (ADR 0007): a two-site graph and a Ledger.

The graph is built here with Memory's own claim types (so ids, provenance and supersession are
Memory's), over four transactions and four embodiments:

- site B (``site-code:S-007``): the lift AMR ``AMR-07`` with its configuration chain, a March run,
  an e-stop event in aisle 3, an identity link and an inferred identity candidate, a reading on
  its own device clock joined to UTC by a stated clock mapping, a renamed display name
  (superseded at transaction 3), a value newer than the graph-schema pin, a resolver finding;
  a second AMR and an inspection drone at the same site;
- site A (``site-code:PLANT-2``): the arm ``ARM-3A`` in cell 3 and the legged robot ``LEG-01``.

``FakeCatalog`` is a Ledger ``CatalogApi`` over a few stream and image rows: it applies a spec's
kinds, window and frame the way ADR 0016 says, and records every spec it was asked, so a test
can assert what was pushed down. Ids are invented; times are UTC nanoseconds.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from neptune_ledger.api import (
    LineageGraph,
    QueryMeta,
    QueryRow,
    QuerySpec,
    Resolution,
    from_json,
    query_table,
)
from neptune_memory.schema.claim import (
    Claim,
    ClaimProvenance,
    ModelRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.clock_map import ClockMap, MapMethod
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import OPEN, CivilClock, LedgerTx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import (
    FindingCode,
    FindingProvenance,
    ResolutionFinding,
)
from neptune_memory.schema.supersede import (
    Resolution as History,
)

from neptune.identity.hashing import content_id
from neptune.identity.ids import config_hash
from neptune.model.alignment import ClockAnchor
from neptune.model.ids import ConfigHash, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
from neptune.model.provenance import evidence_ref_from_json
from neptune.model.time import Duration, Epoch, Timescale, Timestamp

if TYPE_CHECKING:
    from neptune.model.provenance import EvidenceRef

ROOT: Final = Path(__file__).resolve().parents[3]
CATALOG_GOLDEN: Final = ROOT / "contracts" / "catalog-api" / "v1.7.0" / "golden"
UTC_NS: Final = CivilClock(Timescale.UTC, Epoch.UNIX, Fraction(1, 1_000_000_000))
UTC: Final = str(UTC_NS.domain_id)
HOUR: Final = 3600 * 10**9


def ns(year: int, month: int, day: int) -> int:
    """Midnight UTC of a date, in nanoseconds since the Unix epoch."""
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp()) * 10**9  # noqa: UP017


def rec(name: str) -> RecordId:
    return RecordId("rec:sha256:" + content_id(name.encode()).removeprefix("sha256:"))


def source(name: str) -> str:
    return content_id(name.encode())


FEB_1, MAR_1, MAR_10, MAR_15, MAR_20 = (
    ns(2026, 2, 1),
    ns(2026, 3, 1),
    ns(2026, 3, 10),
    ns(2026, 3, 15),
    ns(2026, 3, 20),
)
APR_1, APR_2 = ns(2026, 4, 1), ns(2026, 4, 2)
DEVICE: Final = str(rec("clock:AMR-07 controller"))  # the AMR's own controller clock
MAPPING: Final = str(rec("clock_mapping:AMR-07 controller to UTC"))
UNKNOWN_MAPPING: Final = str(rec("clock_mapping:never ingested"))
BAG: Final = source("amr-07_2026-03-15.mcap")
REGISTER: Final = source("asset_register.csv")
MANUAL: Final = source("AMR-07 service log.pdf")
PACKAGE: Final = source("package:S-007 2026-03")
RUN_MARCH: Final = rec("run:amr-07 2026-03-15")
RUN_APRIL: Final = rec("run:amr-07 2026-04-02")
EVENT: Final = rec("incident:INC-0007")
STREAM: Final = rec("stream:amr-07 2026-03-15 /scan")
OTHER_STREAM: Final = rec("stream:amr-08 2026-03-15 /scan")
IMAGE: Final = rec("image:aisle-3 camera 2026-03-15")
TRANSFORM: Final = rec("transform:mcap 1.0.0")
FRAME_GRAPH: Final = str(rec("frame_graph:S-007 map"))
CONFIG: Final = ConfigHash(content_id(b"fixture consolidator config"))
RESOLVER_CONFIG: Final = {"fixture": "context MVL-144 two-site graph"}
GENERATION: Final = config_hash(RESOLVER_CONFIG)
MODEL: Final = ModelRef("fixture-matcher", "0.3")
HEAD: Final = LedgerTx(4)


def node(kind: NodeType, node_id: str) -> NodeRef:
    return NodeRef(kind, node_id)


SITE_B = node(NodeType.SITE, "site-code:S-007")
SITE_A = node(NodeType.SITE, "site-code:PLANT-2")
AISLE = node(NodeType.ZONE, "zone-code:AISLE-3")
CELL = node(NodeType.ZONE, "zone-code:CELL-3")
AMR = node(NodeType.MACHINE, "asset-tag:AMR-07")
AMR_FLEET_ID = node(NodeType.MACHINE, "fleet-id:amr-7")
AMR_70 = node(NodeType.MACHINE, "asset-tag:AMR-70")
AMR_8 = node(NodeType.MACHINE, "asset-tag:AMR-08")
UAV = node(NodeType.MACHINE, "asset-tag:UAV-5")
ARM = node(NodeType.MACHINE, "asset-tag:ARM-3A")
LEGGED = node(NodeType.MACHINE, "asset-tag:LEG-01")
CFG_A = node(NodeType.CONFIGURATION, "cfg:AMR-07-A")
CFG_B = node(NodeType.CONFIGURATION, "cfg:AMR-07-B")
CFG_C = node(NodeType.CONFIGURATION, "cfg:AMR-07-C")
RUN_3 = node(NodeType.RUN, f"record:{RUN_MARCH}")
RUN_4 = node(NodeType.RUN, f"record:{RUN_APRIL}")
INCIDENT = node(NodeType.EVENT, f"record:{EVENT}")
DEVICE_CLOCK = node(NodeType.CLOCK, DEVICE)


def ref(src: str, row: int) -> EvidenceRef:
    return evidence_ref_from_json({"locator": [{"kind": "row", "row": row}], "source": src})


def claim(
    subject: NodeRef,
    predicate: str,
    obj: Any,
    start: int,
    end: int | None = None,
    *,
    clock: str = UTC,
    tx: int = 1,
    evidence: tuple[EvidenceRef, ...] | None = None,
    records: tuple[RecordId, ...] = (),
    inferred: float | None = None,
    superseded_at: int | None = None,
    supersedes: tuple[str, ...] = (),
    kind: AssertionKind = AssertionKind.STATED,
) -> Claim:
    if isinstance(obj, str):
        obj = TypedLiteral(ValueType.TEXT, obj)
    return Claim(
        subject=subject,
        predicate=predicate,
        object=obj,
        valid_from=Timestamp(start, clock),  # type: ignore[arg-type]
        valid_to=OPEN if end is None else Timestamp(end, clock),  # type: ignore[arg-type]
        recorded_at=LedgerTx(tx),
        assertion_kind="inferred" if inferred is not None else kind,
        confidence=Known(inferred) if inferred is not None else NotApplicable(),
        provenance=ClaimProvenance(
            evidence=evidence or (ref(REGISTER, len(predicate)),),
            records=tuple(sorted(records)),
            consolidator_id="fixture.model" if inferred is not None else "fixture.consolidator",
            consolidator_version="1",
            config_hash=CONFIG,
            model=MODEL if inferred is not None else None,
        ),
        superseded_at=OPEN if superseded_at is None else LedgerTx(superseded_at),
        supersedes=tuple(sorted(supersedes)),  # type: ignore[arg-type]
    )


def _clock_map() -> ClockMap:
    anchor = ClockAnchor(Timestamp(0, DEVICE), Timestamp(MAR_1, UTC))  # type: ignore[arg-type]
    return ClockMap(
        target=UTC,  # type: ignore[arg-type]
        method=MapMethod.STATED,
        anchor=Known(anchor),
        rate=Known(Fraction(1)),
        residual_bound=Known(Duration(0, UTC)),  # type: ignore[arg-type]
    )


def claims() -> list[Claim]:
    old_name = claim(AMR, "has_name", "AMR seven", FEB_1, superseded_at=3)
    out = [
        # Sites, zones and where everything is.
        claim(AISLE, "zone_of", SITE_B, FEB_1),
        claim(CELL, "zone_of", SITE_A, FEB_1),
        claim(AMR, "located_at", SITE_B, FEB_1),
        claim(AMR_8, "located_at", SITE_B, FEB_1),
        claim(UAV, "located_at", SITE_B, FEB_1),
        claim(ARM, "located_at", CELL, FEB_1),
        claim(LEGGED, "located_at", SITE_A, FEB_1),
        # AMR-07's configuration chain: A until March, B from 1 March, C from 20 March.
        claim(AMR, "has_configuration", CFG_A, ns(2026, 1, 1), MAR_1),
        claim(AMR, "has_configuration", CFG_B, MAR_1, MAR_20),
        claim(AMR, "has_configuration", CFG_C, MAR_20),
        claim(CFG_C, "succeeds", CFG_B, MAR_20),
        # Its March run (cites the bag) and April run.
        claim(
            RUN_3,
            "recorded_by",
            AMR,
            MAR_15,
            MAR_15 + HOUR,
            kind=AssertionKind.OBSERVED,
            evidence=(ref(BAG, 1),),
            records=(RUN_MARCH,),
        ),
        claim(RUN_3, "at_site", SITE_B, MAR_15, MAR_15 + HOUR, evidence=(ref(BAG, 2),)),
        claim(RUN_4, "recorded_by", AMR, APR_2, APR_2 + HOUR, kind=AssertionKind.OBSERVED),
        claim(RUN_3, "configuration_candidate", CFG_B, MAR_15, MAR_15 + HOUR, inferred=0.7),
        # The e-stop in aisle 3 during the March run.
        claim(INCIDENT, "involves", AMR, MAR_15 + HOUR // 2, MAR_15 + HOUR // 2 + 10**9),
        claim(INCIDENT, "in_zone", AISLE, MAR_15 + HOUR // 2, MAR_15 + HOUR // 2 + 10**9),
        claim(
            INCIDENT,
            "has_description",
            "E-stop: obstacle in aisle 3",
            MAR_15 + HOUR // 2,
            MAR_15 + HOUR // 2 + 10**9,
            records=(EVENT,),
        ),
        # Identity: a declared link (followed) and an inferred candidate (never followed).
        claim(AMR, "same_as", AMR_FLEET_ID, FEB_1),
        claim(AMR_FLEET_ID, "maintenance_state", "serviced", MAR_10, evidence=(ref(MANUAL, 3),)),
        claim(AMR, "same_as_candidate", AMR_70, FEB_1, inferred=0.4),
        claim(AMR_70, "maintenance_state", "scrapped", MAR_10),
        # A reading on the AMR's own controller clock, and the stated map onto UTC.
        claim(AMR, "maintenance_state", "brake check due", 100, clock=DEVICE),
        claim(
            DEVICE_CLOCK,
            "clock_map",
            TypedLiteral(ValueType.CLOCK_MAP, _clock_map()),
            0,
            clock=DEVICE,
            records=(RecordId(MAPPING),),
        ),
        # A value newer than the graph-schema pin (a predicate Memory added later).
        claim(AMR, "drift", "lidar yaw 0.4 deg", MAR_10),
        # A renamed display name: the old version is superseded at transaction 3.
        old_name,
        claim(AMR, "has_name", "AMR-07 Lift", FEB_1, tx=3, supersedes=(old_name.id,)),
        # Site A's own history (never reached from site B).
        claim(ARM, "has_configuration", node(NodeType.CONFIGURATION, "cfg:cfg-c3-1.5"), MAR_1),
        claim(LEGGED, "has_name", "Patrol dog", FEB_1),
    ]
    return out


def finding(all_claims: list[Claim]) -> ResolutionFinding:
    """A clock mismatch between the device-clock reading and the serviced state."""
    by = {(c.predicate, getattr(c.object, "value", None)): c.id for c in all_claims}
    return ResolutionFinding(
        code=FindingCode.CLOCK_MISMATCH,
        claim=by[("maintenance_state", "brake check due")],
        others=(by[("maintenance_state", "serviced")],),
        provenance=FindingProvenance("memory.supersede", "1", GENERATION),
        recorded_at=LedgerTx(1),
    )


def document() -> GraphDocument:
    all_claims = sorted(claims(), key=lambda c: (c.recorded_at, c.id))
    return GraphDocument(History(tuple(all_claims), (finding(all_claims),)), RESOLVER_CONFIG, HEAD)


def reader() -> ReferenceReader:
    return ReferenceReader(document())


# --- A Ledger catalog over a few rows -------------------------------------------------------


def _golden(name: str) -> Any:
    return json.loads((CATALOG_GOLDEN / name).read_bytes())


def row(record: RecordId, kind: str, src: str, first: int, last: int, line: int = 1) -> QueryRow:
    return QueryRow(
        kind=kind,
        record_id=record,
        package_id=PACKAGE,
        line=line,
        registration_seq=1,
        transform_id=TRANSFORM,
        source_content_id=src,
        source_locator=json.dumps([{"kind": "row", "row": line}], separators=(",", ":")),
        assertion_kind="observed",
        world_clock=UTC,
        world_first=first,
        world_last=last,
    )


ROWS: Final = (
    row(STREAM, "stream", BAG, MAR_15, MAR_15 + HOUR - 1, 1),
    row(OTHER_STREAM, "stream", source("amr-08 bag"), MAR_15, MAR_15 + HOUR - 1, 2),
    row(IMAGE, "image", BAG, MAR_15 + HOUR // 2, MAR_15 + HOUR // 2, 3),
)
IN_FRAME: Final = frozenset({IMAGE})  # what the frame index finds in any box


class FakeCatalog:
    """A ``CatalogApi`` over ``ROWS``: ``query``, ``lineage`` and ``resolve``; it records specs."""

    def __init__(
        self,
        rows: tuple[QueryRow, ...] = ROWS,
        *,
        findings: tuple[Any, ...] = (),
        head: int = 4,
    ) -> None:
        self.rows = rows
        self.findings = findings
        self.specs: list[QuerySpec] = []
        golden = _golden("query_meta.json")
        golden["as_of"]["value"]["tx_seq"] = head
        self.meta: QueryMeta = from_json(QueryMeta, golden)

    def query(self, spec: QuerySpec) -> Any:
        self.specs.append(spec)
        kept = []
        for r in self.rows:
            if r.kind not in spec.kinds:
                continue
            if spec.window is not None and (
                r.world_clock != spec.window.clock
                or r.world_first is None
                or r.world_last is None
                or r.world_last < spec.window.first
                or r.world_first > spec.window.last
            ):
                continue
            if spec.frame is not None and r.record_id not in IN_FRAME:
                continue
            kept.append(r)
        return query_table(kept, dataclasses.replace(self.meta, findings=self.findings))

    def lineage(self, record_id: str, *, as_of: int | None = None) -> LineageGraph:
        golden = from_json(LineageGraph, _golden("drone.lineage.json"))
        nodes = tuple(dataclasses.replace(n, transform_id=TRANSFORM) for n in golden.nodes)
        return dataclasses.replace(golden, record_id=record_id, nodes=nodes)

    def resolve(self, evidence_ref: Any, *, as_of: int | None = None) -> Resolution:
        golden = from_json(Resolution, _golden("drone.resolution.json"))
        return dataclasses.replace(golden, evidence_ref=evidence_ref)

    def register(self, package_root: Any) -> Any:
        raise NotImplementedError

    def verify(self, package_id: str, *, as_of: int | None = None) -> Any:
        raise NotImplementedError

    def thread(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def threads_of(self, record_id: str, *, as_of: int | None = None) -> Any:
        raise NotImplementedError


# --- The Demo v1 corpus snapshot -------------------------------------------------------------

DEMO_SNAPSHOT: Final = (
    ROOT
    / "packages"
    / "neptune-deploy"
    / "tests"
    / "fixtures"
    / "packs"
    / "acceptance_corpus.graph.json"
)


def demo_document() -> GraphDocument:
    """Deploy's frozen Memory graph of the acceptance corpus (Platform ADR 0007; Deploy ADR 0014),
    with each claim id recomputed under Memory's claim-id scheme and the generation recomputed
    from its resolver configuration, then read by Memory's strict codec.

    The snapshot is hand-written in graph-schema's shape (Memory cannot build it from the corpus
    yet): its stored ids follow another scheme and some provenance record lists are unsorted, so
    the ids are recomputed and the record lists sorted; every other part of every claim is used as
    written. Calibration-drift predicates in it are newer than graph-schema 1.6.0.
    """
    from neptune_memory.schema.claim import CLAIM_ID_SCHEME
    from neptune_memory.schema.codec import graph_from_json

    from neptune.identity.canonical_json import dumps

    data = json.loads(DEMO_SNAPSHOT.read_bytes())
    # The two ``drift`` claims hold a ``delta`` value (graph-schema 1.7.0, unmerged); Memory's codec
    # at this commit cannot read them at all, so they are left out. The snapshot's other values
    # newer than Context's pin (``calibrated_with``, ``calibrated_by``) stay and must surface as
    # gaps.
    data["claims"] = [c for c in data["claims"] if c["object"].get("datatype") != "delta"]
    keys = ("assertion_kind", "confidence", "object", "predicate", "provenance", "subject", "valid")
    renamed: dict[str, str] = {}
    for item in data["claims"]:
        provenance = item["provenance"]
        provenance["records"] = sorted(set(provenance["records"]))  # Memory: unique and sorted
        payload: Any = {"claim": {k: item[k] for k in keys}, "scheme": CLAIM_ID_SCHEME}
        renamed[item["id"]] = "claim:" + content_id(dumps(payload))
    for item in data["claims"]:
        item["id"] = renamed[item["id"]]
        item["supersedes"] = sorted(renamed[i] for i in item["supersedes"])
    data["claims"].sort(key=lambda c: (c["recorded_at"], c["id"]))
    data["generation"] = config_hash(data["resolver_config"])
    return graph_from_json(data)
