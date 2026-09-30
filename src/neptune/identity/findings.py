"""Building and verifying ingest findings (ADR 0017 §9).

A finding's id is the record id over its whole content, as for ``TransformRecord``: the same
problem found at the same place by the same transform is one finding, and any difference (another
code, another subject, another message) is another finding.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Final

from neptune.identity.ids import record_id
from neptune.model.finding import FindingCategory, FindingSubject, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import EvidenceRef, TransformRecord

FINDING_KIND: Final = "ingest_finding"


def ingest_finding(
    *,
    code: str,
    category: FindingCategory,
    severity: Severity,
    subject: FindingSubject,
    transform: TransformRecord,
    message: str,
    details: Mapping[str, JsonValue] | None = None,
    related: Sequence[EvidenceRef] = (),
    records: Iterable[RecordId] = (),
) -> IngestFinding:
    """The finding ``transform`` reports about ``subject``, with its id derived from its content."""
    # The id covers every field but itself, so derive it from a draft holding a placeholder.
    draft = IngestFinding(
        id=RecordId("rec:sha256:" + "0" * 64),
        code=code,
        category=category,
        severity=severity,
        subject=subject,
        transform=transform.id,
        message=message,
        details=dict(details or {}),
        related=tuple(related),
        records=tuple(sorted(set(records))),
    )
    return replace(draft, id=finding_id(draft))


def finding_id(finding: IngestFinding) -> RecordId:
    return record_id(FINDING_KIND, finding.content_json())


def check_ingest_finding(finding: IngestFinding) -> IngestFinding:
    """Verify a finding read from a store: its id must recompute from its content."""
    if finding_id(finding) != finding.id:
        raise ValueError(f"finding {finding.id}: id does not match its content")
    return finding
