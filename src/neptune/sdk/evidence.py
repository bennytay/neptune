"""Explain any value of a package: where it came from, how, and what covers it (ADR 0070).

::

    from neptune.sdk import read_package
    from neptune.sdk.evidence import Evidence

    evidence = Evidence(read_package(path))
    print(evidence.value(stream_id, "/schema_name").render())   # one field or value
    print(evidence.row(stream_id, seq=12).render())              # one series row
    print(evidence.record(run_id).render())                      # a container and its members
    print(evidence.coverage().render())                          # every field's states

A value is selected the way the receipt names an ambiguous field: a record's id (an artifact's
content id) and an RFC 6901 pointer into the record's stored JSON line. Its explanation is the
grounding that applies there: the innermost ``Knowledge`` state enclosing the pointer, with its
own provenance or, where it inherits, its record's (ADR 0023 §4: however deep it is nested). A
structural field has its record's provenance (ADR 0006 §1). Each citation resolves to the
source's artifact and the locations holding it, the locator path, the assertion kind and the
transform chain: the transform, then every transform it names as ``upstream``, depth first.

- A table row cited as ``Row(r)`` gives an inheriting cell its ``RowCell`` (ADR 0020 §5); a
  series row rebuilds its provenance from its stream and its locator columns (ADR 0018).
- A derived line (``derived/<kind>.jsonl``) is grounded by its own envelope: its assertion kind,
  the evidence it lists and its transform. A line listing no evidence is explained by the
  records it names.
- A container's provenance is aggregated over the container and its members, transitively: a
  run's streams, a table's rows, a document's blocks, a snapshot's values, a hardware
  configuration's components, a frame graph's frames and transforms, a calibration's extrinsics,
  and the records a derived line names (``PARTS``).
- Coverage counts every state per record kind and field (array positions as ``*``), per source,
  and per series column.

Nothing here reads a source's bytes or changes the package. Citations the package cannot back
are ``IngestFinding``\\s from the ``neptune.evidence`` transform, never exceptions: a source it
holds no artifact for, a byte range past the artifact's end, a transform it lacks. A request the
package cannot answer (an unknown record, a pointer to nothing) is an ``InvalidRequestError``.
Everything is ordered and every ``dumps()`` is canonical JSON, so the same package gives the same
bytes.
"""

import dataclasses
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import record_key
from neptune.model.knowledge import (
    Ambiguous,
    Inherited,
    Known,
    KnownAbsent,
    KnowledgeState,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    Provenance,
    TransformRecord,
    evidence_ref_from_json,
    provenance_from_json,
)
from neptune.model.references import named
from neptune.model.run import Stream
from neptune.model.series import SEQ, STATE, TIME, VALUE, state_column, time_column
from neptune.sdk.errors import InvalidRequestError
from neptune.store.package import IngestPackage
from neptune.store.series import READ_ROWS

SCHEMA: Final = "neptune.evidence/1"
EVIDENCE_ID: Final = "neptune.evidence"
EVIDENCE_VERSION: Final = "0.1.0"
SOURCE_MISSING: Final = f"{EVIDENCE_ID}.source_missing"
LOCATOR_OUT_OF_RANGE: Final = f"{EVIDENCE_ID}.locator_out_of_range"
TRANSFORM_MISSING: Final = f"{EVIDENCE_ID}.transform_missing"
RECORDS: Final = "records"
DERIVED: Final = "derived"

# Records that are the ledger or findings: their ids come from their content and they cite no
# evidence of their own (ADR 0017 §3), so nothing about them is explained or covered.
LEDGER: Final = frozenset(
    {"source_artifact", "source_revision", "source_absence", "transform_record", "ingest_finding"}
)
# Part-of relations between canonical records: ``(member kind, field)`` -> the container kind the
# field names. A container's provenance is aggregated over these members.
PARTS: Final[Mapping[tuple[str, str], str]] = {
    ("configuration_value", "snapshot"): "configuration_snapshot",
    ("document_block", "document"): "document_record",
    ("frame", "ref"): "frame_graph",
    ("frame_transform", "parent"): "frame_graph",
    ("hardware_component", "configuration"): "hardware_configuration",
    ("stream", "run"): "run",
    ("structured_record", "table"): "structured_table",
}
# Containers that list their members themselves: ``(container kind, field)``.
LISTS: Final = frozenset({("calibration", "extrinsics")})
_STATES: Final = (Known, KnownAbsent, Unknown, NotCovered, NotApplicable, Ambiguous)
_OPAQUE: Final = (Provenance, EvidenceRef, TransformRecord)
_STATE_KEYS: Final = frozenset({"knowledge", "value", "provenance", "candidates"})
_RECORD_ID: Final = re.compile(r"rec:sha256:[0-9a-f]{64}")
_CELL: Final = re.compile(r"/cells/(0|[1-9][0-9]*)")
_INDEX: Final = re.compile(r"0|[1-9][0-9]*")


def evidence_transform() -> TransformRecord:
    """The explainer as a producer: its findings name this transform."""
    return transform_record(adapter_id=EVIDENCE_ID, adapter_version=EVIDENCE_VERSION, config={})


def _token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _tokens(pointer: str) -> list[str]:
    """An RFC 6901 pointer's reference tokens, unescaped; ``InvalidRequestError`` if malformed."""
    if not isinstance(pointer, str):
        raise InvalidRequestError(f"a pointer is a string, got {type(pointer).__name__}")
    if pointer == "":
        return []
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise InvalidRequestError(f"{pointer!r} is not an RFC 6901 pointer")
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


def _resolve(document: JsonValue, pointer: str) -> JsonValue:
    """The value ``pointer`` names in ``document``; ``InvalidRequestError`` if it names none."""
    value = document
    for token in _tokens(pointer):
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and _INDEX.fullmatch(token) and int(token) < len(value):
            value = value[int(token)]
        else:
            raise InvalidRequestError(f"{pointer!r} names nothing in the record")
    return value


