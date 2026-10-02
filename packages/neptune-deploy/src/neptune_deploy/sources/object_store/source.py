"""``ObjectStoreSource``: one bucket prefix as a read-only compiler ``Source`` (ADR 0006).

It has the shape of the compiler's ``Source`` protocol (``walk``, ``open``). Its entries mirror
the compiler's ``SourceEntry`` and ``SkippedEntry`` but are its own types, because members may not
import the compiler's discovery package; the compiler's scan accepts them once it ingests plugin
Sources (ADR 0006, compiler gaps). It adds what an external source needs that a folder does not:

- ``listing()``: every object under the prefix, from every page, sorted by key. Its identity is
  ``ExternalObjectRef(connector id, <bucket>/<key>, <version id, generation or etag>)``. Nothing in
  it depends on the wall clock, the page size or the order pages or entries arrive in.
- ``discover(ledger)``: the listing against the compiler's ``SourceLedger``. An object whose ledger
  head carries the same revision token is unchanged and is never fetched or probed; one with another
  token, or none, is to be probed. Objects the ledger holds and the listing no longer does are
  ``gone``, asserted only when the listing was complete.
- ``walk()``: what a job should fingerprint and probe: every listed object without a ledger, and
  only new or changed ones with one. Entries the listing could not use come after, as
  ``SkippedObject``.
- ``open(location)``: a seekable stream that reads lazily with ranged GETs pinned to the listed
  revision; ``reader(location, artifact)``: an adapter's ``SourceReader`` over the same ranges, each
  chunk checked against the artifact's chunk hashes before a byte is served.

Every problem is a finding (``findings()``), deterministic and free of URLs, credentials and error
text. A read that fails also raises ``ObjectReadError`` (an ``OSError``), so the caller quarantines
that object and nothing else.
"""

import hashlib
import io
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import BinaryIO, Final, Protocol, TypeAlias, TypeVar

from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import TransformRecord
from neptune.model.source import SourceArtifact, SourceLocation, SourceRevision
from neptune_deploy.sources.object_store.clients import Cursor, Listed, StoreClient
from neptune_deploy.sources.object_store.config import (
    CONNECTOR_IDS,
    MAX_KEY_BYTES,
    Options,
    StoreLocation,
)
from neptune_deploy.sources.object_store.transport import (
    HttpStatusError,
    NetworkGate,
    ShortRead,
    TransportError,
)

CONNECTOR_VERSION: Final = "0.1.0"
LISTING_TOKEN: Final = "listing"  # the revision token of a finding about the listing itself
MAX_PAGES: Final = 100_000
MAX_EXAMPLES: Final = 10  # keys a finding about many keys cites
MAX_EXAMPLE_BYTES: Final = 256  # of each key a finding cites
MAX_SKIPPED_KEY_BYTES: Final = 256  # of a key the source does not use, kept with length and digest
MIN_WINDOW: Final = 64 * 1024
MAX_WINDOW: Final = 8 * 1024 * 1024  # the most one ranged GET of a stream asks for
READER_CACHE: Final = 4  # checked chunks an ObjectReader keeps

# Finding codes, each prefixed with the connector id. Category and severity are fixed per code.
CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "listing_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a listing request failed; the listing is incomplete and nothing is asserted gone",
    ),
    "redirect_refused": (
        FindingCategory.SKIPPED,
        Severity.ERROR,
        "the store answered with a redirect, which is never followed; the listing or read stopped",
    ),
    "response_invalid": (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "a listing page is not the store's format, or declares a document type; the listing"
        " stopped there and is incomplete",
    ),
    "listing_limit": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "the listing stopped at the object, byte or page limit; keys after the one it names are"
        " not covered",
    ),
    "pagination_loop": (
        FindingCategory.INCONSISTENT,
        Severity.ERROR,
        "the store named a page it had already returned; the listing stopped there",
    ),
    "key_duplicated": (
        FindingCategory.AMBIGUOUS,
        Severity.WARNING,
        "one key was listed with two revisions or sizes; neither is used",
    ),
    "key_outside_prefix": (
        FindingCategory.INCONSISTENT,
        Severity.WARNING,
        "the store listed keys that do not start with the prefix asked for; they are not used",
    ),
    "key_not_utf8": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "listed keys are not valid UTF-8; they are not used",
    ),
    "key_too_long": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        f"listed keys are longer than {MAX_KEY_BYTES} bytes; they are not used",
    ),
    "revision_invalid": (
        FindingCategory.MISSING,
        Severity.WARNING,
        "listed objects state no usable version id, generation or etag; they are not used",
    ),
    "size_invalid": (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "listed objects state no usable size; they are not used",
    ),
    "short_read": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a ranged read ended before the bytes it promised",
    ),
    "object_changed": (
        FindingCategory.INCONSISTENT,
        Severity.ERROR,
        "the object no longer holds the listed revision's bytes (precondition, size or chunk hash)",
    ),
    "object_gone": (
        FindingCategory.MISSING,
        Severity.ERROR,
        "the listed revision of the object no longer exists",
    ),
    "read_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a ranged read failed, or was answered with other bytes than were asked for",
    ),
}
_KEY_REASONS: Final = (
    "key_duplicated",
    "key_not_utf8",
    "key_outside_prefix",
    "key_too_long",
    "revision_invalid",
    "size_invalid",
)


