"""The Foxglove Data Platform API, as far as a read-only connector uses it (ADR 0007 §2).

Five calls, all documented at https://docs.foxglove.dev/docs/api (the REST reference):

- ``GET /recordings``: the index. ``limit`` (at most 2000) and ``offset`` page it.
- ``GET /recordings/{keyOrId}``: one recording; used to check that a recording missing from a
  listing is really gone.
- ``GET /devices``: device names, ids and properties.
- ``GET /data/topics?recordingId=``: the topics a recording declares.
- ``POST /data/stream``: *returns a link*, not data. The link is a signed URL that expires after 15
  seconds, and a ``GET`` of it serves the recording as MCAP. Nothing is written or changed by it; it
  is the API's only way to read recording bytes, so it is the one ``POST`` this module can send.

The link is untrusted input. It is used only if it is ``https`` (or ``http`` to loopback) at the
API's own host or a declared ``link_hosts`` entry. It carries no credentials of ours, a redirect
from it is never followed, and a ranged read of it must answer with exactly the bytes asked for.

Bodies are bounded and parsed as strict JSON: duplicate keys, ``NaN`` and ``Infinity``, and
documents nested beyond Python's recursion limit are ``response_invalid``.
"""

import json
from collections.abc import Sequence
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.foxglove.config import valid_id
from neptune_deploy.sources.object_store.clients import Range, RangeInvalid, read_range
from neptune_deploy.sources.object_store.sigv4 import quote
from neptune_deploy.sources.object_store.transport import (
    DEFAULT_TIMEOUT,
    Endpoint,
    HttpStatusError,
    NetworkGate,
    Response,
    Transport,
    TransportError,
)

MAX_BODY: Final = 32 * 1024 * 1024  # bytes of one JSON response
MAX_LINK_RESPONSE: Final = 64 * 1024
MAX_LINK: Final = 8192  # characters in a download link
STREAM_PATH: Final = "/data/stream"
USER_AGENT: Final = "neptune-deploy-foxglove/0.1.0"


class ResponseInvalid(TransportError):
    """A body that is not the strict JSON this client reads, or not the shape the API documents."""

    code = "response_invalid"


class LinkRefused(TransportError):
    """The API named a download link this connector will not use."""

    code = "link_refused"


def _no_constant(token: str) -> JsonValue:
    raise ValueError(f"{token} is not JSON")


def _no_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate object key")
    return result


def parse_json(body: bytes) -> JsonValue:
    """``body`` as strict JSON: UTF-8, no duplicate keys, no ``NaN``/``Infinity``."""
    try:
        text = body.decode("utf-8").removeprefix("﻿")
        value: JsonValue = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError too
        raise ResponseInvalid("not strict JSON") from None  # nothing of the body is repeated
    return value


