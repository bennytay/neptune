"""Roboto's REST API as a store client: list a dataset's files, read a file's bytes (ADR 0009 §2).

Everything here is read-only and uses only what Roboto documents:
https://docs.roboto.ai/restapi.html and the open-source Python SDK that implements it (``roboto``
0.58.0, MPL-2.0). Requests are:

- ``GET  /v1/datasets/{dataset}``: the dataset record.
- ``POST /v1/datasets/{dataset}/files/query``: one page of the dataset's files. A query, not a
  write: the SDK sends it as idempotent. It is the only ``POST`` this module can send, and the
  transport refuses any other path (``RobotoTransport.post_query``).
- ``GET  /v1/datasets/{dataset}/events`` and ``GET /v1/comments/dataset/{dataset}``: annotations.
- ``GET  /v1/files/record/{file}``: a file's record, to pin the version.
- ``GET  /v1/files/{file}/signed-url``: a time-limited URL for the file's bytes, then a ranged
  ``GET`` of that URL.

Every response is ``{"data": ...}``; a page is ``{"data": {"items": [...], "next_token": ...}}``.

Hostile-input rules, as for the object stores (ADR 0006 §6, §8): the bearer token goes only to the
API host, never to the host a signed URL names; that host must be the API's own or one the operator
declared; no redirect is followed; every number is checked before ``int`` sees it; a response is
bounded, strict JSON; each request has a deadline; errors name no URL, token or header.
"""

import hashlib
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.clients import (
    MAX_CURSOR_BYTES,
    Listed,
    Page,
    PageInvalid,
    Range,
    RangeInvalid,
    Unlisted,
    _size,
    read_range,
)
from neptune_deploy.sources.object_store.sigv4 import quote
from neptune_deploy.sources.object_store.transport import (
    Endpoint,
    HttpStatusError,
    NetworkGate,
    Response,
    ResponseTooLarge,
    Transport,
    TransportError,
)
from neptune_deploy.sources.roboto.config import ID, RobotoLocation, RobotoOptions
from neptune_deploy.sources.stated_records import DocumentInvalid, dumps, parse_json

MAX_PAGE_BYTES: Final = 8 * 1024 * 1024  # one page of records; Roboto's are about 1 KB each
MAX_PAGES: Final = 100_000
MAX_URL_BYTES: Final = 8 * 1024  # a signed URL; longer is refused
MAX_CONTENT_HOSTS: Final = 8
_QUERY_ROUTE: Final = re.compile(r"/v1/datasets/[A-Za-z0-9][A-Za-z0-9_\-]{0,63}/files/query")
_TOKEN: Final = re.compile(r"version:([A-Za-z0-9][A-Za-z0-9_\-]{0,63}):([0-9]{1,19})")
_FILE_STATUS: Final = {"available": True, "deleted": False, "reserved": False}


class RobotoTransport(Transport):
    """``Transport`` that can also send the one documented read-only query, a ``POST``."""

    def __init__(
        self, endpoint: Endpoint, network: NetworkGate, purpose: str, *, timeout: float
    ) -> None:
        super().__init__(endpoint, network, purpose, timeout=timeout)

    def post_query(
        self,
        path: str,
        query: Sequence[tuple[str, str]],
        body: bytes,
        headers: Mapping[str, str],
    ) -> Response:
        """``POST`` ``body`` to the dataset files query, and nowhere else."""
        route = path.removeprefix(self.endpoint.base_path)
        if not _QUERY_ROUTE.fullmatch(route):
            raise ValueError("POST is sent to the dataset files query only")
        return self._request(
            "POST", path, query, {**headers, "Content-Type": "application/json"}, body
        )


Entry = Listed | Unlisted


class _Looped(Exception):
    """The files query named a page it had already returned, or ran past the page limit."""


@dataclass
class Records:
    """Records read from a paged endpoint, and whether all of them were."""

    items: list[JsonValue] = field(default_factory=list)
    complete: bool = False
    stopped: str | None = None  # a finding code's cause when not complete
    status: int | None = None
    pages: int = 0


@dataclass(frozen=True)
class Content:
    """Where one file revision's bytes are: a host's transport and the signed path and query."""

    transport: Transport
    path: str
    query: tuple[tuple[str, str], ...]


