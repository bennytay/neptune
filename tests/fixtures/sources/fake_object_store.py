"""An object store and its connector, in process, for the compiler's connector tests (ADR 0067).

No socket is opened: a store is a directory. ``objects/<key>`` holds each object's bytes and
``store.json`` its revision token (an etag or a version id) and whether reading it fails. The
connector ``fake_store`` reads ``fake://<bucket>/<prefix>`` with ``options={"store": <dir>}`` and
has the shape of Deploy's object-store connector (MVL-153): its own entry and error types, a
``discover`` that, like Deploy's, compares only the token its ledger head was first seen with,
lazy ranged reads, and findings of its own.

``READS`` logs every ranged read the connector serves, ``(object id, offset, length)``, so a test
can assert what was (and was not) fetched. ``put``, ``remove`` and ``retoken`` change a store as
an upload, a deletion and a re-upload of the same bytes would.

Installed as a plugin distribution by the tests (``make_plugin_dists.install``), or imported
directly.
"""

import io
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, BinaryIO

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.model.provenance import TransformRecord
from neptune.model.source import SourceLocation, SourceRevision

CONNECTOR = "fake_store"
READS: list[tuple[str, int, int]] = []
BUILT: list[str] = []  # every URI a source was built for


# --- The store -----------------------------------------------------------------------------------


def _index(store: Path) -> dict[str, Any]:
    path = Path(store) / "store.json"
    return json.loads(path.read_text()) if path.exists() else {"objects": {}}


def _save(store: Path, index: dict[str, Any]) -> None:
    (Path(store) / "store.json").write_text(json.dumps(index, sort_keys=True))


def put(store: Path, key: str, data: bytes, token: str, *, fail: bool = False) -> None:
    """Upload ``data`` under ``key`` with revision ``token``."""
    path = Path(store) / "objects" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    index = _index(store)
    index["objects"][key] = {"fail": fail, "token": token}
    _save(store, index)


def retoken(store: Path, key: str, token: str) -> None:
    """Re-upload ``key``'s bytes unchanged: a new token over the same bytes."""
    index = _index(store)
    index["objects"][key]["token"] = token
    _save(store, index)


def remove(store: Path, key: str) -> None:
    index = _index(store)
    del index["objects"][key]
    _save(store, index)
    (Path(store) / "objects" / key).unlink()


def set_incomplete(store: Path, incomplete: bool) -> None:
    """Make the next listing stop early: nothing it does not hold may be called gone."""
    index = _index(store)
    index["incomplete"] = incomplete
    _save(store, index)


# --- The connector -------------------------------------------------------------------------------


class ObjectReadError(OSError):
    def __init__(self, code: str, location: ExternalObjectRef) -> None:
        super().__init__(f"{location.connector_id}: {code}")
        self.code = code
        self.location = location


@dataclass(frozen=True)
class Entry:
    location: ExternalObjectRef
    size: int
    key: str


@dataclass(frozen=True)
class Discovery:
    new: tuple[Entry, ...]
    changed: tuple[Entry, ...]
    unchanged: tuple[tuple[Entry, SourceRevision], ...]
    gone: tuple[SourceRevision, ...]
    complete: bool


