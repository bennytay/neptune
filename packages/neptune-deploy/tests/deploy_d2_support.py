"""What the D2 gate observes about a connector, whatever the connector (MVL-158).

- ``Wire`` records every request ``http.client`` starts in this process (host, port, method and
  target), so a test can show that a connector reached nothing but the gate's proxy, and, against
  an emulator or a live tenant where no proxy is in the way, which methods it sent.
- ``NoSockets`` makes any socket connection in this process fail loudly: a local connector (or a
  mapper) that opened one would fail the test.
- ``emitted(source)`` is one canonical text of everything a source hands onwards: its entries'
  external identities, findings, transform (config and config hash), catalog and declared records
  (their provenance included), relations, cursor and repr. A credential found in it has leaked.
- ``fingerprint`` does what the compiler's scan does with a walk: digest each entry and observe it
  in the ledger.
"""

import base64
import http.client
import json
import re
import socket
import urllib.parse
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from deploy_d2_proxy import Upstream
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune.model.source import SourceArtifact
from neptune_deploy.sources.fleet_ops import DocumentEntry
from neptune_deploy.sources.object_store import ObjectEntry
from neptune_deploy.sources.records import RecordEntry

ENTRY_TYPES = (ObjectEntry, RecordEntry, DocumentEntry)


@dataclass(frozen=True)
class Sent:
    host: str
    port: int
    method: str
    target: str


@dataclass
class Wire:
    sent: list[Sent] = field(default_factory=list)

    @contextmanager
    def recording(self) -> Iterator["Wire"]:
        original = http.client.HTTPConnection.putrequest
        wire = self

        def putrequest(connection: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            if not isinstance(connection, Upstream):
                wire.sent.append(Sent(connection.host, connection.port, method, url))
            return original(connection, method, url, *args, **kwargs)

        http.client.HTTPConnection.putrequest = putrequest  # type: ignore[method-assign,assignment]
        try:
            yield self
        finally:
            http.client.HTTPConnection.putrequest = original  # type: ignore[method-assign]

    def methods(self) -> set[str]:
        return {sent.method for sent in self.sent}


class SocketUsed(AssertionError):
    pass


@contextmanager
def no_sockets() -> Iterator[None]:
    """Any ``connect`` in this process raises: proves code under it uses no network."""
    original = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise SocketUsed("a socket connection was attempted")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = original  # type: ignore[method-assign]


def entries(walked: Iterable[Any]) -> list[Any]:
    return [entry for entry in walked if isinstance(entry, ENTRY_TYPES)]


def location_of(item: Any) -> ExternalObjectRef:
    """An entry's, a recording's, or an ``(entry, revision)`` pair's external identity."""
    if isinstance(item, tuple):
        item = item[0]
    found = item.location
    assert isinstance(found, ExternalObjectRef)
    return found


def fingerprint(source: Any, ledger: SourceLedger) -> dict[str, SourceArtifact]:
    """What the compiler's scan does with a walk: digest each entry, observe it in the ledger."""
    artifacts: dict[str, SourceArtifact] = {}
    for entry in entries(source.walk()):
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=1024 * 1024)
        ledger.observe(entry.location, artifact)
        artifacts[entry.location.object_id] = artifact
    return artifacts


def _json(value: Any) -> Any:
    if hasattr(value, "to_json"):
        return value.to_json()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return repr(value)


def catalog_json(catalog: Any) -> list[Any]:
    """A stated catalog (Roboto, Rerun) or a fleet-ops catalog as JSON: documents and records."""
    out: list[Any] = []
    for document in catalog.documents:
        out.append({"ref": document.ref.to_json(), "data": document.data.decode("utf-8")})
    for name in ("records", "tables", "rows", "domains"):
        out += [_json(record) for record in getattr(catalog, name, ())]
    return out


def emitted(source: Any, *, walked: list[Any] | None = None) -> str:
    """Everything ``source`` hands onwards, as one canonical text (see the module docstring)."""
    walked = list(source.walk()) if walked is None else walked
    document: dict[str, Any] = {
        "walk": [
            {"location": entry.location.to_json(), "size": entry.size}
            if isinstance(entry, ENTRY_TYPES)
            else repr(entry)
            for entry in walked
        ],
        "findings": [finding.to_json() for finding in source.findings()],
        "transform": source.transform.to_json(),
        "repr": re.sub(r" at 0x[0-9a-f]+", "", repr(source)),
    }
    if hasattr(source, "catalog"):
        document["catalog"] = catalog_json(source.catalog())
    if hasattr(source, "declared"):
        document["declared"] = [
            source.declared(entry.location).to_json()
            for entry in walked
            if isinstance(entry, ObjectEntry)
        ]
    if hasattr(source, "relations"):
        document["relations"] = [repr(relation) for relation in source.relations()]
    if hasattr(source, "cursor"):
        document["cursor"] = source.cursor
    if hasattr(source, "ingest_options"):
        document["ingest_options"] = source.ingest_options()
    # Not canonical JSON: a cursor may be absent (``None``), which canonical JSON forbids.
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def ledger_json(ledger: SourceLedger) -> str:
    """The ledger's revisions and artifacts: what provenance cites for every fetched object."""
    return json.dumps(
        {
            "revisions": [revision.to_json() for revision in ledger.revisions()],
            "absences": [absence.to_json() for absence in ledger.absences()],
        },
        sort_keys=True,
    )


def spellings(secret: str, *, user: str | None = None) -> set[str]:
    """``secret`` as it could appear anywhere: verbatim, percent-encoded, and (with ``user``) as
    the base64 of an HTTP Basic credential."""
    found = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
    if user is not None:
        found.add(base64.b64encode(f"{user}:{secret}".encode()).decode())
    return found


def exception_texts(exc: BaseException | None) -> str:
    """Every message, repr and argument along an exception's cause and context chain."""
    parts: list[str] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts += [str(exc), repr(exc), repr(exc.args), repr(getattr(exc, "__dict__", {}))]
        exc = exc.__cause__ or exc.__context__
    return "\n".join(parts)