class FoxgloveTransport(Transport):
    """``Transport`` plus the one ``POST`` this connector sends: a request for a download link."""

    def __init__(
        self,
        endpoint: Endpoint,
        network: NetworkGate,
        purpose: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(endpoint, network, purpose, timeout=timeout, user_agent=USER_AGENT)

    def post_stream_request(self, document: JsonValue, headers: dict[str, str]) -> Response:
        """``POST <base>/data/stream`` with a JSON body. No other request is sent by a ``POST``."""
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path = self.endpoint.base_path + STREAM_PATH
        return self._request(
            "POST", path, (), {**headers, "Content-Type": "application/json"}, body
        )


def parse_link(link: JsonValue, api: Endpoint, link_hosts: Sequence[str]) -> tuple[Endpoint, str]:
    """The endpoint and request target of a download link, or ``LinkRefused``.

    Allowed: ``https`` (or ``http`` to loopback, as for every endpoint) at the API's own host and
    port, or at a host the operator declared in ``link_hosts``. Never user information, a fragment,
    a backslash, a space or a non-ASCII character. The query is kept verbatim: it is the signature.
    """
    if not isinstance(link, str) or not link or len(link) > MAX_LINK:
        raise LinkRefused("the link is not usable text")
    if not link.isascii() or not link.isprintable() or any(c in link for c in " \\#"):
        raise LinkRefused("the link holds characters a URL does not")
    scheme, sep, rest = link.partition("://")
    authority, slash, tail = rest.partition("/")
    if not sep or not slash:
        raise LinkRefused("the link is not an absolute http(s) URL with a path")
    try:
        endpoint = Endpoint.parse(f"{scheme}://{authority}")
    except ValueError:
        raise LinkRefused("the link's address is not allowed") from None
    same_api = (endpoint.scheme, endpoint.host, endpoint.port) == (api.scheme, api.host, api.port)
    if not same_api and endpoint.host not in link_hosts:
        raise LinkRefused("the link points at a host that was not declared")
    return endpoint, "/" + tail


class FoxgloveClient:
    """Reads from one Foxglove API endpoint with one API key. Not thread-safe."""

    def __init__(
        self,
        transport: FoxgloveTransport,
        api_key: str,
        network: NetworkGate,
        purpose: str,
        *,
        link_hosts: Sequence[str] = (),
        compression: str = "lz4",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.transport = transport
        self._api_key = api_key
        self._network = network
        self._purpose = purpose
        self._link_hosts = tuple(link_hosts)
        self._compression = compression
        self._timeout = timeout
        self._link_transports: dict[Endpoint, Transport] = {}

    def __repr__(self) -> str:
        return f"FoxgloveClient({self.transport.endpoint.authority})"

    # --- Documents -------------------------------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    def _document(self, path: str, query: Sequence[tuple[str, str]]) -> JsonValue:
        base = self.transport.endpoint.base_path
        response = self.transport.get(base + path, query, self._auth())
        return parse_json(response.body(MAX_BODY))

    def _array(self, path: str, query: Sequence[tuple[str, str]]) -> list[JsonValue]:
        document = self._document(path, query)
        if not isinstance(document, list):
            raise ResponseInvalid("a listing is a JSON array")
        return document

    def recordings(
        self, filters: Sequence[tuple[str, str]], offset: int, limit: int
    ) -> list[JsonValue]:
        """One page of the index, oldest import first (``sortBy=createdAt``), from ``offset``."""
        query = [
            *filters,
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("sortBy", "createdAt"),
            ("sortOrder", "asc"),
        ]
        return self._array("/recordings", query)

    def recording(self, recording_id: str) -> "JsonValue | None":
        """One recording, or ``None`` if the API says it does not exist (404)."""
        if not valid_id(recording_id):
            raise ValueError("not a recording id")
        try:
            return self._document(f"/recordings/{quote(recording_id)}", ())
        except HttpStatusError as exc:
            if exc.status == 404:
                return None
            raise

    def devices(self, project: str | None, offset: int, limit: int) -> list[JsonValue]:
        query = [("limit", str(limit)), ("offset", str(offset))]
        if project is not None:
            query.append(("projectId", project))
        return self._array("/devices", query)

    def topics(self, recording_id: str, offset: int, limit: int) -> list[JsonValue]:
        """The topics ``recording_id`` declares. ``recordingId`` alone, as the API requires for a
        recording that is not imported yet; the schemas (``includeSchemas``) are not requested."""
        query = [("recordingId", recording_id), ("limit", str(limit)), ("offset", str(offset))]
        return self._array("/data/topics", query)

    # --- Bytes -----------------------------------------------------------------------------------

    def stream_link(self, recording_id: str) -> str:
        """A fresh download link for ``recording_id`` as MCAP (valid for a few seconds)."""
        request: JsonValue = {
            "compressionFormat": self._compression,
            "includeAttachments": True,
            "outputFormat": "mcap",
            "recordingId": recording_id,
        }
        response = self.transport.post_stream_request(request, self._auth())
        document = parse_json(response.body(MAX_LINK_RESPONSE))
        if not isinstance(document, dict) or "link" not in document:
            raise ResponseInvalid("a stream response holds a link")
        link = document["link"]
        if not isinstance(link, str):
            raise ResponseInvalid("a stream response holds a link")
        return link

    def _link_transport(self, endpoint: Endpoint) -> Transport:
        api = self.transport.endpoint
        if (endpoint.scheme, endpoint.host, endpoint.port) == (api.scheme, api.host, api.port):
            return self.transport  # one connection, and the key is only ever sent by ``_auth``
        if endpoint not in self._link_transports:
            if len(self._link_transports) >= 8:
                for old in self._link_transports.values():
                    old.drop()
                self._link_transports.clear()
            self._link_transports[endpoint] = Transport(
                endpoint, self._network, self._purpose, timeout=self._timeout, user_agent=USER_AGENT
            )
        return self._link_transports[endpoint]

    def get_range(self, link: str, start: int, length: int) -> Range:
        """``length`` bytes from ``start`` of the stream ``link`` serves, with no credentials sent.

        The answer must be a ``206`` for exactly that range, or a ``200`` from offset 0 that states
        its length (only ``length`` bytes are read, and the connection is dropped). A body the
        server encoded (``Content-Encoding``) has no byte positions and is refused.
        """
        endpoint, target = parse_link(link, self.transport.endpoint, self._link_hosts)
        headers = {"Range": f"bytes={start}-{start + length - 1}", "Accept-Encoding": "identity"}
        response = self._link_transport(endpoint).get(target, (), headers)
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            response.discard()
            raise RangeInvalid("the stream is content-encoded: it has no byte positions")
        return read_range(response, start, length)

    def drop(self) -> None:
        """Close every connection."""
        self.transport.drop()
        for transport in self._link_transports.values():
            transport.drop()