def _within(pointer: str, scope: str) -> bool:
    return pointer == scope or pointer.startswith(scope + "/")


def _source_key(source: JsonValue) -> str:
    return source if isinstance(source, str) else canonical_json.dumps(source).decode("utf-8")


def _counts(counter: Mapping[str, int]) -> JsonObject:
    return {key: counter[key] for key in sorted(counter) if counter[key]}


# --- What a citation resolves to ---------------------------------------------------------------


@dataclass(frozen=True)
class CitedSource:
    """A cited source as the package holds it: its artifact's size, how its bytes are stored, and
    every location seen holding them. ``held`` is false for a content id the package has no
    artifact for; an external object not yet fetched is ``external``."""

    source: JsonValue
    held: bool
    external: bool = False
    size: int | None = None
    storage: str | None = None
    locations: tuple[JsonValue, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "external": self.external,
            "held": self.held,
            "locations": list(self.locations),
            "size": self.size,
            "source": self.source,
            "storage": self.storage,
        }


@dataclass(frozen=True)
class TransformStep:
    """One transform of a chain: what produced the value or what that producer consumed.
    ``held`` is false for a transform the package does not hold."""

    id: str
    held: bool
    adapter_id: str | None = None
    adapter_version: str | None = None
    config_hash: str | None = None
    upstream: tuple[str, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "config_hash": self.config_hash,
            "held": self.held,
            "id": self.id,
            "upstream": list(self.upstream),
        }


@dataclass(frozen=True)
class Citation:
    """One grounding, resolved: where (``evidence``, each with its source), how (``chain``, the
    producing transform first) and as what (``assertion_kind``).

    ``origin`` says whose grounding it is: ``record`` (the record's own, or a state inheriting
    it), ``state`` (a state's own), ``candidate`` (one reading of an ``Ambiguous`` state), ``cell``
    (a table cell's ``RowCell`` under a row cited as ``Row``), ``row`` (a series row) or
    ``derived`` (a derived line's envelope). ``cites`` lists the records a derived line names
    when it lists no evidence."""

    origin: str
    assertion_kind: str
    evidence: tuple[EvidenceRef, ...]
    sources: tuple[CitedSource, ...]
    chain: tuple[TransformStep, ...]
    cites: tuple[str, ...] = ()

    @property
    def transform(self) -> str:
        return self.chain[0].id

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": self.assertion_kind,
            "chain": [step.to_json() for step in self.chain],
            "cites": list(self.cites),
            "evidence": [ref.to_json() for ref in self.evidence],
            "origin": self.origin,
            "sources": [source.to_json() for source in self.sources],
        }

    def render(self, indent: str = "  ") -> list[str]:
        lines = [f"{indent}{self.assertion_kind} ({self.origin})"]
        for ref, source in zip(self.evidence, self.sources, strict=True):
            where = ", ".join(_show(location) for location in source.locations) or "-"
            state = "" if source.held or source.external else "  [NOT IN PACKAGE]"
            lines.append(f"{indent}  source  {_source_key(source.source)}  ({where}){state}")
            steps = " > ".join(_show(step) for step in ref.locator_json())
            lines.append(f"{indent}  at      {steps}")
        for cited in self.cites:
            lines.append(f"{indent}  names   {cited}")
        for depth, step in enumerate(self.chain):
            label = "by" if depth == 0 else "from"
            if step.held:
                lines.append(
                    f"{indent}  {label:<7} {step.adapter_id} {step.adapter_version}"
                    f"  config {step.config_hash}  ({step.id})"
                )
            else:
                lines.append(f"{indent}  {label:<7} {step.id}  [NOT IN PACKAGE]")
        return lines


def _show(value: JsonValue) -> str:
    return canonical_json.dumps(value).decode("utf-8")


# --- States inside a record --------------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    """One grounding slot of a state: the state's own, or one candidate's. ``grounding`` is a
    ``Provenance``, ``None`` where it inherits, or a derived state's raw provenance JSON."""

    pointer: str
    origin: str
    grounding: Any


@dataclass(frozen=True)
class _State:
    pointer: str
    field: str
    state: KnowledgeState
    slots: tuple[_Slot, ...]


def _own(slot: object) -> object:
    return None if isinstance(slot, Inherited) else slot


def _slots(state: Any, pointer: str) -> tuple[_Slot, ...]:
    match state:
        case Known(provenance=p) | Unknown(provenance=p) | NotCovered(provenance=p):
            return (_Slot(pointer, "state", _own(p)),)
        case KnownAbsent(provenance=p):
            return (_Slot(pointer, "state", p),)
        case Ambiguous(candidates=candidates):
            return tuple(
                _Slot(f"{pointer}/candidates/{i}", "candidate", _own(c.provenance))
                for i, c in enumerate(candidates)
            )
    return ()  # NotApplicable: a schema fact, grounded in nothing


def _typed_states(value: object, data: JsonValue, pointer: str, path: str) -> Iterator[_State]:
    """Every ``Knowledge`` state in a canonical record, typed, at its pointer into the record's
    JSON. The walk follows the JSON beside the types, so an inheriting ``Known`` list stored as a
    bare array (ADR 0061 §4) is still a state; where they part (a value stored in another shape),
    nothing below holds a state and the walk stops."""
    if isinstance(value, _STATES):
        yield _State(pointer, path, value.state, _slots(value, pointer))
        wrapped = isinstance(data, Mapping) and data.get("knowledge") == str(value.state)
        if isinstance(value, Known):
            if wrapped:
                assert isinstance(data, Mapping)
                yield from _typed_states(
                    value.value, data["value"], f"{pointer}/value", f"{path}/value"
                )
            else:
                yield from _typed_states(value.value, data, pointer, path)
        elif isinstance(value, Ambiguous) and wrapped:
            assert isinstance(data, Mapping)
            rows = data["candidates"]
            assert isinstance(rows, list)
            for i, candidate in enumerate(value.candidates):
                row = rows[i]
                assert isinstance(row, Mapping)
                yield from _typed_states(
                    candidate.value,
                    row["value"],
                    f"{pointer}/candidates/{i}/value",
                    f"{path}/candidates/*/value",
                )
    elif isinstance(value, tuple):
        if isinstance(data, list) and len(data) == len(value):
            for i, (item, raw) in enumerate(zip(value, data, strict=True)):
                yield from _typed_states(item, raw, f"{pointer}/{i}", f"{path}/*")
    elif (
        dataclasses.is_dataclass(value)
        and not isinstance(value, (type, *_OPAQUE))
        and isinstance(data, Mapping)
    ):
        for item in dataclasses.fields(value):
            if item.name == "provenance" or item.name not in data:
                continue
            token = _token(item.name)
            yield from _typed_states(
                getattr(value, item.name), data[item.name], f"{pointer}/{token}", f"{path}/{token}"
            )


