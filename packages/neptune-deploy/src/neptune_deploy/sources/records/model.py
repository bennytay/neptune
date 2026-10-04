"""What a record system's pages say, and what the source makes of them (ADR 0008).

A *system* (Jira, ServiceNow, Google Drive, Confluence, a declared REST CMMS) turns its API's pages
into ``Page`` objects holding ``Item`` s: one record, one document or one attachment each, with the
identity the compiler needs (an id under the scope, a revision token) and either its snapshot bytes
or how to fetch them (``Fetch``). Everything else (which items are kept, order, coverage, findings,
discovery, reads) is the source's, so the systems cannot differ in policy.
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final

from neptune.model.ids import ExternalObjectRef

ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:@=+\-]{0,255}")
MAX_NAME_BYTES: Final = 255
MAX_SKIPPED_ID_BYTES: Final = 256  # of an id the source does not use


@dataclass(frozen=True)
class Fetch:
    """How to download an item's bytes: a path built from validated ids, never a system's URL.

    ``size`` is the length the listing stated; a body of another length is refused. ``md5``,
    ``sha1`` and ``sha256`` (lower hex), if the system states them, must equal the body's.
    ``redirect`` says the system answers with a redirect to a pre-authenticated download URL on
    another host, which is followed only under ``http.pre_authenticated``'s rules (ADR 0008 §6).
    """

    path: str
    query: tuple[tuple[str, str], ...]
    size: int
    md5: str | None = None
    sha1: str | None = None
    sha256: str | None = None
    redirect: bool = False


@dataclass(frozen=True)
class Item:
    """One thing to hand the compiler.

    ``id`` is the part of the object id after the scope: ``issue/10042``,
    ``issue/10042/attachment/9`` (the parent's id is its prefix). ``token`` is the revision token,
    ``<kind>:<value>`` with the value as the system wrote it. ``name`` is advisory. ``children``,
    on a parent whose attachments the system listed in full, is the id prefix of those attachments:
    a revision under it that the listing no longer holds is gone. ``later_wins`` is for a feed the
    system documents as ordered, which may state one item more than once: the last statement is
    the item, where for any other feed two different statements of one id are ambiguous.
    """

    id: str
    token: str
    name: str
    size: int
    body: bytes | None = None
    fetch: Fetch | None = None
    parent: str | None = None
    children: str | None = None
    later_wins: bool = False
    locator: str | None = None


@dataclass(frozen=True)
class Rejected:
    """An item the system listed and the source cannot use: its id as listed, and why."""

    id: str
    reason: str


@dataclass(frozen=True)
class Page:
    """One page of a feed. ``cursor`` names the next page (``None`` on the last): a cursor seen
    twice is a loop. ``resume`` is where a later run may continue if this page is the last one
    processed (``None`` when the feed has no such point); ``partial`` says the system itself
    declared this page incomplete. ``removed`` are ids the system says no longer exist.

    ``events`` is for a feed that states updates and deletions in one ordered stream (Drive
    changes, Graph delta): an ``Item`` is an update and a ``str`` is the id of a deletion, in the
    order the system stated them, and the last event for an id wins. A page that has ``events``
    has no ``items`` or ``removed``; one that does not has all its removals before its items."""

    items: tuple[Item, ...] = ()
    cursor: str | None = None
    resume: str | None = None
    removed: tuple[str, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    partial: bool = False
    events: tuple[Item | str, ...] = ()

    def stream(self) -> tuple[Item | str, ...]:
        """Every update and deletion of the page, in the order they apply."""
        return self.events or (*self.removed, *self.items)

    def updates(self) -> tuple[Item, ...]:
        return tuple(event for event in self.stream() if isinstance(event, Item))


@dataclass(frozen=True)
class RecordEntry:
    """One item to read: where it is (with its revision), its size, id and name.

    Shaped like the compiler's ``SourceEntry`` (``location``, ``size``).
    """

    location: ExternalObjectRef
    size: int
    id: str
    name: str
    parent: ExternalObjectRef | None = None
    locator: str | None = None


def sha256_text(text: str) -> str:
    """The sha256 of ``text`` as UTF-8 (lone surrogates kept), for ids too long to hold."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class SkippedRecord:
    """A listed entry the source does not use, and the finding code saying why.

    Built from the id as listed, it keeps only the first ``MAX_SKIPPED_ID_BYTES`` of it, with the
    whole id's length and sha256: a system that lists huge unusable ids cannot make the source hold
    them. ``raw_id`` is therefore a prefix when ``length`` exceeds it.
    """

    raw_id: str
    reason: str
    length: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        data = self.raw_id.encode("utf-8", "surrogatepass")
        object.__setattr__(self, "length", len(data))
        object.__setattr__(self, "sha256", hashlib.sha256(data).hexdigest())
        object.__setattr__(self, "raw_id", data[:MAX_SKIPPED_ID_BYTES].decode("utf-8", "replace"))

    @property
    def order(self) -> tuple[str, str, int, str]:
        return (self.reason, self.raw_id, self.length, self.sha256)


@dataclass(frozen=True)
class Relation:
    """A declared parent link: ``child`` is an attachment of ``parent``, as the system lists it.

    A relation is the system's statement (``assertion_kind`` is always ``stated``), never an
    inference. ``locator`` is the JSON pointer, within the record the system stated the link in,
    to where it did: the parent's ``/fields/attachment/<n>`` (Jira), its declared attachments
    pointer (REST), or the attachment's own ``/table_sys_id`` (ServiceNow). The compiler has no
    place for relations between sources yet (ADR 0008 compiler gaps), so this is the source's own
    output, and the parent's object id is also the prefix of the child's.
    """

    child: ExternalObjectRef
    parent: ExternalObjectRef
    kind: str = "attachment_of"
    assertion_kind: str = "stated"  # never inferred here: the system lists the link
    locator: str | None = None  # a JSON pointer within the record that states the link


@dataclass(frozen=True)
class Listing:
    """Every usable item, sorted by id, and what was not used.

    ``mode`` is ``snapshot`` (every record the scope holds) or ``incremental`` (what changed since a
    cursor). ``complete`` says every page was read: only a complete snapshot asserts absence.
    ``cursor`` is where the next run may continue: the feed's end if complete, else where this run
    stopped (``None`` if the feed cannot say). ``removed`` are ids the system said no longer exist.
    """

    entries: tuple[RecordEntry, ...]
    skipped: tuple[SkippedRecord, ...]
    removed: tuple[str, ...]
    complete: bool
    mode: str
    cursor: str | None


def _fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= MAX_NAME_BYTES


def safe_name(raw: object, fallback: str) -> str:
    """An advisory file name from a system's text: its last path component, control characters
    removed, at most 255 bytes. Never used as a path, only as a hint (``ProbeHints.name``)."""
    text = raw if isinstance(raw, str) else ""
    text = text.replace("\\", "/").rpartition("/")[2]
    text = "".join(c for c in text if c.isprintable()).strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return fallback
    if text in ("", ".", ".."):
        return fallback
    stem, dot, suffix = text.rpartition(".")
    if not dot or not stem or len(suffix) > 16:
        stem, suffix = text, ""
    else:
        suffix = dot + suffix
    if not _fits(stem + suffix):
        room = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
        stem = stem.encode("utf-8")[: max(room, 0)].decode("utf-8", "ignore")
    return (stem + suffix) or fallback
