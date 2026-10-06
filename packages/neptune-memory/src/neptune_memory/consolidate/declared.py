"""Declared names and values of configurations and runs (ADR 0025).

A deterministic consolidator that reads what other consolidators place and says what it is called
and what it declares, never what it means:

- ``declared_value`` on a ``configuration`` node: each scalar a ``configuration_value`` of its
  snapshot holds (its key path verbatim, its value as the format's schema reads it), and each
  parameter a ``calibration`` declares (its declared name as a one-step path, its text or numbers
  in source order, its unit as declared). Each claim cites the value's own place in the source
  (its span, else its record's JSON pointer); a calibration parameter that cites no place of its
  own is no claim (``value_unlocated``). Nothing is converted, compared or normalised.
- ``has_name`` on a ``configuration`` node: the path its snapshot's bytes were found at (the
  ``source_revision``), verbatim and ``observed``, since a path is an observation of where bytes
  were seen. Bytes found at two paths name nothing (``name_ambiguous``).
- ``has_name`` on a ``run`` node: the name a ``run_declaration`` gives the run (a manifest entry's
  ``name``), ``stated``, verbatim; two declarations naming one run differently name nothing.

A configuration node is the Ledger's (ADR 0024): the thread its snapshot opens (``threads_of``).
Every claim holds where the configuration consolidator places the configuration (a run it was
bound to, a machine's span), one claim per placement, citing that placement's records; a run's
name holds where the run consolidator places the run (its record's ``evidenced_by``). A
configuration placed nowhere states no claim: its values are counted in an ``unplaced`` finding,
never given a time. Values that are not ``Known`` (blank, null, ambiguous) are findings with a
count, never facts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.model.configuration import (
    ConfigScalar,
    ConfigurationSnapshot,
    ConfigurationValue,
    ScalarType,
    configuration_snapshot_from_json,
    configuration_value_from_json,
)
from neptune.model.finding import Severity
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    Unknown,
)
from neptune.model.machine import Calibration, DeclaredParameter, calibration_from_json
from neptune.model.provenance import Provenance
from neptune.model.run import RunDeclaration, run_declaration_from_json
from neptune.model.scalars import NonFinite
from neptune.model.source import LocalPath, SourceRevision, source_revision_from_json
from neptune_memory.consolidate.base import (
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
)
from neptune_memory.consolidate.configuration import CONFIGURATION_CONSOLIDATOR_ID
from neptune_memory.consolidate.identity_records import Malformed
from neptune_memory.consolidate.run_records import Inferred, _strict
from neptune_memory.consolidate.runs import EVIDENCED_BY, RUNS_CONSOLIDATOR_ID
from neptune_memory.consolidate.threads import catalog_threads
from neptune_memory.schema.claim import (
    DeclaredType,
    DeclaredValue,
    LedgerRecordRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    CONFIGURATION_ACTIVE_DURING,
    DECLARED_VALUE,
    is_declared_value,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune.model.time import Timestamp
    from neptune.model.units import Unit
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.interval import Open

DECLARED_CONSOLIDATOR_ID: Final = "memory.declared"
HAS_NAME: Final = "has_name"
HAS_CONFIGURATION: Final = "has_configuration"
MAX_LISTED: Final = 16  # record ids an aggregated finding lists; its details give the count

_T = TypeVar("_T")


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.INFO,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"declared.{code}",
        severity=severity,
        message=message[:1000],
        records=tuple(records),
        details=details,
    )


# --- Reading the Ledger -------------------------------------------------------------------------

_READERS: Final[Mapping[str, Callable[[JsonValue], object]]] = {
    "calibration": calibration_from_json,
    "configuration_snapshot": configuration_snapshot_from_json,
    "configuration_value": configuration_value_from_json,
    "run_declaration": run_declaration_from_json,
    "source_revision": source_revision_from_json,
}


@dataclass
class _View:
    records: dict[str, dict[RecordId, object]]
    findings: list[ConsolidationFinding]

    def of(self, kind: str, cls: type[_T]) -> list[_T]:
        found = self.records.get(kind, {})
        return [r for _, r in sorted(found.items()) if isinstance(r, cls)]


def _read(ledger: LedgerReader) -> _View:
    """Every record of the kinds read, by id; one id with two contents is dropped (a finding)."""
    view = _View({}, [])
    conflicted: set[tuple[str, RecordId]] = set()
    for ref in ledger.list_packages():
        for kind, reader in _READERS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    parsed = _strict(reader, record)
                except Inferred:
                    continue  # a derived/ record is never a ground for a declared value
                except Malformed as exc:
                    view.findings.append(
                        _finding(
                            "malformed_record",
                            f"{kind} record {index} in package {ref.package_id!r} is malformed:"
                            f" {str(exc).encode('utf-8', 'replace').decode('utf-8')}",
                            severity=Severity.ERROR,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                rid: RecordId = parsed.id  # type: ignore[attr-defined]
                key = (kind, rid)
                if key in conflicted:
                    continue
                seen = view.records.setdefault(kind, {})
                if seen.setdefault(rid, parsed) != parsed:
                    conflicted.add(key)
                    del seen[rid]
                    view.findings.append(
                        _finding(
                            "record_conflict",
                            "one record id carries different content in two places; record not"
                            " used",
                            (rid,),
                            Severity.ERROR,
                            kind=kind,
                        )
                    )
    return view


# --- Where a node holds -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Place:
    """An interval another consolidator places a node over, and the records that place it."""

    start: Timestamp
    end: Timestamp | Open
    records: tuple[RecordId, ...]


def _places(previous: Sequence[Claim]) -> tuple[dict[NodeRef, list[_Place]], dict[str, NodeRef]]:
    """Configuration placements (by node) and run nodes (by run record id), with run placements
    under the run node, from the configuration and run consolidators' claims. Claims over one
    interval are one placement citing all their records."""
    found: dict[NodeRef, dict[tuple[Timestamp, Timestamp | Open], set[RecordId]]] = defaultdict(
        lambda: defaultdict(set)
    )
    runs: dict[str, NodeRef] = {}
    for claim in previous:
        cid = claim.provenance.consolidator_id
        target = claim.object
        if (
            cid == CONFIGURATION_CONSOLIDATOR_ID
            and claim.predicate in (CONFIGURATION_ACTIVE_DURING, HAS_CONFIGURATION)
            and isinstance(target, NodeRef)
            and target.node_type is NodeType.CONFIGURATION
        ):
            node = target
        elif (
            cid == RUNS_CONSOLIDATOR_ID
            and claim.predicate == EVIDENCED_BY
            and claim.subject.node_type is NodeType.RUN
            and isinstance(target, LedgerRecordRef)
        ):
            runs.setdefault(target.record_id, claim.subject)
            node = claim.subject
        else:
            continue
        found[node][(claim.valid_from, claim.valid_to)].update(claim.provenance.records)
    places = {
        node: sorted(
            (_Place(start, end, tuple(sorted(records))) for (start, end), records in by.items()),
            key=lambda p: (p.start.domain_id, p.start.ticks, str(p.end), p.records),
        )
        for node, by in found.items()
    }
    return places, runs


# --- What a record declares ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Fact:
    predicate: str
    obj: TypedLiteral
    kind: AssertionKind
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


def _inherited_unit(
    unit: Knowledge[Unit],
) -> tuple[Knowledge[Unit], tuple[EvidenceRef, ...]] | None:
    """A declared unit as a literal carries it (its provenance the claim's), and what it cited;
    ``None`` for a state a number's literal cannot hold (``KnownAbsent``, ``NotCovered``)."""
    if isinstance(unit, Known):
        cited = (unit.provenance.evidence,) if isinstance(unit.provenance, Provenance) else ()
        return Known(unit.value), cited
    if isinstance(unit, Unknown):
        return Unknown(), ()
    if isinstance(unit, Ambiguous):
        readings = tuple(
            c.provenance.evidence for c in unit.candidates if isinstance(c.provenance, Provenance)
        )
        return Ambiguous(tuple(Candidate(c.value) for c in unit.candidates)), readings
    return None


_SCALAR_TYPES: Final[Mapping[ScalarType, DeclaredType]] = {
    ScalarType.BOOL: DeclaredType.BOOLEAN,
    ScalarType.INT: DeclaredType.INTEGER,
    ScalarType.FLOAT: DeclaredType.REAL,
}


class _Counts:
    """Values that state no claim, by reason, for one finding each."""

    def __init__(self) -> None:
        self.by_reason: dict[str, list[RecordId]] = defaultdict(list)

    def add(self, reason: str, record: RecordId) -> None:
        self.by_reason[reason].append(record)

    def findings(self) -> list[ConsolidationFinding]:
        why = {
            "value_absent": "declares a null: a known absence, so no value is claimed",
            "value_ambiguous": "states its value ambiguously; no reading is claimed",
            "value_unstated": "states no value (blank or unreadable); nothing is claimed",
            "value_unlocated": "cites no place of its own for its value; nothing is claimed",
            "unit_unrepresented": "states a number whose unit is neither known, unknown nor"
            " ambiguous; nothing is claimed",
        }
        return [
            _finding(
                reason,
                f"a declared value {why[reason]}",
                sorted(set(records))[:MAX_LISTED],
                count=len(records),
            )
            for reason, records in sorted(self.by_reason.items())
        ]


def _config_value(value: ConfigurationValue, counts: _Counts) -> _Fact | None:
    """A configuration value's scalar as a ``declared_value``; containers and aliases are not
    values (their leaves are), and a value that is not ``Known`` is counted."""
    state = value.value
    if isinstance(state, KnownAbsent):
        counts.add("value_absent", value.id)
        return None
    if isinstance(state, Ambiguous):
        counts.add("value_ambiguous", value.id)
        return None
    if not isinstance(state, Known):
        counts.add("value_unstated", value.id)
        return None
    scalar = state.value
    if not isinstance(scalar, ConfigScalar) or not value.path:
        return None
    kind = _SCALAR_TYPES.get(scalar.type, DeclaredType.TEXT)
    declared = DeclaredValue(value.path, kind, scalar.value)
    unit: Knowledge[Unit] = Unknown()  # a configuration format declares no unit for a number
    literal = (
        TypedLiteral(ValueType.DECLARED_VALUE, declared, unit)
        if declared.numeric
        else TypedLiteral(ValueType.DECLARED_VALUE, declared)
    )
    own = state.provenance
    evidence = own.evidence if isinstance(own, Provenance) else value.provenance.evidence
    kind_of = own.assertion_kind if isinstance(own, Provenance) else value.provenance.assertion_kind
    return _Fact(DECLARED_VALUE, literal, kind_of, (evidence,), (value.id, value.snapshot))


def _parameter(record: Calibration, parameter: DeclaredParameter, counts: _Counts) -> _Fact | None:
    """A calibration parameter as a ``declared_value`` at its declared name, cited where its value
    appears; its unit as declared."""
    state = parameter.value
    if isinstance(state, Ambiguous):
        counts.add("value_ambiguous", record.id)
        return None
    if not isinstance(state, Known):
        counts.add("value_unstated", record.id)
        return None
    if not isinstance(state.provenance, Provenance):
        counts.add("value_unlocated", record.id)
        return None
    raw = state.value
    cited: tuple[EvidenceRef, ...] = (state.provenance.evidence,)
    if isinstance(raw, str):
        literal = TypedLiteral(
            ValueType.DECLARED_VALUE,
            DeclaredValue((parameter.name,), DeclaredType.TEXT, raw),
        )
    else:
        numbers = tuple(v for v in raw if isinstance(v, float | NonFinite))
        unit = _inherited_unit(parameter.unit)
        if not numbers or len(numbers) != len(raw) or unit is None:
            counts.add("unit_unrepresented" if unit is None else "value_unstated", record.id)
            return None
        literal = TypedLiteral(
            ValueType.DECLARED_VALUE,
            DeclaredValue((parameter.name,), DeclaredType.REALS, numbers),
            unit[0],
        )
        cited += unit[1]
    return _Fact(DECLARED_VALUE, literal, state.provenance.assertion_kind, cited, (record.id,))


def _names(view: _View) -> dict[str, list[SourceRevision]]:
    """Every revision with a text path, by the content id of its bytes."""
    out: dict[str, list[SourceRevision]] = defaultdict(list)
    for revision in view.of("source_revision", SourceRevision):
        if isinstance(revision.location, LocalPath):
            out[revision.content_id].append(revision)
    return out


def _name_fact(
    anchor: ConfigurationSnapshot | Calibration,
    revisions: Mapping[str, list[SourceRevision]],
    findings: list[ConsolidationFinding],
) -> _Fact | None:
    """The path the anchor's bytes were found at, ``observed``; none for bytes found at none or
    at several."""
    evidence = anchor.provenance.evidence
    found = revisions.get(str(evidence.source), [])
    paths = sorted({r.location.path for r in found if isinstance(r.location, LocalPath)})
    if len(paths) != 1:
        if paths:
            findings.append(
                _finding(
                    "name_ambiguous",
                    "the configuration's bytes were found at several paths; it is given no name",
                    (anchor.id,),
                    paths=list(paths[:MAX_LISTED]),
                )
            )
        return None
    return _Fact(
        HAS_NAME,
        TypedLiteral(ValueType.TEXT, paths[0]),
        AssertionKind.OBSERVED,
        (evidence,),
        (anchor.id, *sorted(r.id for r in found)),
    )


def _run_names(
    view: _View, runs: Mapping[str, NodeRef], findings: list[ConsolidationFinding]
) -> dict[NodeRef, list[_Fact]]:
    """The name each declaration gives its run, by run node; a run named two ways is named by
    none of them."""
    out: dict[NodeRef, list[_Fact]] = defaultdict(list)
    for declaration in view.of("run_declaration", RunDeclaration):
        node = runs.get(declaration.run)
        name = declaration.logical_id
        if node is None:
            continue
        if isinstance(name, Ambiguous):
            findings.append(
                _finding(
                    "name_ambiguous", "the run's declared name is ambiguous", (declaration.id,)
                )
            )
            continue
        if not isinstance(name, Known) or not is_declared_value(name.value.value):
            continue
        cited = (
            name.provenance.evidence
            if isinstance(name.provenance, Provenance)
            else declaration.provenance.evidence
        )
        kind = (
            name.provenance.assertion_kind
            if isinstance(name.provenance, Provenance)
            else declaration.provenance.assertion_kind
        )
        out[node].append(
            _Fact(
                HAS_NAME,
                TypedLiteral(ValueType.TEXT, name.value.value),
                kind,
                (cited,),
                (declaration.id, declaration.run),
            )
        )
    for node in sorted(out, key=lambda n: n.node_id):
        texts = {f.obj.value for f in out[node]}
        if len(texts) > 1:
            findings.append(
                _finding(
                    "name_conflict",
                    "declarations name the run differently; it is given no name",
                    sorted({r for f in out[node] for r in f.records}),
                    Severity.WARNING,
                    run=node.node_id,
                    names=sorted(str(t) for t in texts),
                )
            )
            del out[node]
    return out


class DeclaredConsolidator:
    """Declared names and values (ADR 0025). Takes no config."""

    consolidator_id: Final = DECLARED_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = _read(ledger)
        findings = view.findings
        if config:
            findings.append(
                _finding(
                    "unknown_config",
                    "the declared consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        places, runs = _places(previous)
        counts = _Counts()
        revisions = _names(view)
        snapshots = view.of("configuration_snapshot", ConfigurationSnapshot)
        calibrations = view.of("calibration", Calibration)
        values: dict[str, list[ConfigurationValue]] = defaultdict(list)
        for value in view.of("configuration_value", ConfigurationValue):
            values[value.snapshot].append(value)
        anchors: list[ConfigurationSnapshot | Calibration] = [*snapshots, *calibrations]
        threads = catalog_threads(ledger, (a.id for a in anchors))
        facts: dict[NodeRef, list[_Fact]] = defaultdict(list)
        unthreaded: list[RecordId] = []
        for anchor in anchors:
            nodes = sorted(
                threads.subject_of(anchor.id, NodeType.CONFIGURATION), key=lambda n: n.node_id
            )
            if not nodes:
                unthreaded.append(anchor.id)
                continue
            stated: list[_Fact] = []
            name = _name_fact(anchor, revisions, findings)
            if name is not None:
                stated.append(name)
            if isinstance(anchor, ConfigurationSnapshot):
                for value in values.get(anchor.id, ()):
                    fact = _config_value(value, counts)
                    if fact is not None:
                        stated.append(fact)
            else:
                for parameter in anchor.parameters:
                    fact = _parameter(anchor, parameter, counts)
                    if fact is not None:
                        stated.append(fact)
            for node in nodes:
                facts[node].extend(stated)
        for node, named in _run_names(view, runs, findings).items():
            facts[node].extend(named)
        drafts: list[ClaimDraft] = []
        unplaced: dict[str, int] = {}
        for node in sorted(facts, key=lambda n: (n.node_type, n.node_id)):
            where = places.get(node, [])
            if not where:
                unplaced[node.node_id] = len(facts[node])
                continue
            for place in where:
                drafts.extend(
                    ClaimDraft(
                        subject=node,
                        predicate=fact.predicate,
                        object=fact.obj,
                        valid_from=place.start,
                        valid_to=place.end,
                        assertion_kind=fact.kind,
                        evidence=fact.evidence,
                        records=(*fact.records, *place.records),
                    )
                    for fact in facts[node]
                )
        if unplaced:
            findings.append(
                _finding(
                    "unplaced",
                    "no consolidator places these configurations or runs in time (no binding,"
                    " no machine span), so what they declare is claimed over no interval",
                    count=sum(unplaced.values()),
                    nodes=sorted(unplaced)[:MAX_LISTED],
                    node_count=len(unplaced),
                )
            )
        if unthreaded:
            findings.append(
                _finding(
                    "unthreaded",
                    "the Ledger names no configuration thread these records open, so what they"
                    " declare names no node",
                    sorted(unthreaded)[:MAX_LISTED],
                    count=len(unthreaded),
                )
            )
        findings.extend(counts.findings())
        return ConsolidatorOutput(tuple(drafts), tuple(findings))
