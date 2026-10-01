"""Evidence, provenance, record ids and findings for one source under one config."""

from collections.abc import Iterable, Mapping, Sequence

from neptune.adapters.contract import AdapterConfig, SourceReader
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import ByteRange, EvidenceRef, Locator, Provenance

PRODUCER = "rosbag2"


def clip(source: SourceReader, offset: int, length: int) -> ByteRange:
    """``[offset, offset + length)`` cut to the source, so a citation always resolves."""
    start = min(max(offset, 0), source.size)
    return ByteRange(start, max(0, min(length, source.size - start)))


class Cite:
    """Cites bytes of the one source an adapter call was given."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config

    def evidence(self, *steps: Locator) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, tuple(steps))

    def provenance(
        self, *steps: Locator, kind: AssertionKind = AssertionKind.OBSERVED
    ) -> Provenance:
        return Provenance(self.evidence(*steps), self.config.transform.id, kind)

    def record_id(self, kind: str, *steps: Locator) -> RecordId:
        return evidence_record_id(kind, self.evidence(*steps), self.config.transform)

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: Sequence[Locator],
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        *,
        related: Iterable[Sequence[Locator]] = (),
        records: Iterable[RecordId] = (),
    ) -> IngestFinding:
        return ingest_finding(
            code=f"{PRODUCER}.{code}",
            category=category,
            severity=severity,
            subject=self.evidence(*subject),
            transform=self.config.transform,
            message=message,
            details=details,
            related=tuple(self.evidence(*steps) for steps in related),
            records=records,
        )
