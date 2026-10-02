"""One client per store, behind one interface: list a page, read a byte range (ADR 0006 §2, §6).

A client speaks one store's wire format and nothing else: it lists one page of objects under a
prefix and reads one byte range of one object revision. Policy (which keys are kept, ordering,
coverage, findings) is the source's, so the three stores cannot differ in it.

- ``S3Client``: ``ListObjectVersions`` (latest versions only; a latest delete marker is no object)
  or ``ListObjectsV2``, and ranged ``GetObject`` pinned by ``versionId`` or ``If-Match``. Signed
  with SigV4, or unsigned for an anonymous (public) bucket.
- ``GcsClient``: the JSON API's ``objects.list`` and a ranged ``alt=media`` download pinned by
  ``generation``. A bearer token.
- ``AzureBlobClient``: ``List Blobs`` and a ranged ``Get Blob`` pinned by ``versionid`` or
  ``If-Match``. A SAS token whose permissions are read and list only.

Keys are passed through exactly as listed: never Unicode-normalised, never stripped of ``//`` or
``..``. Listing XML with a document type declaration is refused before it is parsed, so no entity is
ever expanded.
"""

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from neptune_deploy.sources.object_store.sigv4 import AwsCredentials, quote, sign
from neptune_deploy.sources.object_store.transport import Response, Transport, TransportError

MAX_PAGE_BYTES: Final = 32 * 1024 * 1024
MAX_TOKEN: Final = 1024  # characters in a version id, etag or generation
MAX_CURSOR_BYTES: Final = 4096  # a continuation token or marker; a longer one stops the listing
AZURE_VERSION: Final = "2021-08-06"  # the x-ms-version every Azure request names
_DECLARATION: Final = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

Cursor = tuple[str, ...]


class Provider(StrEnum):
    S3 = "s3"
    GCS = "gcs"
    AZURE = "azure"


class PageInvalid(TransportError):
    """A listing body that is not the store's format, or that this client refuses to parse."""

    code = "response_invalid"


class RangeInvalid(TransportError):
    """A ranged read answered with other bytes than were asked for."""

    code = "range_invalid"


@dataclass(frozen=True)
class Listed:
    """One object as a listing states it: its key, revision token and size."""

    key: str
    token: str  # ``version:<id>``, ``etag:<etag>`` or ``generation:<n>``
    size: int


@dataclass(frozen=True)
class Unlisted:
    """An entry in a listing that cannot become an object: its key bytes, as listed, and why."""

    raw: bytes
    reason: str  # key_not_utf8, revision_invalid, size_invalid


@dataclass(frozen=True)
class Page:
    objects: tuple[Listed, ...]
    unlisted: tuple[Unlisted, ...]
    cursor: Cursor | None  # where the next page starts; ``None`` on the last page


@dataclass(frozen=True)
class Range:
    """``data`` read from ``start``; ``total`` is the object's size as the response states it."""

    data: bytes
    total: int | None


class StoreClient(Protocol):
    """What the source needs of a store."""

    provider: Provider
    transport: Transport

    def list_page(self, prefix: str, cursor: Cursor | None, page_size: int) -> Page:
        """One page of the objects whose keys start with ``prefix``, from ``cursor``."""
        ...

    def get_range(self, key: str, token: str, start: int, length: int) -> Range:
        """``length`` bytes of revision ``token`` of ``key`` from ``start``; raises otherwise."""
        ...


# --- Shared parsing ------------------------------------------------------------------------------


def _xml(body: bytes) -> ET.Element:
    """A listing page, decoded as UTF-8 here and refused if it declares a type or an entity.

    Decoding first means the search sees exactly the text the parser parses: a page in another
    encoding (UTF-16 would hide ``<!ENTITY`` from a byte search) is refused, and expat, given
    text, ignores whatever encoding the document declares.
    """
    try:
        text = body.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError as exc:
        raise PageInvalid("a listing is not UTF-8") from exc
    if _DECLARATION.search(text):
        raise PageInvalid("a listing declares a document type or an entity")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise PageInvalid("a listing is not XML") from exc


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    return (child for child in element if _local(child.tag) == name)


