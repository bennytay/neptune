"""Evidence references resolved to bytes: where cited bytes are, read lazily and verified.

The catalog's ``resolve`` (ADR 0004, ADR 0006 §5) says which registered packages hold a source and
the routes each states: a materialised blob in the package, or the locations a referenced source
was seen at. It never fetches. ``EvidenceResolver`` turns that answer into bytes (ADR 0014 §2-§4):

- it finds the first route, in registration order, whose object exists with the source's stated
  size: a materialised blob through the package's ``ObjectStore``, or a referenced location's
  root-relative path in one of the deployment's ``SourceStore``s, never following a link and
  never leaving a store (``..``, empty parts and NUL are refused);
- it splits the locator into the **span** of stored bytes that step 0 addresses (a ``byte_range``,
  else the whole source) and the **inner** steps a decoder addresses inside them;
- its ``SourceReader`` reads any range of the span lazily, verifying every chunk it touches
  against the chunk hashes the package's ``source_artifact`` states, so no byte that differs from
  the cited content is ever returned, and nothing larger than a chunk is read to serve a slice.

Every problem is a ``MediaFinding``, never an exception: a source that moved or was removed, one
that changed, a locator outside the source, a hostile path.
"""

import hashlib
import io
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, TypeAlias

import psycopg
from psycopg import sql

from neptune.identity import canonical_json
from neptune.model.provenance import ByteRange, Locator, locator_from_json
from neptune.store.package import blob_path
from neptune_ledger.api.types import CatalogFinding, EvidenceAnchor, Resolution, SourceLocation
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.catalog.sources import SourceStore, location_path
from neptune_ledger.lake.store import ObjectStore, local_store

Conn = psycopg.Connection[tuple[Any, ...]]
Locate = Callable[[str, str], ObjectStore]
SourceRoots = Callable[[str, str], Sequence[SourceStore]]

MediaFindingCode: TypeAlias = Literal[
    "as_of_out_of_range",
    "file_digest_mismatch",
    "file_missing",
    "invalid_request",
    "no_decoder",
    "undecodable",
    "unknown_artefact",
    "unresolvable_evidence",
    "unsafe_entry",
]
_CONTENT_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
_ARTIFACT: Final = """
SELECT p.package_id, p.root_locator, r.body
  FROM package p
  LEFT JOIN record r ON r.tenant_id = p.tenant_id AND r.package_id = p.package_id
       AND r.kind = 'source_artifact' AND r.record_id = %(content)s
 WHERE p.tenant_id = %(tenant)s AND p.package_id = ANY(%(packages)s)
"""


@dataclass(frozen=True)
class MediaFinding:
    """Why evidence could not be resolved, read or hydrated (ADR 0014 §6).

    Codes shared with the catalog API mean what they mean there; ``no_decoder`` (this Ledger has
    no decoder for a step or encoding), ``undecodable`` (the bytes are not the format a step
    needs) and ``unknown_artefact`` (no artefact at a pinned media snapshot) are the media store's.
    """

    code: MediaFindingCode
    subject: str
    detail: str

    @staticmethod
    def of(finding: CatalogFinding) -> "MediaFinding":
        return MediaFinding(finding.code, finding.subject, finding.detail)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ByteSpan:
    """``length`` bytes from ``offset`` of the source's stored bytes."""

    offset: int
    length: int


@dataclass(frozen=True)
class SourceRoute:
    """The route the bytes are read through: a package's blob, or a stated location in a store.

    ``where`` names the store and key or path (never credentials). ``location`` is the stated
    location for a referenced source, as the package states it, and None for a materialised one.
    """

    package_id: str
    storage: Literal["materialised", "referenced"]
    where: str
    location: dict[str, Any] | None


class _Source:
    """One object holding the whole source: its size, and ranges of it."""

    def describe(self) -> str:
        raise NotImplementedError

    def size(self, stated: int) -> int | None:
        """The object's size; at most ``stated + 1``, which means larger than stated."""
        raise NotImplementedError

    def read(self, offset: int, length: int) -> bytes | None:
        raise NotImplementedError


class _Blob(_Source):
    def __init__(self, store: ObjectStore, key: str) -> None:
        self._store, self._key = store, key

    def describe(self) -> str:
        return f"{self._store.describe()}/{self._key}"

    def size(self, stated: int) -> int | None:
        return self._store.size(self._key)

    def read(self, offset: int, length: int) -> bytes | None:
        return self._store.read_range(self._key, offset, length)