def _json_states(data: JsonValue, pointer: str, path: str) -> Iterator[_State]:
    """Every state in a derived line, by its JSON shape alone (``neptune.derived`` owns the
    types). A derived state inherits its line's grounding unless it carries its own."""
    if isinstance(data, Mapping):
        tag = data.get("knowledge")
        if tag in set(KnowledgeState) and data.keys() <= _STATE_KEYS:
            state = KnowledgeState(str(tag))
            if state is KnowledgeState.AMBIGUOUS:
                rows = data.get("candidates")
                rows = rows if isinstance(rows, list) else []
                slots = tuple(
                    _Slot(
                        f"{pointer}/candidates/{i}",
                        "candidate",
                        row.get("provenance") if isinstance(row, Mapping) else None,
                    )
                    for i, row in enumerate(rows)
                )
                yield _State(pointer, path, state, slots)
                for i, row in enumerate(rows):
                    if isinstance(row, Mapping) and "value" in row:
                        yield from _json_states(
                            row["value"],
                            f"{pointer}/candidates/{i}/value",
                            f"{path}/candidates/*/value",
                        )
                return
            slots = () if state is KnowledgeState.NOT_APPLICABLE else (
                _Slot(pointer, "state", data.get("provenance")),
            )
            yield _State(pointer, path, state, slots)
            if "value" in data:
                yield from _json_states(data["value"], f"{pointer}/value", f"{path}/value")
            return
        for key in sorted(data):
            if pointer == "" and key in ("evidence", "transform", "id", "kind"):
                continue
            yield from _json_states(data[key], f"{pointer}/{_token(key)}", f"{path}/{_token(key)}")
    elif isinstance(data, list):
        for i, item in enumerate(data):
            yield from _json_states(item, f"{pointer}/{i}", f"{path}/*")


def _named_ids(data: JsonValue, top: bool = True) -> Iterator[str]:
    """Every record id a derived line names, its own id and transform aside."""
    if isinstance(data, str):
        if _RECORD_ID.fullmatch(data):
            yield data
    elif isinstance(data, Mapping):
        for key in sorted(data):
            if not (top and key in ("id", "transform")):
                yield from _named_ids(data[key], top=False)
    elif isinstance(data, list):
        for item in data:
            yield from _named_ids(item, top=False)


# --- Explanations ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordRef:
    """A record by its key, kind and table (``records`` or ``derived``)."""

    id: str
    kind: str
    table: str

    def to_json(self) -> JsonObject:
        return {"id": self.id, "kind": self.kind, "table": self.table}

    def show(self) -> str:
        return f"{self.table}/{self.kind} {self.id}"


def _findings_json(findings: Sequence[IngestFinding]) -> list[JsonValue]:
    return [finding.to_json() for finding in findings]


def _render_findings(findings: Sequence[IngestFinding]) -> list[str]:
    return [f"  finding {f.severity} {f.code}: {f.message}" for f in findings]


@dataclass(frozen=True)
class ValueExplanation:
    """One selected value: the record, the pointer, the value there, the innermost state enclosing
    it (``None`` for a structural field) and the citations that ground it. ``record_citation`` is
    the record's own (``None`` for the ledger)."""

    record: RecordRef
    pointer: str
    value: JsonValue
    state: str | None
    state_pointer: str | None
    field: str | None
    citations: tuple[Citation, ...]
    record_citation: Citation | None
    findings: tuple[IngestFinding, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "citations": [c.to_json() for c in self.citations],
            "field": self.field,
            "findings": _findings_json(self.findings),
            "pointer": self.pointer,
            "record": self.record.to_json(),
            "record_citation": None if self.record_citation is None
            else self.record_citation.to_json(),
            "schema": SCHEMA,
            "state": self.state,
            "state_pointer": self.state_pointer,
            "type": "value",
            "value": self.value,
        }

    def dumps(self) -> bytes:
        return canonical_json.dumps(self.to_json())

    def render(self) -> str:
        lines = [f"{self.record.show()}  {self.pointer or '(whole record)'}"]
        lines.append(f"  value   {_abridge(_show(self.value))}")
        if self.state is None:
            lines.append("  state   structural: the record's own citation covers it")
        else:
            lines.append(f"  state   {self.state} at {self.state_pointer} (field {self.field})")
        if not self.citations:
            lines.append("  cites   nothing: not applicable is a schema fact" if self.state
                         == str(KnowledgeState.NOT_APPLICABLE) else "  cites   nothing")
        for citation in self.citations:
            lines += citation.render()
        lines += _render_findings(self.findings)
        return "\n".join(lines) + "\n"


def _abridge(text: str, width: int = 160) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


