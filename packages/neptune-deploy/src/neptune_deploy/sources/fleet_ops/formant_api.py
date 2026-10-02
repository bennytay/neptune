"""Formant's admin API as a read-only client: five queries, one ``POST`` shape (ADR 0010 §2).

Everything here reads. The requests are:

- ``POST /v1/admin/<route>/query`` for ``devices``, ``events``, ``annotations``,
  ``intervention-requests`` and ``files``: one page of what the organisation holds. Formant's
  documented lists are queries with a JSON filter, so they are ``POST``; they change nothing, and
  they are the only ``POST`` this module can send (``QueryTransport.post_query`` refuses any other
  path, and any method but ``GET`` and that one ``POST`` does not exist here).

A page is ``{"items": [...], "continuationToken": "..."}``. The bearer token goes to the declared
endpoint and nowhere else. Hostile-input rules are ADR 0006 §6 and §8 as for the object stores: no
redirect is followed; every response is bounded, strict JSON; a continuation token is bounded and a
repeated one stops the listing; each request has a deadline; errors name no URL, token or header.

The response shapes are written from Formant's public API documentation. They have not been run
against a live tenant (ADR 0010 §8).
"""

import hashlib
import http.client
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.fleet_ops.documents import DocumentInvalid, dumps, parse_json
from neptune_deploy.sources.object_store.transport import (
    Endpoint,
    NetworkGate,
    Response,
    Transport,
    TransportError,
    _Deadline,
)

DEFAULT_ENDPOINT: Final = "https://api.formant.io"
MAX_PAGE_BYTES: Final = 8 * 1024 * 1024
MAX_PAGES: Final = 100_000
MAX_CURSOR_BYTES: Final = 4096
MAX_TOKEN_BYTES: Final = 4096
ROUTES: Final = {
    "devices": "devices",
    "events": "events",
    "annotations": "annotations",
    "interventions": "intervention-requests",
    "recordings": "files",
}
_QUERY_ROUTE: Final = re.compile(
    r"/v1/admin/(devices|events|annotations|intervention-requests|files)/query"
)
_TOKEN: Final = re.compile(r"[\x21-\x7e]{1,4096}")


def valid_token(token: str) -> bool:
    """Printable ASCII, no space: a bearer token never carries a header break."""
    return isinstance(token, str) and _TOKEN.fullmatch(token) is not None


class QueryTransport(Transport):
    """``Transport`` that can also send the documented read-only queries, as ``POST``."""

    def __init__(
        self, endpoint: Endpoint, network: NetworkGate, purpose: str, *, timeout: float
    ) -> None:
        super().__init__(endpoint, network, purpose, timeout=timeout)
        self._method = "GET"
        self._body: bytes | None = None

    def post_query(self, path: str, body: bytes, headers: Mapping[str, str]) -> Response:
        """``POST`` ``body`` to one of the five query routes, and nowhere else."""
        route = path.removeprefix(self.endpoint.base_path)
        if not _QUERY_ROUTE.fullmatch(route):
            raise ValueError("POST is sent to the five documented query routes only")
        self._method, self._body = "POST", body
        try:
            return self.get(
                path,
                (),
                {**headers, "Content-Type": "application/json", "Content-Length": str(len(body))},
            )
        finally:
            self._method, self._body = "GET", None

    def _send(
        self, target: str, headers: Mapping[str, str], deadline: _Deadline
    ) -> http.client.HTTPResponse:
        # Transport._send with a method and a body. A kept-alive connection the server closed while
        # idle is reopened once: a query is idempotent, and nothing was received.
        for attempt in (0, 1):
            reused = self._connection is not None
            connection = self._connect()
            self.requests += 1
            try:
                connection.putrequest(
                    self._method, target, skip_host=True, skip_accept_encoding=True
                )
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.endheaders(self._body)
                return connection.getresponse()
            except (ConnectionResetError, BrokenPipeError) as exc:
                self.drop()
                if not (reused and attempt == 0) or deadline.expired:
                    raise deadline.error(exc, TransportError(type(exc).__name__)) from exc
            except (OSError, http.client.HTTPException) as exc:
                self.drop()
                raise deadline.error(exc, TransportError(type(exc).__name__)) from exc
        raise AssertionError("unreachable")


@dataclass
class Records:
    """Records read from a paged query, and whether all of them were."""

    items: list[JsonValue] = field(default_factory=list)
    complete: bool = False
    stopped: str | None = None  # a cause when not complete
    status: int | None = None
    pages: int = 0


class FormantApi:
    """One organisation's read-only queries."""

    def __init__(
        self,
        organization: str,
        endpoint: Endpoint,
        network: NetworkGate,
        *,
        token: str,
        timeout: float,
    ) -> None:
        self.organization = organization
        self._endpoint = endpoint
        self.transport = QueryTransport(
            endpoint, network, "reading deploy_formant sources", timeout=timeout
        )
        self._headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def __repr__(self) -> str:
        return f"FormantApi({self.organization})"

    def _page(self, response: Response) -> tuple[list[JsonValue], str | None, int]:
        body = response.body(MAX_PAGE_BYTES)
        try:
            document = parse_json(body)
        except DocumentInvalid as exc:
            raise ValueError("a response is not strict JSON") from exc
        if not isinstance(document, Mapping) or not isinstance(document.get("items"), list):
            raise ValueError("a page has no items")
        items = list(document["items"])  # type: ignore[arg-type]
        token = document.get("continuationToken")
        if token is None or token == "":
            return items, None, len(body)
        if not isinstance(token, str):
            raise ValueError("a continuation token is not text")
        if len(token.encode("utf-8", "surrogateescape")) > MAX_CURSOR_BYTES:
            raise ValueError(f"a continuation token is longer than {MAX_CURSOR_BYTES} bytes")
        return items, token, len(body)

    def query(
        self,
        part: str,
        filters: Mapping[str, JsonValue],
        *,
        page_size: int,
        limit: int,
        budget: int,
    ) -> Records:
        """Every record of one part, up to ``limit`` records and ``budget`` bytes."""
        path = self._endpoint.base_path + f"/v1/admin/{ROUTES[part]}/query"
        found = Records()
        seen: set[bytes] = set()
        token: str | None = None
        used = 0
        while True:
            if found.pages >= MAX_PAGES:
                found.stopped = "page_limit"
                return found
            request: dict[str, JsonValue] = {
                "organizationId": self.organization,
                **filters,
                "limit": page_size,
            }
            if token is not None:
                request["continuationToken"] = token
            try:
                items, token, size = self._page(
                    self.transport.post_query(path, dumps(request), self._headers)
                )
            except (TransportError, ValueError) as exc:
                found.stopped = exc.code if isinstance(exc, TransportError) else "response_invalid"
                found.status = exc.status if isinstance(exc, TransportError) else None
                return found
            used += size
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


def filters_for(
    part: str, since: str | None, until: str | None, devices: Sequence[str]
) -> dict[str, JsonValue]:
    """The declared filter a part's query carries: a window and device ids, never more."""
    out: dict[str, JsonValue] = {}
    if part != "devices":
        if since is not None:
            out["from"] = since
        if until is not None:
            out["to"] = until
    if devices:
        out["ids" if part == "devices" else "deviceIds"] = list(devices)
    return out
