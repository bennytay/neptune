"""The adapter contract's laws as checks (ADR 0008 §4, ADR 0024).

``check_plan``, ``check_chunk_output`` and ``check_source_output`` raise ``ContractError`` at the
first broken law. The harness runs them on every call, so every adapter's tests do too, and the
runtime (MVL-6) runs them before it stores anything: a buggy adapter fails loudly instead of
writing a package the reader would refuse.

What they check, beyond the types' own validation:

- every record is of a kind the descriptor declares, and its id derives from its record-level
  evidence and the config's transform (ADR 0017 §5); it survives a canonical JSON round trip;
- every provenance in a record names the config's transform, and every evidence reference cites
  the one source the adapter was given;
- every finding was made by that transform, has a declared code, and cites that source;
- every adapter-specific locator step is declared;
- the chunk that emits a ``Stream`` also emits a series batch for it, empty if need be, so every
  stream's columns are typed even when it has no samples;
- across a source's chunks: no record or finding twice, every series batch belongs to a stream
  of the output and keeps its row contract, and the output cites the source at least once.
"""

from collections import defaultdict
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from neptune.adapters.contract import (
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ContractError,
    Plan,
    SourceReader,
)
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.provenance import check_evidence_record_id
from neptune.model.finding import IngestFinding, subject_to_json
from neptune.model.jsonvalue import JsonValue
from neptune.model.kinds import RECORD_KINDS
from neptune.model.provenance import EvidenceRef
from neptune.model.run import Stream

if TYPE_CHECKING:
    from neptune.model.ids import RecordId
    from neptune.model.series import ColumnType