@dataclass(frozen=True)
class RowExplanation:
    """One series row: its stream, its ``seq``, the provenance its stream's template and its
    locator columns rebuild, its time on each clock and its value columns with their states."""

    stream: str
    seq: int
    citation: Citation
    times: tuple[JsonObject, ...]
    values: tuple[JsonObject, ...]
    findings: tuple[IngestFinding, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "citation": self.citation.to_json(),
            "findings": _findings_json(self.findings),
            "schema": SCHEMA,
            "seq": self.seq,
            "stream": self.stream,
            "times": list(self.times),
            "type": "row",
            "values": list(self.values),
        }

    def dumps(self) -> bytes:
        return canonical_json.dumps(self.to_json())

    def render(self) -> str:
        lines = [f"series row seq {self.seq} of stream {self.stream}"]
        for time in self.times:
            ticks = "" if time["ticks"] is None else f" {time['ticks']}"
            lines.append(f"  time    clock {time['clock']}: {time['state']}{ticks}")
        for value in self.values:
            lines.append(f"  {value['column']}: {value['state']} {_abridge(_show(value['value']))}")
        lines += self.citation.render()
        lines += _render_findings(self.findings)
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Member:
    """A record aggregated into a container, and the relation that reached it."""

    record: RecordRef
    via: str

    def to_json(self) -> JsonObject:
        return {**self.record.to_json(), "via": self.via}


@dataclass
class _Tally:
    records: int = 0
    values: int = 0
    states: Counter[str] = field(default_factory=Counter)
    kinds: Counter[str] = field(default_factory=Counter)
    sources: Counter[str] = field(default_factory=Counter)
    transforms: Counter[str] = field(default_factory=Counter)

    def cite(self, citation: Citation) -> None:
        self.kinds[citation.assertion_kind] += 1
        for source in {_source_key(s.source) for s in citation.sources}:
            self.sources[source] += 1
        self.transforms[citation.transform] += 1


@dataclass(frozen=True)
class RecordProvenance:
    """A record's provenance aggregated over it and its members (``PARTS``), transitively.

    ``citations`` counts every grounding: each record's own and each state's (an inheriting state
    counts its record's evidence again; each ``Ambiguous`` candidate counts once). ``states``
    counts the values by state, ``assertion_kinds`` the citations by kind, ``sources`` and
    ``transforms`` the citations per source and per producing transform. ``series`` lists each
    member stream's rows with the provenance its rows share. ``qualified_by`` names the package's
    findings that name any of these records."""

    record: RecordRef
    citation: Citation | None
    members: tuple[Member, ...]
    records: int
    values: int
    states: JsonObject
    assertion_kinds: JsonObject
    sources: tuple[JsonObject, ...]
    transforms: tuple[JsonObject, ...]
    series: tuple[JsonObject, ...]
    qualified_by: tuple[str, ...]
    findings: tuple[IngestFinding, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "assertion_kinds": self.assertion_kinds,
            "citation": None if self.citation is None else self.citation.to_json(),
            "findings": _findings_json(self.findings),
            "members": [m.to_json() for m in self.members],
            "qualified_by": list(self.qualified_by),
            "record": self.record.to_json(),
            "records": self.records,
            "schema": SCHEMA,
            "series": list(self.series),
            "sources": list(self.sources),
            "states": self.states,
            "transforms": list(self.transforms),
            "type": "record",
            "values": self.values,
        }

    def dumps(self) -> bytes:
        return canonical_json.dumps(self.to_json())

    def render(self) -> str:
        lines = [self.record.show()]
        if self.citation is not None:
            lines += self.citation.render()
        lines.append(
            f"  {self.records} records ({len(self.members)} members), {self.values} values:"
            f" {_pairs(self.states)}; citations {_pairs(self.assertion_kinds)}"
        )
        for member in self.members:
            lines.append(f"  member  {member.record.show()}  via {member.via}")
        for source in self.sources:
            lines.append(f"  source  {_source_key(source['source'])}: {source['citations']}")
        for transform in self.transforms:
            lines.append(
                f"  by      {transform['adapter_id']} {transform['adapter_version']}"
                f" ({transform['id']}): {transform['citations']}"
            )
        for series in self.series:
            lines.append(f"  series  {series['stream']}: {series['rows']} rows")
        for finding in self.qualified_by:
            lines.append(f"  qualified by finding {finding}")
        lines += _render_findings(self.findings)
        return "\n".join(lines) + "\n"


def _pairs(counts: Mapping[str, JsonValue]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(counts.items())) or "none"


@dataclass(frozen=True)
class FieldCoverage:
    """One field of one kind: how many values hold each state, and their assertion kinds."""

    field: str
    states: JsonObject
    assertion_kinds: JsonObject

    def to_json(self) -> JsonObject:
        return {
            "assertion_kinds": self.assertion_kinds,
            "field": self.field,
            "states": self.states,
        }


@dataclass(frozen=True)
class KindCoverage:
    """One record kind (of ``records`` or ``derived``): its records and each field's states."""

    kind: str
    table: str
    records: int
    fields: tuple[FieldCoverage, ...]

    def to_json(self) -> JsonObject:
        return {
            "fields": [f.to_json() for f in self.fields],
            "kind": self.kind,
            "records": self.records,
            "table": self.table,
        }


@dataclass(frozen=True)
class SourceCoverage:
    """What one source grounds: per kind, its records and their fields' states. ``source`` is
    ``None`` for derived lines that list no evidence."""

    source: JsonValue
    kinds: tuple[KindCoverage, ...]

    def to_json(self) -> JsonObject:
        return {"kinds": [k.to_json() for k in self.kinds], "source": self.source}


@dataclass(frozen=True)
class SeriesCoverage:
    """One stream's series: its rows, the provenance they share, and each time and value
    column's states."""

    stream: str
    source: str
    transform: str
    assertion_kind: str
    rows: int
    columns: tuple[JsonObject, ...]

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": self.assertion_kind,
            "columns": list(self.columns),
            "rows": self.rows,
            "source": self.source,
            "stream": self.stream,
            "transform": self.transform,
        }


