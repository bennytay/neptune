"""An engine behind HTTP (ADR 0004 §3): ``HttpEngine`` speaks ``wire`` with the standard library.

Safety by construction: only ``http`` and ``https`` URLs without credentials, query or fragment; a
bearer token is sent only over ``https`` or to a loopback host; redirects are never followed (a
redirect would carry the token somewhere the caller did not name); responses are size-bounded and
read strictly. A token is never part of ``repr``, an error message or a log line.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from neptune_ledger.api import CodecError, Resolution, loads

from neptune_context import __version__
from neptune_context.packets.codec import decode
from neptune_context.packets.findings import PacketRefused
from neptune_context.query.codec import canonical_bytes
from neptune_context.sdk import wire
from neptune_context.sdk.errors import ErrorCode, SdkError

if TYPE_CHECKING:
    from neptune.model.provenance import EvidenceRef
    from neptune_context.packets.model import ContextPacket
    from neptune_context.query.model import Query

LOOPBACK: Final = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_TIMEOUT_S: Final = 30.0


def _timed_out() -> SdkError:
    """A fresh error per timeout: a shared instance would grow its traceback on every raise and
    keep each call's frames (request headers, partial bodies) alive across threads."""
    return SdkError(ErrorCode.TIMEOUT, "the engine did not answer in time")


_CHUNK: Final = 64 * 1024


def _unreachable(reason: object) -> SdkError:
    return SdkError(
        ErrorCode.UNAVAILABLE, f"the engine is unreachable or broke the connection: {reason}"
    )


def _read(stream: Any, limit: int, deadline: float) -> bytes:
    """Up to ``limit`` bytes of ``stream`` by ``deadline``; ``timeout`` when it is not all there.

    The socket timeout bounds each wait, not the whole answer, so a server that drips one byte per
    wait is cut off here.
    """
    chunks: list[bytes] = []
    size = 0
    take = getattr(stream, "read1", stream.read)  # read1 returns after one socket wait
    while size < limit:
        if time.monotonic() > deadline:
            raise _timed_out()
        chunk = take(min(_CHUNK, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _check_url(url: str, has_token: bool) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as error:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, f"not a URL: {error}") from error
    if parts.scheme not in {"http", "https"} or not host:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, "an engine URL is http(s)://host[:port][/base]")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise SdkError(
            ErrorCode.INVALID_ARGUMENT, "an engine URL carries no credentials, query or fragment"
        )
    if has_token and parts.scheme != "https" and host not in LOOPBACK:
        raise SdkError(
            ErrorCode.INVALID_ARGUMENT, "a token is only sent over https or to a loopback host"
        )
    return url.rstrip("/")


class HttpEngine:
    """The two engine calls over ``wire``. ``timeout`` bounds each request in seconds."""

    def __init__(
        self, url: str, *, token: str | None = None, timeout: float = DEFAULT_TIMEOUT_S
    ) -> None:
        if token is not None and (not token or token != token.strip() or not token.isascii()):
            raise SdkError(ErrorCode.INVALID_ARGUMENT, "the token is empty, padded or not ASCII")
        if token is not None and any(ord(c) < 0x21 or ord(c) == 0x7F for c in token):
            raise SdkError(ErrorCode.INVALID_ARGUMENT, "the token contains control characters")
        if not 0 < timeout <= 3600:
            raise SdkError(ErrorCode.INVALID_ARGUMENT, "timeout is between 0 and 3600 seconds")
        self._base = _check_url(url, token is not None)
        self._token = token
        self._timeout = timeout
        # No proxies from the environment: the caller named this engine, and a proxy would see a
        # plaintext token sent to a loopback host.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def __repr__(self) -> str:
        return f"HttpEngine({self._base!r}, token={'set' if self._token else None})"

    def _post(self, path: str, body: bytes) -> bytes:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"neptune-context-sdk/{__version__}",
            "X-Neptune-Wire": str(wire.WIRE_VERSION),
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self._base + path, data=body, headers=headers, method="POST"
        )
        deadline = time.monotonic() + self._timeout
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                data = _read(response, wire.MAX_RESPONSE_BYTES, deadline)
        except urllib.error.HTTPError as error:
            with error:
                try:
                    detail = _read(error, wire.MAX_REQUEST_BYTES, deadline)
                except (OSError, http.client.HTTPException, SdkError):
                    detail = b""
            raise wire.error_from_status(error.code, detail) from None
        except SdkError:
            raise
        except TimeoutError:
            raise _timed_out() from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise _timed_out() from None
            raise _unreachable(error.reason) from None
        except (OSError, http.client.HTTPException) as error:
            raise _unreachable(error) from None
        if len(data) >= wire.MAX_RESPONSE_BYTES:
            raise SdkError(ErrorCode.INVALID_RESPONSE, "the engine's answer is too large")
        return data

    def query(self, query: Query) -> ContextPacket:
        data = self._post(wire.QUERY_PATH, canonical_bytes(query))
        packet = decode(data)
        if isinstance(packet, PacketRefused):
            first = packet.findings[0]
            raise SdkError(
                ErrorCode.INVALID_RESPONSE,
                f"the answer is not a packet: {first.code} at {first.at!r}: {first.message}",
            )
        return packet

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        data = self._post(wire.HYDRATE_PATH, wire.hydrate_request(evidence, as_of))
        try:
            return loads(Resolution, data)
        except (CodecError, ValueError) as error:
            raise SdkError(
                ErrorCode.INVALID_RESPONSE, f"the answer is not a resolution: {error}"
            ) from None
