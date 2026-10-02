"""The record connectors' HTTP boundary: authenticated, bounded GETs that never follow (ADR 0008).

It reuses the object-store connector's ``Transport`` (ADR 0006 §6): ``GET`` is the only method, the
workspace is asked before every request, a redirect is refused and never followed, ``http`` is
allowed to a loopback host only. This module adds what a record system needs on top:

- an ``Authorization`` or API-key header the caller declared, never printed (``Auth.__repr__``);
- ``429`` as ``RateLimited`` with the integer ``Retry-After``, if the system sent one. Nothing
  sleeps and nothing retries: a retry is a decision about time, and the listing stops, says so, and
  is resumable;
- JSON bodies read with a bound, as UTF-8, strictly (``jsontext``), and only if the response says it
  is JSON and is not compressed;
- downloads read to exactly the size the listing stated;
- two narrow exceptions to "GET only, never follow", each for a system whose API has no other form
  (ADR 0008 §6): a GraphQL *query* may be sent with ``POST`` (Linear), and a download redirect to
  a pre-authenticated URL may be followed to a host the operator's allow-list names, with no
  credential sent (Microsoft Graph).

A path is built from validated ids and percent-encoded here. A URL the system supplies (a Jira
attachment's ``content``, a Confluence ``_links.next``) is never requested: that would let a hostile
record point the client, and its credentials, anywhere.
"""

import base64
import http.client
import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from neptune_deploy.sources.object_store.transport import (
    Endpoint,
    HttpStatusError,
    RedirectRefused,
    Response,
    ResponseTooLarge,
    ShortRead,
    Transport,
    TransportError,
    _Deadline,
)
from neptune_deploy.sources.records import jsontext

MAX_PAGE_BYTES: Final = 32 * 1024 * 1024
USER_AGENT: Final = "neptune-deploy-records/0.1.0"
_MAX_RETRY_AFTER: Final = 7 * 24 * 3600
FOLLOWED_REDIRECTS: Final = (302, 303, 307)  # a download redirect; never any other 3xx
_MAX_LOCATION: Final = 8192
_PATH: Final = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@/%\-]*")
_RAW_QUERY: Final = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/?%\-]*")
_QUOTA_REASONS: Final = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_MAX_ERROR_BODY: Final = 64 * 1024
_READ_ONLY_OPERATION: Final = re.compile(r"\s*query\b")
_WRITING_OPERATION: Final = re.compile(r"\b(mutation|subscription)\b")


class RateLimited(HttpStatusError):
    """``429``. ``retry_after`` is the system's own integer seconds, else ``None``."""

    code = "rate_limited"

    def __init__(self, detail: str, status: int, retry_after: int | None) -> None:
        super().__init__(detail, status)
        self.retry_after = retry_after


class AccessDenied(HttpStatusError):
    """``401`` or ``403``: the declared credentials do not allow this read."""

    code = "access_denied"


class ResponseInvalid(TransportError):
    """A body that is not what the system documents, or that this client refuses to parse."""

    code = "response_invalid"


class PaginationLoop(TransportError):
    """A system that names, or returns, a page it already returned."""

    code = "pagination_loop"


class SizeMismatch(TransportError):
    """A download whose length is not the length the listing stated."""

    code = "size_mismatch"


@dataclass(frozen=True)
class Auth:
    """One request header that carries the declared credential. Never printed."""

    header: str = field(repr=False)
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return "Auth(<redacted>)"

    @classmethod
    def basic(cls, user: str, secret: str) -> "Auth":
        token = base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")
        return cls("Authorization", f"Basic {token}")

    @classmethod
    def bearer(cls, token: str) -> "Auth":
        return cls("Authorization", f"Bearer {token}")


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after", "")
    if value.isascii() and value.isdigit() and len(value) <= 9 and int(value) <= _MAX_RETRY_AFTER:
        return int(value)
    return None  # a date, or nonsense: a date is a wall-clock reading this client does not use