class ObjectReadError(OSError):
    """Reading an object failed; ``code`` is the finding code without the connector prefix."""

    def __init__(self, code: str, location: ExternalObjectRef) -> None:
        super().__init__(f"{location.connector_id}: {code}")
        self.code = code
        self.location = location


@dataclass(frozen=True)
class ObjectEntry:
    """One object to read: where it is (with its revision), its key and listed size.

    Shaped like the compiler's ``SourceEntry`` (``location``, ``size``).
    """

    location: ExternalObjectRef
    size: int
    key: str

    @property
    def name(self) -> str:
        """The key's last part: an adapter's ``ProbeHints.name``. Advisory, as every name is."""
        return self.key.rpartition("/")[2]


KeyOrder: TypeAlias = tuple[bytes, int, str]


def _sha256(key: str | bytes) -> str:
    raw = key.encode("utf-8", "surrogateescape") if isinstance(key, str) else key
    return hashlib.sha256(raw).hexdigest()


def key_order(raw: bytes) -> KeyOrder:
    """How keys are ordered when a listing is cut: by their first ``MAX_SKIPPED_KEY_BYTES``, then
    length and digest. For keys no longer than that, this is plain byte order."""
    return raw[:MAX_SKIPPED_KEY_BYTES], len(raw), _sha256(raw)


def _within(order: KeyOrder, cutoff: KeyOrder | None) -> bool:
    return cutoff is not None and order <= cutoff


@dataclass(frozen=True)
class SkippedObject:
    """A listed entry the source does not use, and the finding code saying why.

    Built from the key bytes as listed, it keeps only their first ``MAX_SKIPPED_KEY_BYTES``, with
    the whole key's length and sha256: a store that lists huge unusable keys cannot make the source
    hold them.
    """

    raw_key: bytes
    reason: str
    length: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "length", len(self.raw_key))
        object.__setattr__(self, "sha256", _sha256(self.raw_key))
        object.__setattr__(self, "raw_key", self.raw_key[:MAX_SKIPPED_KEY_BYTES])

    @property
    def order(self) -> KeyOrder:
        return self.raw_key, self.length, self.sha256


@dataclass(frozen=True)
class Listing:
    """Every usable object under the prefix, sorted by key, and what was not used."""

    entries: tuple[ObjectEntry, ...]
    skipped: tuple[SkippedObject, ...]
    complete: bool  # every page was read; only then is anything asserted gone


@dataclass(frozen=True)
class Discovery:
    """A listing against a ledger: what needs probing, what does not, and what is gone."""

    new: tuple[ObjectEntry, ...]
    changed: tuple[ObjectEntry, ...]
    unchanged: tuple[tuple[ObjectEntry, SourceRevision], ...]
    gone: tuple[SourceRevision, ...]
    complete: bool

    @property
    def to_probe(self) -> tuple[ObjectEntry, ...]:
        """New and changed objects, in key order: the only ones a job fetches and probes."""
        return tuple(sorted((*self.new, *self.changed), key=lambda entry: entry.key))


class Located(Protocol):
    """Anything a ledger can be asked about: an entry with its external location."""

    @property
    def location(self) -> ExternalObjectRef: ...


E = TypeVar("E", bound=Located)


