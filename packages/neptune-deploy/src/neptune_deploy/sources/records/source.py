"""``RecordSource``: one record system's export as a read-only compiler ``Source`` (ADR 0008).

It has the shape of the compiler's ``Source`` protocol (``walk``, ``open``) and, like the
object-store source (ADR 0006), its own entry types, because members may not import the compiler's
discovery package. What it adds for a record system:

- ``listing()``: every page of the system's feed, read once. A snapshot reads every record the
  scope holds; an incremental run (a ``since`` cursor) reads what changed. Each record, document
  or attachment is ``ExternalObjectRef(connector id, <scope><id>, <revision token>)``. Nothing in
  it depends on the wall clock, the order entries arrive in within a page, or whether a retry
  happened.
- ``discover(ledger)``: the listing against the compiler's ``SourceLedger``: new, changed,
  unchanged (never fetched) and gone. ``gone`` is a complete snapshot's absences, the ids the
  system said were deleted, and the attachments a parent's own listing no longer holds.
- ``walk()``: what a job should fingerprint and probe, in id order, then what was not used.
- ``open(location)`` / ``reader(location, artifact)``: the item's bytes. A record's snapshot is
  held from its page; an attachment or a document is fetched once, to exactly its listed size, and
  checked against the system's own checksum if it states one.
- ``relations()``: each attachment's declared parent.
- ``cursor``: where the next run may continue; the caller stores it after the job's receipt is
  durable.

Every problem is a finding (``findings()``), deterministic and free of URLs, credentials and error
text. A failed read also raises ``ObjectReadError`` (an ``OSError``), so the caller quarantines
that item and nothing else.
"""

import hashlib
import io
from collections import OrderedDict, defaultdict
from collections.abc import Iterator
from functools import cached_property
from typing import BinaryIO, Final

from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import TransformRecord
from neptune.model.source import SourceArtifact, SourceLocation, SourceRevision
from neptune_deploy.sources.object_store.source import ObjectReadError
from neptune_deploy.sources.object_store.transport import (
    HttpStatusError,
    NetworkGate,
    RedirectRefused,
    ShortRead,
    TransportError,
)
from neptune_deploy.sources.records.config import Location, Options
from neptune_deploy.sources.records.http import (
    AccessDenied,
    PaginationLoop,
    RateLimited,
    ResponseInvalid,
    SizeMismatch,
)
from neptune_deploy.sources.records.model import (
    Fetch,
    Item,
    Listing,
    Page,
    RecordEntry,
    Relation,
    SkippedRecord,
    sha256_text,
)
from neptune_deploy.sources.records.systems import System

CONNECTOR_VERSION: Final = "0.1.0"
LISTING_TOKEN: Final = "listing"  # the revision token of a finding about the listing itself
MAX_PAGES: Final = 100_000
MAX_EXAMPLES: Final = 10  # ids a finding about many ids cites
MAX_EXAMPLE_BYTES: Final = 256  # of each id a finding cites
ATTACHMENTS: Final = "/attachment/"  # what separates a parent's id from an attachment's
MAX_CURSOR: Final = 4096  # bytes of a continuation cursor; a longer one stops the listing
_MALFORMED: Final = (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError)
MAX_ID_BYTES: Final = 1024
MAX_TOKEN_CHARS: Final = 1024
MAX_SNAPSHOT_BYTES: Final = 8 * 1024 * 1024  # one record's snapshot
CACHED_BODIES: Final = 2  # fetched bodies kept for reads that follow the first