def _dicts(value: JsonValue) -> Iterator[dict[str, JsonValue]]:
    """Every JSON object inside ``value``, ``value`` included."""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _dicts(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _dicts(item)


def _check_citations(
    what: str, data: JsonValue, descriptor: AdapterDescriptor, source: SourceReader, transform: str
) -> None:
    """Provenance names ``transform``; evidence cites ``source``; adapter steps are declared."""
    steps = {step.name for step in descriptor.locator_steps}
    for obj in _dicts(data):
        provenance = obj.keys() == {"assertion_kind", "evidence", "transform"}
        if provenance and obj["transform"] != transform:
            raise ContractError(f"{what}: provenance names transform {obj['transform']}")
        if "locator" in obj and "source" in obj:
            if obj["source"] != source.content_id:
                raise ContractError(f"{what} cites {obj['source']!r}, not its source")
            locator = obj["locator"]
            for step in locator if isinstance(locator, list | tuple) else ():
                kind = step.get("kind") if isinstance(step, dict) else None
                if isinstance(kind, str) and ":" in kind and kind not in steps:
                    raise ContractError(f"{what} uses undeclared locator step {kind!r}")


def check_finding(
    descriptor: AdapterDescriptor,
    source: SourceReader,
    config: AdapterConfig,
    finding: IngestFinding,
) -> None:
    """One finding the adapter made about its source."""
    what = f"finding {finding.code}"
    if finding.transform != config.transform.id:
        raise ContractError(f"{what} names transform {finding.transform}, not the config's")
    try:
        check_ingest_finding(finding)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if finding.code not in {code.name for code in descriptor.finding_codes}:
        raise ContractError(f"{what} is not declared by adapter {descriptor.id}")
    if not isinstance(finding.subject, EvidenceRef):
        raise ContractError(f"{what}: an adapter's finding cites bytes, not a location")
    data: dict[str, JsonValue] = {
        "related": [ref.to_json() for ref in finding.related],
        "subject": subject_to_json(finding.subject),
    }
    _check_citations(what, data, descriptor, source, config.transform.id)


def check_record(
    descriptor: AdapterDescriptor, source: SourceReader, config: AdapterConfig, record: Any
) -> None:
    """One evidence record the adapter emitted."""
    kind = getattr(record, "kind", None)
    what = f"{kind} {getattr(record, 'id', '?')}"
    if kind not in descriptor.record_kinds:
        raise ContractError(f"{what}: adapter {descriptor.id} does not declare kind {kind!r}")
    try:
        check_evidence_record_id(record, config.transform)
        data = record.to_json()
        canonical_json.dumps(data)
        if RECORD_KINDS[kind][1](data) != record:
            raise ContractError(f"{what} does not read back as itself from its JSON")
    except ContractError:
        raise
    except ValueError as exc:
        raise ContractError(f"{what}: {exc}") from exc
    _check_citations(what, data, descriptor, source, config.transform.id)


def _check_owner(chunk: Chunk, source: SourceReader, config: AdapterConfig) -> None:
    if chunk.source != source.content_id or chunk.transform != config.transform.id:
        raise ContractError(f"chunk {chunk.id} is not of this source and transform")


def check_plan(
    descriptor: AdapterDescriptor, source: SourceReader, config: AdapterConfig, plan: Plan
) -> None:
    """A plan: chunks of this source and transform, and well-formed findings."""
    if not isinstance(plan, Plan):
        raise ContractError(f"adapter {descriptor.id}: plan returned {plan!r}")
    for chunk in plan.chunks:
        _check_owner(chunk, source, config)
    for finding in plan.findings:
        check_finding(descriptor, source, config, finding)


def check_chunk_output(
    descriptor: AdapterDescriptor,
    source: SourceReader,
    config: AdapterConfig,
    chunk: Chunk,
    output: ChunkOutput,
) -> None:
    """What ``ingest`` returned for one chunk."""
    if not isinstance(output, ChunkOutput):
        raise ContractError(f"adapter {descriptor.id}: ingest returned {output!r}")
    _check_owner(chunk, source, config)
    for record in output.records:
        check_record(descriptor, source, config, record)
    for finding in output.findings:
        check_finding(descriptor, source, config, finding)
    batched = {batch.stream for batch in output.series}
    for record in output.records:
        if isinstance(record, Stream) and record.id not in batched:
            raise ContractError(
                f"stream {record.id} has no series batch in the chunk that declares it;"
                " emit one, empty if the chunk holds none of its rows"
            )


def check_source_output(
    descriptor: AdapterDescriptor,
    source: SourceReader,
    config: AdapterConfig,
    plan: Plan,
    outputs: Sequence[ChunkOutput],
) -> None:
    """Everything one source produced: the plan's findings and every chunk's output, in order."""
    if len(outputs) != len(plan.chunks):
        raise ContractError(f"{len(plan.chunks)} chunks planned, {len(outputs)} outputs")
    for chunk, output in zip(plan.chunks, outputs, strict=True):
        check_chunk_output(descriptor, source, config, chunk, output)
    records = [record for output in outputs for record in output.records]
    findings = [*plan.findings, *(finding for output in outputs for finding in output.findings)]
    for what, ids in (
        ("record", [record.id for record in records]),
        ("finding", [finding.id for finding in findings]),
    ):
        if len(set(ids)) != len(ids):
            raise ContractError(f"a {what} is emitted twice; each belongs to exactly one chunk")
    if not records and not findings:
        raise ContractError(
            f"adapter {descriptor.id} said nothing about its source: emit a record or a finding"
        )

    streams = {record.id: record for record in records if isinstance(record, Stream)}
    schemas: dict[RecordId, tuple[tuple[str, ColumnType, bool], ...]] = {}
    seqs: dict[RecordId, set[int]] = defaultdict(set)
    for batch in (batch for output in outputs for batch in output.series):
        stream = streams.get(batch.stream)
        if stream is None:
            raise ContractError(f"a series batch names {batch.stream}, not a stream of this source")
        if schemas.setdefault(batch.stream, batch.schema()) != batch.schema():
            raise ContractError(f"stream {batch.stream}: batches disagree on their columns")
        for row in batch.rows():
            try:
                stream.check_row(row)
            except ValueError as exc:
                raise ContractError(f"stream {batch.stream}: {exc}") from exc
            seq = row["seq"]
            assert isinstance(seq, int)  # check_row checked it
            if seq in seqs[batch.stream]:
                raise ContractError(f"stream {batch.stream}: seq {seq} appears twice")
            seqs[batch.stream].add(seq)
