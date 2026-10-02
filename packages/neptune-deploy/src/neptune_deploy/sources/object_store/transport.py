"""The connector's one network boundary: GET requests over ``http.client`` (ADR 0006 §1, §6).

- Every request asks the workspace first (``NetworkGate.require_network``), so a local-only
  workspace refuses each one, not only the first.
- ``GET`` is the only method this module can send. Nothing here can write, delete or acknowledge.
- Redirects are never followed: ``http.client`` does not follow them, and a ``3xx`` is raised as
  ``RedirectRefused``. Following one would send the request, and its credentials, wherever the
  server says, which is the object store's symlink.
- An endpoint is ``https``, or ``http`` on a loopback address only (a local MinIO or emulator).
- Bodies are read with a bound. A body shorter than its ``Content-Length`` is ``ShortRead``.
- One connection is kept open between requests and dropped after any failure.
- The timeout bounds each socket operation, and the whole request as a deadline: a server that
  trickles bytes cannot hold a request open past it.

No error text, URL or header from here reaches a finding: the source turns each error into a code
and a status.
"""

import contextlib
import http.client
import ipaddress
import socket
import ssl
import threading
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from neptune_deploy.sources.object_store.sigv4 import quote

DEFAULT_TIMEOUT: Final = 60.0
USER_AGENT: Final = "neptune-deploy-object-store/0.1.0"


class NetworkGate(Protocol):
    """What decides whether this process may use the network: the compiler's workspace."""

    def require_network(self, purpose: str) -> None:
        """Return to allow; raise (the workspace raises ``LocalOnlyError``) to refuse."""
        ...


class TransportError(Exception):
    """A request did not produce a usable response. ``code`` names why, for a finding."""

    code: str = "transport_failed"

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.status = status


class RedirectRefused(TransportError):
    code = "redirect_refused"


class HttpStatusError(TransportError):
    code = "http_status"


class ResponseTooLarge(TransportError):
    code = "response_too_large"


class DeadlineExceeded(TransportError):
    """The request as a whole took longer than its timeout (a server trickling bytes)."""

    code = "deadline_exceeded"


class ShortRead(TransportError):
    """The body ended before the bytes its headers promised."""

    code = "short_read"

    def __init__(self, detail: str, received: int, expected: int) -> None:
        super().__init__(detail)
        self.received = received
        self.expected = expected


def redact(url: str) -> str:
    """``url`` as an error may show it: scheme, host and port, never user information, a path or
    a query (either may hold a credential or a signature)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "<endpoint>"
    if not parts.scheme or not host:
        return "<endpoint>"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Endpoint:
    """Where requests go: scheme, host, port and a base path (no trailing ``/``)."""

    scheme: str
    host: str
    port: int
    base_path: str = ""

    @classmethod
    def parse(cls, url: str) -> "Endpoint":
        """An endpoint URL: ``https://host[:port][/base]``, or ``http://`` to a loopback host.

        No user information, query or fragment: credentials never ride in a URL. A refusal names
        the URL only as ``redact`` writes it (scheme, host and port), never whole.
        """
        if not isinstance(url, str) or not url.isprintable() or " " in url:
            raise ValueError("not an endpoint URL")
        scheme, sep, rest = url.partition("://")
        scheme = scheme.lower()
        if not sep or scheme not in ("http", "https"):
            raise ValueError("an endpoint is an http or https URL")
        authority, _, path = rest.partition("/")
        if "@" in authority:
            raise ValueError(
                "an endpoint URL holds no user information; declare credentials instead"
            )
        if any(ch in rest for ch in "?#@\\"):
            raise ValueError(f"an endpoint has no query or fragment: {redact(url)}")
        host, port_text = authority, ""
        if authority.startswith("["):
            close = authority.find("]")
            if close < 0:
                raise ValueError("an endpoint's IPv6 address is not closed")
            host, port_text = authority[1:close], authority[close + 1 :].removeprefix(":")
        elif ":" in authority:
            host, _, port_text = authority.rpartition(":")
        if not host or (":" in host and not authority.startswith("[")):
            raise ValueError("an endpoint names one host (an IPv6 address in brackets)")
        host = host.lower()
        port = 443 if scheme == "https" else 80
        if port_text:
            if not (port_text.isascii() and port_text.isdigit() and len(port_text) <= 5) or not (
                0 < int(port_text) < 65536
            ):
                raise ValueError("an endpoint's port is not a port number")
            port = int(port_text)
        if scheme == "http" and not _is_loopback(host):
            raise ValueError(f"plain http is allowed to a loopback host only: {redact(url)}")
        base = "/" + path.strip("/") if path.strip("/") else ""
        return cls(scheme, host, port, base)

    @property
    def authority(self) -> str:
        """The ``Host`` header: the port only when it is not the scheme's default."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"


class _Deadline:
    """The whole request's time limit: when it passes, the socket is shut down, so whatever read
    is waiting (headers or body, however slowly the bytes come) ends at once."""

    def __init__(self, transport: "Transport", seconds: float) -> None:
        self.expired = False
        self._transport = transport
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        self.expired = True
        self._transport.abort()

    def cancel(self) -> None:
        self._timer.cancel()

    def error(self, exc: BaseException, default: TransportError) -> TransportError:
        """``default``, unless the deadline is what ended the read."""
        return DeadlineExceeded("the request outlived its deadline") if self.expired else default


