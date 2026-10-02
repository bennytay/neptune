"""Sources a connector reads: object stores and other external systems (ADR 0067).

A connector is a ``neptune.sources`` plugin (ADR 0058). Its factory is called with the source's
URI and returns an ``ExternalSource``. The compiler never imports the connector's types: the
connector's entries, discovery and errors are its own, and the protocols here say only what the
compiler reads from them.

- ``discover(ledger)`` lists every object under the URI against the root's ledger: new, changed,
  unchanged (with the ledger's revision), and ``gone`` (revisions the listing no longer holds,
  only when it was ``complete``). The compiler takes the listed objects from it, never their
  classification: every one is classified again here against the ledger, which knows every
  revision token seen over each revision's bytes (``SourceLedger.recognise``). ``gone`` is the
  connector's, because only it knows which keys it saw and could not use (a key listed twice,
  one too long), where nothing may be asserted; each is checked against the ledger and the
  listing.
- An object whose token the ledger recognises, at the size it was hashed at, is **carried
  forward**: observed again under that token, never fetched, never hashed.
- Any other object is **fetched once**: streamed through ``open`` into the job's spool while it is
  hashed, so the bytes the adapters later read are exactly the ones the digest names. A fetch that
  fails is a finding and leaves the object unobserved this time: nothing is asserted about it.
- ``gone`` revisions become ``SourceAbsence`` records, and only from a complete listing: a
  history is never deleted, and an incomplete listing asserts nothing.

Adapters read a connector's source through ``ExternalReader``: lazy, ranged reads, each chunk
hashed against the artifact before a byte is served; a sandboxed call, which may not use the
network, is given the spooled copy's descriptor instead (``fileno``), read under the same checks.
"""

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Protocol, cast, runtime_checkable

from neptune.discovery.policy import DISCOVERY_TRANSFORM, SIZE_CHANGED, UNREADABLE
from neptune.discovery.reader import SourceChangedError, VerifiedReader, pread_exactly
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import DEFAULT_CHUNK_SIZE, digest_stream
from neptune.identity.revisions import Observation, SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import TransformRecord
from neptune.model.source import SourceAbsence, SourceArtifact, SourceLocation, SourceRevision

_READ: Final = 1024 * 1024  # what one read of a connector's stream asks for


class ExternalSourceError(Exception):
    """A connector broke the protocol: the job cannot trust what it listed (``JobError``)."""


class SpoolError(Exception):
    """The job's spool could not be written (a full disk): the workspace's failure, never the
    store's, so it is no ``OSError`` a fetch's handling could take for an unreadable object."""


class NetworkGate(Protocol):
    """What a connector asks before using the network: the workspace (``require_network``)."""

    def require_network(self, purpose: str) -> None: ...


class ExternalEntry(Protocol):
    """One listed object: where it is, with its revision token, and its listed size."""

    @property
    def location(self) -> ExternalObjectRef: ...

    @property
    def size(self) -> int: ...


class ExternalDiscovery(Protocol):
    """A connector's listing against a ledger (Deploy's ``Discovery`` has this shape)."""

    @property
    def new(self) -> Sequence[ExternalEntry]: ...

    @property
    def changed(self) -> Sequence[ExternalEntry]: ...

    @property
    def unchanged(self) -> Sequence[tuple[ExternalEntry, SourceRevision]]: ...

    @property
    def gone(self) -> Sequence[SourceRevision]: ...

    @property
    def complete(self) -> bool: ...


@runtime_checkable
class ExternalSource(Protocol):
    """What the compiler needs of a connector's source (ADR 0067).

    ``connector_id`` is the connector's id, which every location it lists names; ``transform``
    the connector as a producer, which its findings name; ``findings()`` every finding so far,
    sorted by id; ``open(location)`` a read-only, seekable stream over the listed revision, whose
    failures are ``OSError``s.
    """

    @property
    def connector_id(self) -> str: ...

    @property
    def transform(self) -> TransformRecord: ...

    def discover(self, ledger: SourceLedger) -> ExternalDiscovery: ...

    def open(self, location: SourceLocation) -> BinaryIO: ...

    def findings(self) -> tuple[IngestFinding, ...]: ...