class _Located(_Source):
    """A referenced source at a root-relative path in a ``SourceStore``."""

    def __init__(self, store: SourceStore, path: bytes) -> None:
        self._store, self._path = store, path

    def describe(self) -> str:
        return f"{self._store.describe()}/{self._path.decode('utf-8', 'backslashreplace')}"

    def size(self, stated: int) -> int | None:
        stream = self._store.open(self._path)
        if stream is None:
            return None
        with stream:
            try:
                if stream.seekable():
                    return int(stream.seek(0, io.SEEK_END))
                size = 0  # stop one byte past the stated size: a larger object is not the source
                while size <= stated and (block := stream.read(min(1 << 20, stated + 1 - size))):
                    size += len(block)
                return size
            except OSError:
                return None

    def read(self, offset: int, length: int) -> bytes | None:
        stream = self._store.open(self._path)
        if stream is None:
            return None
        with stream:
            try:
                if stream.seekable():
                    stream.seek(offset)
                else:
                    skipped = 0
                    while skipped < offset and (
                        block := stream.read(min(1 << 20, offset - skipped))
                    ):
                        skipped += len(block)
                data = stream.read(length)
            except OSError:
                return None
        return data if len(data) == length else None


class SourceChanged(Exception):
    """A chunk read for a slice no longer hashes to the source's stated chunk (internal)."""

    def __init__(self, finding: MediaFinding) -> None:
        super().__init__(finding.detail)
        self.finding = finding


class SourceReader:
    """Lazy, verified reads of one span of a source (ADR 0014 §3).

    Offsets are relative to the span. Every read touches only the chunks it overlaps, hashes each
    against the stated chunk id, and keeps the last chunk read, so sequential reads hash each
    chunk once. ``read_range`` returns a finding for a slice outside the span or a changed chunk.
    """

    def __init__(
        self,
        source: _Source,
        content_id: str,
        span: ByteSpan,
        chunk_size: int,
        chunks: Sequence[str],
        size: int,
    ) -> None:
        self._source = source
        self._content = content_id
        self.span = span
        self._chunk_size = chunk_size
        self._chunks = tuple(chunks)
        self._size = size
        self._cached: tuple[int, bytes] | None = None

    @property
    def size(self) -> int:
        return self.span.length

    def read_range(self, offset: int, length: int) -> bytes | MediaFinding:
        """``length`` bytes from ``offset`` within the span, verified, or a finding."""
        if not _is_count(offset) or not _is_count(length) or offset + length > self.span.length:
            detail = f"[{offset!r}, +{length!r}) is outside the span's {self.span.length} bytes"
            return MediaFinding("invalid_request", self._content, detail)
        try:
            return self._read(self.span.offset + offset, length)
        except SourceChanged as changed:
            return changed.finding

    def read_all(self) -> bytes | MediaFinding:
        return self.read_range(0, self.span.length)

    def file(self) -> io.BufferedReader:
        """The span as a seekable binary file; a changed chunk raises ``SourceChanged``."""
        return io.BufferedReader(_SpanFile(self), buffer_size=min(self._chunk_size, 1 << 20))

    def _read(self, at: int, length: int) -> bytes:
        out = bytearray()
        end = at + length
        while at < end:
            index = at // self._chunk_size
            chunk = self._chunk(index)
            start = at - index * self._chunk_size
            piece = chunk[start : start + (end - at)]
            out += piece
            at += len(piece)
        return bytes(out)

    def _chunk(self, index: int) -> bytes:
        if self._cached is not None and self._cached[0] == index:
            return self._cached[1]
        at = index * self._chunk_size
        length = min(self._chunk_size, self._size - at)
        data = self._source.read(at, length)
        if data is None:
            detail = f"{self._source.describe()} can no longer be read at [{at}, +{length})"
            raise SourceChanged(MediaFinding("file_missing", self._content, detail))
        if (
            index >= len(self._chunks)
            or "sha256:" + hashlib.sha256(data).hexdigest() != (self._chunks[index])
        ):
            detail = f"chunk {index} of {self._source.describe()} is not the stated bytes"
            raise SourceChanged(MediaFinding("file_digest_mismatch", self._content, detail))
        self._cached = (index, data)
        return data


class _SpanFile(io.RawIOBase):
    def __init__(self, reader: SourceReader) -> None:
        self._reader = reader
        self._at = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._at

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._at, io.SEEK_END: self._reader.size}[whence]
        if base + offset < 0:
            raise ValueError("negative seek position")
        self._at = base + offset
        return self._at

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        length = max(0, min(len(view), self._reader.size - self._at))
        if not length:
            return 0
        data = self._reader._read(self._reader.span.offset + self._at, length)
        view[:length] = data
        self._at += length
        return length


