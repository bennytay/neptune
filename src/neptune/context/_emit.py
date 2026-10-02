"""What one context transform writes, and the helpers every reader shares (ADR 0063 §6)."""

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final

from neptune.derived.context import ContextCandidate, candidate_id
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model._fields import Identifiers, Names
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import Ambiguous, AssertionKind, Knowledge, Known, KnownAbsent, Unknown
from neptune.model.provenance import EvidenceRef, Provenance, Span, TransformRecord

STATED: Final = AssertionKind.STATED
_SEPARATORS: Final = re.compile(r"[;,]")
_SPACE_OR_DASH: Final = re.compile(r"[\s\-]+")


def field_key(name: str) -> str:
    """A declared column name, key or label as the rules compare it: case-folded, with runs of
    spaces and dashes as one ``_`` (``Asset ID`` and ``asset-id`` are ``asset_id``)."""
    return _SPACE_OR_DASH.sub("_", name.strip()).casefold()


def sub_span(evidence: EvidenceRef, start: int, end: int) -> EvidenceRef:
    """The code points ``[start, end)`` of the text ``evidence`` cites, cited exactly.

    A span inside a span is the outer span shifted; inside any other step (a cell, a pointer) it
    is that step followed by a span, as ADR 0020 §1 cites an alias split out of a cell.
    """
    *outer, last = evidence.locator
    if isinstance(last, Span):
        return EvidenceRef(evidence.source, (*outer, Span(last.start + start, last.start + end)))
    return EvidenceRef(evidence.source, (*evidence.locator, Span(start, end)))


def split_items(text: str) -> list[tuple[int, int]]:
    """The non-blank items of a list written in one value (``A-1; A-2``, ``STR-14, STR-15``) as
    code-point ranges, surrounding spaces excluded. ``;`` and ``,`` separate; nothing else does."""
    items: list[tuple[int, int]] = []
    start = 0
    for end in [m.start() for m in _SEPARATORS.finditer(text)] + [len(text)]:
        piece = text[start:end]
        stripped = piece.strip()
        if stripped:
            left = start + (len(piece) - len(piece.lstrip()))
            items.append((left, left + len(stripped)))
        start = end + 1
    return items


class Output:
    """The records, candidates and findings of one context transform."""

    def __init__(self, transform: TransformRecord) -> None:
        self.transform = transform
        self.records: dict[RecordId, Any] = {}
        self.candidates: dict[RecordId, ContextCandidate] = {}
        self.findings: dict[RecordId, IngestFinding] = {}

    def guarded(self, subject: EvidenceRef, read: Callable[[], None]) -> None:
        """Run one declaration-holder's reader; if it fails, drop what it wrote and say so.

        The readers are written not to raise, but one unforeseen document must cost its own
        records and a finding, never the job (non-negotiable 7).
        """
        before = (dict(self.records), dict(self.candidates), dict(self.findings))
        try:
            read()
        except Exception as exc:
            self.records, self.candidates, self.findings = before
            self.finding(
                "failed",
                FindingCategory.FAILED,
                subject,
                "the context pass failed on this declaration's holder; nothing was read from it",
                {"error": type(exc).__name__},
                severity=Severity.ERROR,
            )

    def prov(self, evidence: EvidenceRef) -> Provenance:
        return Provenance(evidence, self.transform.id, STATED)

    def known(self, value: Any, evidence: EvidenceRef) -> Known[Any]:
        return Known(value, self.prov(evidence))

    def unknown(self, evidence: EvidenceRef) -> Unknown:
        return Unknown(self.prov(evidence))

    def absent(self, evidence: EvidenceRef) -> KnownAbsent:
        return KnownAbsent(self.prov(evidence))

    def record_id(self, kind: str, evidence: EvidenceRef) -> RecordId:
        return evidence_record_id(kind, evidence, self.transform)

    def add(self, record: Any) -> None:
        # Ids derive from the cited declaration, and no two declarations share one: a collision
        # would be a rule writing one declaration twice.
        if record.id in self.records:
            raise AssertionError(f"{record.kind} {record.id} written twice")
        self.records[record.id] = record

    def candidate(
        self,
        subject: RecordId,
        proposes: str,
        rule: str,
        confidence: float,
        text: str,
        evidence: EvidenceRef,
    ) -> None:
        cid = candidate_id(self.transform.id, subject, proposes, rule)
        if cid not in self.candidates:  # one rule, one subject, one kind: one line
            self.candidates[cid] = ContextCandidate(
                cid, self.transform.id, subject, proposes, rule, confidence, text, (evidence,)
            )

    def finding(
        self,
        code: str,
        category: FindingCategory,
        subject: EvidenceRef,
        message: str,
        details: dict[str, JsonValue] | None = None,
        related: Sequence[EvidenceRef] = (),
        severity: Severity = Severity.WARNING,
    ) -> None:
        found = ingest_finding(
            code=f"context.{code}",
            category=category,
            severity=severity,
            subject=subject,
            transform=self.transform,
            message=message,
            details=details,
            related=related,
        )
        self.findings[found.id] = found


def as_id(state: Knowledge[str], namespace: str) -> Knowledge[LogicalId]:
    """A stated text as a declared id in ``namespace``, citing the same place; any other state
    stays as it is. The readers here state one value per field, never ``Ambiguous``."""
    if isinstance(state, Known):
        return Known(LogicalId(namespace, state.value), state.provenance)
    if isinstance(state, Ambiguous):
        raise AssertionError("the context readers never state an ambiguous value")
    return state


def identifiers(items: Iterable[Known[LogicalId]]) -> Identifiers:
    """Stated ids, each once (the first citation of a repeat), sorted as ``Identifiers`` are."""
    seen: dict[LogicalId, Known[LogicalId]] = {}
    for item in items:
        seen.setdefault(item.value, item)
    return tuple(seen[key] for key in sorted(seen, key=lambda i: (i.namespace, i.value)))


def names(items: Iterable[Known[str]]) -> Names:
    """Stated names, each once (the first citation of a repeat), sorted by text."""
    seen: dict[str, Known[str]] = {}
    for item in items:
        seen.setdefault(item.value, item)
    return tuple(seen[key] for key in sorted(seen))