class SourceFactory(Protocol):
    """A ``neptune.sources`` entry point's value: builds the source a URI names (ADR 0067).

    It may raise ``LocalOnlyError`` (from ``network``) and any error saying the URI or options
    cannot be used; it declares the URI schemes it reads as ``schemes``.
    """

    def __call__(
        self, url: str, *, network: NetworkGate, options: Mapping[str, JsonValue] | None
    ) -> ExternalSource: ...


@dataclass(frozen=True)
class ExternalRoot:
    """A job's root when a connector reads it: the URI as given, the connector, its source."""

    uri: str
    connector: str
    source: ExternalSource

    def __post_init__(self) -> None:
        if not isinstance(self.source, ExternalSource):
            raise ExternalSourceError(f"connector {self.connector} built no ExternalSource")
        if self.source.connector_id != self.connector:
            raise ExternalSourceError(
                f"connector {self.connector} built a source of {self.source.connector_id!r}"
            )


# --- The spool -----------------------------------------------------------------------------------


class Spool:
    """Whole copies of a connector's bytes for one job, each named by its content id.

    Lives in the job's own scratch directory, which the job removes when it ends (and a sweep
    removes if it dies), so nothing fetched outlives the job. Every file is written by this
    process from a stream it hashed, and renamed into place only when its digest is known: a
    spooled file is always exactly its artifact. Files are opened with ``O_NOFOLLOW`` and must
    be regular.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, content: ContentId) -> Path:
        return self.directory / content.removeprefix("sha256:")

    def holds(self, content: ContentId) -> bool:
        return self._path(content).is_file()

    def open(self, content: ContentId) -> BinaryIO | None:
        """The spooled copy of ``content``, opened read-only, or ``None`` if there is none."""
        try:
            fd = os.open(self._path(content), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(f"the spool of {content} is not a regular file")
        return os.fdopen(fd, "rb")

    def fill(
        self,
        stream: BinaryIO,
        *,
        size: int,
        expected: SourceArtifact | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> SourceArtifact:
        """Copy ``stream`` into the spool while hashing it; the artifact of what it read.

        The copy is kept only if the stream held exactly ``size`` bytes, the listed size: at
        most ``size + 1`` are read, so a stream that runs on past its listing costs one byte
        more, never a disk. ``expected``: the bytes must be that artifact's, else
        ``SourceChangedError`` and nothing is kept.
        """
        try:
            handle, name = tempfile.mkstemp(dir=self.directory, prefix=".fill-")
            out = os.fdopen(handle, "wb")
        except OSError as exc:
            raise SpoolError(f"the spool cannot be written: {exc}") from exc
        try:
            with out:
                copying = _Copying(stream, out, size + 1)
                artifact = digest_stream(cast("BinaryIO", copying), chunk_size=chunk_size)
                _spooling(out.flush)
            if expected is not None and artifact.content_id != expected.content_id:
                raise SourceChangedError(
                    f"{expected.content_id}: the store now serves other bytes under its token"
                )
            if artifact.size == size:
                _spooling(lambda: Path(name).replace(self._path(artifact.content_id)))
                name = ""
            return artifact
        finally:
            if name:
                with contextlib.suppress(OSError):
                    Path(name).unlink(missing_ok=True)

    def remove(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def _spooling(write: Callable[[], object]) -> None:
    """``write`` to the spool: an ``OSError`` there is the spool's (``SpoolError``)."""
    try:
        write()
    except OSError as exc:
        raise SpoolError(f"the spool cannot be written: {exc}") from exc