def classify(
    entries: Sequence[E], ledger: SourceLedger
) -> tuple[tuple[E, ...], tuple[E, ...], tuple[tuple[E, SourceRevision], ...]]:
    """Entries sorted against ``ledger``: new (no head, or an absent head), changed (a head with
    another revision token) and unchanged (a head with the same token), each in the order given.
    Shared by every connector, so they cannot differ in what "changed" means (ADR 0006 §5)."""
    new: list[E] = []
    changed: list[E] = []
    unchanged: list[tuple[E, SourceRevision]] = []
    for entry in entries:
        head = ledger.head(entry.location)
        if not isinstance(head, SourceRevision):
            new.append(entry)  # never seen, or seen absent
        elif (
            isinstance(head.location, ExternalObjectRef)
            and head.location.revision_token == entry.location.revision_token
        ):
            unchanged.append((entry, head))
        else:
            changed.append(entry)
    return tuple(new), tuple(changed), tuple(unchanged)


def absent_candidates(
    ledger: SourceLedger,
    connector_id: str,
    scope: str,
    seen: set[str],
    *,
    blind: Callable[[str], bool] = lambda object_id: False,
) -> tuple[SourceRevision, ...]:
    """Ledger revisions of ``connector_id`` whose object id starts with ``scope`` and is not in
    ``seen`` (nor ``blind``: seen, but not kept), by location. The caller decides whether the
    listing was complete enough to say they are gone."""
    gone: list[SourceRevision] = []
    for head in ledger.heads():
        where = head.location
        if (
            isinstance(head, SourceRevision)
            and isinstance(where, ExternalObjectRef)
            and where.connector_id == connector_id
            and where.object_id.startswith(scope)
            and where.object_id not in seen
            and not blind(where.object_id)
        ):
            gone.append(head)
    return tuple(sorted(gone, key=lambda revision: revision.location.key))


class RangedSource(Protocol):
    """What a stream or an adapter reader needs of a source: ranged fetches and findings."""

    def fetch(self, entry: ObjectEntry, start: int, length: int) -> bytes: ...

    def report(
        self, code: str, subject: ExternalObjectRef, details: dict[str, JsonValue]
    ) -> None: ...


