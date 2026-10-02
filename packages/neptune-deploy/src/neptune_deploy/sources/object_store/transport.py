"""The connector's one network boundary: GET requests over ``http.client`` (ADR 0006 §1, §6).

- Every request asks the workspace first (``NetworkGate.require_network``), so a local-only
  workspace refuses each one, not only the first.
- ``GET`` is the only method ``Transport`` exposes. Nothing here can write, delete or acknowledge. A
  connector whose one read is a POST (Foxglove's ``/data/stream`` returns a download link)
  subclasses it and names that one request; ``_request`` is not a public way to send anything else.
- Redirects are never followed: ``http.client`` does not follow them, and a ``3xx`` is raised as
  ``RedirectRefused``. Following one would send the request, and its credentials, wherever the
  server says, which is the object store's symlink.
- An endpoint is ``https``, or ``http`` on a loopback address only (a local MinIO or emulator).
- Bodies are read with a bound. A body shorter than its ``Content-Length`` is ``ShortRead``.
- One connection is kept open between requests and dropped after any failure.

No error text, URL or header from here reaches a finding: the source turns each error into a code
and a status.
"""

import http.client
import ipaddress
import ssl
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


class ShortRead(TransportError):
    """The body ended before the bytes its headers promised."""

    code = "short_read"

    def __init__(self, detail: str, received: int, expected: int) -> None:
        super().__init__(detail)
        self.received = received
        self.expected = expected


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

        No user information, query or fragment: credentials never ride in a URL.
        """
        if not isinstance(url, str) or not url.isprintable() or " " in url:
            raise ValueError(f"not an endpoint URL: {url!r}")
        scheme, sep, rest = url.partition("://")
        scheme = scheme.lower()
        if not sep or scheme not in ("http", "https"):
            raise ValueError(f"an endpoint is an http(s) URL: {url!r}")
        if any(ch in rest for ch in "?#@\\"):
            raise ValueError(f"an endpoint has no query, fragment or user information: {url!r}")
        authority, _, path = rest.partition("/")
        host, port_text = authority, ""
        if authority.startswith("["):
            close = authority.find("]")
            if close < 0:
                raise ValueError(f"unclosed IPv6 address: {url!r}")
            host, port_text = authority[1:close], authority[close + 1 :].removeprefix(":")
        elif ":" in authority:
            host, _, port_text = authority.rpartition(":")
        if not host or (":" in host and not authority.startswith("[")):
            raise ValueError(f"an endpoint names one host (IPv6 in brackets): {url!r}")
        host = host.lower()
        port = 443 if scheme == "https" else 80
        if port_text:
            if not port_text.isdigit() or not 0 < int(port_text) < 65536:
                raise ValueError(f"not a port: {port_text!r}")
            port = int(port_text)
        if scheme == "http" and not _is_loopback(host):
            raise ValueError(f"plain http is allowed to a loopback host only: {url!r}")
        base = "/" + path.strip("/") if path.strip("/") else ""
        return cls(scheme, host, port, base)

    @property
    def authority(self) -> str:
        """The ``Host`` header: the port only when it is not the scheme's default."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"


@dataclass
class Response:
    """A response whose body has not been read yet. Read it once, through ``body`` or ``exact``."""

    status: int
    headers: dict[str, str]  # lower-cased names; a repeated header keeps its last value
    _raw: http.client.HTTPResponse
    _transport: "Transport"

    def body(self, limit: int) -> bytes:
        """The whole body, refused past ``limit`` bytes."""
        try:
            data = self._raw.read(limit + 1)
        except http.client.IncompleteRead as exc:
            self._transport.drop()
            raise ShortRead("the body ended early", len(exc.partial), -1) from exc
        except (OSError, http.client.HTTPException) as exc:
            self._transport.drop()
            raise TransportError(type(exc).__name__) from exc
        if len(data) > limit:
            self._transport.drop()
            raise ResponseTooLarge(f"more than {limit} bytes", self.status)
        self._done()
        return data

    def exact(self, length: int) -> bytes:
        """Exactly ``length`` bytes of body; fewer is ``ShortRead``. More are left unread."""
        try:
            data = self._raw.read(length)
        except http.client.IncompleteRead as exc:
            self._transport.drop()
            raise ShortRead("the body ended early", len(exc.partial), length) from exc
        except (OSError, http.client.HTTPException) as exc:
            self._transport.drop()
            raise TransportError(type(exc).__name__) from exc
        if len(data) < length:
            self._transport.drop()
            raise ShortRead("the body ended early", len(data), length)
        if not self._raw.isclosed():  # bytes we did not ask for: never read them
            self._transport.drop()
        else:
            self._done()
        return data

    def discard(self) -> None:
        """Close without reading (an error response, or one whose body is not wanted)."""
        self._transport.drop()

    def _done(self) -> None:
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
        user_agent: str = USER_AGENT,
    ) -> None:
        self.endpoint = endpoint
        self._network = network
        self._purpose = purpose
        self._timeout = timeout
        self._tls = tls
        self._user_agent = user_agent
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

    def drop(self) -> None:
        """Close the connection; the next request opens a new one."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _send(
        self,
        target: str,
        headers: Mapping[str, str],
        method: str = "GET",
        body: bytes | None = None,
    ) -> http.client.HTTPResponse:
        """One request; sent again, once, on a new connection if a kept-alive one was closed by
        the server while idle (the request is a read, and nothing was received)."""
        for attempt in (0, 1):
            reused = self._connection is not None
            connection = self._connect()
            self.requests += 1
            try:
                connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
                for name, value in headers.items():
                    connection.putheader(name, value)
                if body is not None:
                    connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body)
                return connection.getresponse()
            except (ConnectionResetError, BrokenPipeError) as exc:  # RemoteDisconnected too
                self.drop()
                if not (reused and attempt == 0):
                    raise TransportError(type(exc).__name__) from exc
            except (OSError, http.client.HTTPException) as exc:
                self.drop()
                raise TransportError(type(exc).__name__) from exc
        raise AssertionError("unreachable")

    def get(
        self, path: str, query: Sequence[tuple[str, str]] = (), headers: Mapping[str, str] = {}
    ) -> Response:
        """Send ``GET path?query``. ``path`` is percent-encoded already; ``query`` is raw text,
        encoded here as ``sigv4.canonical_query`` encodes it, so what is signed is what is sent.

        A ``2xx`` is returned unread. A ``3xx`` is ``RedirectRefused``; any other status is
        ``HttpStatusError``; both are raised with the body unread and the connection dropped.
        """
        return self._request("GET", path, query, headers)

    def _request(
        self,
        method: str,
        path: str,
        query: Sequence[tuple[str, str]] = (),
        headers: Mapping[str, str] = {},
        body: bytes | None = None,
    ) -> Response:
        self._network.require_network(self._purpose)
        pairs = [(quote(k), quote(v)) for k, v in query]
        target = path + ("?" + "&".join(f"{k}={v}" if v else k for k, v in pairs) if pairs else "")
        sent = {"Host": self.endpoint.authority, "User-Agent": self._user_agent, **headers}
        raw = self._send(target, sent, method, body)
        response = Response(
            raw.status, {k.lower(): v for k, v in raw.getheaders()}, raw, _transport=self
        )
        if 300 <= raw.status < 400:
            response.discard()
            raise RedirectRefused(f"status {raw.status}", raw.status)
        if not 200 <= raw.status < 300:
            response.discard()
            raise HttpStatusError(f"status {raw.status}", raw.status)
        return response