# Finding codes, each prefixed with the connector id. Category and severity are fixed per code.
CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "listing_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a listing request failed; the listing is incomplete and nothing is asserted gone",
    ),
    "rate_limited": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "the system answered 429; nothing was retried, the listing or read stopped, and any cursor"
        " points at where to continue",
    ),
    "access_denied": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "the declared credentials are not allowed to read this (401 or 403)",
    ),
    "redirect_refused": (
        FindingCategory.SKIPPED,
        Severity.ERROR,
        "the system answered with a redirect, which is never followed; the listing or read stopped",
    ),
    "response_invalid": (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "a response is not the system's documented JSON, is compressed, or exceeds the size limit;"
        " the listing stopped there and is incomplete",
    ),
    "listing_limit": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "the listing stopped at the record, page or snapshot-size limit; it is incomplete",
    ),
    "listing_partial": (
        FindingCategory.INCONSISTENT,
        Severity.WARNING,
        "the system said a page was incomplete, or listed fewer records than it counted; the"
        " listing is incomplete and nothing is asserted gone",
    ),
    "pagination_loop": (
        FindingCategory.INCONSISTENT,
        Severity.ERROR,
        "the system named a page, or returned records, it had already returned; the listing"
        " stopped there",
    ),
    "record_duplicated": (
        FindingCategory.AMBIGUOUS,
        Severity.WARNING,
        "one id was listed with two revisions or bodies; neither is used",
    ),
    "id_invalid": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "listed ids are not in the form the system documents; they are not used",
    ),
    "record_invalid": (
        FindingCategory.MISSING,
        Severity.WARNING,
        "listed records state no usable id, revision or required field; they are not used",
    ),
    "record_unrepresentable": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "listed records have no deterministic text (a lone surrogate, nesting too deep); they are"
        " not used",
    ),
    "record_too_large": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "listed records or attachments are larger than the limit; they are not used",
    ),
    "type_unsupported": (
        FindingCategory.UNSUPPORTED,
        Severity.INFO,
        "listed documents are of a type this connector does not export (Google-native files have"
        " no bytes or checksum until exported); they are not used",
    ),
    "size_invalid": (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "listed attachments or files state no usable size; they are not used",
    ),
    "short_read": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a read ended before the bytes it promised",
    ),
    "object_changed": (
        FindingCategory.INCONSISTENT,
        Severity.ERROR,
        "the item no longer holds the listed revision's bytes (size, checksum or chunk hash)",
    ),
    "object_gone": (
        FindingCategory.MISSING,
        Severity.ERROR,
        "the listed item no longer exists",
    ),
    "read_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a read failed",
    ),
}
_SKIP_REASONS: Final = (
    "record_duplicated",
    "id_invalid",
    "record_invalid",
    "record_unrepresentable",
    "record_too_large",
    "type_unsupported",
    "size_invalid",
)


class Discovery:
    """A listing against a ledger: what needs probing, what does not, and what is gone."""

    def __init__(
        self,
        new: tuple[RecordEntry, ...],
        changed: tuple[RecordEntry, ...],
        unchanged: tuple[tuple[RecordEntry, SourceRevision], ...],
        gone: tuple[SourceRevision, ...],
        complete: bool,
    ) -> None:
        self.new = new
        self.changed = changed
        self.unchanged = unchanged
        self.gone = gone
        self.complete = complete

    @property
    def to_probe(self) -> tuple[RecordEntry, ...]:
        """New and changed items, in id order: the only ones a job fetches and probes."""
        return tuple(sorted((*self.new, *self.changed), key=lambda entry: entry.id))


def _hex(raw: str) -> str:
    return raw.encode("utf-8", "surrogatepass")[:MAX_EXAMPLE_BYTES].hex()