def _text(element: ET.Element, name: str) -> str | None:
    for child in _children(element, name):
        return child.text or ""
    return None


def _token(kind: str, value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if kind == "etag":
        value = value.strip('"')
    if not value or len(value) > MAX_TOKEN or not value.isprintable():
        return None
    return f"{kind}:{value}"


def _size(value: object) -> int | None:
    """A non-negative decimal count of at most 19 ASCII digits, or ``None``.

    ``str.isdigit`` alone accepts ``²`` and other digits ``int`` refuses, and an unbounded string
    of digits costs quadratic time (or, past 4,300 digits, a ``ValueError``): a header or a
    listing field is never given to ``int`` before this check.
    """
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        return int(value)
    return None


def _entry(raw: bytes, token: str | None, size: int | None) -> Listed | Unlisted:
    try:
        key = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Unlisted(raw, "key_not_utf8")
    if token is None:
        return Unlisted(raw, "revision_invalid")
    if size is None:
        return Unlisted(raw, "size_invalid")
    return Listed(key, token, size)


def _page(found: list[Listed | Unlisted], cursor: Cursor | None) -> Page:
    if cursor is not None and any(
        len(part.encode("utf-8", "surrogateescape")) > MAX_CURSOR_BYTES for part in cursor
    ):
        raise PageInvalid(f"a continuation token or marker is longer than {MAX_CURSOR_BYTES} bytes")
    objects = tuple(item for item in found if isinstance(item, Listed))
    unlisted = tuple(item for item in found if isinstance(item, Unlisted))
    return Page(objects, unlisted, cursor)


def _url_decoded(text: str) -> bytes:
    """A key as S3's ``encoding-type=url`` and Azure's ``Encoded`` write it (``+`` is space)."""
    return urllib.parse.unquote_to_bytes(text.replace("+", " "))


def _content_range(response: Response, start: int, length: int) -> int | None:
    """Check a ``206``'s ``Content-Range`` covers exactly what was asked; return the total size."""
    value = response.headers.get("content-range", "")
    unit, _, rest = value.partition(" ")
    span, _, total = rest.partition("/")
    first, last = (_size(part) for part in span.partition("-")[::2])
    if unit != "bytes" or first is None or last is None:
        raise RangeInvalid("a partial response states no byte range")
    if first != start or last != start + length - 1:
        raise RangeInvalid("a partial response holds other bytes than were asked for")
    if total == "*":
        return None
    size = _size(total)
    if size is None:
        raise RangeInvalid("a partial response states no object size")
    return size


def read_range(response: Response, start: int, length: int) -> Range:
    """The bytes a ranged GET answered: a ``206`` for exactly the range, or a ``200`` from 0.

    A ``200`` means the store ignored the range and sent the object from its first byte: only the
    ``length`` asked for are read, and the connection is dropped before the rest arrives. It must
    state its length, which the source checks against the listed size: a body the store
    transformed on the way (GCS's decompressive transcoding sends no length) is refused.
    """
    if response.status == 206:
        try:
            total = _content_range(response, start, length)
        except RangeInvalid:
            response.discard()  # its body is unread: the connection cannot be reused
            raise
        return Range(response.exact(length), total)
    declared = _size(response.headers.get("content-length", ""))
    if response.status == 200 and start == 0 and declared is not None:
        return Range(response.exact(length), declared)
    response.discard()
    raise RangeInvalid(f"status {response.status} to a ranged read", response.status)


def _range_header(start: int, length: int) -> str:
    return f"bytes={start}-{start + length - 1}"


# --- S3 ------------------------------------------------------------------------------------------


class Addressing(StrEnum):
    VIRTUAL = "virtual"  # https://<bucket>.<endpoint>/<key>
    PATH = "path"  # https://<endpoint>/<bucket>/<key>


class S3Client:
    """S3 and S3-compatible stores (MinIO, Ceph RGW, R2, Wasabi, GCS's XML interoperability API)."""

    provider = Provider.S3

    def __init__(
        self,
        transport: Transport,
        bucket: str,
        *,
        addressing: Addressing,
        region: str,
        credentials: AwsCredentials | None,
        versions: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.transport = transport
        self.bucket = bucket
        self.addressing = addressing
        self.region = region
        self.credentials = credentials
        self.versions = versions
        self._clock = clock

    def _path(self, key: str | None) -> str:
        base = self.transport.endpoint.base_path
        if self.addressing is Addressing.PATH:
            base = f"{base}/{quote(self.bucket)}"
            return base if key is None else f"{base}/{quote(key, safe='/')}"
        return f"{base}/" if key is None else f"{base}/{quote(key, safe='/')}"

    def _get(
        self, key: str | None, query: Sequence[tuple[str, str]], headers: dict[str, str]
    ) -> Response:
        path = self._path(key)
        if self.credentials is not None:
            headers = headers | sign(
                method="GET",
                host=self.transport.endpoint.authority,
                path=path,
                query=query,
                headers=headers,
                credentials=self.credentials,
                region=self.region,
                when=self._clock(),
            )
        return self.transport.get(path, query, headers)

    def list_page(self, prefix: str, cursor: Cursor | None, page_size: int) -> Page:
        query = [("encoding-type", "url"), ("max-keys", str(page_size)), ("prefix", prefix)]
        if self.versions:
            query.append(("versions", ""))
            if cursor is not None:
                query += [("key-marker", cursor[0]), ("version-id-marker", cursor[1])]
        else:
            query.append(("list-type", "2"))
            if cursor is not None:
                query.append(("continuation-token", cursor[0]))
        root = _xml(self._get(None, query, {}).body(MAX_PAGE_BYTES))
        encoded = _text(root, "EncodingType") == "url"

        def key_of(element: ET.Element) -> bytes:
            text = _text(element, "Key") or ""
            return _url_decoded(text) if encoded else text.encode("utf-8")

        found: list[Listed | Unlisted] = []
        if self.versions:
            if _local(root.tag) != "ListVersionsResult":
                raise PageInvalid("not a ListVersionsResult")
            for element in root:
                if _local(element.tag) != "Version" or _text(element, "IsLatest") != "true":
                    continue  # an older version, or a delete marker
                version = _text(element, "VersionId")
                token = (
                    _token("version", version)
                    if version not in (None, "", "null")
                    else _token("etag", _text(element, "ETag"))
                )
                found.append(_entry(key_of(element), token, _size(_text(element, "Size"))))
        else:
            if _local(root.tag) != "ListBucketResult":
                raise PageInvalid("not a ListBucketResult")
            for element in _children(root, "Contents"):
                token = _token("etag", _text(element, "ETag"))
                found.append(_entry(key_of(element), token, _size(_text(element, "Size"))))
        next_cursor: Cursor | None = None
        if _text(root, "IsTruncated") == "true":
            if self.versions:
                marker = _text(root, "NextKeyMarker") or ""
                raw = _url_decoded(marker) if encoded else marker.encode("utf-8")
                next_cursor = (
                    raw.decode("utf-8", "surrogateescape"),
                    _text(root, "NextVersionIdMarker") or "",
                )
            else:
                next_cursor = (_text(root, "NextContinuationToken") or "",)
            if not next_cursor[0]:
                raise PageInvalid("a truncated listing names no next page")
        return _page(found, next_cursor)

    def get_range(self, key: str, token: str, start: int, length: int) -> Range:
        kind, _, value = token.partition(":")
        query = [("versionId", value)] if kind == "version" else []
        headers = {"Range": _range_header(start, length)}
        if kind == "etag":
            headers["If-Match"] = f'"{value}"'
        return read_range(self._get(key, query, headers), start, length)


# --- Google Cloud Storage ------------------------------------------------------------------------


class GcsClient:
    """Google Cloud Storage through its JSON API, with an OAuth 2.0 bearer token (or anonymous)."""

    provider = Provider.GCS

    def __init__(self, transport: Transport, bucket: str, *, access_token: str | None) -> None:
        self.transport = transport
        self.bucket = bucket
        self._headers = {} if access_token is None else {"Authorization": f"Bearer {access_token}"}

    def list_page(self, prefix: str, cursor: Cursor | None, page_size: int) -> Page:
        path = f"{self.transport.endpoint.base_path}/storage/v1/b/{quote(self.bucket)}/o"
        query = [("maxResults", str(page_size)), ("prefix", prefix)]
        if cursor is not None:
            query.append(("pageToken", cursor[0]))
        body = self.transport.get(path, query, self._headers).body(MAX_PAGE_BYTES)
        try:
            document = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise PageInvalid("a listing is not JSON") from exc
        if not isinstance(document, dict):
            raise PageInvalid("a listing is not a JSON object")
        items = document.get("items", [])
        if not isinstance(items, list):
            raise PageInvalid("a listing's items are not a list")
        found: list[Listed | Unlisted] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise PageInvalid("a listed object has no name")
            name: str = item["name"]
            raw = name.encode("utf-8", "surrogatepass")
            generation = item.get("generation")
            token = _token("generation", generation if isinstance(generation, str) else None)
            found.append(_entry(raw, token, _size(item.get("size"))))
        next_token = document.get("nextPageToken")
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            raise PageInvalid("a listing's next page token is not text")
        return _page(found, (next_token,) if next_token else None)

    def get_range(self, key: str, token: str, start: int, length: int) -> Range:
        base = self.transport.endpoint.base_path
        path = f"{base}/storage/v1/b/{quote(self.bucket)}/o/{quote(key)}"
        query = [("alt", "media"), ("generation", token.partition(":")[2])]
        # Asking for gzip makes GCS serve a gzip-encoded object as stored, ranges honoured, rather
        # than decompressed (its "decompressive transcoding"): the bytes read are the bytes held.
        headers = {
            **self._headers,
            "Accept-Encoding": "gzip",
            "Range": _range_header(start, length),
        }
        return read_range(self.transport.get(path, query, headers), start, length)


# --- Azure Blob Storage --------------------------------------------------------------------------


class AzureBlobClient:
    """Azure Blob Storage with a read-and-list SAS token (or anonymous, for a public container)."""

    provider = Provider.AZURE

    def __init__(
        self, transport: Transport, container: str, *, sas: Sequence[tuple[str, str]] = ()
    ) -> None:
        self.transport = transport
        self.container = container
        self._sas = list(sas)

    def _headers(self) -> dict[str, str]:
        return {"x-ms-version": AZURE_VERSION}

    def list_page(self, prefix: str, cursor: Cursor | None, page_size: int) -> Page:
        path = f"{self.transport.endpoint.base_path}/{quote(self.container)}"
        query = [
            ("comp", "list"),
            ("maxresults", str(page_size)),
            ("prefix", prefix),
            ("restype", "container"),
        ]
        if cursor is not None:
            query.append(("marker", cursor[0]))
        root = _xml(
            self.transport.get(path, query + self._sas, self._headers()).body(MAX_PAGE_BYTES)
        )
        if _local(root.tag) != "EnumerationResults":
            raise PageInvalid("not an EnumerationResults")
        found: list[Listed | Unlisted] = []
        for blobs in _children(root, "Blobs"):
            for blob in _children(blobs, "Blob"):
                names = list(_children(blob, "Name"))
                if not names:
                    raise PageInvalid("a listed blob has no name")
                text = names[0].text or ""
                raw = (
                    _url_decoded(text)
                    if names[0].get("Encoded") == "true"
                    else text.encode("utf-8")
                )
                properties = next(_children(blob, "Properties"), None)
                version = _text(blob, "VersionId")
                if version:
                    token = _token("version", version)
                else:
                    etag = _text(properties, "Etag") if properties is not None else None
                    token = _token("etag", etag)
                size = (
                    _size(_text(properties, "Content-Length")) if properties is not None else None
                )
                found.append(_entry(raw, token, size))
        marker = _text(root, "NextMarker")
        return _page(found, (marker,) if marker else None)

    def get_range(self, key: str, token: str, start: int, length: int) -> Range:
        base = self.transport.endpoint.base_path
        path = f"{base}/{quote(self.container)}/{quote(key, safe='/')}"
        kind, _, value = token.partition(":")
        query = [("versionid", value)] if kind == "version" else []
        headers = {**self._headers(), "Range": _range_header(start, length)}
        if kind == "etag":
            headers["If-Match"] = f'"{value}"'
        return read_range(self.transport.get(path, query + self._sas, headers), start, length)