@dataclass(frozen=True)
class CoverageReport:
    """Every state of a package, per record kind and field, per source, and per series column,
    with the findings for citations the package cannot back."""

    package: str
    kinds: tuple[KindCoverage, ...]
    sources: tuple[SourceCoverage, ...]
    series: tuple[SeriesCoverage, ...]
    findings: tuple[IngestFinding, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "findings": _findings_json(self.findings),
            "kinds": [k.to_json() for k in self.kinds],
            "package": self.package,
            "schema": SCHEMA,
            "series": [s.to_json() for s in self.series],
            "sources": [s.to_json() for s in self.sources],
            "type": "coverage",
        }

    def dumps(self) -> bytes:
        return canonical_json.dumps(self.to_json())

    def render(self) -> str:
        lines = [f"coverage of package {self.package}"]
        for kind in self.kinds:
            lines.append(f"  {kind.table}/{kind.kind}: {kind.records} records")
            for item in kind.fields:
                lines.append(f"    {item.field:<40} {_pairs(item.states)}")
        for source in self.sources:
            key = "(no evidence)" if source.source is None else _source_key(source.source)
            total = sum(k.records for k in source.kinds)
            lines.append(f"  source {key}: {total} records")
            for kind in source.kinds:
                states: Counter[str] = Counter()
                for item in kind.fields:
                    states.update({k: int(str(v)) for k, v in item.states.items()})
                lines.append(f"    {kind.kind}: {kind.records} records; {_pairs(dict(states))}")
        for series in self.series:
            lines.append(f"  series {series.stream}: {series.rows} rows")
            for column in series.columns:
                states = column["states"]
                assert isinstance(states, Mapping)
                lines.append(f"    {column['column']:<40} {_pairs(states)}")
        lines += _render_findings(self.findings)
        return "\n".join(lines) + "\n"


# --- The explainer -----------------------------------------------------------------------------


class _Findings:
    def __init__(self, transform: TransformRecord) -> None:
        self.transform = transform
        self.found: dict[str, IngestFinding] = {}

    def add(self, finding: IngestFinding) -> None:
        self.found.setdefault(finding.id, finding)

    def sorted(self) -> tuple[IngestFinding, ...]:
        return tuple(sorted(self.found.values(), key=lambda f: (f.code, f.id)))