class RecordSource:
    """One record system's scope, read only, as a compiler ``Source``."""

    def __init__(
        self,
        location: Location,
        system: System,
        network: NetworkGate,
        options: Options,
        config: dict[str, JsonValue],
        *,
        ledger: SourceLedger | None = None,
    ) -> None:
        self.location = location
        self.system = system
        self.options = options
        self.connector_id = location.connector_id
        self._network = network
        self._ledger = ledger
        self._config = config
        self._findings: dict[str, IngestFinding] = {}
        self._bodies: dict[str, bytes] = {}
        self._fetches: dict[str, Fetch] = {}
        self._children: dict[str, str] = {}
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    # --- Identity ------------------------------------------------------------------------------

    @cached_property
    def transform(self) -> TransformRecord:
        """The connector as a producer: what decided which records were seen. Findings name it.

        The endpoint, credentials and cursor are not in it: they say where, as whom and from when
        bytes were read, not what was read.
        """
        config: dict[str, JsonValue] = {
            **self._config,
            "max_attachment_bytes": self.options.max_attachment_bytes,
            "max_listing_bytes": self.options.max_listing_bytes,
            "max_records": self.options.max_records,
            "max_snapshot_bytes": self.options.max_snapshot_bytes,
            "page_size": self.options.page_size or 0,
            "scope": self.location.scope,
        }
        return transform_record(
            adapter_id=self.connector_id, adapter_version=CONNECTOR_VERSION, config=config
        )

    @property
    def listing_ref(self) -> ExternalObjectRef:
        """The subject of a finding about the listing as a whole."""
        return ExternalObjectRef(self.connector_id, self.location.scope, LISTING_TOKEN)

    def ref(self, item_id: str, token: str) -> ExternalObjectRef:
        return ExternalObjectRef(self.connector_id, self.location.scope + item_id, token)

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
        return _Collect(self).run()

    def listing(self) -> Listing:
        """Every page of the feed, read once per source and kept."""
        return self._listing

    @property
    def cursor(self) -> str | None:
        """Where the next run may continue (``since``), or ``None`` to keep the previous cursor."""
        return self.listing().cursor

    def ingest_options(self) -> dict[str, dict[str, JsonValue]]:
        """Adapter options the snapshots need declared when they are ingested, by adapter id: a CSV
        snapshot's first row is its header, which the compiler's tabular adapter reads only if told
        (``csv_header: first_row``, root ADR 0042). The compiler has no way yet for a plugin Source
        to declare them itself (ADR 0008 compiler gaps), so the caller writes them to the
        manifest."""
        return {adapter: dict(options) for adapter, options in self.system.declared_options.items()}

    def relations(self) -> tuple[Relation, ...]:
        """Each attachment's declared parent, in child id order."""
        by_id = {entry.id: entry for entry in self.listing().entries}
        return tuple(
            Relation(entry.location, entry.parent, locator=entry.locator)
            for entry in by_id.values()
            if entry.parent is not None
        )

    def discover(self, ledger: SourceLedger) -> Discovery:
        """The listing against ``ledger`` (a ledger of this ingest root, ADR 0009)."""
        listing = self.listing()
        new, changed, unchanged = self._classify(listing, ledger)
        return Discovery(new, changed, unchanged, self._gone(listing, ledger), listing.complete)

    def _classify(
        self, listing: Listing, ledger: SourceLedger
    ) -> tuple[
        tuple[RecordEntry, ...],
        tuple[RecordEntry, ...],
        tuple[tuple[RecordEntry, SourceRevision], ...],
    ]:
        new: list[RecordEntry] = []
        changed: list[RecordEntry] = []
        unchanged: list[tuple[RecordEntry, SourceRevision]] = []
        for entry in listing.entries:
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

    def _gone(self, listing: Listing, ledger: SourceLedger) -> tuple[SourceRevision, ...]:
        """Ledger revisions of this scope that the system no longer holds, as far as it says so."""
        scope = self.location.scope
        heads = [
            head
            for head in ledger.heads()
            if isinstance(head, SourceRevision)
            and isinstance(head.location, ExternalObjectRef)
            and head.location.connector_id == self.connector_id
            and head.location.object_id.startswith(scope)
        ]
        seen = {scope + entry.id for entry in listing.entries}
        blind = {item.sha256 for item in listing.skipped}  # seen, not used: nothing is known
        gone: dict[str, SourceRevision] = {}
        by_name: dict[str, SourceRevision] = {}
        by_dir: defaultdict[str, list[SourceRevision]] = defaultdict(list)
        for head in heads:
            assert isinstance(head.location, ExternalObjectRef)
            name = head.location.object_id
            by_name[name] = head
            by_dir[name[: name.rfind("/") + 1]].append(head)  # an attachment's parent directory

        def unknown(name: str) -> bool:
            return name not in seen and sha256_text(name[len(scope) :]) not in blind

        if listing.mode == "snapshot" and listing.complete:
            for name, head in by_name.items():
                if unknown(name):
                    gone[head.id] = head
        for removed in listing.removed:  # the system said so: the record and what hangs under it
            target = scope + removed
            if target in by_name:
                gone[by_name[target].id] = by_name[target]
            for head in by_dir.get(target + ATTACHMENTS, ()):
                gone[head.id] = head
        for entry in listing.entries:  # a parent's own full list of its attachments
            prefix = self._children.get(entry.id)
            if prefix is not None:
                for head in by_dir.get(scope + prefix, ()):
                    assert isinstance(head.location, ExternalObjectRef)
                    if unknown(head.location.object_id):
                        gone[head.id] = head
        return tuple(sorted(gone.values(), key=lambda revision: revision.location.key))

    # --- The Source protocol ---------------------------------------------------------------------

    def walk(self) -> Iterator[RecordEntry | SkippedRecord]:
        """What to fingerprint and probe, in id order, then what was not used.

        With a ledger, unchanged items are left out: they keep their ledger revision and are never
        fetched (``discover`` lists them).
        """
        listing = self.listing()
        if self._ledger is None:
            yield from listing.entries
        else:
            new, changed, _ = self._classify(listing, self._ledger)
            yield from sorted((*new, *changed), key=lambda entry: entry.id)
        yield from listing.skipped

    def entry(self, location: SourceLocation) -> RecordEntry:
        """The listed item at ``location``, at the revision the listing holds.

        Another connector's location is a ``TypeError``; one of this connector's that the listing
        does not hold at that revision is ``ObjectReadError("not_listed")``, with no finding (it is
        the caller's mistake, not the system's).
        """
        if (
            isinstance(location, ExternalObjectRef)
            and location.connector_id == self.connector_id
            and location.object_id.startswith(self.location.scope)
        ):
            entry = self._by_id.get(location.object_id[len(self.location.scope) :])
            if entry is not None and entry.location == location:
                return entry
            raise ObjectReadError("not_listed", location)
        raise TypeError(f"{self.connector_id} cannot open {location!r}")

    @cached_property
    def _by_id(self) -> dict[str, RecordEntry]:
        return {entry.id: entry for entry in self.listing().entries}

    def open(self, location: SourceLocation) -> BinaryIO:
        """A seekable, read-only stream over the listed revision's bytes."""
        return io.BytesIO(self.read_all(self.entry(location)))

    def reader(self, location: SourceLocation, artifact: SourceArtifact) -> "RecordReader":
        """An adapter's reader over ``location``, whose bytes were fingerprinted as ``artifact``."""
        return RecordReader(self, self.entry(location), artifact)

    # --- Reads -----------------------------------------------------------------------------------

    def read_all(self, entry: RecordEntry) -> bytes:
        """The item's whole body: held from its page, or fetched once and checked."""
        held = self._bodies.get(entry.id)
        if held is not None:
            return held
        if entry.id in self._cache:
            self._cache.move_to_end(entry.id)
            return self._cache[entry.id]
        fetch = self._fetches[entry.id]
        where = entry.location
        span: dict[str, JsonValue] = {"size": fetch.size}
        try:
            data = self.system.download(fetch)
        except ShortRead as exc:
            self.report("short_read", where, span)
            raise ObjectReadError("short_read", where) from exc
        except SizeMismatch as exc:
            self.report("object_changed", where, span)
            raise ObjectReadError("object_changed", where) from exc
        except RateLimited as exc:
            self.report("rate_limited", where, _retry(exc))
            raise ObjectReadError("rate_limited", where) from exc
        except AccessDenied as exc:
            self.report("access_denied", where, {"status": exc.status or 0})
            raise ObjectReadError("access_denied", where) from exc
        except RedirectRefused as exc:
            self.report("redirect_refused", where, {"status": exc.status or 0})
            raise ObjectReadError("redirect_refused", where) from exc
        except HttpStatusError as exc:
            code = "object_gone" if exc.status in (404, 410) else "read_failed"
            self.report(code, where, {"status": exc.status or 0})
            raise ObjectReadError(code, where) from exc
        except TransportError as exc:
            self.report("read_failed", where, {"cause": exc.code})
            raise ObjectReadError("read_failed", where) from exc
        except _MALFORMED as exc:  # a system let a malformed answer through: still this item's
            self.report("read_failed", where, {"cause": "response_invalid"})
            raise ObjectReadError("read_failed", where) from exc
        for algorithm, stated in (
            ("md5", fetch.md5),
            ("sha1", fetch.sha1),
            ("sha256", fetch.sha256),
        ):
            if stated is not None and (
                hashlib.new(algorithm, data, usedforsecurity=False).hexdigest() != stated
            ):
                self.report("object_changed", where, {**span, "checksum": algorithm})
                raise ObjectReadError("object_changed", where)
        self._cache[entry.id] = data
        while len(self._cache) > CACHED_BODIES:
            self._cache.popitem(last=False)
        return data


