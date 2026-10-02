"""The record connectors' one HTTP boundary: authenticated, bounded GETs that never follow (ADR 0008).

It reuses the object-store connector's ``Transport`` (ADR 0006 §6): ``GET`` is the only method, the
workspace is asked before every request, a redirect is refused and never followed, ``http`` is
allowed to a loopback host only. This module adds what a record system needs on top:

- an ``Authorization`` or API-key header the caller declared, never printed (``Auth.__repr__``);
- ``429`` as ``RateLimited`` with the integer ``Retry-After``, if the system sent one. Nothing
  sleeps and nothing retries: a retry is a decision about time, and the listing stops, says so, and
  is resumable;
- JSON bodies read with a bound, as UTF-8, strictly (``jsontext``), and only if the response says it
  is JSON and is not compressed;
- downloads read to exactly the size the listing stated.

A path is built from validated ids and percent-encoded here. A URL the system supplies (a Jira
attachment's ``content``, a Confluence ``_links.next``) is never requested: that would let a hostile
record point the client, and its credentials, anywhere.
"""

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from neptune_deploy.sources.object_store.sigv4 import quote
from neptune_deploy.sources.object_store.transport import (
    HttpStatusError,
    RedirectRefused,
    Response,
    ResponseTooLarge,
    Transport,
    TransportError,
)
from neptune_deploy.sources.records import jsontext

MAX_PAGE_BYTES: Final = 32 * 1024 * 1024
USER_AGENT: Final = "neptune-deploy-records/0.1.0"
_MAX_RETRY_AFTER: Final = 7 * 24 * 3600


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
    if value.isascii() and value.isdigit() and int(value) <= _MAX_RETRY_AFTER:
        return int(value)
    return None  # a date, or nonsense: a date is a wall-clock reading this client does not use


class RecordTransport(Transport):
    """``Transport`` whose error responses keep what a finding needs (the status, ``Retry-After``).

    It sends what the parent sends (the workspace is asked first, ``GET`` only, ``3xx`` refused)
    and differs only in the exceptions it raises for ``401``, ``403`` and ``429``.
    """

    def get(
        self, path: str, query: Sequence[tuple[str, str]] = (), headers: Mapping[str, str] = {}
    ) -> Response:
        self._network.require_network(self._purpose)
        pairs = [(quote(k), quote(v)) for k, v in query]
        target = path + ("?" + "&".join(f"{k}={v}" if v else k for k, v in pairs) if pairs else "")
        sent = {"Host": self.endpoint.authority, "User-Agent": USER_AGENT, **headers}
        raw = self._send(target, sent)
        response = Response(
            raw.status, {k.lower(): v for k, v in raw.getheaders()}, raw, _transport=self
        )
        status = raw.status
        if 200 <= status < 300:
            return response
        response.discard()
        if 300 <= status < 400:
            raise RedirectRefused(f"status {status}", status)
        if status == 429:
            raise RateLimited("rate limited", status, _retry_after(response.headers))
        if status in (401, 403):
            raise AccessDenied(f"status {status}", status)
        raise HttpStatusError(f"status {status}", status)


class Api:
    """A record system's HTTP API: JSON documents and bounded downloads, with declared credentials."""

    def __init__(self, transport: Transport, auth: Auth, *, base_path: str = "") -> None:
        self.transport = transport
        self._auth = auth
        self._base = base_path

    def _headers(self, accept: str) -> dict[str, str]:
        return {self._auth.header: self._auth.value, "Accept": accept}

    def json(
        self, path: str, query: Sequence[tuple[str, str]] = ()
    ) -> tuple[Any, Mapping[str, str]]:
        """The JSON document at ``path`` and the response headers (names lower-cased)."""
        response = self._get(path, query, "application/json")
        _require_plain(response)
        if "json" not in response.headers.get("content-type", "").lower():
            response.discard()
            raise ResponseInvalid("the response is not JSON")
        try:
            body = response.body(MAX_PAGE_BYTES)
        except ResponseTooLarge as exc:
            raise ResponseInvalid("the response is too large") from exc
        try:
            return jsontext.loads(body), response.headers
        except jsontext.JsonTextError as exc:
            raise ResponseInvalid(str(exc)) from exc

    def download(self, path: str, query: Sequence[tuple[str, str]], size: int) -> bytes:
        """Exactly ``size`` bytes. A body of another length is ``SizeMismatch``, never accepted."""
        response = self._get(path, query, "*/*")
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
            raise SizeMismatch("fewer bytes than the listed size")
        return data

    def _get(self, path: str, query: Sequence[tuple[str, str]], accept: str) -> Response:
        return self.transport.get(self._base + path, query, self._headers(accept))


def _require_plain(response: Response) -> None:
    """A compressed body is never decoded here: a decompression bomb needs a decompressor."""
    if response.headers.get("content-encoding", "identity").lower() not in ("identity", ""):
        response.discard()
        raise ResponseInvalid("the response is compressed")
