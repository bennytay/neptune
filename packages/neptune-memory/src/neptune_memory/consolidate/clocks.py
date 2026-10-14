"""Clock descriptions as stated claims (ADR 0029): what the Ledger declares about each clock.

Every ``TimestampDomain`` record (root ADR 0005 §2) is one clock node (ADR 0011 §1). This
consolidator writes one ``clock_description`` claim about it: its field and scope, tick length,
epoch, timescale and role exactly as the domain states them, and the zone its
``civil_time_zone`` companion declares (root ADR 0061 §1), with the ``adapter_id`` of the
transform whose record declares that zone. Nothing is converted, completed or guessed:

- a state the domain leaves ``Unknown`` or ``NotCovered`` stays so;
- a clock no companion names has ``civil_time_zone`` ``NotApplicable`` (the Ledger holds no zone
  record for it), never a zone; a companion that states none is ``Unknown`` or ``NotCovered``, as
  it says;
- two companions naming one clock leave its zone in doubt even when they agree (root ADR 0061
  §3, ``civil_zone_repeated``): a finding, and the zone is ``Ambiguous`` between the names they
  state, or ``Unknown`` when they state fewer than two names.

Each shared ``CivilClock`` a domain names (a civil timescale, an absolute epoch and a tick
length, all ``Known``: ADR 0002 §3) is described too, since consolidators place that domain's
instants on it: its definition, citing every domain that names it.

The claim holds over the clock's whole time (from ``INT64_MIN`` ticks on the clock, open-ended):
it describes how the ticks count, not something that happened. It cites the domain's evidence and
the companions' (and any evidence a state cites of its own), its records are the domain, the
companions and their transforms, and it is ``stated`` when every one of those is, else
``observed``. A malformed record, or one id carrying two contents, is a finding and grounds
nothing; the rest of the build is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from neptune.model.finding import Severity
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import Provenance, TransformRecord, transform_record_from_json
from neptune.model.reference import (
    CivilTimeZone,
    TimestampDomain,
    civil_time_zone_from_json,
    timestamp_domain_from_json,
)
from neptune.model.time import INT64_MIN, Timestamp
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.clock_description import CIVIL_CLOCK_FIELD, ClockDescription
from neptune_memory.schema.interval import OPEN, CivilClock
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CLOCK_DESCRIPTION

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

CLOCKS_CONSOLIDATOR_ID: Final = "memory.clocks"
# Ledger record kinds read here: the compiler's, each with its own strict reader.
TIMESTAMP_DOMAIN: Final = "timestamp_domain"
CIVIL_TIME_ZONE: Final = "civil_time_zone"
TRANSFORM_RECORD: Final = "transform_record"


def _finding(
    code: str,
    message: str,
    records: Sequence[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"clocks.{code}",
        severity=severity,
        message=message[:1000],
        records=tuple(records),
        details=details,
    )


@dataclass
class _View:
    """What one build reads, de-duplicated by record id across packages."""

    domains: dict[RecordId, TimestampDomain]
    zones: dict[RecordId, list[CivilTimeZone]]  # by the domain they name
    transforms: dict[RecordId, TransformRecord]
    findings: list[ConsolidationFinding]


def _read(ledger: LedgerReader) -> _View:
    """Every domain, companion and transform record, parsed strictly. A record that does not
    read is a finding (domains and companions only: a transform is context, not a clock); an id
    seen with two contents is used nowhere."""
    parsers: tuple[tuple[str, Callable[[JsonValue], object]], ...] = (
        (TIMESTAMP_DOMAIN, timestamp_domain_from_json),
        (CIVIL_TIME_ZONE, civil_time_zone_from_json),
        (TRANSFORM_RECORD, transform_record_from_json),
    )
    seen: dict[RecordId, object] = {}
    conflicted: set[RecordId] = set()
    findings: list[ConsolidationFinding] = []
    for ref in ledger.list_packages():
        for kind, parse in parsers:
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    value = parse(dict(record))  # type: ignore[arg-type]
                except (ValueError, TypeError, KeyError, RecursionError) as exc:
                    if kind != TRANSFORM_RECORD:
                        findings.append(
                            _finding(
                                "malformed_record",
                                f"{kind} record {index} in package {ref.package_id!r} is"
                                f" malformed: {str(exc) or type(exc).__name__}",
                                severity=Severity.ERROR,
                                index=index,
                                kind=kind,
                                package_id=ref.package_id,
                            )
                        )
                    continue
                rid: RecordId = value.id  # type: ignore[attr-defined]
                if rid in conflicted:
                    continue
                if seen.setdefault(rid, value) != value:
                    conflicted.add(rid)
                    if kind != TRANSFORM_RECORD:
                        findings.append(
                            _finding(
                                "record_conflict",
                                "one record id carries different content in two places; record"
                                " not used",
                                (rid,),
                                Severity.ERROR,
                            )
                        )
    view = _View({}, {}, {}, findings)
    for rid in sorted(seen.keys() - conflicted):
        value = seen[rid]
        if isinstance(value, TimestampDomain):
            view.domains[rid] = value
        elif isinstance(value, CivilTimeZone):
            view.zones.setdefault(value.domain, []).append(value)
        elif isinstance(value, TransformRecord):
            view.transforms[rid] = value
    return view


def _bare(state: Knowledge[object]) -> tuple[Knowledge[object], list[Provenance]]:
    """``state`` with its own provenance lifted to the claim (the description's states inherit
    the claim's), and the provenances it cited of its own. ``KnownAbsent`` cannot inherit: it is
    returned as is, and the caller refuses it."""
    own: list[Provenance] = []
    if isinstance(state, Known):
        if isinstance(state.provenance, Provenance):
            own.append(state.provenance)
        return Known(state.value), own
    if isinstance(state, Unknown | NotCovered):
        if isinstance(state.provenance, Provenance):
            own.append(state.provenance)
        return type(state)(), own
    if isinstance(state, Ambiguous):
        candidates = []
        for candidate in state.candidates:
            if isinstance(candidate.provenance, Provenance):
                own.append(candidate.provenance)
            candidates.append(Candidate(candidate.value))
        return Ambiguous(tuple(candidates)), own
    return state, own


def _zone(
    clock: RecordId, zones: Sequence[CivilTimeZone], findings: list[ConsolidationFinding]
) -> tuple[Knowledge[str], list[Provenance]]:
    """The clock's declared zone from its companions; several leave it in doubt (a finding)."""
    if len(zones) == 1:
        zone, lifted = _bare(zones[0].zone)  # type: ignore[arg-type]
        return zone, lifted  # type: ignore[return-value]
    findings.append(
        _finding(
            "zone_repeated",
            f"{len(zones)} civil_time_zone records name one clock, which leaves its zone in"
            " doubt even when they agree",
            (clock, *(z.id for z in zones)),
            clock=clock,
        )
    )
    names: list[str] = []
    own: list[Provenance] = []
    for companion in zones:
        state, cited = _bare(companion.zone)  # type: ignore[arg-type]
        own += cited
        if isinstance(state, Known):
            names.append(state.value)  # type: ignore[arg-type]
        elif isinstance(state, Ambiguous):
            names += [c.value for c in state.candidates]  # type: ignore[misc]
    distinct = sorted(set(names))
    if len(distinct) < 2:
        return Unknown(), own
    return Ambiguous(tuple(Candidate(name) for name in distinct)), own


def _declared_by(
    zones: Sequence[CivilTimeZone], transforms: Mapping[RecordId, TransformRecord]
) -> Knowledge[str]:
    """The one ``adapter_id`` whose transform records declare the zone; ``Unknown`` when a
    transform is not in the Ledger or several adapters declare it."""
    found = {getattr(transforms.get(z.provenance.transform), "adapter_id", None) for z in zones}
    if len(found) != 1 or None in found:
        return Unknown()
    (adapter,) = found
    return Known(str(adapter))


def describe(view: _View) -> list[ClaimDraft]:
    """One ``clock_description`` draft per readable domain, by domain id."""
    drafts: list[ClaimDraft] = []
    for clock, domain in sorted(view.domains.items()):
        provenances: list[Provenance] = [domain.provenance]
        states: dict[str, Knowledge[object]] = {}
        refused = False
        for name in ("role", "resolution", "epoch", "timescale"):
            state, own = _bare(getattr(domain, name))
            if isinstance(state, KnownAbsent):
                refused = True
            states[name] = state
            provenances += own
        zones = sorted(view.zones.get(clock, ()), key=lambda z: z.id)
        zone: Knowledge[str] = NotApplicable()
        declared_by: Knowledge[str] = NotApplicable()
        if zones:
            zone, own = _zone(clock, zones, view.findings)
            provenances += [z.provenance for z in zones] + own
            declared_by = _declared_by(zones, view.transforms)
        if refused:
            view.findings.append(
                _finding(
                    "unreadable_state",
                    "the domain states a property of the clock as absent, which a clock"
                    " description cannot carry; no claim",
                    (clock,),
                    clock=clock,
                )
            )
            continue
        description = ClockDescription(
            field=domain.field,
            scope=domain.scope,
            role=states["role"],  # type: ignore[arg-type]
            resolution=states["resolution"],  # type: ignore[arg-type]
            epoch=states["epoch"],  # type: ignore[arg-type]
            timescale=states["timescale"],  # type: ignore[arg-type]
            civil_time_zone=zone,
            zone_declared_by=declared_by,
        )
        transforms = [
            z.provenance.transform for z in zones if z.provenance.transform in view.transforms
        ]
        stated = all(p.assertion_kind is AssertionKind.STATED for p in provenances)
        drafts.append(
            ClaimDraft(
                subject=NodeRef(NodeType.CLOCK, clock),
                predicate=CLOCK_DESCRIPTION,
                object=TypedLiteral(ValueType.CLOCK_DESCRIPTION, description),
                valid_from=Timestamp(INT64_MIN, clock),
                valid_to=OPEN,
                assertion_kind=AssertionKind.STATED if stated else AssertionKind.OBSERVED,
                evidence=_evidence(provenances),
                records=(clock, *(z.id for z in zones), *transforms),
            )
        )
    return drafts


def _civil(domain: TimestampDomain) -> CivilClock | None:
    """The shared ``CivilClock`` a domain names when it declares a civil timescale, an absolute
    epoch and its tick length (ADR 0002 §3, as the consolidators place its instants)."""
    timescale, epoch, resolution = domain.timescale, domain.epoch, domain.resolution
    if not (
        isinstance(timescale, Known) and isinstance(epoch, Known) and isinstance(resolution, Known)
    ):
        return None
    try:
        return CivilClock(timescale.value, epoch.value, resolution.value)
    except ValueError:  # a timescale or epoch that is not civil
        return None


def describe_civil(view: _View) -> list[ClaimDraft]:
    """One ``clock_description`` per shared ``CivilClock`` a readable domain names: Memory
    places that domain's instants on it, so its ticks need a description too. It states exactly
    the definition the domains declare (timescale, epoch, tick length), cites every domain that
    names it, and has no field, role or zone of its own (``CIVIL_CLOCK_FIELD``)."""
    named: dict[RecordId, tuple[CivilClock, list[TimestampDomain]]] = {}
    for _, domain in sorted(view.domains.items()):
        civil = _civil(domain)
        if civil is not None:
            named.setdefault(civil.domain_id, (civil, []))[1].append(domain)
    drafts: list[ClaimDraft] = []
    for clock, (civil, domains) in sorted(named.items()):
        provenances = [d.provenance for d in domains]
        for domain in domains:
            for name in ("resolution", "epoch", "timescale"):
                provenances += _bare(getattr(domain, name))[1]
        na = NotApplicable()
        description = ClockDescription(
            field=CIVIL_CLOCK_FIELD,
            scope=(),
            role=na,
            resolution=Known(civil.resolution),
            epoch=Known(civil.epoch),
            timescale=Known(civil.timescale),
            civil_time_zone=na,
            zone_declared_by=na,
        )
        stated = all(p.assertion_kind is AssertionKind.STATED for p in provenances)
        drafts.append(
            ClaimDraft(
                subject=NodeRef(NodeType.CLOCK, clock),
                predicate=CLOCK_DESCRIPTION,
                object=TypedLiteral(ValueType.CLOCK_DESCRIPTION, description),
                valid_from=Timestamp(INT64_MIN, clock),
                valid_to=OPEN,
                assertion_kind=AssertionKind.STATED if stated else AssertionKind.OBSERVED,
                evidence=_evidence(provenances),
                records=tuple(d.id for d in domains),
            )
        )
    return drafts


def _evidence(provenances: Sequence[Provenance]) -> tuple[EvidenceRef, ...]:
    return tuple(p.evidence for p in provenances)


class ClockDescriptionConsolidator:
    """Deterministic ``clock_description`` claims, one per declared clock. Takes no config."""

    consolidator_id: Final = CLOCKS_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        del previous
        view = _read(ledger)
        if config:
            view.findings.append(
                _finding(
                    "unknown_config",
                    f"{CLOCKS_CONSOLIDATOR_ID} takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        drafts = [*describe(view), *describe_civil(view)]
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))
