"""Fixtures for why and diff (ADR 0010): a resolved history across seven embodiments.

The history is resolved by Memory's own ``resolve`` (so split closures, ``supersedes`` links,
lineage retirements and findings are Memory's), over four transactions, then one claim newer
than the pinned graph-schema is added as Memory would hold it:

- manipulator ``ARM-7`` and its wrist camera ``WCAM-7``: a March calibration replaced by an April
  one (tx 2), and an observed ``drift`` claim citing both calibration files (beyond the pin);
- legged robot ``LEG-9``: firmware 3.1.4 believed open-ended (tx 1), then a new configuration
  lineage restates it as ended on 1 May and adds firmware 3.2.0 from 1 May (tx 3);
- mobile robot ``AMR-9``: moved from the dock to aisle 9 (tx 2), and a run it recorded;
- humanoid ``HUM-1``: a run whose configuration is one of two undecided candidates;
- marine ``USV-3``: its harbour berth stated by four sources (corroboration, a cycle), one of
  them only for March;
- mobile robot ``X9``: first seen at the dock (tx 2), moved to the aisle (tx 3);
- autonomous truck ``AV-2``: its yard stated on UTC and contradicted on its own clock
  (a ``clock_mismatch`` finding);
- aerial ``UAV-8``: an inferred location fully covered by a stated one on arrival
  (``overridden_on_arrival``), an identity link and an inferred identity candidate.

``Catalog`` resolves every source but one (``LOST``); times are UTC nanoseconds.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any, Final

from neptune_ledger.api import QueryMeta, Resolution, from_json, query_table
from neptune_memory.schema.claim import Claim, ClaimProvenance, ModelRef, TypedLiteral, ValueType
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import OPEN, CivilClock, LedgerTx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.supersede import Resolution as History
from neptune_memory.schema.supersede import resolve, resolver_config

from neptune.identity.hashing import content_id
from neptune.model.ids import ConfigHash, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune.model.units import unit_from_json

ROOT: Final = Path(__file__).resolve().parents[3]
CATALOG_GOLDEN: Final = ROOT / "contracts" / "catalog-api" / "v1.7.0" / "golden"
UTC_NS: Final = CivilClock(Timescale.UTC, Epoch.UNIX, Fraction(1, 1_000_000_000))
UTC: Final = str(UTC_NS.domain_id)
HEAD: Final = LedgerTx(4)


def ns(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp()) * 10**9  # noqa: UP017


def rec(name: str) -> RecordId:
    return RecordId("rec:sha256:" + content_id(name.encode()).removeprefix("sha256:"))


def source(name: str) -> str:
    return content_id(name.encode())


JAN_1, FEB_1, MAR_1, APR_1 = ns(2026, 1, 1), ns(2026, 2, 1), ns(2026, 3, 1), ns(2026, 4, 1)
APR_14, MAY_1, JUN_1 = ns(2026, 4, 14), ns(2026, 5, 1), ns(2026, 6, 1)
TRUCK_CLOCK: Final = str(rec("clock:AV-2 vehicle"))

CAL_MARCH: Final = source("wcam-7_extrinsics_2026-03-01.yaml")
CAL_APRIL: Final = source("wcam-7_extrinsics_2026-04-14.yaml")
REGISTER: Final = source("plant-9_asset_register.csv")
LEG_LOG: Final = source("leg-9_2026-05-02.mcap")
LEG_MANIFEST: Final = source("leg-9_firmware_manifest.json")
AMR_LOG: Final = source("amr-9_2026-03-02.mcap")
HUM_LOG: Final = source("hum-1_2026-03-05.mcap")
AIS: Final = source("usv-3_ais_2026-02.nmea")
HARBOUR_LOG: Final = source("harbour_berth_log.csv")
TELEMETRY: Final = source("usv-3_telemetry.mcap")
TRUCK_LOG: Final = source("av-2_drive_2026-03-03.mcap")
FLIGHT: Final = source("uav-8_flight_2026-03-04.ulg")
LOST: Final = source("never registered.pdf")  # cited, held by no package

CAL_MARCH_REC: Final = rec("calibration:wcam-7 2026-03-01")
CAL_APRIL_REC: Final = rec("calibration:wcam-7 2026-04-14")
RUN_AMR: Final = rec("run:amr-9 2026-03-02")
RUN_HUM: Final = rec("run:hum-1 2026-03-05")

PRIORITIES: Final = {
    "fixture.calibration": 6,
    "fixture.configuration": 3,
    "fixture.identity": 2,
    "fixture.matcher": 1,
    "fixture.register": 4,
    "fixture.telemetry": 5,
}
MODEL: Final = ModelRef("fixture-matcher", "0.3")


def node(kind: NodeType, node_id: str) -> NodeRef:
    return NodeRef(kind, node_id)


SITE = node(NodeType.SITE, "site-code:PLANT-9")
CELL = node(NodeType.ZONE, "zone-code:CELL-7")
DOCK = node(NodeType.ZONE, "zone-code:DOCK-1")
AISLE = node(NodeType.ZONE, "zone-code:AISLE-9")
HARBOUR = node(NodeType.SITE, "site-code:HARBOUR-2")
YARD = node(NodeType.SITE, "site-code:YARD-4")
DEPOT = node(NodeType.SITE, "site-code:DEPOT-5")
FIELD = node(NodeType.SITE, "site-code:FIELD-6")
ARM = node(NodeType.MACHINE, "asset-tag:ARM-7")
WCAM = node(NodeType.SENSOR, "asset-tag:WCAM-7")
LEG = node(NodeType.MACHINE, "asset-tag:LEG-9")
AMR = node(NodeType.MACHINE, "asset-tag:AMR-9")
HUM = node(NodeType.MACHINE, "asset-tag:HUM-1")
USV = node(NodeType.MACHINE, "asset-tag:USV-3")
TRUCK = node(NodeType.MACHINE, "vin:av-2")
UAV = node(NodeType.MACHINE, "asset-tag:UAV-8")
X9 = node(NodeType.MACHINE, "asset-tag:X9")
UAV_SERIAL = node(NodeType.MACHINE, "serial:uav-8-0042")
UAV_SPARE = node(NodeType.MACHINE, "serial:uav-8-0043")
CAL_A = node(NodeType.CONFIGURATION, "cfg:wcam-7-cal-2026-03")
CAL_B = node(NodeType.CONFIGURATION, "cfg:wcam-7-cal-2026-04")
FW_OLD = node(NodeType.CONFIGURATION, "cfg:leg-9-fw-3.1.4")
FW_NEW = node(NodeType.CONFIGURATION, "cfg:leg-9-fw-3.2.0")
HUM_C1 = node(NodeType.CONFIGURATION, "cfg:hum-1-gait-a")
HUM_C2 = node(NodeType.CONFIGURATION, "cfg:hum-1-gait-b")
AMR_RUN = node(NodeType.RUN, f"record:{RUN_AMR}")
HUM_RUN = node(NodeType.RUN, f"record:{RUN_HUM}")


def ref(src: str, row: int | None = None, pointer: str | None = None) -> EvidenceRef:
    locator: dict[str, Any] = (
        {"kind": "json_pointer", "pointer": pointer}
        if pointer is not None
        else {"kind": "row", "row": row if row is not None else 1}
    )
    return evidence_ref_from_json({"locator": [locator], "source": src})


def claim(
    subject: NodeRef,
    predicate: str,
    obj: Any,
    start: int,
    end: int | None = None,
    *,
    evidence: tuple[EvidenceRef, ...],
    tx: int = 1,
    clock: str = UTC,
    by: str = "fixture.register",
    version: str = "1",
    records: tuple[RecordId, ...] = (),
    inferred: float | None = None,
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
            evidence=evidence,
            records=tuple(sorted(records)),
            consolidator_id=by,
            consolidator_version=version,
            config_hash=ConfigHash(content_id(f"{by} {version}".encode())),
            model=MODEL if inferred is not None else None,
        ),
    )


def assertions() -> list[Claim]:
    return [
        # Manipulator cell: the arm, its wrist camera and the camera's calibration chain.
        claim(CELL, "zone_of", SITE, JAN_1, evidence=(ref(REGISTER, 2),)),
        claim(ARM, "located_at", CELL, JAN_1, evidence=(ref(REGISTER, 3),)),
        claim(WCAM, "mounted_on", ARM, JAN_1, evidence=(ref(REGISTER, 4),)),
        claim(
            WCAM,
            "has_calibration",
            CAL_A,
            MAR_1,
            evidence=(ref(CAL_MARCH, pointer="/extrinsics"),),
            by="fixture.calibration",
            records=(CAL_MARCH_REC,),
        ),
        claim(
            WCAM,
            "has_calibration",
            CAL_B,
            APR_14,
            evidence=(ref(CAL_APRIL, pointer="/extrinsics"),),
            tx=2,
            by="fixture.calibration",
            records=(CAL_APRIL_REC,),
        ),
        # Legged robot: firmware believed open-ended, then restated by a new lineage at tx 3.
        claim(LEG, "located_at", SITE, JAN_1, evidence=(ref(REGISTER, 5),)),
        claim(
            LEG,
            "has_configuration",
            FW_OLD,
            JAN_1,
            evidence=(ref(LEG_MANIFEST, pointer="/firmware"),),
            by="fixture.configuration",
        ),
        claim(
            LEG,
            "has_configuration",
            FW_OLD,
            JAN_1,
            MAY_1,
            evidence=(ref(LEG_MANIFEST, pointer="/firmware"), ref(LEG_LOG, 1)),
            tx=3,
            by="fixture.configuration",
            version="2",
        ),
        claim(
            LEG,
            "has_configuration",
            FW_NEW,
            MAY_1,
            evidence=(ref(LEG_LOG, 1),),
            tx=3,
            by="fixture.configuration",
            version="2",
        ),
        claim(
            FW_NEW,
            "succeeds",
            FW_OLD,
            MAY_1,
            evidence=(ref(LEG_LOG, 1),),
            tx=3,
            by="fixture.configuration",
            version="2",
        ),
        # Mobile robot: from the dock to aisle 9 (located_at is one-valued: Memory splits it).
        claim(AMR, "located_at", DOCK, FEB_1, evidence=(ref(REGISTER, 6),)),
        claim(
            AMR,
            "located_at",
            AISLE,
            MAR_1,
            evidence=(ref(AMR_LOG, 7),),
            tx=2,
            by="fixture.telemetry",
            kind=AssertionKind.OBSERVED,
        ),
        claim(
            AMR_RUN,
            "recorded_by",
            AMR,
            MAR_1,
            MAR_1 + 3600 * 10**9,
            evidence=(ref(AMR_LOG, 1), ref(LOST, 1)),
            tx=2,
            by="fixture.telemetry",
            kind=AssertionKind.OBSERVED,
            records=(RUN_AMR,),
        ),
        # Humanoid: a run, and two undecided readings of its configuration.
        claim(HUM_RUN, "recorded_by", HUM, MAR_1, MAR_1 + 600 * 10**9, evidence=(ref(HUM_LOG, 1),)),
        claim(
            HUM_RUN,
            "configuration_candidate",
            HUM_C1,
            MAR_1,
            MAR_1 + 600 * 10**9,
            evidence=(ref(HUM_LOG, 2),),
            by="fixture.telemetry",
        ),
        claim(
            HUM_RUN,
            "configuration_candidate",
            HUM_C2,
            MAR_1,
            MAR_1 + 600 * 10**9,
            evidence=(ref(HUM_LOG, 3),),
            by="fixture.telemetry",
        ),
        # Humanoid on the floor: dock, then aisle (tx 2), then cell (tx 3); the aisle version is
        # recorded and replaced between transactions 1 and 3.
        claim(HUM, "located_at", DOCK, JAN_1, evidence=(ref(REGISTER, 11),)),
        claim(HUM, "located_at", AISLE, FEB_1, evidence=(ref(HUM_LOG, 4),), tx=2),
        claim(HUM, "located_at", CELL, MAR_1, evidence=(ref(HUM_LOG, 5),), tx=3),
        # Marine: three sources agree on the berth.
        claim(USV, "located_at", HARBOUR, FEB_1, evidence=(ref(HARBOUR_LOG, 2),)),
        claim(
            USV,
            "located_at",
            HARBOUR,
            FEB_1,
            evidence=(ref(AIS, 9),),
            by="fixture.telemetry",
            kind=AssertionKind.OBSERVED,
        ),
        claim(
            USV,
            "located_at",
            HARBOUR,
            FEB_1,
            JUN_1,
            evidence=(ref(TELEMETRY, 4),),
            by="fixture.calibration",
            kind=AssertionKind.OBSERVED,
        ),
        claim(
            USV,
            "located_at",
            HARBOUR,
            MAR_1,
            APR_1,
            evidence=(ref(HARBOUR_LOG, 3),),
            by="fixture.identity",
        ),
        # A second mobile robot first seen at the dock (tx 2) and moved to the aisle (tx 3).
        claim(X9, "located_at", DOCK, FEB_1, evidence=(ref(REGISTER, 12),), tx=2),
        claim(X9, "located_at", AISLE, MAR_1, evidence=(ref(AMR_LOG, 8),), tx=3),
        # Autonomous truck: the yard on UTC, the depot on its own clock (never compared).
        claim(TRUCK, "located_at", YARD, MAR_1, evidence=(ref(REGISTER, 8),)),
        claim(
            TRUCK,
            "located_at",
            DEPOT,
            1000,
            evidence=(ref(TRUCK_LOG, 3),),
            tx=2,
            clock=TRUCK_CLOCK,
            by="fixture.telemetry",
            kind=AssertionKind.OBSERVED,
        ),
        # Aerial: a stated field, an inferred one it covers on arrival, and an identity link.
        claim(UAV, "located_at", FIELD, MAR_1, evidence=(ref(FLIGHT, 1),), tx=1),
        claim(
            UAV,
            "located_at",
            DEPOT,
            MAR_1,
            APR_1,
            evidence=(ref(FLIGHT, 2),),
            tx=2,
            by="fixture.matcher",
            inferred=0.6,
        ),
        claim(
            UAV, "same_as", UAV_SERIAL, JAN_1, evidence=(ref(REGISTER, 9),), by="fixture.identity"
        ),
        claim(UAV_SERIAL, "has_name", "Survey eight", JAN_1, evidence=(ref(REGISTER, 10),)),
        claim(
            UAV,
            "same_as_candidate",
            UAV_SPARE,
            JAN_1,
            evidence=(ref(FLIGHT, 3),),
            by="fixture.matcher",
            inferred=0.4,
        ),
    ]


def drift() -> Claim:
    """The wrist camera's drift between its two calibrations: observed, citing both calibration
    files, and newer than the pinned graph-schema 1.6.0 (``drift`` is Memory ADR 0014)."""
    return dataclasses.replace(
        claim(
            WCAM,
            "drift",
            TypedLiteral(ValueType.QUANTITY, 4.3, Known(unit_from_json("mm"))),
            MAR_1,
            APR_14,
            evidence=(
                ref(CAL_MARCH, pointer="/translation"),
                ref(CAL_APRIL, pointer="/translation"),
            ),
            tx=2,
            by="fixture.calibration",
            records=(CAL_MARCH_REC, CAL_APRIL_REC),
            kind=AssertionKind.OBSERVED,
        )
    )


@cache
def document() -> GraphDocument:
    history = resolve(assertions(), CORE_PREDICATES, PRIORITIES)
    claims = sorted((*history.claims, drift()), key=lambda c: (c.recorded_at, c.id))
    return GraphDocument(
        History(tuple(claims), history.findings),
        resolver_config(CORE_PREDICATES, PRIORITIES),
        HEAD,
    )


def find(subject: NodeRef, predicate: str, obj: Any = None, *, current: bool = True) -> Claim:
    """The one stored version matching (current only, unless ``current=False``: the first)."""
    out = [
        c
        for c in document().resolution.claims
        if c.subject == subject
        and c.predicate == predicate
        and (obj is None or c.object == obj)
        and (not current or c.is_current)
    ]
    assert out, (subject, predicate, obj)
    return sorted(out, key=lambda c: (c.recorded_at, c.id))[0]


def _golden(name: str) -> Any:
    return json.loads((CATALOG_GOLDEN / name).read_bytes())


class Catalog:
    """A Ledger ``CatalogApi`` that resolves every source but ``LOST``, and records the calls."""

    def __init__(self, head: int = int(HEAD)) -> None:
        self.resolved: list[tuple[Any, int | None]] = []
        meta = _golden("query_meta.json")
        meta["as_of"]["value"]["tx_seq"] = head
        self.meta: QueryMeta = from_json(QueryMeta, meta)

    def resolve(self, evidence_ref: Any, *, as_of: int | None = None) -> Resolution:
        self.resolved.append((evidence_ref, as_of))
        golden = from_json(Resolution, _golden("drone.resolution.json"))
        if evidence_ref.source == LOST:
            unresolvable = from_json(Resolution, _golden("error.resolution_unresolvable.json"))
            return dataclasses.replace(unresolvable, evidence_ref=evidence_ref)
        return dataclasses.replace(golden, evidence_ref=evidence_ref)

    def query(self, spec: Any) -> Any:
        """No rows: the engine asks only for the catalog's head."""
        return query_table([], self.meta)

    def lineage(self, record_id: str, *, as_of: int | None = None) -> Any:
        raise NotImplementedError

    def register(self, package_root: Any) -> Any:
        raise NotImplementedError

    def verify(self, package_id: str, *, as_of: int | None = None) -> Any:
        raise NotImplementedError

    def thread(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def threads_of(self, record_id: str, *, as_of: int | None = None) -> Any:
        raise NotImplementedError