class ObjectStoreSource:
    """A bucket prefix (or Azure container prefix), read only, as a compiler ``Source``."""

    def __init__(
        self,
        location: StoreLocation,
        client: StoreClient,
        network: NetworkGate,
        options: Options,
        *,
        ledger: SourceLedger | None = None,
    ) -> None:
        self.location = location
        self.client = client
        self.options = options
        self.connector_id = CONNECTOR_IDS[location.provider]
        self._network = network
        self._ledger = ledger
        self._findings: dict[str, IngestFinding] = {}

    # --- Identity ------------------------------------------------------------------------------

    @cached_property
    def transform(self) -> TransformRecord:
        """The connector as a producer: what decided which objects were seen. Its findings name it.

        The endpoint and credentials are not in it: like a local root path, they say where the
        bytes were read from, not what was read.
        """
        config: dict[str, JsonValue] = {
            "bucket": self.location.bucket,
            "max_objects": self.options.max_objects,
            "max_listing_bytes": self.options.max_listing_bytes,
            "prefix": self.location.prefix,
            "provider": self.location.provider.value,
        }
        if self.location.account is not None:
            config["account"] = self.location.account
        if self.location.store is not None:
            config["store"] = self.location.store
        if self.location.provider.value == "s3":
            config["versions"] = self.options.versions
        return transform_record(
            adapter_id=self.connector_id, adapter_version=CONNECTOR_VERSION, config=config
        )

    @property
    def listing_ref(self) -> ExternalObjectRef:
        """The subject of a finding about the listing as a whole."""
        return ExternalObjectRef(
            self.connector_id, self.location.scope + self.location.prefix, LISTING_TOKEN
        )

    def ref(self, key: str, token: str) -> ExternalObjectRef:
        return ExternalObjectRef(self.connector_id, self.location.object_id(key), token)

    # --- Findings -------------------------------------------------------------------------------

    def report(self, code: str, subject: ExternalObjectRef, details: dict[str, JsonValue]) -> None:
        category, severity, message = CODES[code]
        finding = ingest_finding(
            code=f"{self.connector_id}.{code}",
            category=category,
            severity=severity,
            subject=subject,
            transform=self.transform,
            message=message,
            details=details,
        )
        self._findings[finding.id] = finding

    def findings(self) -> tuple[IngestFinding, ...]:
        """Every finding so far, sorted by id."""
        return tuple(self._findings[key] for key in sorted(self._findings))

    # --- Listing --------------------------------------------------------------------------------

    @cached_property
    def _listing(self) -> Listing:
        prefix = self.location.prefix
        limit = self.options.max_objects
        budget = self.options.max_listing_bytes
        kept: dict[str, Listed] = {}
        duplicated: set[str] = set()
        skipped: set[SkippedObject] = set()
        skipped_keys: set[KeyOrder] = set()
        held = 0  # key and token bytes the listing holds: kept keys whole, skipped keys capped
        cursors: set[bytes] = set()  # digests of the cursors seen, never the cursors themselves
        cursor: Cursor | None = None
        complete = False
        stopped: dict[str, JsonValue] | None = None  # why a limit stopped the listing
        pages = 0
        while True:
            if pages >= MAX_PAGES:
                stopped = {"pages": pages}
                break
            try:
                page = self.client.list_page(prefix, cursor, self.options.page_size)
            except (TransportError, ValueError) as exc:
                # One finding: the specific refusal if it has a code, else the failure's cause.
                code = exc.code if isinstance(exc, TransportError) else "response_invalid"
                details: dict[str, JsonValue] = {"page": pages}
                if isinstance(exc, TransportError) and exc.status is not None:
                    details["status"] = exc.status
                if code in ("redirect_refused", "response_invalid"):
                    self.report(code, self.listing_ref, details)
                else:
                    self.report("listing_failed", self.listing_ref, {**details, "cause": code})
                break
            pages += 1
            found = [SkippedObject(item.raw, item.reason) for item in page.unlisted]
            for item in page.objects:
                raw = item.key.encode("utf-8")
                if not item.key.startswith(prefix):
                    found.append(SkippedObject(raw, "key_outside_prefix"))
                elif len(raw) > MAX_KEY_BYTES:
                    found.append(SkippedObject(raw, "key_too_long"))
                elif item.key in kept and kept[item.key] != item:
                    duplicated.add(item.key)
                elif item.key not in kept:
                    kept[item.key] = item
                    held += len(raw) + len(item.token)
            for skip in found:
                if skip not in skipped:
                    skipped.add(skip)
                    skipped_keys.add(skip.order)
                    held += len(skip.raw_key)
            # Every distinct key counts, used or not, and so does every byte held: no listing
            # grows without bound, whatever the store sends.
            if len(kept) + len(skipped_keys) > limit:
                stopped = {"max_objects": limit}
                break
            if held > budget:
                stopped = {"max_listing_bytes": budget}
                break
            if page.cursor is None:
                complete = True
                break
            digest = hashlib.sha256("\x00".join(page.cursor).encode("utf-8", "surrogateescape"))
            if digest.digest() in cursors:
                self.report("pagination_loop", self.listing_ref, {"page": pages})
                break
            cursors.add(digest.digest())
            cursor = page.cursor
        if stopped is not None and (kept or skipped_keys):
            # Keep what any page size keeps. Stores list in key order, so every key below the
            # greatest one seen has been seen whole, duplicates included; keep the first ``limit``
            # of those, and nothing after them. The rest of the prefix is not covered.
            seen = {key_order(key.encode("utf-8")) for key in kept} | skipped_keys
            greatest = max(seen)
            below = sorted(order for order in seen if order < greatest)[:limit]
            cutoff = below[-1] if below else None
            kept = {k: v for k, v in kept.items() if _within(key_order(k.encode("utf-8")), cutoff)}
            duplicated = {k for k in duplicated if _within(key_order(k.encode("utf-8")), cutoff)}
            skipped = {item for item in skipped if _within(item.order, cutoff)}
            last = cutoff[0][:MAX_EXAMPLE_BYTES].hex() if cutoff else ""
            self.report("listing_limit", self.listing_ref, {**stopped, "covered_through_hex": last})
        elif stopped is not None:
            self.report("listing_limit", self.listing_ref, {**stopped, "covered_through_hex": ""})
        for key in duplicated:
            del kept[key]
            skipped.add(SkippedObject(key.encode("utf-8"), "key_duplicated"))
        entries = tuple(
            ObjectEntry(self.ref(key, item.token), item.size, key)
            for key, item in sorted(kept.items())
        )
        unique = tuple(sorted(skipped, key=lambda s: (s.reason, s.order)))
        self._report_skipped(unique)
        return Listing(entries, unique, complete)

    def _report_skipped(self, skipped: tuple[SkippedObject, ...]) -> None:
        """One finding per reason, citing the first keys (as hex) and counting them all."""
        by_reason: defaultdict[str, list[bytes]] = defaultdict(list)
        for item in skipped:
            by_reason[item.reason].append(item.raw_key)
        for reason in _KEY_REASONS:
            keys = by_reason.get(reason)
            if keys:
                examples: list[JsonValue] = [
                    key[:MAX_EXAMPLE_BYTES].hex() for key in keys[:MAX_EXAMPLES]
                ]
                self.report(reason, self.listing_ref, {"count": len(keys), "keys_hex": examples})

    def listing(self) -> Listing:
        """Every page of the listing, read once per source and kept."""
        return self._listing

    def discover(self, ledger: SourceLedger) -> Discovery:
        """The listing against ``ledger`` (a ledger of this ingest root, ADR 0009)."""
        listing = self.listing()
        new, changed, unchanged = self._classify(listing, ledger)
        return Discovery(new, changed, unchanged, self._gone(listing, ledger), listing.complete)

    def _classify(
        self, listing: Listing, ledger: SourceLedger
    ) -> tuple[
        tuple[ObjectEntry, ...],
        tuple[ObjectEntry, ...],
        tuple[tuple[ObjectEntry, SourceRevision], ...],
    ]:
        """New, changed and unchanged objects, each in key order."""
        return classify(listing.entries, ledger)

    def _gone(self, listing: Listing, ledger: SourceLedger) -> tuple[SourceRevision, ...]:
        """Ledger revisions under this scope and prefix that a complete listing no longer holds."""
        gone: tuple[SourceRevision, ...] = ()
        if listing.complete:
            seen = {entry.location.object_id for entry in listing.entries}
            blind = {item.sha256 for item in listing.skipped}  # seen, not used: nothing known
            scope = self.location.scope + self.location.prefix
            gone = absent_candidates(
                ledger,
                self.connector_id,
                scope,
                seen,
                blind=lambda object_id: _sha256(object_id[len(self.location.scope) :]) in blind,
            )
        return gone

    # --- The Source protocol ---------------------------------------------------------------------

    def walk(self) -> Iterator[ObjectEntry | SkippedObject]:
        """What to fingerprint and probe, in key order, then what was not used.

        With a ledger, unchanged objects are left out: they keep their ledger revision and are never
        fetched (``discover`` lists them).
        """
        listing = self.listing()
        if self._ledger is None:
            yield from listing.entries
        else:
            new, changed, _ = self._classify(listing, self._ledger)
            yield from sorted((*new, *changed), key=lambda entry: entry.key)
        yield from listing.skipped

    def entry(self, location: SourceLocation) -> ObjectEntry:
        """The listed object at ``location``, at the revision the listing holds.

        Another connector's location is a ``TypeError``; one of this connector's that the listing
        does not hold at that revision is ``ObjectReadError("not_listed")``, with no finding (it is
        the caller's mistake, not the store's).
        """
        if (
            isinstance(location, ExternalObjectRef)
            and location.connector_id == self.connector_id
            and location.object_id.startswith(self.location.scope)
        ):
            entry = self._by_key.get(location.object_id[len(self.location.scope) :])
            if entry is not None and entry.location == location:
                return entry
            raise ObjectReadError("not_listed", location)
        raise TypeError(f"{self.connector_id} cannot open {location!r}")

    @cached_property
    def _by_key(self) -> dict[str, ObjectEntry]:
        return {entry.key: entry for entry in self.listing().entries}

    def open(self, location: SourceLocation) -> BinaryIO:
        """A seekable, read-only stream over the listed revision, fetched in ranges as read.

        Buffered, so ``read(n)`` returns ``n`` bytes unless the object ends first, as a local
        file's stream does; no single request asks for more than 8 MiB.
        """
        return io.BufferedReader(ObjectStream(self, self.entry(location)), MIN_WINDOW)

    def reader(self, location: SourceLocation, artifact: SourceArtifact) -> "ObjectReader":
        """An adapter's reader over ``location``, whose bytes were fingerprinted as ``artifact``."""
        return ObjectReader(self, self.entry(location), artifact)

    # --- Ranged reads ----------------------------------------------------------------------------

    def fetch(self, entry: ObjectEntry, start: int, length: int) -> bytes:
        """``length`` bytes of ``entry`` from ``start``, pinned to its listed revision."""
        if start < 0 or length <= 0 or start + length > entry.size:
            raise ValueError(f"bytes {start}+{length} are outside an object of {entry.size}")
        where = entry.location
        span: dict[str, JsonValue] = {"length": length, "offset": start}
        try:
            got = self.client.get_range(entry.key, where.revision_token, start, length)
        except ShortRead as exc:
            self.report("short_read", where, span)
            raise ObjectReadError("short_read", where) from exc
        except HttpStatusError as exc:
            code = {404: "object_gone", 412: "object_changed", 416: "object_changed"}.get(
                exc.status or 0, "read_failed"
            )
            self.report(code, where, {**span, "status": exc.status or 0})
            raise ObjectReadError(code, where) from exc
        except TransportError as exc:
            code = "redirect_refused" if exc.code == "redirect_refused" else "read_failed"
            self.report(code, where, {**span, "cause": exc.code})
            raise ObjectReadError(code, where) from exc
        except ValueError as exc:  # a client let a malformed answer through: still this object's
            self.report("read_failed", where, {**span, "cause": "response_invalid"})
            raise ObjectReadError("read_failed", where) from exc
        if got.total is not None and got.total != entry.size:
            self.report("object_changed", where, {**span, "size": got.total})
            raise ObjectReadError("object_changed", where)
        return got.data