class RecordTransport(Transport):
    """``Transport`` whose error responses keep what a finding needs (the status, ``Retry-After``).

    It sends what the parent sends (the workspace is asked first, ``GET`` only, ``3xx`` refused,
    the whole request under one deadline) and differs only in the exceptions it raises for ``401``,
    ``403`` and ``429``. It remembers the last response's headers for that.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_headers: dict[str, str] = {}
        self._error_reason: str | None = None  # the reason a 403 states, if it states one
        # Statuses this system answers a throttled request with (a Retry-After comes with them).
        self.throttle_statuses: frozenset[int] = frozenset({429})

    @property
    def last_location(self) -> str | None:
        """The ``Location`` of the last response, if it stated one."""
        return self._last_headers.get("location")

    def derive(self, endpoint: Endpoint) -> "RecordTransport":
        """A transport to another endpoint under the same gate, purpose, timeout and TLS."""
        other = RecordTransport(
            endpoint, self._network, self._purpose, timeout=self._timeout, tls=self._tls
        )
        other.throttle_statuses = self.throttle_statuses
        return other

    def _send(
        self,
        target: str,
        headers: Mapping[str, str],
        deadline: _Deadline,
        method: str = "GET",
        body: bytes | None = None,
    ) -> http.client.HTTPResponse:
        raw = super()._send(target, headers, deadline, method, body)
        self._last_headers = {k.lower(): v for k, v in raw.getheaders()}
        self._error_reason = self._reason_of(raw) if raw.status == 403 else None
        return raw

    @staticmethod
    def _reason_of(raw: http.client.HTTPResponse) -> str | None:
        """The ``reason`` a 403's JSON error states (Google: ``error.errors[0].reason``), if it
        is one of the quota reasons. The body is read bounded and only for a 403; anything else it
        holds is never kept."""
        try:
            body = raw.read(_MAX_ERROR_BODY + 1)
            if len(body) > _MAX_ERROR_BODY:
                return None
            document = jsontext.loads(body)
        except (OSError, http.client.HTTPException, jsontext.JsonTextError):
            return None
        error = document.get("error") if isinstance(document, dict) else None
        listed = error.get("errors") if isinstance(error, dict) else None
        for entry in listed if isinstance(listed, list) else ():
            reason = entry.get("reason") if isinstance(entry, dict) else None
            if isinstance(reason, str) and reason in _QUOTA_REASONS:
                return str(reason)
        return None

    def post_query(
        self, path: str, body: bytes, headers: Mapping[str, str] = MappingProxyType({})
    ) -> Response:
        """``POST`` a GraphQL query document (``Api.graphql`` builds and checks it): a read, sent
        again once on a new connection if a kept-alive one was closed while idle, as ``GET`` is."""
        return self._request(
            "POST", path, (), {**headers, "Content-Type": "application/json"}, body
        )

    def _request(
        self,
        method: str,
        path: str,
        query: Sequence[tuple[str, str]] = (),
        headers: Mapping[str, str] = MappingProxyType({}),
        body: bytes | None = None,
    ) -> Response:
        try:
            return super()._request(method, path, query, headers, body)
        except HttpStatusError as exc:
            if exc.status in self.throttle_statuses or (
                exc.status == 403 and self._error_reason is not None
            ):  # Drive states a quota stop as a 403 whose reason says so
                raise RateLimited(
                    "rate limited", exc.status or 0, _retry_after(self._last_headers)
                ) from exc
            if exc.status == 400 and "0" in (
                self._last_headers.get("x-ratelimit-requests-remaining"),
                self._last_headers.get("x-ratelimit-complexity-remaining"),
            ):  # Linear answers 400, not 429, and says so only in these headers
                raise RateLimited("rate limited", 400, None) from exc
            if exc.status in (401, 403):
                raise AccessDenied(f"status {exc.status}", exc.status) from exc
            raise


class Api:
    """A record system's HTTP API: JSON documents and bounded downloads, declared credentials."""

    def __init__(self, transport: RecordTransport, auth: Auth, *, base_path: str = "") -> None:
        self.transport = transport
        self._auth = auth
        self._base = base_path

    def _headers(self, accept: str) -> dict[str, str]:
        return {self._auth.header: self._auth.value, "Accept": accept, "User-Agent": USER_AGENT}

    def json(
        self, path: str, query: Sequence[tuple[str, str]] = ()
    ) -> tuple[Any, Mapping[str, str]]:
        """The JSON document at ``path`` and the response headers (names lower-cased)."""
        return _json_of(self._get(path, query, "application/json"))

    def graphql(
        self, path: str, document: str, variables: Mapping[str, Any]
    ) -> tuple[Any, Mapping[str, str]]:
        """The JSON answer to one GraphQL *query*. ``document`` is a constant of the connector, and
        must be a query: an operation that is a mutation or subscription is refused before it is
        sent. Variables ride in the JSON body, never in the document."""
        if not _READ_ONLY_OPERATION.match(document) or _WRITING_OPERATION.search(document):
            raise ValueError("a record connector sends GraphQL queries only")
        body = json.dumps(
            {"query": document, "variables": dict(variables)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        response = self.transport.post_query(
            self._base + path, body, self._headers("application/json")
        )
        return _json_of(response)

    def download(self, path: str, query: Sequence[tuple[str, str]], size: int) -> bytes:
        """Exactly ``size`` bytes. A body of another length is ``SizeMismatch``, never accepted."""
        return _exactly(self._get(path, query, "*/*"), size)

    def download_redirected(
        self, path: str, query: Sequence[tuple[str, str]], size: int, hosts: Sequence[str]
    ) -> bytes:
        """``download``, for a system that answers with a redirect to a pre-authenticated URL.

        That URL is followed once, to a host in ``hosts`` (a domain and its subdomains), over
        https (http to a loopback host), with no credential, and its own redirect is refused. The
        redirect is the system's statement, so it is checked, never trusted: any other target is
        ``RedirectRefused``, and no request is sent to it.
        """
        try:
            return self.download(path, query, size)
        except RedirectRefused as exc:
            if exc.status not in FOLLOWED_REDIRECTS:
                raise
            location = self.transport.last_location
            status = exc.status
        endpoint, target = pre_authenticated(location, hosts, status)
        other = self.transport.derive(endpoint)
        try:
            return _exactly(
                other.get(target, (), {"Accept": "*/*", "User-Agent": USER_AGENT}), size
            )
        finally:
            other.drop()

    def _get(self, path: str, query: Sequence[tuple[str, str]], accept: str) -> Response:
        return self.transport.get(self._base + path, query, self._headers(accept))


def pre_authenticated(
    location: str | None, hosts: Sequence[str], status: int
) -> tuple[Endpoint, str]:
    """``(endpoint, target)`` of a redirect target, or ``RedirectRefused`` if it is not one
    the operator allowed: a host of ``hosts`` (or a subdomain of one), https on port 443 (http to a
    loopback host), no user information, no fragment, a plain path. ``target`` is the path and the
    query exactly as the system wrote them: a pre-authenticated URL is signed over its bytes, so
    nothing here decodes and re-encodes it (a literal ``+`` stays a ``+``)."""

    def refuse() -> RedirectRefused:
        return RedirectRefused(f"status {status}", status)

    if (
        location is None
        or len(location) > _MAX_LOCATION
        or not location.isascii()
        or not location.isprintable()
        or " " in location
    ):
        raise refuse()
    try:
        parts = urllib.parse.urlsplit(location)
        endpoint = Endpoint.parse(f"{parts.scheme}://{parts.netloc}")
    except ValueError as exc:
        raise refuse() from exc
    allowed = any(endpoint.host == h or endpoint.host.endswith("." + h) for h in hosts)
    default = 443 if endpoint.scheme == "https" else endpoint.port
    if (
        not allowed
        or parts.fragment
        or endpoint.port != default
        or not _PATH.fullmatch(parts.path)
        or not _RAW_QUERY.fullmatch(parts.query)
    ):
        raise refuse()
    return endpoint, parts.path + ("?" + parts.query if parts.query else "")


def _json_of(response: Response) -> tuple[Any, Mapping[str, str]]:
    _require_plain(response)
    if not _is_json(response.headers.get("content-type", "")):
        response.discard()
        raise ResponseInvalid("the response is not JSON")
    try:
        body = response.body(MAX_PAGE_BYTES)
    except ResponseTooLarge as exc:
        raise ResponseInvalid("the response is too large") from exc
    declared = response.headers.get("content-length", "")
    promised = declared.isascii() and declared.isdigit() and len(declared) <= 18
    if promised and int(declared) != len(body):  # http.client does not raise for a short read
        raise ShortRead("the body ended early", len(body), int(declared))
    try:
        return jsontext.loads(body), response.headers
    except jsontext.JsonTextError as exc:
        raise ResponseInvalid(str(exc)) from exc


def _exactly(response: Response, size: int) -> bytes:
    _require_plain(response)
    declared = response.headers.get("content-length")
    if declared is not None and declared != str(size):
        response.discard()
        raise SizeMismatch("the length differs from the listed size")
    try:
        data = response.body(size)
    except ResponseTooLarge as exc:
        raise SizeMismatch("more bytes than the listed size") from exc
    if len(data) != size:
        if declared == str(size):  # it promised the listed length, then stopped early
            raise ShortRead("the body ended early", len(data), size)
        raise SizeMismatch("fewer bytes than the listed size")
    return data


def _is_json(content_type: str) -> bool:
    """``application/json`` or an ``application/*+json`` type, parameters ignored; ASCII only."""
    media = content_type.partition(";")[0].strip()
    if not media.isascii():
        return False
    media = media.lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


def _require_plain(response: Response) -> None:
    """A compressed body is never decoded here: a decompression bomb needs a decompressor."""
    encoding = response.headers.get("content-encoding", "identity")
    if not encoding.isascii() or encoding.strip().lower() not in ("identity", ""):
        response.discard()
        raise ResponseInvalid("the response is compressed")
