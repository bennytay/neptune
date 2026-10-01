"""What the plan and the chunks share: the config's selection and how a finding is built."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import AdapterConfig, ConfigError, SourceReader
from neptune.adapters.mcap.records import Text
from neptune.adapters.mcap.scan import Place
from neptune.identity.findings import ingest_finding
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import EvidenceRef

LOG_TIME_MAX: Final = 2**64 - 1


@dataclass(frozen=True)
class Selection:
    """Which messages get series rows: a topic pattern and an inclusive ``log_time`` window."""

    pattern: re.Pattern[str] | None
    start: int
    end: int

    @property
    def active(self) -> bool:
        return self.pattern is not None or self.start > 0 or self.end < LOG_TIME_MAX

    @property
    def windowed(self) -> bool:
        return self.start > 0 or self.end < LOG_TIME_MAX

    def selects(self, topic: Text) -> bool:
        if self.pattern is None:
            return True
        value = topic.value
        return value is not None and self.pattern.fullmatch(value) is not None

    def admits(self, log_time: int) -> bool:
        return self.start <= log_time <= self.end

    def overlaps(self, first: int, last: int) -> bool:
        return first <= self.end and last >= self.start


def selection(config: AdapterConfig) -> Selection:
    """The config's selection. A pattern that does not compile is a config error, not a finding."""
    text = config.text("topic_pattern")
    try:
        pattern = re.compile(text) if text else None
    except re.error as exc:
        raise ConfigError(f"topic_pattern is not a regular expression: {exc}") from exc
    start, end = config.integer("log_time_start"), config.integer("log_time_end")
    if not 0 <= start <= end <= LOG_TIME_MAX:
        raise ConfigError(f"log_time_start and log_time_end are 0 <= start <= end < 2^64: {start}")
    return Selection(pattern, start, end)


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
            code=f"mcap.{code}",
            category=category,
            severity=severity,
            subject=self.evidence(subject),
            transform=self.config.transform,
            message=message,
            details=details,
            related=tuple(self.evidence(place) for place in related),
            records=records,
        )