class ObjectStream(io.RawIOBase):
    """A listed object as a seekable stream. Sequential reads fetch growing windows (64 KiB up to
    8 MiB), so a probe's head costs one small request and hashing the object a few large ones."""

    def __init__(self, source: RangedSource, entry: ObjectEntry) -> None:
        super().__init__()
        self._source = source
        self._entry = entry
        self._pos = 0
        self._buffer = b""
        self._buffer_start = 0
        self._window = MIN_WINDOW

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._entry.size}[whence]
        if base + offset < 0:
            raise ValueError("negative seek position")
        if base + offset != self._pos:
            self._window = MIN_WINDOW
        self._pos = base + offset
        return self._pos

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        view = memoryview(buffer).cast("B")
        want = min(len(view), self._entry.size - self._pos)
        if want <= 0:
            return 0
        offset = self._pos - self._buffer_start
        if not 0 <= offset < len(self._buffer):
            length = min(max(want, self._window), MAX_WINDOW, self._entry.size - self._pos)
            self._buffer = self._source.fetch(self._entry, self._pos, length)
            self._buffer_start, offset = self._pos, 0
            self._window = min(self._window * 2, MAX_WINDOW)
        piece = self._buffer[offset : offset + want]
        view[: len(piece)] = piece
        self._pos += len(piece)
        return len(piece)