class Evidence:
    """Explanations over one package: build once, ask many times (the indexes are shared).

    The package is the one ``read_package`` read and verified; an ``IngestPackage`` built by hand
    is explained as far as it goes, and what its citations lack is a finding.
    """

    def __init__(self, package: IngestPackage) -> None:
        self.package = package
        self.transform = evidence_transform()
        self._records: dict[str, Any] = {}
        self._json: dict[str, JsonObject] = {}
        self._derived: dict[str, tuple[str, JsonObject]] = {}
        self._transforms: dict[str, TransformRecord] = {}
        self._artifacts: dict[str, Any] = {}
        self._locations: dict[str, list[JsonValue]] = defaultdict(list)
        self._storage = {h.content_id: str(h.storage) for h in package.manifest.sources}
        self._parts: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._qualified: dict[str, set[str]] = defaultdict(set)
        for record in package.records:
            key = record_key(record)
            self._records[key] = record
            if record.kind == "transform_record":
                self._transforms[record.id] = record
            elif record.kind == "source_artifact":
                self._artifacts[record.content_id] = record
            elif record.kind == "source_revision":
                self._locations[record.content_id].append(record.to_json()["location"])
            elif record.kind == "ingest_finding":
                for named_id in record.records:
                    self._qualified[named_id].add(record.id)
            for name, target in named(record):
                if (record.kind, name) in PARTS:
                    self._parts[target].append((key, f"{record.kind}.{name}"))
        for kind, lines in sorted(package.derived.items()):
            for line in lines:
                self._derived[str(line["id"])] = (kind, line)

    # -- lookups --

    def _ref(self, key: str) -> RecordRef:
        if key in self._records:
            return RecordRef(key, self._records[key].kind, RECORDS)
        if key in self._derived:
            return RecordRef(key, self._derived[key][0], DERIVED)
        raise InvalidRequestError(f"the package holds no record {key!r}")

    def _document(self, key: str) -> JsonObject:
        if key in self._derived:
            return self._derived[key][1]
        if key not in self._json:
            self._json[key] = self._records[key].to_json()
        return self._json[key]

    def _states(self, ref: RecordRef) -> tuple[_State, ...]:
        document = self._document(ref.id)
        if ref.table == DERIVED:
            return tuple(_json_states(document, "", ""))
        if ref.kind in LEDGER:
            return ()
        return tuple(_typed_states(self._records[ref.id], document, "", ""))

    def _source(self, source: object) -> CitedSource:
        if isinstance(source, ExternalObjectRef):
            return CitedSource(source.to_json(), held=False, external=True)
        key = str(source)
        artifact = self._artifacts.get(key)
        if artifact is None:
            return CitedSource(key, held=False)
        locations = sorted(self._locations.get(key, ()), key=_source_key)
        return CitedSource(
            key, True, False, artifact.size, self._storage.get(key), tuple(locations)
        )

    def _chain(self, transform: str) -> tuple[TransformStep, ...]:
        steps: list[TransformStep] = []
        seen: set[str] = set()

        def visit(current: str) -> None:
            if current in seen:
                return
            seen.add(current)
            record = self._transforms.get(current)
            if record is None:
                steps.append(TransformStep(current, held=False))
                return
            steps.append(
                TransformStep(
                    current,
                    True,
                    record.adapter_id,
                    record.adapter_version,
                    record.config_hash,
                    tuple(record.upstream),
                )
            )
            for upstream in record.upstream:
                visit(upstream)

        visit(transform)
        return tuple(steps)

    def _citation(
        self,
        origin: str,
        assertion_kind: str,
        evidence: tuple[EvidenceRef, ...],
        transform: str,
        found: _Findings,
        record: RecordRef,
        pointer: str,
        cites: tuple[str, ...] = (),
    ) -> Citation:
        sources = tuple(self._source(ref.source) for ref in evidence)
        chain = self._chain(transform)
        for ref, source in zip(evidence, sources, strict=True):
            self._check(ref, source, found, record, pointer)
        missing = [step.id for step in chain if not step.held]
        if missing and evidence:
            found.add(
                ingest_finding(
                    code=TRANSFORM_MISSING,
                    category=FindingCategory.MISSING,
                    severity=Severity.WARNING,
                    subject=evidence[0],
                    transform=self.transform,
                    message=f"{record.kind} {record.id} at {pointer or '/'} was produced by"
                    f" {len(missing)} transform(s) the package does not hold",
                    details={"kind": record.kind, "pointer": pointer, "transforms": missing},
                    records=[record.id],
                )
            )
        return Citation(origin, assertion_kind, evidence, sources, chain, cites)

    def _check(
        self,
        ref: EvidenceRef,
        source: CitedSource,
        found: _Findings,
        record: RecordRef,
        pointer: str,
    ) -> None:
        details: dict[str, JsonValue] = {"kind": record.kind, "pointer": pointer}
        if not source.held and not source.external:
            found.add(
                ingest_finding(
                    code=SOURCE_MISSING,
                    category=FindingCategory.MISSING,
                    severity=Severity.WARNING,
                    subject=ref,
                    transform=self.transform,
                    message=f"{record.kind} {record.id} at {pointer or '/'} cites a source the"
                    " package holds no artifact for",
                    details=details,
                    records=[record.id],
                )
            )
            return
        first = ref.locator[0]
        if source.size is not None and isinstance(first, ByteRange):
            if first.offset + first.length > source.size:
                found.add(
                    ingest_finding(
                        code=LOCATOR_OUT_OF_RANGE,
                        category=FindingCategory.INCONSISTENT,
                        severity=Severity.WARNING,
                        subject=ref,
                        transform=self.transform,
                        message=f"{record.kind} {record.id} at {pointer or '/'} cites bytes"
                        f" past the end of its {source.size}-byte source",
                        details={**details, "size": source.size},
                        records=[record.id],
                    )
                )

    def _record_citation(self, ref: RecordRef, found: _Findings) -> Citation | None:
        if ref.table == DERIVED:
            line = self._derived[ref.id][1]
            raw = line.get("evidence")
            evidence = tuple(evidence_ref_from_json(r) for r in raw) if isinstance(raw, list) else ()
            cites = () if evidence else tuple(
                dict.fromkeys(i for i in _named_ids(line) if i != ref.id)
            )
            return self._citation(
                "derived",
                str(line.get("assertion_kind")),
                evidence,
                str(line.get("transform")),
                found,
                ref,
                "",
                cites,
            )
        provenance = getattr(self._records[ref.id], "provenance", None)
        if ref.kind in LEDGER or not isinstance(provenance, Provenance):
            return None
        return self._provenance("record", provenance, found, ref, "")

    def _provenance(
        self, origin: str, provenance: Provenance, found: _Findings, ref: RecordRef, pointer: str
    ) -> Citation:
        return self._citation(
            origin,
            str(provenance.assertion_kind),
            (provenance.evidence,),
            provenance.transform,
            found,
            ref,
            pointer,
        )

    def _slot_citation(
        self,
        ref: RecordRef,
        state: _State,
        slot: _Slot,
        record_citation: Citation | None,
        found: _Findings,
    ) -> Citation | None:
        grounding = slot.grounding
        if isinstance(grounding, Provenance):
            return self._provenance(slot.origin, grounding, found, ref, slot.pointer)
        if isinstance(grounding, Mapping):  # a derived state's own grounding, as stored
            try:
                return self._provenance(
                    slot.origin, provenance_from_json(grounding), found, ref, slot.pointer
                )
            except (ValueError, TypeError):
                raw = grounding.get("evidence")
                refs = tuple(evidence_ref_from_json(r) for r in raw) if isinstance(raw, list) else ()
                return self._citation(
                    slot.origin,
                    str(grounding.get("assertion_kind")),
                    refs,
                    str(grounding.get("transform")),
                    found,
                    ref,
                    slot.pointer,
                )
        if ref.kind == "structured_record" and _CELL.fullmatch(state.pointer):
            cell = self._cell(ref, int(state.pointer.rsplit("/", 1)[1]))
            if cell is not None and record_citation is not None:
                return dataclasses.replace(
                    record_citation, origin="cell", evidence=(cell,)
                )
        return record_citation

    def _cell(self, ref: RecordRef, column: int) -> EvidenceRef | None:
        row = self._records[ref.id]
        table = self._records.get(row.table)
        if table is None or table.kind != "structured_table":
            return None  # validation reports the dangling table; the row's citation stands
        return row.cell_evidence(table, column)  # type: ignore[no-any-return]

    def _state_citations(
        self, ref: RecordRef, state: _State, record_citation: Citation | None, found: _Findings
    ) -> list[Citation]:
        out = []
        for slot in state.slots:
            citation = self._slot_citation(ref, state, slot, record_citation, found)
            if citation is not None:
                out.append(citation)
        return out

    # -- the questions --

    def value(self, record: str, pointer: str = "") -> ValueExplanation:
        """Explain the value ``pointer`` names in record ``record`` (an id, or an artifact's
        content id): its state and every citation grounding it."""
        ref = self._ref(record)
        document = self._document(ref.id)
        value = _resolve(document, pointer)
        found = _Findings(self.transform)
        record_citation = self._record_citation(ref, found)
        enclosing = [s for s in self._states(ref) if _within(pointer, s.pointer)]
        if not enclosing:
            citations = () if record_citation is None else (record_citation,)
            return ValueExplanation(
                ref, pointer, value, None, None, None, citations, record_citation, found.sorted()
            )
        state = max(enclosing, key=lambda s: len(s.pointer))
        slots = [s for s in state.slots if s.origin == "candidate" and _within(pointer, s.pointer)]
        chosen = dataclasses.replace(state, slots=tuple(slots)) if slots else state
        citations = tuple(self._state_citations(ref, chosen, record_citation, found))
        return ValueExplanation(
            ref,
            pointer,
            value,
            str(state.state),
            state.pointer,
            state.field,
            citations,
            record_citation,
            found.sorted(),
        )

    def row(self, stream: str, seq: int, column: str | None = None) -> RowExplanation:
        """Explain series row ``seq`` (its position in source order) of stream ``stream``, and
        one value column of it (``value/<name>``) or all of them."""
        record = self._records.get(stream)
        if not isinstance(record, Stream):
            raise InvalidRequestError(f"the package holds no stream {stream!r}")
        if stream not in self.package.series:
            raise InvalidRequestError(f"stream {stream} has no series file in this package")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise InvalidRequestError(f"seq is a row's position in source order, got {seq!r}")
        row = _find_row(self.package.series[stream], seq)
        if row is None:
            raise InvalidRequestError(f"stream {stream} has no row with seq {seq}")
        names = sorted(c for c in row if c.startswith(f"{VALUE}/"))
        if column is not None:
            if column not in names:
                raise InvalidRequestError(f"stream {stream} has no value column {column!r}")
            names = [column]
        found = _Findings(self.transform)
        ref = RecordRef(stream, "stream", RECORDS)
        try:
            provenance = record.row_provenance(row)
        except ValueError as exc:
            raise InvalidRequestError(f"row {seq} of stream {stream}: {exc}") from exc
        citation = self._provenance("row", provenance, found, ref, f"{SEQ}={seq}")
        times = tuple(
            {
                "clock": clock,
                "state": _cell_state(row, time_column(i)),
                "ticks": row[time_column(i)],  # type: ignore[dict-item]
            }
            for i, clock in enumerate(record.clocks)
        )
        values = tuple(
            {"column": name, "state": _cell_state(row, name), "value": _cell_json(row[name])}
            for name in names
        )
        return RowExplanation(stream, seq, citation, times, values, found.sorted())

    def members(self, record: str) -> tuple[Member, ...]:
        """Every record aggregated into ``record``, transitively, in id order."""
        root = self._ref(record).id
        reached: dict[str, str] = {}
        frontier = [root]
        while frontier:
            following: list[str] = []
            for key in frontier:
                for member, via in self._children(key):
                    if member != root and member not in reached:
                        reached[member] = via
                        following.append(member)
            frontier = sorted(following)
        return tuple(Member(self._ref(key), reached[key]) for key in sorted(reached))

    def _children(self, key: str) -> Iterator[tuple[str, str]]:
        yield from sorted(self._parts.get(key, ()))
        if key in self._derived:
            kind, line = self._derived[key]
            for target in dict.fromkeys(_named_ids(line)):
                if target != key and (target in self._records or target in self._derived):
                    yield target, f"{kind}.names"
        elif key in self._records:
            record = self._records[key]
            for name, target in named(record):
                if (record.kind, name) in LISTS and target in self._records:
                    yield target, f"{record.kind}.{name}"

    def record(self, record: str) -> RecordProvenance:
        """The provenance of ``record`` aggregated over it and every member (``members``)."""
        ref = self._ref(record)
        found = _Findings(self.transform)
        members = self.members(ref.id)
        tally = _Tally()
        series: list[JsonObject] = []
        qualified: set[str] = set()
        own: Citation | None = None
        for current in (ref, *(m.record for m in members)):
            citation = self._tally(current, tally, found)
            if current == ref:
                own = citation
            qualified |= self._qualified.get(current.id, set())
            stream = self._records.get(current.id)
            if isinstance(stream, Stream) and current.id in self.package.series:
                series.append(
                    {
                        "assertion_kind": str(stream.series.assertion_kind),
                        "rows": _row_count(self.package.series[current.id]),
                        "source": stream.series.source,
                        "stream": current.id,
                        "transform": stream.provenance.transform,
                    }
                )
        sources = tuple(
            {**self._source(_parse_source(key)).to_json(), "citations": tally.sources[key]}
            for key in sorted(tally.sources)
        )
        transforms = tuple(
            {
                **self._chain(key)[0].to_json(),
                "chain": [step.id for step in self._chain(key)],
                "citations": tally.transforms[key],
            }
            for key in sorted(tally.transforms)
        )
        return RecordProvenance(
            ref,
            own,
            members,
            tally.records,
            tally.values,
            _counts(tally.states),
            _counts(tally.kinds),
            sources,
            transforms,
            tuple(series),
            tuple(sorted(qualified)),
            found.sorted(),
        )

    def _tally(self, ref: RecordRef, tally: _Tally, found: _Findings) -> Citation | None:
        """Count one record's citations and states into ``tally``; its own citation."""
        if ref.table == RECORDS and ref.kind in LEDGER:
            return None
        citation = self._record_citation(ref, found)
        tally.records += 1
        if citation is not None:
            tally.cite(citation)
        for state in self._states(ref):
            tally.values += 1
            tally.states[str(state.state)] += 1
            for cited in self._state_citations(ref, state, citation, found):
                tally.cite(cited)
        return citation

    def coverage(self, *, series: bool = True) -> CoverageReport:
        """Every state of the package per kind and field, per source and, unless ``series`` is
        false, per series column; with a finding for every citation the package cannot back."""
        found = _Findings(self.transform)
        by_kind: dict[tuple[str, str], _KindTally] = {}
        by_source: dict[str, dict[tuple[str, str], _KindTally]] = defaultdict(dict)
        keys = [k for k in sorted(self._records) if self._records[k].kind not in LEDGER]
        refs = [self._ref(k) for k in keys] + [self._ref(k) for k in sorted(self._derived)]
        for ref in refs:
            slot = (ref.table, ref.kind)
            kind_tally = by_kind.setdefault(slot, _KindTally())
            citation = self._record_citation(ref, found)
            record_sources = _sources_of(citation)
            kind_tally.records += 1
            for source in record_sources:
                by_source[source].setdefault(slot, _KindTally()).records += 1
            for state in self._states(ref):
                citations = self._state_citations(ref, state, citation, found)
                assertion = {c.assertion_kind for c in citations}
                kind_tally.add(state, assertion)
                sources = set().union(*(_sources_of(c) for c in citations)) if citations else (
                    record_sources
                )
                for source in sources:
                    by_source[source].setdefault(slot, _KindTally()).add(state, assertion)
        streams: list[SeriesCoverage] = []
        if series:
            for key in sorted(self.package.series):
                stream = self._records[key]
                rows, columns = _series_states(self.package.series[key])
                streams.append(
                    SeriesCoverage(
                        key,
                        stream.series.source,
                        stream.provenance.transform,
                        str(stream.series.assertion_kind),
                        rows,
                        columns,
                    )
                )
        return CoverageReport(
            str(self.package.id),
            tuple(t.coverage(*slot) for slot, t in sorted(by_kind.items())),
            tuple(
                SourceCoverage(
                    None if source == "" else _parse_source_json(source),
                    tuple(t.coverage(*slot) for slot, t in sorted(kinds.items())),
                )
                for source, kinds in sorted(by_source.items())
            ),
            tuple(streams),
            found.sorted(),
        )


