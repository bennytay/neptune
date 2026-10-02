"""What the plan and the chunks share: the config's limits and how a finding is built."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from neptune.adapters.contract import AdapterConfig, ConfigError, SourceReader
from neptune.adapters.rosbag1.records import MAX_CHUNK_BYTES_CEILING, MAX_HEADER_BYTES_CEILING
from neptune.adapters.rosbag1.scan import Place
from neptune.identity.findings import ingest_finding
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import EvidenceRef


@dataclass(frozen=True)
class Limits:
    """The config's bounds on what is read: a chunk's bytes, a header's bytes."""

    chunk_bytes: int
    header_bytes: int


def limits(config: AdapterConfig) -> Limits:
    """The config's limits; one outside what the adapter's declared memory covers is a config
    error, not a finding: a larger one would be killed by the sandbox and lose its whole chunk."""
    chunk, header = config.integer("max_chunk_bytes"), config.integer("max_header_bytes")
    if not 1 <= chunk <= MAX_CHUNK_BYTES_CEILING:
        raise ConfigError(f"max_chunk_bytes is 1 <= n <= {MAX_CHUNK_BYTES_CEILING}: {chunk}")
    if not 1 <= header <= MAX_HEADER_BYTES_CEILING:
        raise ConfigError(f"max_header_bytes is 1 <= n <= {MAX_HEADER_BYTES_CEILING}: {header}")
    return Limits(chunk, header)


class Reporter:
    """Builds this source's findings under this config's transform."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config

    def evidence(self, place: Place) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, place.locator())

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: Place,
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        *,
        related: Iterable[Place] = (),
        records: Iterable[RecordId] = (),
    ) -> IngestFinding:
        return ingest_finding(
            code=f"rosbag1.{code}",
            category=category,
            severity=severity,
            subject=self.evidence(subject),
            transform=self.config.transform,
            message=message,
            details=details,
            related=tuple(self.evidence(place) for place in related),
            records=records,
        )
