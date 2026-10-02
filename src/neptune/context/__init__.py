"""World and task context records from what the adapters already parsed (ADR 0063).

``extract_context`` reads the records the document, tabular and configuration adapters wrote (a
``DocumentRecord`` and its blocks, a ``StructuredTable`` and its rows, a ``ConfigurationSnapshot``
and its values) and writes the sites, assets, task briefs, requirements, procedure steps and work
orders they explicitly declare. It never reads source bytes, never calls an adapter, and so keeps
adapters from importing each other: a register is a table first and a register second.

One transform ``neptune.context`` per upstream adapter transform, with that transform as its
``upstream``: a record's spans and cells are in the text or table its upstream produced, and its
id moves only when that lineage does (ADR 0003, ADR 0016). Every record is ``stated``; whatever
only looks like a declaration is a ``context_candidate`` in ``derived/`` (ADR 0063 §7).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final

from neptune.context._configs import read_config
from neptune.context._documents import read_document
from neptune.context._emit import Output
from neptune.context._tables import read_table, rows_by_table
from neptune.derived.context import CANDIDATE_KIND, ContextCandidate
from neptune.identity.provenance import transform_record
from neptune.model.configuration import ConfigurationSnapshot, ConfigurationValue
from neptune.model.finding import IngestFinding
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.provenance import TransformRecord
from neptune.model.world import DocumentBlock, DocumentRecord, StructuredRecord, StructuredTable

CONTEXT_ID: Final = "neptune.context"
CONTEXT_VERSION: Final = "0.1.0"

_INPUTS: Final = (
    DocumentRecord,
    DocumentBlock,
    StructuredTable,
    StructuredRecord,
    ConfigurationSnapshot,
    ConfigurationValue,
)


@dataclass(frozen=True)
class ContextExtraction:
    """What the context pass wrote: its transforms (one per upstream transform that declared
    anything), the stated records, the inferred candidates and the findings."""

    transforms: tuple[TransformRecord, ...]
    records: tuple[Any, ...]
    candidates: tuple[ContextCandidate, ...]
    findings: tuple[IngestFinding, ...]

    def tables(self) -> dict[str, Iterator[JsonObject]]:
        """The package's derived candidate table, in id order (ADR 0036 §8)."""
        ordered = sorted(self.candidates, key=lambda c: c.id)
        return {CANDIDATE_KIND: (candidate.to_json() for candidate in ordered)}

    def summary(self) -> JsonObject:
        kinds: dict[str, int] = {}
        for record in self.records:
            kinds[record.kind] = kinds.get(record.kind, 0) + 1
        return {
            "candidates": len(self.candidates),
            "findings": len(self.findings),
            "records": dict(sorted(kinds.items())),
            "transforms": len(self.transforms),
        }


def context_transform(upstream: RecordId) -> TransformRecord:
    """The context pass over one upstream transform's output."""
    return transform_record(
        adapter_id=CONTEXT_ID, adapter_version=CONTEXT_VERSION, config={}, upstream=[upstream]
    )


def extract_context(records: Iterable[Any]) -> ContextExtraction | None:
    """The context records ``records`` declare; ``None`` when they declare none, so a package
    without any gains no transform and no table."""
    by_upstream: dict[RecordId, dict[type, list[Any]]] = {}
    for record in records:
        if isinstance(record, _INPUTS):
            group = by_upstream.setdefault(record.provenance.transform, {})
            group.setdefault(type(record), []).append(record)
    outputs: list[Output] = []
    for upstream in sorted(by_upstream):
        group = by_upstream[upstream]
        out = Output(context_transform(upstream))
        blocks: dict[RecordId, list[DocumentBlock]] = {}
        for block in group.get(DocumentBlock, []):
            blocks.setdefault(block.document, []).append(block)
        for document in sorted(group.get(DocumentRecord, []), key=lambda r: r.id):
            read_document(out, document, blocks.get(document.id, []))
        rows = rows_by_table(group.get(StructuredRecord, []))
        for table in sorted(group.get(StructuredTable, []), key=lambda r: r.id):
            read_table(out, table, rows.get(table.id, []))
        values: dict[RecordId, list[ConfigurationValue]] = {}
        for value in group.get(ConfigurationValue, []):
            values.setdefault(value.snapshot, []).append(value)
        for snapshot in sorted(group.get(ConfigurationSnapshot, []), key=lambda r: r.id):
            read_config(out, snapshot, values.get(snapshot.id, []))
        if out.records or out.candidates or out.findings:
            outputs.append(out)
    if not outputs:
        return None
    return ContextExtraction(
        transforms=tuple(out.transform for out in outputs),
        records=tuple(r for out in outputs for _, r in sorted(out.records.items())),
        candidates=tuple(c for out in outputs for _, c in sorted(out.candidates.items())),
        findings=tuple(f for out in outputs for _, f in sorted(out.findings.items())),
    )
