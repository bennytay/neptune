"""What an ingest or a dry run returns, and reading a package back (ADR 0035 §4).

``IngestResult`` wraps the runtime's ``JobOutcome`` without copying or reinterpreting it: every
field is the runtime's, and the receipt id is the one the job wrote. Findings are the runtime's
``IngestFinding`` objects and the cache report its ``CacheReport``.
"""

from dataclasses import dataclass
from pathlib import Path

from neptune.model.finding import IngestFinding, Severity
from neptune.model.ids import ContentId, RecordId
from neptune.runtime import CacheReport, JobOutcome, JobState
from neptune.sdk.errors import InvalidRequestError, PackageInvalidError
from neptune.store.package import IngestPackage, PackageError
from neptune.store.package import read_package as _read_package


@dataclass(frozen=True)
class IngestResult:
    """How one job ended, typed: committed with a package, cancelled, or planned (a dry run).

    ``outcome`` is the runtime's record; the properties read it. ``receipt`` is the id of the
    receipt in the package, a function of the evidence alone: the same sources, adapters and
    config give the same receipt and package id, whatever the workspace held (ADR 0031).
    """

    outcome: JobOutcome

    @property
    def state(self) -> JobState:
        return self.outcome.state

    @property
    def committed(self) -> bool:
        """The package is in place at ``destination``."""
        return self.outcome.state is JobState.COMMITTED

    @property
    def cancelled(self) -> bool:
        """Stopped at a checkpoint on request; the workspace keeps the work for the next job."""
        return self.outcome.state is JobState.CANCELLED

    @property
    def planned(self) -> bool:
        """A dry run that reached its end: every source selected and planned, nothing parsed."""
        return self.outcome.state is JobState.PLANNED

    @property
    def job(self) -> str:
        return self.outcome.job

    @property
    def destination(self) -> Path | None:
        return self.outcome.destination

    @property
    def package(self) -> ContentId | None:
        """The package's content id, if one was committed."""
        return self.outcome.package

    @property
    def receipt(self) -> RecordId | None:
        """The receipt's record id, if a package was committed."""
        return self.outcome.cache.receipt

    @property
    def ingested(self) -> tuple[tuple[ContentId, RecordId], ...]:
        """Each (source, transform) pair the package holds."""
        return self.outcome.ingested

    @property
    def findings(self) -> tuple[IngestFinding, ...]:
        """Every finding the job recorded, by id: adapters', discovery's, the probe's, its own."""
        return self.outcome.findings

    @property
    def errors(self) -> tuple[IngestFinding, ...]:
        """The findings of severity ``error``: evidence that produced no canonical output."""
        return tuple(f for f in self.outcome.findings if f.severity is Severity.ERROR)

    @property
    def cache(self) -> CacheReport:
        """What the job reused and recomputed, and why; a dry run's says what is left to do."""
        return self.outcome.cache

    @property
    def durations(self) -> tuple[tuple[str, float], ...]:
        """Seconds per phase: volatile, never in the package's identity."""
        return self.outcome.durations

    def read_package(self) -> IngestPackage:
        """The committed package, read back and verified."""
        if self.outcome.package is None or self.outcome.destination is None:
            raise InvalidRequestError(f"a {self.outcome.state} job wrote no package")
        return read_package(self.outcome.destination)


def read_package(path: Path) -> IngestPackage:
    """Read the package at ``path`` and verify it: every file, id, series and the receipt."""
    try:
        return _read_package(Path(path))
    except (PackageError, ValueError, OSError) as exc:
        raise PackageInvalidError(f"{path} is not a valid package: {exc}") from exc
