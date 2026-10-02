"""Citing a catalog document, and what a fleet-ops source returns (ADR 0010 §3).

The document, its byte form, its tables and its clocks are ``sources/stated_records.py`` (ADR 0009
§4), shared with the Roboto and Rerun connectors. This module holds only what the fleet-ops sources
add: shorthand for an evidence reference and a ``stated`` provenance inside a document, and the
catalog a source returns, whose records are not only tables.
"""

from dataclasses import dataclass
from typing import Any

from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, TransformRecord
from neptune_deploy.sources.stated_records import CatalogDocument, pointer


def cite(document: CatalogDocument, *parts: str | int) -> EvidenceRef:
    """The place in ``document`` at ``parts``."""
    return EvidenceRef(document.content_id, (JsonPointer(pointer(*parts)),))


def stated(document: CatalogDocument, transform: TransformRecord, *parts: str | int) -> Provenance:
    """Provenance of a value the document states at ``parts``."""
    return Provenance(cite(document, *parts), transform.id, AssertionKind.STATED)


@dataclass(frozen=True)
class Catalog:
    """What a fleet-ops source states: its documents and the evidence records built over them.

    ``records`` is every record, in one deterministic order: for each document in order, its
    clocks, table, rows, then the records built from it (runs, interventions, frames, maps).
    """

    documents: tuple[CatalogDocument, ...] = ()
    records: tuple[Any, ...] = ()

    def of(self, kind: str) -> tuple[Any, ...]:
        return tuple(record for record in self.records if record.kind == kind)