class ObjectReader:
    """An adapter's ``SourceReader`` over one object: ranged reads, each chunk checked first.

    Reads go through the artifact's chunks: each is fetched with one ranged GET, hashed and compared
    with ``artifact.chunks`` before any byte of it is served, and the last few are kept. An adapter
    never downloads more of an object than the chunks it reads.
    """

    def __init__(self, source: RangedSource, entry: ObjectEntry, artifact: SourceArtifact) -> None:
        if artifact.size != entry.size:
            raise ValueError(f"the artifact has {artifact.size} bytes, the object {entry.size}")
        self._source = source
        self._entry = entry
        self._artifact = artifact
        self._cache: OrderedDict[int, bytes] = OrderedDict()

    @property
    def content_id(self) -> ContentId:
        return self._artifact.content_id

    @property
    def size(self) -> int:
        return self._artifact.size

    def _chunk(self, index: int) -> bytes:
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        step = self._artifact.chunk_size
        start = index * step
        data = self._source.fetch(self._entry, start, min(step, self.size - start))
        if content_id(data) != self._artifact.chunks[index]:
            where = self._entry.location
            span: dict[str, JsonValue] = {"chunk": index, "length": len(data), "offset": start}
            self._source.report("object_changed", where, span)
            raise ObjectReadError("object_changed", where)
        self._cache[index] = data
        if len(self._cache) > READER_CACHE:
            self._cache.popitem(last=False)
        return data

    def read(self, offset: int, length: int) -> bytes:
        for name, value in (("offset", offset), ("length", length)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        if offset > self.size:
            raise ValueError(f"offset {offset} is past the end of {self.size} bytes")
        end = min(offset + length, self.size)
        step = self._artifact.chunk_size
        parts: list[bytes] = []
        position = offset
        while position < end:
            index = position // step
            chunk = self._chunk(index)
            within = position - index * step
            piece = chunk[within : within + (end - position)]
            parts.append(piece)
            position += len(piece)
        return b"".join(parts)