@dataclass(frozen=True)
class EvidenceBytes:
    """An evidence reference resolved to bytes (ADR 0014 §2).

    ``status``: ``resolved`` (``route`` reaches the bytes and ``open`` reads them),
    ``unavailable`` (registered, but no route reaches intact bytes: moved, removed or changed),
    ``unresolvable`` (no registered package holds the source) or ``invalid`` (the reference or
    ``as_of`` is outside the contract). ``span`` is the stored bytes step 0 addresses and
    ``inner`` the steps a decoder addresses inside them; both are set whenever the locator parses
    and the source's size is known. ``resolution`` is the catalog's own answer.
    """

    evidence_ref: EvidenceAnchor
    status: Literal["invalid", "resolved", "unavailable", "unresolvable"]
    resolution: Resolution
    span: ByteSpan | None
    inner: tuple[Locator, ...]
    route: SourceRoute | None
    findings: tuple[MediaFinding, ...]
    _reader: Callable[[], SourceReader] | None = field(default=None, compare=False, repr=False)

    def open(self) -> SourceReader:
        """A lazy reader of ``span``; nothing is read until it is asked for bytes."""
        if self._reader is None:
            raise ValueError(f"evidence is {self.status}, not resolved: {self.findings}")
        return self._reader()


def parse_locator(anchor: EvidenceAnchor) -> tuple[Locator, ...] | MediaFinding:
    """The anchor's steps parsed strictly with the compiler's locator reader."""
    steps = anchor.locator if isinstance(anchor.locator, tuple) else ()
    if not steps or not isinstance(anchor.source, str) or not _CONTENT_ID.fullmatch(anchor.source):
        detail = "an evidence anchor is a content id and a non-empty locator of JSON objects"
        return MediaFinding("invalid_request", str(anchor.source)[:200], detail)
    try:
        return tuple(locator_from_json(step) for step in steps)
    except (TypeError, ValueError) as exc:
        detail = f"not a locator step: {str(exc).splitlines()[0][:300]}"
        return MediaFinding("invalid_request", anchor.source, detail)


def split_span(steps: tuple[Locator, ...], size: int) -> tuple[ByteSpan, tuple[Locator, ...]]:
    """Step 0's stored bytes and the steps inside them: a leading ``byte_range`` is the span,
    anything else addresses the whole source (root ADR 0016 §1)."""
    first = steps[0]
    if isinstance(first, ByteRange):
        return ByteSpan(first.offset, first.length), steps[1:]
    return ByteSpan(0, size), steps