class _Copying:
    """A stream that writes what is read from it to ``out``, and ends after ``limit`` bytes.

    A failed read is the store's (an ``OSError``); a failed write the spool's (``SpoolError``).
    """

    def __init__(self, stream: BinaryIO, out: BinaryIO, limit: int) -> None:
        self._stream, self._out, self._left = stream, out, limit

    def read(self, size: int = -1) -> bytes:
        want = self._left if size < 0 else min(size, self._left)
        if want <= 0:
            return b""
        block = self._stream.read(want)
        if not isinstance(block, bytes):
            raise OSError(f"the connector's stream returned {type(block).__name__}, not bytes")
        self._left -= len(block)
        _spooling(lambda: self._out.write(block))
        return block


# --- Reading -------------------------------------------------------------------------------------


class ExternalReader(VerifiedReader):
    """An adapter's reader over one object of a connector's source (ADR 0067).

    Every chunk is hashed against the artifact before a byte of it is served. Chunks come from the
    spooled copy when the job holds one, else from the connector in ranged reads, so a reader
    that reads a header and an index fetches only the chunks holding them. ``fileno()`` is what
    a sandboxed call keeps: the call has no network, so the whole object is spooled first (once
    per job), checked against the artifact, and every read after it comes from that copy.
    """

    def __init__(
        self,
        source: ExternalSource,
        location: SourceLocation,
        artifact: SourceArtifact,
        spool: Spool,
        cache: int = 4,
    ) -> None:
        super().__init__(artifact, cache)
        self._source, self._location, self._spool = source, location, spool
        self._file = spool.open(artifact.content_id)
        self._stream: BinaryIO | None = None

    def __enter__(self) -> "ExternalReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for held in (self._file, self._stream):
            if held is not None:
                held.close()
        self._file = self._stream = None

    def fileno(self) -> int:
        """The spooled copy's descriptor, spooling the object first if the job has no copy."""
        if self._file is None:
            with self._source.open(self._location) as stream:
                self._spool.fill(stream, size=self.size, expected=self._artifact)
            self._file = self._spool.open(self.content_id)
            if self._file is None:
                raise SourceChangedError(f"{self.content_id}: the store served another size")
        return self._file.fileno()

    def _fetch(self, start: int, length: int) -> bytes:
        if self._file is not None:
            return pread_exactly(self._file.fileno(), start, length)
        if self._stream is None:
            self._stream = self._source.open(self._location)
        self._stream.seek(start)
        pieces: list[bytes] = []
        got = 0
        while got < length:
            piece = self._stream.read(min(_READ, length - got))
            if not piece:
                break
            pieces.append(piece)
            got += len(piece)
        return b"".join(pieces)


# --- Fingerprinting ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Listed:
    """One listed object observed. ``location`` is as listed now, with the token the bytes are
    read under: the observation's revision may name another token for the same bytes.
    ``fetched`` is false when the object was recognised by its token."""

    location: ExternalObjectRef
    observation: Observation
    fetched: bool


@dataclass(frozen=True)
class ExternalScan:
    """One pass over a connector's listing (``fingerprint_external``)."""

    listed: tuple[Listed, ...]  # in location order
    unread: tuple[ExternalObjectRef, ...]  # fetches that failed: nothing asserted about them
    absences: tuple[SourceAbsence, ...]
    findings: tuple[IngestFinding, ...]  # discovery's, in location order
    complete: bool

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(item.observation for item in self.listed)


