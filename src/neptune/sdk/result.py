"""What an ingest or a dry run returns, and reading a package back (ADR 0035 §4).

``IngestResult`` wraps the runtime's ``JobOutcome`` without copying or reinterpreting it: every
property reads the runtime's record, and the receipt is the one the job wrote into the package.
Findings are the runtime's ``IngestFinding`` objects and the cache report its ``CacheReport``.

``committed_result`` finds the result an interrupted call carries when its job had already
passed its last checkpoint and published its package (ADR 0035 §3).
"""

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import neptune.sdk.contents as _contents
from neptune.identity import canonical_json
from neptune.model.finding import IngestFinding
from neptune.model.ids import ContentId, RecordId
from neptune.model.package import IngestReceipt, ingest_receipt_from_json
from neptune.runtime import CacheReport, Explanation, JobOutcome, JobState
from neptune.sdk.errors import InvalidRequestError, PackageInvalidError
from neptune.store.package import RECEIPT, IngestPackage, open_file
from neptune.store.package import read_package as _read_package
from neptune.store.receipt import receipt_id


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
        """The findings the job made itself, by id: discovery's, the probe engine's and the
        runtime's (a quarantined source, a crash, a limit). They are known even when nothing was
        committed. Adapters' findings are in the package: ``read_receipt().findings``."""
        return self.outcome.findings

    @property
    def cache(self) -> CacheReport:
        """What the job reused and recomputed, and why; a dry run's says what is left to do."""
        return self.outcome.cache

    @property
    def explanation(self) -> Explanation | None:
        """A planned dry run's explanation of what an ingest would do, and why (ADR 0044):
        ``to_json``/``dumps`` for programs, ``render`` for people. ``None`` for any other job."""
        return self.outcome.explanation

    @property
    def durations(self) -> tuple[tuple[str, float], ...]:
        """Seconds per phase: volatile, never in the package's identity."""
        return self.outcome.durations

    def _committed(self) -> Path:
        if self.outcome.package is None or self.outcome.destination is None:
            raise InvalidRequestError(f"a {self.outcome.state} job wrote no package")
        return self.outcome.destination

    def read_receipt(self) -> IngestReceipt:
        """The committed package's receipt: every source, transform and finding, adapters'
        included. Checked to hash to ``receipt``; ``read_package`` verifies everything else."""
        path = self._committed() / RECEIPT
        try:
            with open_file(path) as handle:
                receipt = ingest_receipt_from_json(canonical_json.loads(handle.read()))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise PackageInvalidError(f"{path} cannot be read: {exc}") from exc
        if receipt.id != self.receipt or receipt_id(receipt) != receipt.id:
            raise PackageInvalidError(f"{path} is not the receipt this job wrote")
        return receipt

    def read_package(self) -> IngestPackage:
        """The committed package, read back and verified."""
        return read_package(self._committed())

    def contents(self) -> tuple[_contents.RunContents, ...]:
        """What each run of the committed package contains: streams, declared field paths and
        inferred semantics, read from its records and derived tables (ADR 0049)."""
        return _contents.run_contents(self.read_package())


# The attribute an interruption carries its job's committed result under (``committed_result``).
_COMMITTED: Final = "_neptune_committed"


def attach_committed(error: BaseException, result: IngestResult) -> None:
    """Make ``error``, which interrupted a call whose job committed anyway, carry ``result``.

    A note names the package for people reading the traceback; ``committed_result`` returns the
    result for code. Attaching twice changes nothing.
    """
    if not result.committed or getattr(error, _COMMITTED, None) is not None:
        return
    with contextlib.suppress(AttributeError, TypeError):  # an exception that takes no attributes
        setattr(error, _COMMITTED, result)
        error.add_note(
            f"Neptune: the job had passed its last checkpoint and committed package "
            f"{result.package} at {result.destination}; committed_result(error) returns it"
        )


def committed_result(error: BaseException) -> IngestResult | None:
    """The committed result an interrupted SDK call carries, else ``None`` (ADR 0035 §3).

    A job past its last checkpoint (the start of ``commit``) publishes its package whatever
    interrupts the call running it: the awaiting task cancelled, ``on_event`` raising, a
    ``with`` block left by an exception. The interruption still propagates, carrying the job's
    committed ``IngestResult``: the package exists, and retrying into the same destination would
    be ``destination_exists``. ``None`` means the job stopped without one, so nothing was written
    to the destination. The chain is followed (``__cause__``, then ``__context__``), so the result
    is found behind the ``TimeoutError`` of ``asyncio.timeout`` or a ``CancelledError`` a task
    re-raises for its awaiter.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        found = getattr(current, _COMMITTED, None)
        if isinstance(found, IngestResult):
            return found
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return None


def read_package(path: Path) -> IngestPackage:
    """Read the package at ``path`` and verify it: every file, id, series and the receipt."""
    try:
        return _read_package(Path(path))
    except (ValueError, TypeError, KeyError, OSError) as exc:  # PackageError is a ValueError
        raise PackageInvalidError(f"{path} is not a valid package: {exc}") from exc