@dataclass
class _KindTally:
    records: int = 0
    states: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    kinds: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))

    def add(self, state: _State, assertion: Iterable[str]) -> None:
        self.states[state.field][str(state.state)] += 1
        for kind in assertion:
            self.kinds[state.field][kind] += 1

    def coverage(self, table: str, kind: str) -> KindCoverage:
        fields = tuple(
            FieldCoverage(name, _counts(self.states[name]), _counts(self.kinds[name]))
            for name in sorted(self.states)
        )
        return KindCoverage(kind, table, self.records, fields)


def _sources_of(citation: Citation | None) -> set[str]:
    """The keys of the sources a citation grounds in; ``""`` for one citing no evidence."""
    if citation is None or not citation.sources:
        return {""}
    return {_source_key(s.source) for s in citation.sources}


def _parse_source_json(key: str) -> JsonValue:
    return key if key.startswith("sha256:") else canonical_json.loads(key.encode("utf-8"))


def _parse_source(key: str) -> object:
    if key.startswith("sha256:"):
        return key
    data = canonical_json.loads(key.encode("utf-8"))
    assert isinstance(data, Mapping)
    return ExternalObjectRef(
        str(data["connector_id"]), str(data["object_id"]), str(data["revision_token"])
    )


# --- Series files ------------------------------------------------------------------------------


