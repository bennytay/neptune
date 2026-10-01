"""One deterministic pass over a local source: walk, digest, record, reconcile absences (ADR 0010).

``scan`` walks and fingerprints in one call. The runtime (MVL-6) walks first, as its discover
phase, and hands the entries to ``fingerprint``; the two give the same result.

Absence is asserted only with coverage. A previously present location that this scan did not yield
is marked absent unless the scan could not see it: it, or an ancestor, was unreadable or vanished
mid-walk, or an ancestor is now a symlink (not followed, so what lies behind it is unknown). A
symlink, FIFO or directory now sitting exactly at the location *is* coverage: the regular file is
gone.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from neptune.discovery.source import (
    LocalSource,
    SkippedEntry,
    SkipReason,
    SourceAccessError,
    SourceEntry,
    SymlinkEntry,
    WalkEntry,
)
from neptune.identity.hashing import DEFAULT_CHUNK_SIZE, digest_stream
from neptune.identity.revisions import Observation, SourceLedger
from neptune.model.source import LocalPath, RawLocalPath, SourceAbsence, SourceRevision


@dataclass(frozen=True)
class ScanResult:
    observations: tuple[Observation, ...]
    absences: tuple[SourceAbsence, ...]
    symlinks: tuple[SymlinkEntry, ...]
    skipped: tuple[SkippedEntry, ...]


def scan(
    source: LocalSource, ledger: SourceLedger, *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> ScanResult:
    """Walk ``source`` and fingerprint what it finds into ``ledger``."""
    return fingerprint(source, ledger, source.walk(), chunk_size=chunk_size)


def fingerprint(
    source: LocalSource,
    ledger: SourceLedger,
    entries: Iterable[WalkEntry],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ScanResult:
    """Digest the regular files among ``entries`` (one walk of ``source``) and reconcile absences.

    A file that changed or vanished between the walk and its digest is skipped as blind for this
    pass, so nothing is asserted about it; the next pass decides.
    """
    observations: list[Observation] = []
    symlinks: list[SymlinkEntry] = []
    skipped: list[SkippedEntry] = []
    seen: set[bytes] = set()
    for entry in entries:
        if isinstance(entry, SymlinkEntry):
            symlinks.append(entry)
        elif isinstance(entry, SkippedEntry):
            skipped.append(entry)
        else:
            observation = _digest(source, ledger, entry, chunk_size, skipped)
            if observation is not None:
                observations.append(observation)
                seen.add(_raw(entry))

    unseen_blind = {s.raw_path for s in skipped if s.reason in _BLIND}
    link_paths = {link.location.raw for link in symlinks}
    absences: list[SourceAbsence] = []
    for location in _present_local_locations(ledger):
        raw = location.raw
        if raw in seen or not _covered(raw, unseen_blind, link_paths):
            continue
        absence = ledger.mark_absent(location)
        if absence is not None:
            absences.append(absence)
    return ScanResult(tuple(observations), tuple(absences), tuple(symlinks), tuple(skipped))


# Reasons that mean "could not look", as opposed to "looked and it is not a regular file".
_BLIND = frozenset({SkipReason.MISSING, SkipReason.UNREADABLE})


def _digest(
    source: LocalSource,
    ledger: SourceLedger,
    entry: SourceEntry,
    chunk_size: int,
    skipped: list[SkippedEntry],
) -> Observation | None:
    try:
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=chunk_size)
    except SourceAccessError as exc:
        # Changed between walk and open. Blind for this scan; the next scan decides.
        reason = SkipReason.MISSING if exc.reason is SkipReason.MISSING else SkipReason.UNREADABLE
        skipped.append(SkippedEntry(_raw(entry), reason, f"at open: {exc.reason}: {exc.detail}"))
        return None
    except OSError as exc:
        skipped.append(SkippedEntry(_raw(entry), SkipReason.UNREADABLE, exc.strerror or str(exc)))
        return None
    return ledger.observe(entry.location, artifact)


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


def _raw(entry: SourceEntry) -> bytes:
    location = entry.location
    assert isinstance(location, (LocalPath, RawLocalPath))
    return location.raw