class FakeSource:
    """One bucket prefix of a store directory, read only."""

    def __init__(self, bucket: str, prefix: str, store: Path) -> None:
        self.connector_id = CONNECTOR
        self.bucket, self.prefix, self.store = bucket, prefix, Path(store)
        self._findings: dict[str, IngestFinding] = {}

    @cached_property
    def transform(self) -> TransformRecord:
        config = {"bucket": self.bucket, "prefix": self.prefix}
        return transform_record(adapter_id=CONNECTOR, adapter_version="0.1.0", config=config)

    def findings(self) -> tuple[IngestFinding, ...]:
        return tuple(self._findings[key] for key in sorted(self._findings))

    def _report(self, code: str, location: ExternalObjectRef) -> None:
        finding = ingest_finding(
            code=f"{CONNECTOR}.{code}",
            category=FindingCategory.FAILED,
            severity=Severity.ERROR,
            subject=location,
            transform=self.transform,
            message="a ranged read failed",
            details={},
        )
        self._findings[finding.id] = finding

    @cached_property
    def _listing(self) -> tuple[tuple[Entry, ...], bool]:
        index = _index(self.store)
        entries = []
        for key, item in sorted(index["objects"].items()):
            if key.startswith(self.prefix):
                size = (self.store / "objects" / key).stat().st_size
                ref = ExternalObjectRef(CONNECTOR, f"{self.bucket}/{key}", item["token"])
                entries.append(Entry(ref, size, key))
        if index.get("incomplete"):
            return tuple(entries[: len(entries) // 2]), False
        return tuple(entries), True

    def discover(self, ledger: SourceLedger) -> Discovery:
        entries, complete = self._listing
        new, changed, unchanged = [], [], []
        for entry in entries:
            head = ledger.head(entry.location)
            if not isinstance(head, SourceRevision):
                new.append(entry)
            elif (
                isinstance(head.location, ExternalObjectRef)
                and head.location.revision_token == entry.location.revision_token
            ):
                unchanged.append((entry, head))
            else:
                changed.append(entry)  # only the first token is compared, as Deploy's does
        gone = []
        if complete:
            listed = {entry.location.object_id for entry in entries}
            scope = f"{self.bucket}/{self.prefix}"
            for head in ledger.heads():
                where = head.location
                if (
                    isinstance(head, SourceRevision)
                    and isinstance(where, ExternalObjectRef)
                    and where.connector_id == CONNECTOR
                    and where.object_id.startswith(scope)
                    and where.object_id not in listed
                ):
                    gone.append(head)
        return Discovery(tuple(new), tuple(changed), tuple(unchanged), tuple(gone), complete)

    def walk(self) -> Iterator[Entry]:
        yield from self._listing[0]

    def open(self, location: SourceLocation) -> BinaryIO:
        for entry in self._listing[0]:
            if entry.location == location:
                return io.BufferedReader(_Ranged(self, entry), 64 * 1024)
        raise TypeError(f"{CONNECTOR} cannot open {location!r}")

    def fetch(self, entry: Entry, start: int, length: int) -> bytes:
        index = _index(self.store)
        item = index["objects"].get(entry.key)
        if item is None or item["token"] != entry.location.revision_token:
            self._report("object_changed", entry.location)
            raise ObjectReadError("object_changed", entry.location)
        if item.get("fail"):
            self._report("read_failed", entry.location)
            raise ObjectReadError("read_failed", entry.location)
        READS.append((entry.location.object_id, start, length))
        with (self.store / "objects" / entry.key).open("rb") as stream:
            stream.seek(start)
            return stream.read(length)


class _Ranged(io.RawIOBase):
    def __init__(self, source: FakeSource, entry: Entry) -> None:
        super().__init__()
        self._source, self._entry, self._pos = source, entry, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._entry.size}[whence]
        self._pos = base + offset
        return self._pos

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        want = min(len(view), self._entry.size - self._pos)
        if want <= 0:
            return 0
        data = self._source.fetch(self._entry, self._pos, want)
        view[: len(data)] = data
        self._pos += len(data)
        return len(data)


def make(
    url: str, *, network: Any, options: Mapping[str, Any] | None = None, **_: Any
) -> FakeSource:
    """``fake://<bucket>/<prefix>``; ``options["store"]`` is the store directory."""
    network.require_network(f"reading {CONNECTOR} sources")
    if not url.startswith("fake://"):
        raise ValueError(f"not a fake:// URL: {url}")
    if not options or "store" not in options:
        raise ValueError("options name the store directory")
    bucket, _slash, prefix = url.removeprefix("fake://").partition("/")
    BUILT.append(url)
    return FakeSource(bucket, prefix, Path(str(options["store"])))


make.schemes = ("fake",)  # type: ignore[attr-defined]