def listed_entries(source: ExternalSource, discovery: ExternalDiscovery) -> list[ExternalEntry]:
    """Every object the listing holds, in location order, checked against the protocol."""
    entries: list[ExternalEntry] = [
        *discovery.new,
        *discovery.changed,
        *(entry for entry, _ in discovery.unchanged),
    ]
    seen: set[tuple[str, ...]] = set()
    for entry in entries:
        location, size = entry.location, entry.size
        if not isinstance(location, ExternalObjectRef):
            raise ExternalSourceError(f"{source.connector_id} listed a location {location!r}")
        if location.connector_id != source.connector_id:
            raise ExternalSourceError(
                f"{source.connector_id} listed an object of {location.connector_id!r}"
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ExternalSourceError(f"{source.connector_id} listed a size {size!r}")
        if location.key in seen:
            raise ExternalSourceError(
                f"{source.connector_id} listed {location.object_id!r} more than once"
            )
        seen.add(location.key)
    return sorted(entries, key=lambda entry: entry.location.key)


def fingerprint_external(
    source: ExternalSource,
    ledger: SourceLedger,
    discovery: ExternalDiscovery,
    spool: Spool,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ExternalScan:
    """Observe every listed object into ``ledger``, fetching only what it cannot recognise.

    A fetch that fails is the store's: a finding, and the object is left unobserved. The spool
    failing to write (``SpoolError``) is the workspace's, and propagates.
    """
    entries = listed_entries(source, discovery)
    listed: list[Listed] = []
    unread: list[ExternalObjectRef] = []
    findings: list[IngestFinding] = []
    for entry in entries:
        location = entry.location
        known = ledger.recognise(location)
        artifact = ledger.artifact(known.content_id) if known is not None else None
        if artifact is not None and artifact.size == entry.size:
            listed.append(Listed(location, ledger.observe(location, artifact), fetched=False))
            continue
        try:
            with source.open(location) as stream:
                artifact = spool.fill(stream, size=entry.size, chunk_size=chunk_size)
        except (OSError, SourceChangedError) as exc:
            unread.append(location)
            findings.append(unreadable_finding(location, exc))
            continue
        if artifact.size != entry.size:
            unread.append(location)
            findings.append(size_changed_finding(location, entry.size, artifact.size))
            continue
        listed.append(Listed(location, ledger.observe(location, artifact), fetched=True))
    absences = _absences(source, ledger, discovery, {e.location.key for e in entries})
    return ExternalScan(
        tuple(listed), tuple(unread), tuple(absences), tuple(findings), discovery.complete
    )


def _absences(
    source: ExternalSource,
    ledger: SourceLedger,
    discovery: ExternalDiscovery,
    listed: Iterable[tuple[str, ...]],
) -> list[SourceAbsence]:
    """The connector's ``gone`` revisions marked absent, only from a complete listing."""
    if not discovery.complete:
        return []
    keys = set(listed)
    absences: list[SourceAbsence] = []
    for revision in sorted(discovery.gone, key=lambda revision: revision.location.key):
        where = revision.location
        if not isinstance(revision, SourceRevision) or not isinstance(where, ExternalObjectRef):
            raise ExternalSourceError(f"{source.connector_id} called {revision!r} gone")
        if where.connector_id != source.connector_id or ledger.head(where) != revision:
            raise ExternalSourceError(
                f"{source.connector_id} called {where.object_id!r} gone, which the ledger does not"
                " hold at that revision"
            )
        if where.key in keys:
            raise ExternalSourceError(
                f"{source.connector_id} both listed {where.object_id!r} and called it gone"
            )
        absence = ledger.mark_absent(where)
        if absence is not None:
            absences.append(absence)
    return absences


def unreadable_finding(location: ExternalObjectRef, exc: Exception) -> IngestFinding:
    """A listed object that could not be fetched: not read, and nothing asserted about it."""
    details: dict[str, JsonValue] = {"error": type(exc).__name__}
    code = getattr(exc, "code", None)  # a connector's own code for it, if it gives one
    if isinstance(code, str) and code.isidentifier():
        details["cause"] = code
    return ingest_finding(
        code=UNREADABLE,
        category=FindingCategory.SKIPPED,
        severity=Severity.ERROR,
        subject=location,
        transform=DISCOVERY_TRANSFORM,
        message="the object could not be fetched from its store; not read, and nothing is"
        " asserted about it",
        details=details,
    )


def size_changed_finding(location: ExternalObjectRef, listed: int, fetched: int) -> IngestFinding:
    """The object held another number of bytes when fetched than its listing said."""
    return ingest_finding(
        code=SIZE_CHANGED,
        category=FindingCategory.INCONSISTENT,
        severity=Severity.WARNING,
        subject=location,
        transform=DISCOVERY_TRANSFORM,
        message="the object's size changed between the listing and the fetch; not read, and the"
        " next listing decides",
        details={"size_fetched": fetched, "size_listed": listed},
    )