class EvidenceResolver:
    """Evidence references of one tenant resolved to bytes (ADR 0014 §2).

    ``locate(package_id, root_locator)`` gives the store a package's objects (its blobs) are read
    from, as for series (ADR 0013 §2). ``source_roots(package_id, root_locator)`` gives the stores
    a referenced source's root-relative locations are looked up in, in order: the ingest roots
    the deployment keeps (ADR 0007). By default there are none, so a referenced source is
    ``unavailable`` until a deployment names where its ingest roots are.
    """

    def __init__(
        self,
        conninfo: str,
        tenant_id: str,
        *,
        locate: Locate = local_store,
        source_roots: SourceRoots = lambda package_id, root: (),
    ) -> None:
        self._conninfo = conninfo
        self._tenant = tenant_id
        self._schema = tenant_schema(tenant_id)
        self._locate = locate
        self._source_roots = source_roots
        self._catalog = PostgresCatalog(conninfo, tenant_id, package_roots=None)
        self._conn: Conn | None = None

    def __enter__(self) -> "EvidenceResolver":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._catalog.close()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> Conn:
        if self._conn is None or self._conn.closed:
            conn: Conn = psycopg.connect(self._conninfo, autocommit=True)
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self._schema)))
            self._conn = conn
        return self._conn

    def resolve(self, anchor: EvidenceAnchor, *, as_of: int | None = None) -> EvidenceBytes:
        """Where the anchor's bytes are, at one catalog point; a finding when nowhere."""
        resolution = self._catalog.resolve(anchor, as_of=as_of)
        if resolution.status != "resolved":
            findings = tuple(MediaFinding.of(f) for f in resolution.findings)
            unresolvable = any(f.code == "unresolvable_evidence" for f in findings)
            status: Literal["invalid", "unresolvable"] = (
                "unresolvable" if unresolvable else "invalid"
            )
            return EvidenceBytes(anchor, status, resolution, None, (), None, findings)
        steps = parse_locator(anchor)
        if isinstance(steps, MediaFinding):
            return EvidenceBytes(anchor, "invalid", resolution, None, (), None, (steps,))
        size = resolution.size.value  # type: ignore[union-attr]
        span, inner = split_span(steps, size)
        if span.offset + span.length > size:
            detail = (
                f"byte range [{span.offset}, +{span.length}) lies outside the {size}-byte source"
            )
            finding = MediaFinding("invalid_request", anchor.source, detail)
            return EvidenceBytes(anchor, "invalid", resolution, span, inner, None, (finding,))
        return self._route(anchor, resolution, size, span, inner)

    def _route(
        self,
        anchor: EvidenceAnchor,
        resolution: Resolution,
        size: int,
        span: ByteSpan,
        inner: tuple[Locator, ...],
    ) -> EvidenceBytes:
        packages = self._packages(anchor.source, [route.package_id for route in resolution.fetch])
        findings: list[MediaFinding] = []
        tried: list[str] = []
        wrong: list[str] = []
        for route in resolution.fetch:
            root, body = packages.get(route.package_id, (None, None))
            chunks = _chunks(body, size)
            if root is None or chunks is None:
                continue
            for source, made in self._candidates(anchor.source, root, route, findings):
                tried.append(source.describe())
                found = source.size(size)
                if found is None:
                    continue
                if found != size:
                    wrong.append(f"{source.describe()} holds {found} bytes")
                    continue
                chunk_size, hashes = chunks

                def reader(
                    source: _Source = source, chunk_size: int = chunk_size, hashes: Any = hashes
                ) -> SourceReader:
                    return SourceReader(source, anchor.source, span, chunk_size, hashes, size)

                return EvidenceBytes(
                    anchor, "resolved", resolution, span, inner, made, tuple(findings), reader
                )
        if wrong:
            detail = f"the stated {size} bytes are not where stated: {'; '.join(wrong[:5])}"
            findings.append(MediaFinding("file_digest_mismatch", anchor.source, detail))
        else:
            where = "; ".join(tried[:5]) if tried else "no store reaches any stated location"
            detail = f"the source is at none of {len(tried)} route(s): {where}"
            findings.append(MediaFinding("file_missing", anchor.source, detail))
        return EvidenceBytes(anchor, "unavailable", resolution, span, inner, None, tuple(findings))

    def _candidates(
        self, content_id: str, root: str, route: SourceLocation, findings: list[MediaFinding]
    ) -> list[tuple[_Source, SourceRoute]]:
        """Every object that may hold the bytes on one package's route, in order."""
        package_id = route.package_id
        if route.storage == "materialised":
            blob = _Blob(self._locate(package_id, root), blob_path(content_id))  # type: ignore[arg-type]
            return [(blob, SourceRoute(package_id, "materialised", blob.describe(), None))]
        out: list[tuple[_Source, SourceRoute]] = []
        stores = self._source_roots(package_id, root)
        for location in route.locations:
            text = canonical_json.dumps(location).decode("utf-8")
            path = location_path(text)
            if path is None:
                kind = location.get("kind") if isinstance(location, dict) else None
                if kind in ("local", "local_raw"):
                    detail = f"location {text[:200]} would leave its store; it is never opened"
                    findings.append(MediaFinding("unsafe_entry", package_id, detail))
                continue  # another kind of location is reached by its connector, not the Ledger
            for store in stores:
                source = _Located(store, path)
                made = SourceRoute(package_id, "referenced", source.describe(), dict(location))
                out.append((source, made))
        return out

    def _packages(
        self, content_id: str, packages: list[str]
    ) -> dict[str, tuple[str, dict[str, Any] | None]]:
        params = {"tenant": self._tenant, "content": content_id, "packages": packages}
        with self._connection().transaction():
            rows = self._connection().execute(_ARTIFACT, params).fetchall()
        return {str(p): (str(root), body) for p, root, body in rows}


def _chunks(body: Any, size: int) -> tuple[int, tuple[str, ...]] | None:
    """The chunk size and chunk ids a package's ``source_artifact`` states, if they fit ``size``."""
    if not isinstance(body, dict):
        return None
    chunk_size, chunks = body.get("chunk_size"), body.get("chunks")
    if not _is_count(chunk_size) or not chunk_size or not isinstance(chunks, list):
        return None
    if len(chunks) != -(-size // chunk_size) or not all(
        isinstance(c, str) and _CONTENT_ID.fullmatch(c) for c in chunks
    ):
        return None
    return chunk_size, tuple(chunks)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
