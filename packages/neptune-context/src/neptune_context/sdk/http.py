"""An engine behind HTTP (ADR 0004 §3): ``HttpEngine`` speaks ``wire`` with the standard library.

Safety by construction: only ``http`` and ``https`` URLs without credentials, query or fragment; a
bearer token is sent only over ``https`` or to a loopback host; redirects are never followed (a
redirect would carry the token somewhere the caller did not name); responses are size-bounded and
read strictly. A token is never part of ``repr``, an error message or a log line.

``timeout`` is one deadline for the whole answer (ADR 0004 §4): connecting, sending, the status
line and headers and the body all draw on the same remaining time, because every socket wait is
given only what is left. A server that drips its headers one byte at a time is cut off like one
that drips its body (MVL-147, from the MVL-110 review).
"""

from __future__ import annotations

import http.client
import io
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
            # ``read1`` ends quietly at EOF even when the server promised more: a body shorter
            # than its Content-Length is a broken connection, not a short answer.
            missing = getattr(stream, "length", None)
            if missing:
                raise http.client.IncompleteRead(b"".join(chunks), missing)
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


class _Deadline:
    """One point in time every socket wait of a request must finish by."""

    def __init__(self, seconds: float) -> None:
        self.at = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self.at - time.monotonic()
        if left <= 0:
            raise TimeoutError("the engine did not answer in time")
        return left


class _DeadlineReader(io.RawIOBase):
    """The socket's receive side, each wait bounded by what is left of the deadline."""

    def __init__(self, owner: _DeadlineSocket) -> None:
        self._owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        sock = self._owner.sock
        sock.settimeout(self._owner.deadline.remaining())
        return int(sock.recv_into(buffer))

    def close(self) -> None:
        if not self.closed:
            super().close()
            self._owner.release()


class _DeadlineSocket:
    """A connected socket whose reads and writes share one deadline. ``http.client`` reads the
    status line, headers and body through ``makefile``; ``close`` is deferred until every file
    made from it is closed, as ``socket.socket`` does for its own files."""

    def __init__(self, sock: Any, deadline: _Deadline) -> None:
        self.sock = sock
        self.deadline = deadline
        self._files = 0
        self._closing = False

    def makefile(self, mode: str = "rb", *args: Any, **kwargs: Any) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError(f"only binary reads are supported, not {mode!r}")
        self._files += 1
        return io.BufferedReader(_DeadlineReader(self))

    def sendall(self, data: Any, *args: Any) -> None:
        self.sock.settimeout(self.deadline.remaining())
        self.sock.sendall(data, *args)

    def release(self) -> None:
        self._files -= 1
        if self._closing and self._files <= 0:
            self.sock.close()

    def close(self) -> None:
        self._closing = True
        if self._files <= 0:
            self.sock.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sock, name)


def _bounded(base: type[http.client.HTTPConnection], deadline: _Deadline) -> Any:
    class Bounded(base):  # type: ignore[valid-type,misc]
        def connect(self) -> None:
            self.timeout = deadline.remaining()  # the TCP connect (and TLS handshake) wait
            super().connect()
            self.sock: Any = _DeadlineSocket(self.sock, deadline)

    return Bounded


class _HttpHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._deadline = deadline

    def http_open(self, req: Any) -> Any:
        return self.do_open(_bounded(http.client.HTTPConnection, self._deadline), req)


class _HttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._deadline = deadline

    def https_open(self, req: Any) -> Any:
        context = getattr(self, "_context", None)
        return self.do_open(
            _bounded(http.client.HTTPSConnection, self._deadline), req, context=context
        )


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

    @staticmethod
    def _opener(deadline: _Deadline) -> urllib.request.OpenerDirector:
        # No proxies from the environment: the caller named this engine, and a proxy would see a
        # plaintext token sent to a loopback host. Connections draw on the request's deadline.
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect,
            _HttpHandler(deadline),
            _HttpsHandler(deadline),
        )

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
        bound = _Deadline(self._timeout)
        deadline = bound.at
        try:
            with self._opener(bound).open(request, timeout=self._timeout) as response:
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