def _parquet(content: object) -> Any:
    return pq.ParquetFile(pa.BufferReader(content) if isinstance(content, bytes) else content)


def _row_count(content: object) -> int:
    return int(_parquet(content).metadata.num_rows)


def _find_row(content: object, seq: int) -> dict[str, object] | None:
    """The row whose ``seq`` is ``seq``: one row group is read whole, the rest by ``seq``."""
    parquet = _parquet(content)
    for group in range(parquet.num_row_groups):
        column = parquet.read_row_group(group, columns=[SEQ])[SEQ]
        index = pc.index(column, pa.scalar(seq, pa.int64())).as_py()
        if index is not None and index >= 0:
            table = parquet.read_row_group(group).slice(index, 1)
            rows: list[dict[str, object]] = table.to_pylist()
            return rows[0]
    return None


def _cell_state(row: Mapping[str, object], column: str) -> str:
    tag = row.get(state_column(column), str(KnowledgeState.KNOWN))
    return str(tag)


def _cell_json(value: object) -> JsonValue:
    """A series cell as JSON: bytes by their length, non-finite floats tagged."""
    if isinstance(value, bytes):
        return {"binary_length": len(value)}
    if isinstance(value, float) and value != value:
        return {"non_finite": "nan"}
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        return {"non_finite": "inf" if value > 0 else "-inf"}
    if isinstance(value, list):
        return [_cell_json(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _series_states(content: object) -> tuple[int, tuple[JsonObject, ...]]:
    """A series file's rows and each time and value column's states, read a batch of state
    columns at a time: no value is decoded."""
    parquet = _parquet(content)
    names = parquet.schema_arrow.names
    rows = int(parquet.metadata.num_rows)
    wrapped = [n for n in names if n.startswith((f"{TIME}/", f"{VALUE}/"))]
    stated = [state_column(n) for n in wrapped if state_column(n) in names]
    counts: dict[str, Counter[str]] = {n: Counter() for n in wrapped}
    for name in wrapped:
        if state_column(name) not in names:
            counts[name][str(KnowledgeState.KNOWN)] = rows
    if stated:
        for batch in parquet.iter_batches(batch_size=READ_ROWS, columns=stated):
            for state in stated:
                column = batch.column(state)
                if pa.types.is_dictionary(column.type):
                    column = column.cast(column.type.value_type)
                for item in pc.value_counts(column).to_pylist():
                    counts[state.removeprefix(f"{STATE}/")][str(item["values"])] += int(
                        item["counts"]
                    )
    return rows, tuple(
        {"column": name, "states": _counts(counts[name])} for name in sorted(wrapped)
    )


# --- Shorthands --------------------------------------------------------------------------------


def explain_value(package: IngestPackage, record: str, pointer: str = "") -> ValueExplanation:
    """``Evidence(package).value(record, pointer)``."""
    return Evidence(package).value(record, pointer)


def explain_row(
    package: IngestPackage, stream: str, seq: int, column: str | None = None
) -> RowExplanation:
    """``Evidence(package).row(stream, seq, column)``."""
    return Evidence(package).row(stream, seq, column)


def explain_record(package: IngestPackage, record: str) -> RecordProvenance:
    """``Evidence(package).record(record)``."""
    return Evidence(package).record(record)


def coverage(package: IngestPackage, *, series: bool = True) -> CoverageReport:
    """``Evidence(package).coverage(series=series)``."""
    return Evidence(package).coverage(series=series)