class RobotoApi:
    """The ``StoreClient`` of one Roboto dataset: ``list_page`` and ``get_range`` (ADR 0006 §2)."""

    def __init__(
        self,
        location: RobotoLocation,
        options: RobotoOptions,
        network: NetworkGate,
        *,
        token: str,
    ) -> None:
        self.location = location
        self.options = options
        self._network = network
        self._endpoint = Endpoint.parse(options.endpoint)
        self.transport = RobotoTransport(
            self._endpoint, network, "reading deploy_roboto sources", timeout=options.timeout
        )
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Roboto-Api-Version": options.api_version,
            "X-Roboto-Resource-Owner-Id": location.org,
        }
        self._allowed = frozenset(
            {
                self._endpoint.authority,
                *(form for host in options.content_hosts for form in _authorities(host)),
            }
        )
        self._content_transports: dict[str, Transport] = {}
        self._content: dict[tuple[str, int], Content] = {}
        self.file_records: list[tuple[str, str, JsonValue]] = []  # key, token, the record
        self._listing_bytes = 0
        self._sorted: list[tuple[tuple[bytes, int, str], Entry]] | None = None
        self._stopped: Exception | None = None
        self.last_bytes = 0  # the size of the body ``_data`` last read

    def __repr__(self) -> str:
        return f"RobotoApi({self.location.org}/{self.location.dataset})"

    # --- Requests ----------------------------------------------------------------------------

    def _path(self, *parts: str) -> str:
        return self._endpoint.base_path + "/v1/" + "/".join(quote(part) for part in parts)

    def _data(self, response: Response, limit: int = MAX_PAGE_BYTES) -> JsonValue:
        """The ``data`` of a response: strict JSON, bounded, an object with a ``data`` key."""
        body = response.body(limit)
        self.last_bytes = len(body)
        try:
            document = parse_json(body)
        except DocumentInvalid as exc:
            raise PageInvalid("a response is not strict JSON") from exc
        if not isinstance(document, Mapping) or "data" not in document:
            raise PageInvalid("a response has no data")
        return document["data"]

    def _page_items(self, data: JsonValue) -> tuple[list[JsonValue], str | None]:
        if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
            raise PageInvalid("a page has no items")
        token = data.get("next_token")
        if token is None or token == "":
            return list(data["items"]), None  # type: ignore[arg-type]
        if not isinstance(token, str):
            raise PageInvalid("a page's next token is not text")
        if len(token.encode("utf-8", "surrogateescape")) > MAX_CURSOR_BYTES:
            raise PageInvalid(f"a next token is longer than {MAX_CURSOR_BYTES} bytes")
        return list(data["items"]), token  # type: ignore[arg-type]

    def dataset(self) -> JsonValue:
        """The dataset's record, as Roboto states it."""
        data = self._data(
            self.transport.get(self._path("datasets", self.location.dataset), (), self._headers)
        )
        if not isinstance(data, Mapping):
            raise PageInvalid("a dataset record is not an object")
        return data

    def records(self, route: tuple[str, ...], *, limit: int, budget: int) -> Records:
        """Every record of a paged ``GET`` (events, comments), up to ``limit`` and ``budget``
        bytes."""
        found = Records()
        seen: set[bytes] = set()
        token: str | None = None
        used = 0
        while True:
            if found.pages >= MAX_PAGES:
                found.stopped = "page_limit"
                return found
            query = [("page_token", token)] if token else []
            try:
                response = self.transport.get(self._path(*route), query, self._headers)
                items, token = self._page_items(self._data(response))
                used += self.last_bytes
            except (TransportError, ValueError) as exc:
                found.stopped = exc.code if isinstance(exc, TransportError) else "response_invalid"
                found.status = exc.status if isinstance(exc, TransportError) else None
                return found
            found.pages += 1
            found.items.extend(items)
            if len(found.items) > limit:
                del found.items[limit:]
                found.stopped = "record_limit"
                return found
            if used > budget:
                found.stopped = "byte_limit"
                return found
            if token is None:
                found.complete = True
                return found
            digest = hashlib.sha256(token.encode("utf-8", "surrogateescape")).digest()
            if digest in seen:
                found.stopped = "pagination_loop"
                return found
            seen.add(digest)

    # --- StoreClient -------------------------------------------------------------------------

    def list_page(self, prefix: str, cursor: tuple[str, ...] | None, page_size: int) -> Page:
        """One page of the dataset's files whose ``relative_path`` starts with ``prefix``, in byte
        order of their paths.

        Roboto's files query documents no order, and a limit keeps the first entries it meets. So
        every page of the query is read on the first call, sorted by path, and then served in
        ``page_size`` slices: what a limit keeps never depends on Roboto's order or paging. The
        read is bounded by the listing's byte budget and the page limit. A failure part-way is
        raised after the slices read before it are served, so the listing says where it stopped.

        A directory, a deleted file and a reserved (not yet uploaded) file are not objects and are
        not listed. A file's revision token is ``version:<file id>:<version>``: the file id is in
        it, so a file deleted and uploaded again at one path never reads as unchanged.
        """
        if self._sorted is None:
            self._sorted = self._read_all(prefix, page_size)
        start = int(cursor[0]) if cursor else 0
        if start >= len(self._sorted) and self._stopped is not None:
            if isinstance(self._stopped, _Looped):
                return Page((), (), (str(start),))  # the cursor repeats: the source reports it
            raise self._stopped
        chunk = self._sorted[start : start + page_size]
        end = start + len(chunk)
        more = end < len(self._sorted) or self._stopped is not None
        return Page(
            tuple(i for _, i in chunk if isinstance(i, Listed)),
            tuple(i for _, i in chunk if isinstance(i, Unlisted)),
            (str(end),) if more else None,
        )

    def _read_all(self, prefix: str, page_size: int) -> list[tuple[tuple[bytes, int, str], Entry]]:
        found: list[tuple[tuple[bytes, int, str], Entry]] = []
        seen: set[bytes] = set()
        token: str | None = None
        for _ in range(MAX_PAGES):
            request: dict[str, JsonValue] = {"limit": page_size}
            query: list[tuple[str, str]] = []
            if token is not None:
                request["page_token"] = token
                query.append(("page_token", token))
            path = self._path("datasets", self.location.dataset, "files", "query")
            try:
                response = self.transport.post_query(path, query, dumps(request), self._headers)
                data = self._data(response)
                self._listing_bytes += self.last_bytes
                if self._listing_bytes > self.options.max_listing_bytes:
                    raise ResponseTooLarge(
                        f"more than {self.options.max_listing_bytes} bytes listed"
                    )
                items, token = self._page_items(data)
            except (TransportError, ValueError) as exc:
                self._stopped = exc
                break
            for record in items:
                entry = self._file(record, prefix)
                if isinstance(entry, Unlisted):
                    found.append(((entry.raw, 0, entry.reason), entry))
                elif entry is not None:
                    found.append(((entry.key.encode("utf-8"), 1, ""), entry))
            if token is None:
                break
            digest = hashlib.sha256(token.encode("utf-8", "surrogateescape")).digest()
            if digest in seen:
                self._stopped = _Looped()
                break
            seen.add(digest)
        else:
            self._stopped = _Looped()  # the page limit reads as a loop: it stops the same way
        found.sort(key=lambda pair: pair[0])
        return found

    def _file(self, record: JsonValue, prefix: str) -> Listed | Unlisted | None:
        """A file record as a listing entry; ``None`` when it is not an object under ``prefix``."""
        if not isinstance(record, Mapping):
            return Unlisted(b"", "revision_invalid")
        path = record.get("relative_path")
        if not isinstance(path, str):
            return Unlisted(b"", "revision_invalid")
        raw = path.encode("utf-8", "surrogatepass")
        if not path.startswith(prefix):
            return None
        if record.get("fs_type", "file") != "file":
            return None
        status = record.get("status", "available")
        if not isinstance(status, str) or status not in _FILE_STATUS:
            return Unlisted(raw, "revision_invalid")
        if not _FILE_STATUS[status]:
            return None
        file_id, version, size = record.get("file_id"), record.get("version"), record.get("size")
        token = None
        if isinstance(file_id, str) and ID.fullmatch(file_id) and _count(version) is not None:
            token = f"version:{file_id}:{version}"
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            return Unlisted(raw, "key_not_utf8")
        if token is None:
            return Unlisted(raw, "revision_invalid")
        sized = _count(size)
        if sized is None:
            return Unlisted(raw, "size_invalid")
        self.file_records.append((path, token, record))
        return Listed(path, token, sized)

    def get_range(self, key: str, token: str, start: int, length: int) -> Range:
        """``length`` bytes of one file version: its record is checked, then its signed URL read."""
        match = _TOKEN.fullmatch(token)
        if match is None:
            raise HttpStatusError("not a Roboto revision token", 404)
        file_id, version = match.group(1), int(match.group(2))
        for attempt in (0, 1):
            content = self._content.get((file_id, version))
            fresh = content is None
            if content is None:
                content = self._resolve(file_id, version)
                self._content[(file_id, version)] = content
            headers = {"Range": f"bytes={start}-{start + length - 1}"}  # no credential goes here
            try:
                response = content.transport.get(content.path, content.query, headers)
            except HttpStatusError as exc:
                self._content.pop((file_id, version), None)
                # An expired or refused signed URL is asked for again, once.
                if exc.status in (400, 401, 403) and not fresh and attempt == 0:
                    continue
                raise
            return read_range(response, start, length)
        raise AssertionError("unreachable")

    def _resolve(self, file_id: str, version: int) -> Content:
        """The file's record (its version must be the listed one) and a signed URL for its bytes."""
        record = self._data(
            self.transport.get(self._path("files", "record", file_id), (), self._headers)
        )
        if (
            not isinstance(record, Mapping)
            or record.get("file_id") != file_id
            or _count(record.get("version")) != version
        ):
            raise HttpStatusError("the file is no longer at the listed version", 412)
        signed = self._data(
            self.transport.get(self._path("files", file_id, "signed-url"), (), self._headers)
        )
        url = signed.get("url") if isinstance(signed, Mapping) else None
        if not isinstance(url, str):
            raise RangeInvalid("a signed URL response names no URL")
        return self._signed(url)

    def _signed(self, url: str) -> Content:
        """A signed URL as a request: https (or loopback http), a host the operator allowed, no
        credentials in it, its path as given and its query decoded once, in order."""
        if len(url) > MAX_URL_BYTES or not url.isprintable() or " " in url or "#" in url:
            raise RangeInvalid("a signed URL is not a usable URL")
        scheme, sep, rest = url.partition("://")
        if not sep:
            raise RangeInvalid("a signed URL is not a URL")
        authority, _, remainder = rest.partition("/")
        path, _, query = ("/" + remainder).partition("?")
        try:
            endpoint = Endpoint.parse(f"{scheme}://{authority}")
        except ValueError as exc:
            raise RangeInvalid("a signed URL names no usable host") from exc
        if endpoint.authority not in self._allowed:
            raise RangeInvalid("a signed URL names a host the operator did not allow")
        if "\\" in path or "%2e" in path.lower() or "/../" in path or path.endswith("/.."):
            raise RangeInvalid("a signed URL has a dot segment in its path")
        pairs: list[tuple[str, str]] = []
        for part in query.split("&") if query else ():
            name, _, value = part.partition("=")
            if "+" in part or not name:
                raise RangeInvalid("a signed URL has a query this connector will not rewrite")
            try:
                pairs.append(
                    (
                        urllib.parse.unquote_to_bytes(name).decode("utf-8"),
                        urllib.parse.unquote_to_bytes(value).decode("utf-8"),
                    )
                )
            except UnicodeDecodeError as exc:
                raise RangeInvalid("a signed URL's query is not UTF-8") from exc
        transport = self._content_transports.get(endpoint.authority)
        if transport is None:
            if len(self._content_transports) >= MAX_CONTENT_HOSTS:
                raise RangeInvalid("too many content hosts")
            if endpoint.authority == self._endpoint.authority:
                transport = self.transport
            else:
                transport = Transport(
                    endpoint,
                    self._network,
                    "reading deploy_roboto sources",
                    timeout=self.options.timeout,
                )
            self._content_transports[endpoint.authority] = transport
        return Content(transport, path, tuple(pairs))


def _authorities(host: str) -> set[str]:
    """The ``Host`` forms of a declared content host: ``host:443`` is ``host`` under https."""
    forms = {host.lower()}
    for scheme in ("https", "http"):
        try:
            forms.add(Endpoint.parse(f"{scheme}://{host}").authority.lower())
        except ValueError:
            continue
    return forms


def _count(value: object) -> int | None:
    """A non-negative integer of at most 19 digits, from JSON that wrote a number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return _size(str(value)) if value >= 0 else None