def _retry(exc: RateLimited) -> dict[str, JsonValue]:
    details: dict[str, JsonValue] = {"status": exc.status or 429}
    if exc.retry_after is not None:
        details["retry_after"] = exc.retry_after
    return details


class RecordReader:
    """An adapter's ``SourceReader`` over one item: bytes checked against the artifact first."""

    def __init__(self, source: RecordSource, entry: RecordEntry, artifact: SourceArtifact) -> None:
        if artifact.size != entry.size:
            raise ValueError(f"the artifact has {artifact.size} bytes, the item {entry.size}")
        self._source = source
        self._entry = entry
        self._artifact = artifact
        self._data: bytes | None = None

    @property
    def content_id(self) -> ContentId:
        return self._artifact.content_id

    @property
    def size(self) -> int:
        return self._artifact.size

    def _bytes(self) -> bytes:
        if self._data is None:
            data = self._source.read_all(self._entry)
            if content_id(data) != self._artifact.content_id:
                where = self._entry.location
                self._source.report("object_changed", where, {"size": len(data)})
                raise ObjectReadError("object_changed", where)
            self._data = data
        return self._data

    def read(self, offset: int, length: int) -> bytes:
        for name, value in (("offset", offset), ("length", length)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        if offset > self.size:
            raise ValueError(f"offset {offset} is past the end of {self.size} bytes")
        return self._bytes()[offset : offset + length]


class _Collect:
    """One pass over a system's feed: the bounded, deduplicated, sorted result and its findings.

    Everything held is bounded: records and rejected ids by ``max_records``, the id, token and name
    bytes by ``max_listing_bytes`` (a rejected id counts as at most 256 bytes), snapshot bodies by
    ``max_snapshot_bytes``, and cursors are kept as digests.
    """

    def __init__(self, source: RecordSource) -> None:
        self.source = source
        self.options = source.options
        self.kept: dict[str, Item] = {}
        self.duplicated: set[str] = set()
        self.skipped: dict[tuple[str, str], SkippedRecord] = {}
        self.removed: dict[str, None] = {}
        self.children: dict[str, list[str]] = defaultdict(list)  # parent id -> kept child ids
        self.body_bytes = 0
        self.held = 0  # id, token and name bytes the listing holds

    def run(self) -> Listing:
        source = self.source
        options = self.options
        mode = "snapshot" if options.since is None else "incremental"
        cursors: set[bytes] = set()  # digests of the cursors seen, never the cursors themselves
        previous: frozenset[str] = frozenset()
        resume: str | None = None
        complete = partial = False
        pages = 0
        feed = source.system.pages(options.since)
        while True:
            if pages >= MAX_PAGES:
                self._limit({"pages": pages})
                break
            try:
                page = next(feed)
            except StopIteration:
                complete = True
                break
            except TransportError as exc:
                self._failed(exc, pages)
                break
            except _MALFORMED:  # a parser met a shape it did not expect: the page is hostile
                source.report(
                    "response_invalid", source.listing_ref, {"page": pages, "cause": "malformed"}
                )
                break
            pages += 1
            if page.cursor is not None and len(page.cursor.encode("utf-8", "replace")) > MAX_CURSOR:
                source.report("response_invalid", source.listing_ref, {"page": pages})
                break
            # An ordered feed may state one item twice in a row (a page of one): that is a loop only
            # if it states the same revision.
            ids = frozenset(
                [
                    item.id + ("\0" + item.token if item.later_wins else "")
                    for item in page.updates()
                ]
                + [r.id for r in page.rejected]
            )
            if ids and ids == previous:
                source.report("pagination_loop", source.listing_ref, {"page": pages})
                break
            previous = ids or previous
            if not self._take(page):
                break  # a limit
            resume = page.resume
            partial = partial or page.partial
            if page.cursor is not None:
                digest = hashlib.sha256(page.cursor.encode("utf-8", "replace")).digest()
                if digest in cursors:
                    source.report("pagination_loop", source.listing_ref, {"page": pages})
                    break
                cursors.add(digest)
        feed.close()
        if partial:
            source.report("listing_partial", source.listing_ref, {"pages": pages})
            complete = False
            resume = None  # pages were missed: a cursor taken from the end would skip them
        return self._finish(mode, complete, resume)

    def _limit(self, details: dict[str, JsonValue]) -> None:
        self.source.report("listing_limit", self.source.listing_ref, details)

    def _failed(self, exc: TransportError, pages: int) -> None:
        source = self.source
        details: dict[str, JsonValue] = {"page": pages}
        if exc.status is not None:
            details["status"] = exc.status
        if isinstance(exc, RateLimited):
            details.update(_retry(exc))
            source.report("rate_limited", source.listing_ref, details)
        elif isinstance(exc, AccessDenied):
            source.report("access_denied", source.listing_ref, details)
        elif isinstance(exc, RedirectRefused):
            source.report("redirect_refused", source.listing_ref, details)
        elif isinstance(exc, PaginationLoop):
            source.report("pagination_loop", source.listing_ref, details)
        elif isinstance(exc, ResponseInvalid):
            source.report("response_invalid", source.listing_ref, details)
        else:
            source.report("listing_failed", source.listing_ref, {**details, "cause": exc.code})

    def _reject(self, reason: str, raw_id: str) -> None:
        skip = SkippedRecord(raw_id, reason)
        if (reason, skip.sha256) not in self.skipped:
            self.skipped[(reason, skip.sha256)] = skip
            self.held += len(skip.raw_id.encode("utf-8", "replace"))

    def _count(self) -> int:
        return len(self.kept) + len(self.skipped) + len(self.removed)

    def _full(self) -> bool:
        """Whether the record limit is reached (and, if so, say it)."""
        if self._count() < self.options.max_records:
            return False
        self._limit({"max_records": self.options.max_records})
        return True

    def _take(self, page: Page) -> bool:
        """Fold one page in. ``False`` once a limit is reached (the page's remainder is dropped)."""
        for rejected in page.rejected:
            if self._full():
                return False
            self._reject(rejected.reason, rejected.id)
        for event in page.stream():  # in the order the system stated them: the last one wins
            if self._full():
                return False
            if isinstance(event, str):
                self._remove(event)
            elif not self._update(event):
                return False
        return True

    def _remove(self, removed: str) -> None:
        if removed not in self.removed:
            self.removed[removed] = None
            self.held += len(removed.encode("utf-8", "replace"))
        self.kept.pop(removed, None)
        self.duplicated.discard(removed)  # the system says it is gone: no longer ambiguous
        for child in self.children.pop(removed, ()):  # what hung under it goes too
            self.kept.pop(child, None)
            self.duplicated.discard(child)

    def _update(self, item: Item) -> bool:
        """Fold one updated item in. ``False`` once a limit is reached."""
        problem = self._problem(item)
        if problem is not None:
            self._reject(problem, item.id)
            return True
        self.removed.pop(item.id, None)  # stated again after a deletion: it is live
        held = self.kept.get(item.id)
        if held is not None and held != item and item.later_wins and held.later_wins:
            # an ordered feed's later statement replaces the earlier
            size = len(item.body) if item.body is not None else 0
            before = len(held.body) if held.body is not None else 0
            if self.body_bytes - before + size > self.options.max_snapshot_bytes:
                self._limit({"max_snapshot_bytes": self.options.max_snapshot_bytes})
                return False
            self.body_bytes += size - before
            self.kept[item.id] = item
            self.held += sum(len(t.encode("utf-8")) for t in (item.token, item.name))
            if self.held > self.options.max_listing_bytes:
                self._limit({"max_listing_bytes": self.options.max_listing_bytes})
                return False
        elif held is not None and held != item:
            self.duplicated.add(item.id)
        elif held is None:
            size = len(item.body) if item.body is not None else 0
            if self.body_bytes + size > self.options.max_snapshot_bytes:
                self._limit({"max_snapshot_bytes": self.options.max_snapshot_bytes})
                return False
            self.held += sum(len(t.encode("utf-8")) for t in (item.id, item.token, item.name))
            if self.held > self.options.max_listing_bytes:
                self._limit({"max_listing_bytes": self.options.max_listing_bytes})
                return False
            self.body_bytes += size
            self.kept[item.id] = item
            if item.parent is not None:
                self.children[item.parent].append(item.id)
        return True

    def _problem(self, item: Item) -> str | None:
        """Why ``item`` cannot be used, as a finding code, or ``None``."""
        try:
            id_ok = 0 < len(item.id.encode("utf-8")) <= MAX_ID_BYTES
            token_ok = 0 < len(item.token) <= MAX_TOKEN_CHARS and item.token.isprintable()
            item.token.encode("utf-8")
            item.name.encode("utf-8")
        except UnicodeEncodeError:
            return "id_invalid"
        if not id_ok:
            return "id_invalid"
        if not token_ok:
            return "record_invalid"
        if (item.body is None) == (item.fetch is None):
            return "record_invalid"
        if item.size < 0:
            return "size_invalid"
        if item.body is not None:
            if len(item.body) != item.size:
                return "size_invalid"
            if item.size > MAX_SNAPSHOT_BYTES:
                return "record_too_large"
        elif item.size > self.options.max_attachment_bytes:
            return "record_too_large"
        return None

    def _finish(self, mode: str, complete: bool, resume: str | None) -> Listing:
        source = self.source
        for key in sorted(self.duplicated):
            self.kept.pop(key, None)
            self._reject("record_duplicated", key)
        refs = {key: source.ref(key, item.token) for key, item in self.kept.items()}
        entries = tuple(
            RecordEntry(
                refs[key],
                item.size,
                key,
                item.name,
                refs.get(item.parent) if item.parent is not None else None,
                item.locator,
            )
            for key, item in sorted(self.kept.items())
        )
        for key, item in self.kept.items():
            if item.body is not None:
                source._bodies[key] = item.body
            if item.fetch is not None:
                source._fetches[key] = item.fetch
            if item.children is not None:
                source._children[key] = item.children
        skipped = tuple(sorted(self.skipped.values(), key=lambda skip: skip.order))
        self._report_skipped(skipped)
        return Listing(entries, skipped, tuple(sorted(self.removed)), complete, mode, resume)

    def _report_skipped(self, skipped: tuple[SkippedRecord, ...]) -> None:
        """One finding per reason, citing the first ids (as hex) and counting them all."""
        by_reason: defaultdict[str, list[SkippedRecord]] = defaultdict(list)
        for item in skipped:
            by_reason[item.reason].append(item)
        for reason in _SKIP_REASONS:
            found = by_reason.get(reason)
            if found:
                examples: list[JsonValue] = [_hex(item.raw_id) for item in found[:MAX_EXAMPLES]]
                self.source.report(
                    reason, self.source.listing_ref, {"count": len(found), "ids_hex": examples}
                )