@dataclass
class Response:
    """A response whose body has not been read yet. Read it once, through ``body`` or ``exact``."""

    status: int
    headers: dict[str, str]  # lower-cased names; a repeated header keeps its last value
    _raw: http.client.HTTPResponse
    _transport: "Transport"
    _deadline: _Deadline

    def _fail(self, exc: BaseException | None, default: TransportError) -> TransportError:
        self._deadline.cancel()
        self._transport.drop()
        return self._deadline.error(exc or default, default)

    def body(self, limit: int) -> bytes:
        """The whole body, refused past ``limit`` bytes."""
        try:
            data = self._raw.read(limit + 1)
        except http.client.IncompleteRead as exc:
            raise self._fail(exc, ShortRead("the body ended early", len(exc.partial), -1)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise self._fail(exc, TransportError(type(exc).__name__)) from exc
        if self._deadline.expired:
            raise self._fail(None, DeadlineExceeded("the request outlived its deadline"))
        if len(data) > limit:
            raise self._fail(None, ResponseTooLarge(f"more than {limit} bytes", self.status))
        self._done()
        return data

    def exact(self, length: int) -> bytes:
        """Exactly ``length`` bytes of body; fewer is ``ShortRead``. More are left unread."""
        try:
            data = self._raw.read(length)
        except http.client.IncompleteRead as exc:
            short = ShortRead("the body ended early", len(exc.partial), length)
            raise self._fail(exc, short) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise self._fail(exc, TransportError(type(exc).__name__)) from exc
        if len(data) < length or self._deadline.expired:
            raise self._fail(None, ShortRead("the body ended early", len(data), length))
        if not self._raw.isclosed():  # bytes we did not ask for: never read them
            self._deadline.cancel()
            self._transport.drop()
        else:
            self._done()
        return data

    def discard(self) -> None:
        """Close without reading (an error response, or one whose body is not wanted)."""
        self._deadline.cancel()
        self._transport.drop()

    def _done(self) -> None:
        self._deadline.cancel()
        if self._raw.will_close:
            self._transport.drop()


class Transport:
    """GET requests to one endpoint through one kept-alive connection. Not thread-safe."""

    def __init__(
        self,
        endpoint: Endpoint,
        network: NetworkGate,
        purpose: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._network = network
        self._purpose = purpose
        self._timeout = timeout
        self._tls = tls
        self._connection: http.client.HTTPConnection | None = None
        self.requests = 0

    def _connect(self) -> http.client.HTTPConnection:
        if self._connection is None:
            endpoint = self.endpoint
            if endpoint.scheme == "https":
                context = self._tls or ssl.create_default_context()
                self._connection = http.client.HTTPSConnection(
                    endpoint.host, endpoint.port, timeout=self._timeout, context=context
                )
            else:
                self._connection = http.client.HTTPConnection(
                    endpoint.host, endpoint.port, timeout=self._timeout
                )
        return self._connection

    def abort(self) -> None:
        """Shut the connection's socket down from another thread (a deadline passed)."""
        connection = self._connection
        sock = connection.sock if connection is not None else None
        if isinstance(sock, socket.socket):
            with contextlib.suppress(OSError):  # already closed
                sock.shutdown(socket.SHUT_RDWR)

    def drop(self) -> None:
        """Close the connection; the next request opens a new one."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _send(
        self, target: str, headers: Mapping[str, str], deadline: _Deadline
    ) -> http.client.HTTPResponse:
        """One request; sent again, once, on a new connection if a kept-alive one was closed by
        the server while idle (``GET`` is idempotent, and nothing was received)."""
        for attempt in (0, 1):
            reused = self._connection is not None
            connection = self._connect()
            self.requests += 1
            try:
                connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.endheaders()
                return connection.getresponse()
            except (ConnectionResetError, BrokenPipeError) as exc:  # RemoteDisconnected too
                self.drop()
                if not (reused and attempt == 0) or deadline.expired:
                    raise deadline.error(exc, TransportError(type(exc).__name__)) from exc
            except (OSError, http.client.HTTPException) as exc:
                self.drop()
                raise deadline.error(exc, TransportError(type(exc).__name__)) from exc
        raise AssertionError("unreachable")

    def get(
        self, path: str, query: Sequence[tuple[str, str]] = (), headers: Mapping[str, str] = {}
    ) -> Response:
        """Send ``GET path?query``. ``path`` is percent-encoded already; ``query`` is raw text,
        encoded here as ``sigv4.canonical_query`` encodes it, so what is signed is what is sent.

        A ``2xx`` is returned unread. A ``3xx`` is ``RedirectRefused``; any other status is
        ``HttpStatusError``; both are raised with the body unread and the connection dropped.
        The timeout bounds each socket operation and, as a deadline, the whole request: headers
        and body together (``DeadlineExceeded``).
        """
        self._network.require_network(self._purpose)
        pairs = [(quote(k), quote(v)) for k, v in query]
        target = path + ("?" + "&".join(f"{k}={v}" if v else k for k, v in pairs) if pairs else "")
        sent = {"Host": self.endpoint.authority, "User-Agent": USER_AGENT, **headers}
        deadline = _Deadline(self, self._timeout)
        try:
            raw = self._send(target, sent, deadline)
        except BaseException:
            deadline.cancel()
            raise
        headers_got = {k.lower(): v for k, v in raw.getheaders()}
        response = Response(raw.status, headers_got, raw, _transport=self, _deadline=deadline)
        if 300 <= raw.status < 400:
            response.discard()
            raise RedirectRefused(f"status {raw.status}", raw.status)
        if not 200 <= raw.status < 300:
            response.discard()
            raise HttpStatusError(f"status {raw.status}", raw.status)
        return response
