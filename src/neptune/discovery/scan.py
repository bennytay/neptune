"""One deterministic pass over a local source: walk, digest, record, reconcile absences (ADR 0010).

Absence is asserted only with coverage. A previously present location that this scan did not yield
is marked absent unless the scan could not see it: it, or an ancestor, was unreadable or vanished
mid-walk, or an ancestor is now a symlink (not followed, so what lies behind it is unknown). A
symlink, FIFO or directory now sitting exactly at the location *is* coverage: the regular file is
gone.

Everything the walk saw and did not read is also a finding (ADR 0029 §1): every symlink, every
skipped entry below the root, and every file whose size changed between the walk and the digest.
``ScanResult.transform`` is the discovery transform the findings name; store it with them.
"""

from dataclasses import dataclass

from neptune.discovery.policy import (
    DISCOVERY_TRANSFORM,
    ROOT_ENTRY,
    root_forms,
    size_changed_finding,
    skipped_finding,
    symlink_finding,
)
from neptune.discovery.source import (
    LocalSource,
    SkippedEntry,
    SkipReason,
    SourceAccessError,
    SourceEntry,
    SymlinkEntry,
)
from neptune.identity.hashing import DEFAULT_CHUNK_SIZE, digest_stream
from neptune.identity.revisions import Observation, SourceLedger
from neptune.model.finding import IngestFinding
from neptune.model.provenance import TransformRecord
from neptune.model.source import LocalPath, RawLocalPath, SourceAbsence, SourceRevision


@dataclass(frozen=True)
class ScanResult:
    observations: tuple[Observation, ...]
    absences: tuple[SourceAbsence, ...]
    symlinks: tuple[SymlinkEntry, ...]
    skipped: tuple[SkippedEntry, ...]
    findings: tuple[IngestFinding, ...]  # walk order
    transform: TransformRecord


def scan(
    source: LocalSource, ledger: SourceLedger, *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> ScanResult:
    observations: list[Observation] = []
    symlinks: list[SymlinkEntry] = []
    skipped: list[SkippedEntry] = []
    findings: list[IngestFinding] = []
    seen: set[bytes] = set()
    blind_at_open: set[bytes] = set()
    forms = root_forms(source.root)
    for entry in source.walk():
        if isinstance(entry, SymlinkEntry):
            symlinks.append(entry)
            findings.append(symlink_finding(forms, entry))
        elif isinstance(entry, SkippedEntry):
            skipped.append(entry)
            if entry.raw_path != ROOT_ENTRY:
                findings.append(skipped_finding(entry))
        else:
            outcome = _digest(source, ledger, entry, chunk_size)
            if isinstance(outcome, SkippedEntry):
                # Changed between walk and open. Blind for this scan; the next scan decides.
                skipped.append(outcome)
                blind_at_open.add(outcome.raw_path)
                findings.append(skipped_finding(outcome))
                continue
            observation, digested = outcome
            observations.append(observation)
            seen.add(_raw(entry))
            if digested != entry.size:
                findings.append(size_changed_finding(_location(entry), entry.size, digested))

    unseen_blind = {s.raw_path for s in skipped if s.reason in _BLIND} | blind_at_open
    link_paths = {link.location.raw for link in symlinks}
    absences: list[SourceAbsence] = []
    for location in _present_local_locations(ledger):
        raw = location.raw
        if raw in seen or not _covered(raw, unseen_blind, link_paths):
            continue
        absence = ledger.mark_absent(location)
        if absence is not None:
            absences.append(absence)
    return ScanResult(
        tuple(observations),
        tuple(absences),
        tuple(symlinks),
        tuple(skipped),
        tuple(findings),
        DISCOVERY_TRANSFORM,
    )


# Reasons that mean "could not look", as opposed to "looked and it is not a regular file".
_BLIND = frozenset({SkipReason.MISSING, SkipReason.UNREADABLE})


def _digest(
    source: LocalSource, ledger: SourceLedger, entry: SourceEntry, chunk_size: int
) -> tuple[Observation, int] | SkippedEntry:
    """Hash and record one file; the int is the number of bytes digested."""
    try:
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=chunk_size)
    except SourceAccessError as exc:
        # The reason is what open found: a symlink at or above the file, a FIFO swapped in.
        detail = (
            exc.detail if exc.reason is SkipReason.NOT_REGULAR_FILE else f"at open: {exc.detail}"
        )
        return SkippedEntry(_raw(entry), exc.reason, detail)
    except OSError as exc:
        return SkippedEntry(_raw(entry), SkipReason.UNREADABLE, exc.strerror or str(exc))
    return ledger.observe(entry.location, artifact), artifact.size


def _present_local_locations(ledger: SourceLedger) -> list[LocalPath | RawLocalPath]:
    """Local locations whose latest entry is a revision, in byte order."""
    locations = [
        head.location
        for head in ledger.heads()
        if isinstance(head, SourceRevision) and isinstance(head.location, (LocalPath, RawLocalPath))
    ]
    return sorted(locations, key=lambda location: location.raw)


def _covered(raw: bytes, blind: set[bytes], links: set[bytes]) -> bool:
    """True if this scan could see whether ``raw`` holds a regular file."""
    if b"." in blind or raw in blind:
        return False
    parts = raw.split(b"/")
    for depth in range(1, len(parts)):
        ancestor = b"/".join(parts[:depth])
        if ancestor in blind or ancestor in links:
            return False
    return True


def _location(entry: SourceEntry) -> LocalPath | RawLocalPath:
    location = entry.location
    assert isinstance(location, (LocalPath, RawLocalPath))
    return location


def _raw(entry: SourceEntry) -> bytes:
    return _location(entry).raw
