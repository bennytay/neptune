"""Compiler-shaped time records for tests, built with the compiler's own types.

Every ``timestamp_domain``, ``run``, ``stream`` and ``clock_mapping`` here is constructed as the
compiler model class and serialised with its ``to_json`` (an estimate as the compiler's
``derived/`` line), so a test can never feed the time-domain registry a shape the compiler would
not write (root ADRs 0018, 0050 and 0060). Ids are the compiler's evidence record ids under one
test transform; clocks are named, and a name is one clock wherever it is used.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.derived.clocks import InferredClockMapping
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.alignment import ClockAnchor, ClockMapping, MappingMethod, ValidityWindow
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import (
    AssertionKind,
    Known,
    KnownAbsent,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SeriesProvenance, StepTemplate
from neptune.model.time import Duration, Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import rebuild
from neptune_memory.consolidate.time import TimeDomainConsolidator
from neptune_memory.derived.clocks import CLOCKS_MODEL, EstimatedClocksConsolidator
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import TypedLiteral
from neptune_memory.schema.clock_map import ClockMap
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import resolve, resolver_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune_memory.consolidate.base import Consolidation, Consolidator
    from neptune_memory.schema.claim import Claim

Record = dict[str, object]
TRANSFORM: Final = transform_record(adapter_id="test.time", adapter_version="1", config={})
FIT: Final = transform_record(adapter_id="neptune.clocks", adapter_version="0.1.0", config={})
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED
MICRO, MILLI, NANO = Fraction(1, 10**6), Fraction(1, 10**3), Fraction(1, 10**9)
# A window side the evidence states open, as ``KnownAbsent`` (root ADR 0050 §3).
OPEN_SIDE: Final = "open"
UNSTATED: Final = "unstated"
Side = int | str
Plan = tuple[tuple["Consolidator", "Mapping[str, JsonValue]"], ...]

TIME_PLAN: Final[Plan] = ((TimeDomainConsolidator(), {}),)
BOTH_PLAN: Final[Plan] = (
    (TimeDomainConsolidator(), {}),
    (EstimatedClocksConsolidator(), {"model": CLOCKS_MODEL.to_json()}),
)
PRIORITIES: Final = {"memory.time": 0, "memory.time_estimates": 1}


def cite(name: str, offset: int = 0, length: int = 64) -> EvidenceRef:
    return EvidenceRef(content_id(name.encode()), (ByteRange(offset, length),))


def _provenance(name: str, kind: AssertionKind = OBSERVED) -> Provenance:
    return Provenance(cite(name), TRANSFORM.id, kind)


def _rid(kind: str, name: str) -> RecordId:
    return evidence_record_id(kind, cite(name), TRANSFORM)


def domain(
    name: str,
    resolution: Fraction = MICRO,
    *,
    timescale: Timescale | None = None,
    epoch: Epoch | None = None,
) -> Record:
    """A clock: a ``TimestampDomain`` declared by ``name``; civil when timescale and epoch are."""
    record = TimestampDomain(
        id=_rid("timestamp_domain", name),
        provenance=_provenance(name),
        field=name,
        scope=(),
        role=Unknown(),
        resolution=Known(resolution),
        epoch=Unknown() if epoch is None else Known(epoch),
        timescale=Unknown() if timescale is None else Known(timescale),
        declared_monotonic=Unknown(),
    )
    return dict(record.to_json())


def clock(name: str) -> RecordId:
    """The record id of the clock ``domain(name)`` declares."""
    return _rid("timestamp_domain", name)


def at(name: str, ticks: int) -> Timestamp:
    return Timestamp(ticks, clock(name))


def run(
    name: str,
    machine: LogicalId | None,
    first: Timestamp | None,
    last: Timestamp | None = None,
    kind: AssertionKind = OBSERVED,
    last_read_from: str | None = None,
) -> Record:
    """A ``Run`` declared by ``name``; ``None`` fields are not stated. ``last_read_from`` gives
    the last instant its own observed citation (a recording's last message, not its header)."""
    own = None if last_read_from is None else _provenance(last_read_from, OBSERVED)
    record = Run(
        id=run_id(name),
        provenance=_provenance(name, kind),
        logical_id=Unknown(),
        machine=NotCovered() if machine is None else Known(machine),
        first=Unknown() if first is None else Known(first),
        last=Unknown() if last is None else Known(last) if own is None else Known(last, own),
    )
    return dict(record.to_json())


def run_id(name: str) -> RecordId:
    return _rid("run", name)


def stream(
    name: str,
    run_name: str,
    clocks: Sequence[str],
    first: Timestamp | None = None,
    last: Timestamp | None = None,
) -> Record:
    """A ``Stream`` of run ``run_name`` whose samples carry ``clocks`` (by name)."""
    record = Stream(
        id=_rid("stream", name),
        provenance=_provenance(name),
        run=run_id(run_name),
        topic=Known(name),
        schema_name=Unknown(),
        schema_encoding=Unknown(),
        schema_definition=Unknown(),
        message_encoding=Unknown(),
        metadata=(),
        clocks=tuple(clock(c) for c in clocks),
        message_count=Unknown(),
        first=Unknown() if first is None else Known(first),
        last=Unknown() if last is None else Known(last),
        series=SeriesProvenance(
            content_id(name.encode()),
            (StepTemplate("byte_range", (), ("length", "offset")),),
            OBSERVED,
        ),
    )
    return dict(record.to_json())


def _side(value: Side, source: str, name: str) -> Knowledge[Timestamp]:
    if value == OPEN_SIDE:
        return KnownAbsent(_provenance(f"{name}#open"))
    if value == UNSTATED:
        return Unknown()
    assert isinstance(value, int)
    return Known(at(source, value))


def validity(source: str, start: Side, end: Side, name: str) -> Knowledge[ValidityWindow]:
    return Known(
        ValidityWindow(clock(source), _side(start, source, name), _side(end, source, name))
    )


def _parameters(
    source: str,
    target: str,
    anchor: tuple[int, int] | None,
    rate: Fraction | None,
    residual: int | None,
) -> dict[str, Knowledge[object]]:
    return {
        "anchor": Unknown()
        if anchor is None
        else Known(ClockAnchor(at(source, anchor[0]), at(target, anchor[1]))),
        "rate": Unknown() if rate is None else Known(rate),
        "residual_bound": Unknown()
        if residual is None
        else Known(Duration(residual, clock(target))),
    }


def mapping(
    name: str,
    source: str,
    target: str,
    *,
    anchor: tuple[int, int] | None,
    rate: Fraction | None = Fraction(1),
    residual: int | None = 0,
    start: Side = 0,
    end: Side = OPEN_SIDE,
    kind: AssertionKind = STATED,
    method: MappingMethod = MappingMethod.STATED,
    window: Knowledge[ValidityWindow] | None = None,
) -> Record:
    """A declared ``ClockMapping`` from ``source`` ticks to ``target`` ticks, stated by ``name``:
    ``target(t) = anchor[1] + rate * (t - anchor[0])`` over ``[start, end)`` on ``source``."""
    record = ClockMapping(
        id=mapping_id(name),
        provenance=_provenance(name, kind),
        source=clock(source),
        target=clock(target),
        method=method,
        validity=validity(source, start, end, name) if window is None else window,
        **_parameters(source, target, anchor, rate, residual),  # type: ignore[arg-type]
    )
    return dict(record.to_json())


def mapping_id(name: str) -> RecordId:
    return _rid("clock_mapping", name)


def estimate(
    name: str,
    source: str,
    target: str,
    *,
    anchor: tuple[int, int] | None,
    rate: Fraction | None = Fraction(1),
    residual: int | None = None,
    start: int = 0,
    end: int = 1_000_000,
) -> Record:
    """A compiler-fitted mapping, as its ``derived/clock_mapping`` line (root ADR 0060 §6)."""
    record = InferredClockMapping(
        id=mapping_id(name),
        transform=FIT.id,
        evidence=(cite(name),),
        source=clock(source),
        target=clock(target),
        method=MappingMethod.CO_SAMPLED,
        validity=Known(
            ValidityWindow(clock(source), Known(at(source, start)), Known(at(source, end)))
        ),
        **_parameters(source, target, anchor, rate, residual),  # type: ignore[arg-type]
    )
    return dict(record.to_json())


def ledger(packages: Mapping[str, Sequence[Record]]) -> StubLedger:
    return StubLedger({pid: (6, list(records)) for pid, records in packages.items()})


def build(
    packages: Mapping[str, Sequence[Record]],
    tx: int = 1,
    plan: Sequence[tuple[Consolidator, Mapping[str, JsonValue]]] = TIME_PLAN,
) -> tuple[Consolidation, ...]:
    return rebuild(ledger(packages), list(plan), recorded_at=ledger_tx(tx))


# --- Shared scenarios and readers --------------------------------------------------------------

DRONE: Final = LogicalId("px4.sys_uuid", "000200000000343233345117003a0027")


def claims(results: Sequence[Consolidation], predicate: str | None = None) -> list[Claim]:
    return [c for r in results for c in r.claims if predicate is None or c.predicate == predicate]


def findings(results: Sequence[Consolidation]) -> list[str]:
    return sorted(f.code for r in results for f in r.findings)


def reader(*builds: Sequence[Consolidation]) -> ReferenceReader:
    """Resolve the claims of every build with the builds themselves (ADR 0007 §5)."""
    every = [c for results in builds for c in claims(results)]
    runs = [r.build for results in builds for r in results]
    resolution = resolve(every, CORE_PREDICATES, PRIORITIES, runs)
    head = max((c.recorded_at for c in every), default=0)
    config = resolver_config(CORE_PREDICATES, PRIORITIES)
    return ReferenceReader(GraphDocument(resolution, config, ledger_tx(head)))


def clock_map(claim: Claim) -> ClockMap:
    assert isinstance(claim.object, TypedLiteral) and isinstance(claim.object.value, ClockMap)
    return claim.object.value


def drone_flight() -> list[Record]:
    boot, gps = "px4 boot", "px4 gps"
    return [
        domain(boot, MICRO),
        domain(gps, MILLI, timescale=Timescale.GPS, epoch=Epoch.GPS),
        run("flight-17.ulg", DRONE, at(boot, 12_000_000), at(boot, 900_000_000)),
        stream("sensor_accel", "flight-17.ulg", [boot]),
        stream(
            "vehicle_gps_position",
            "flight-17.ulg",
            [boot, gps],
            first=at(gps, 1_400_000_000_000),
            last=at(gps, 1_400_000_880_000),
        ),
        # The flight log states its GPS sync: boot µs 20 s in was GPS ms 1_400_000_008_000.
        mapping(
            "gps-sync",
            boot,
            gps,
            anchor=(20_000_000, 1_400_000_008_000),
            rate=Fraction(1, 1000),  # GPS ms per boot µs
            residual=2,
            start=12_000_000,
            end=900_000_001,
        ),
    ]


def revised(tx: int) -> list[Consolidation]:
    """tx 1: the quadruped's first sync. tx 2: a re-sync from boot tick 1000 lands in another
    package. Both packages stay in the Ledger."""
    first = [
        domain("spot boot", MICRO),
        domain("dock", MICRO),
        domain("site gps", MICRO),
        mapping("sync-v1", "spot boot", "dock", anchor=(0, 50_000), start=0, end=OPEN_SIDE),
        mapping("dock-gps", "dock", "site gps", anchor=(0, 7), start=0, end=OPEN_SIDE),
    ]
    later = [mapping("sync-v2", "spot boot", "dock", anchor=(1_000, 50_030), start=1_000)]
    packages = {"bag-1": first, **({"bag-2": later} if tx >= 2 else {})}
    return list(build(packages, tx))


def two_sites() -> dict[str, list[Record]]:
    """AMR-12 at warehouse A and AMR-31 at warehouse B; each site's NTP server logs civil time;
    both servers' sync logs state their offset to the site GPS receivers' common GPS time."""
    return {
        "site-a": [
            domain("warehouse-a ntp", MICRO, timescale=Timescale.POSIX, epoch=Epoch.UNIX),
            domain("gps time", MICRO, timescale=Timescale.GPS, epoch=Epoch.GPS),
            mapping("site-a-sync", "warehouse-a ntp", "gps time", anchor=(0, 18_000_000)),
        ],
        "site-b": [
            domain("warehouse-b ntp", MICRO, timescale=Timescale.POSIX, epoch=Epoch.UNIX),
            mapping("site-b-sync", "warehouse-b ntp", "gps time", anchor=(0, 18_000_003)),
        ],
        "amr-12": [
            domain("amr-12 boot", NANO),
            run("amr-12-shift.bag", LogicalId("asset-tag", "AMR-12"), at("amr-12 boot", 0)),
            stream("/odom", "amr-12-shift.bag", ["amr-12 boot"]),
            # The AMR's chrony log: boot ns 0 was warehouse A's µs 1_790_000_000_000_000.
            mapping(
                "amr-12-chrony",
                "amr-12 boot",
                "warehouse-a ntp",
                anchor=(0, 1_790_000_000_000_000),
                rate=Fraction(1, 1000),
                residual=40,
            ),
        ],
        "amr-31": [
            domain("amr-31 boot", NANO),
            run("amr-31-shift.bag", LogicalId("asset-tag", "AMR-31"), at("amr-31 boot", 0)),
        ],
    }
