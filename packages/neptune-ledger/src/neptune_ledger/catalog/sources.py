"""Re-hash a package's referenced sources where it says they are (MVL-90; Ledger ADR 0007).

A referenced source stays outside the package; the package records where it was seen as
``source_revision`` locations relative to the ingest root (root ADRs 0009, 0010). On request, the
Ledger looks for those bytes under one or more read-only **source stores** (a local directory, or
an S3-compatible bucket and prefix), and reports each location as ``present``, ``changed``,
``absent`` or ``moved`` (absent there, but intact at a location another registered package states).
It reads only; it never records what it found, so the catalog stays a function of packages and the
registration log (ADR 0002 §4).

``SourceStore`` is the whole read-only interface: ``open`` a root-relative path, or None when no
object is there. ``LocalSourceStore`` serves a directory. An S3-compatible store implements the same
two methods with ``GetObject`` on ``prefix + path``; this package ships no S3 client (ADR 0007).
"""

import errno
import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, Protocol

from neptune.identity import canonical_json
from neptune.model.knowledge import Knowledge
from neptune_ledger.api.types import CatalogFinding, TransactionKey

_READ_SIZE: Final = 1024 * 1024

SourceState = Literal["absent", "changed", "moved", "present", "unsupported"]


class SourceStore(Protocol):
    """A read-only root that referenced sources' locations are relative to."""

    def describe(self) -> str:
        """A name for this store in a report's detail text, for example its URL."""
        ...

    def open(self, path: bytes) -> BinaryIO | None:
        """The object at ``path`` (``/``-separated, no ``.``, ``..`` or empty parts), or None."""
        ...


class LocalSourceStore:
    """A local directory as a source store. A path that resolves outside it is not there."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    def describe(self) -> str:
        return str(self._root)

    def open(self, path: bytes) -> BinaryIO | None:
        target = (self._root / os.fsdecode(path)).resolve()
        if not target.is_relative_to(self._root):
            return None
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.ELOOP):
                return None
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):  # a FIFO or device is never read
            os.close(descriptor)
            return None
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, "rb")


@dataclass(frozen=True)
class SourceCheck:
    """One stated location of one referenced source, as found in the stores.

    ``location`` is the location's canonical JSON as the package states it. ``found_at`` is set
    for ``moved``: the canonical JSON of the location, stated by another registered package, where
    the bytes were found intact.
    """

    content_id: str
    location: str
    state: SourceState
    detail: str
    found_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "content_id": self.content_id,
            "detail": self.detail,
            "location": canonical_json.loads(self.location.encode("utf-8")),
            "state": self.state,
        }
        if self.found_at is not None:
            out["found_at"] = canonical_json.loads(self.found_at.encode("utf-8"))
        return out


@dataclass(frozen=True)
class SourceReport:
    """``verify_sources``: every current location of the package's referenced sources.

    ``findings`` carries request problems with catalog-API codes (``unknown_package``,
    ``as_of_out_of_range``, ``invalid_request``); then ``checks`` is empty.
    """

    package_id: str
    as_of: Knowledge[TransactionKey]
    checks: tuple[SourceCheck, ...]
    findings: tuple[CatalogFinding, ...]

    def to_json(self) -> dict[str, Any]:
        from neptune_ledger.api.codec import to_json

        return {
            "as_of": to_json(self.as_of),
            "checks": [check.to_json() for check in self.checks],
            "findings": [to_json(finding) for finding in self.findings],
            "package_id": self.package_id,
        }


@dataclass(frozen=True)
class Stated:
    """A source a package references, its size, and the locations to look at, in order."""

    content_id: str
    size: int
    locations: tuple[str, ...]  # this package's current locations (ADR 0006 §5)
    elsewhere: tuple[str, ...]  # other packages' current locations, by registration order


def check_sources(
    stated: Sequence[Stated], stores: Sequence[SourceStore]
) -> tuple[SourceCheck, ...]:
    """Look for every stated location in the stores, in order; report each one."""
    out: list[SourceCheck] = []
    for source in stated:
        for location in source.locations:
            out.append(_check(source, location, stores))
    return tuple(out)


def _check(source: Stated, location: str, stores: Sequence[SourceStore]) -> SourceCheck:
    path = _path(location)
    if path is None:
        detail = "not a root-relative location; its connector resolves it, not the Ledger"
        return SourceCheck(source.content_id, location, "unsupported", detail)
    for store in stores:
        found = _digest(store, path)
        if found is None:
            continue
        if found == (source.size, source.content_id):
            return SourceCheck(
                source.content_id, location, "present", f"intact in {store.describe()}"
            )
        detail = f"in {store.describe()} with size {found[0]} and {found[1]}, not the stated bytes"
        return SourceCheck(source.content_id, location, "changed", detail)
    for other in source.elsewhere:
        other_path = _path(other)
        if other_path is None or other == location:
            continue
        for store in stores:
            if _digest(store, other_path) == (source.size, source.content_id):
                detail = f"not at this location; intact at another stated one in {store.describe()}"
                return SourceCheck(source.content_id, location, "moved", detail, found_at=other)
    detail = f"in none of {len(stores)} source store(s)"
    return SourceCheck(source.content_id, location, "absent", detail)


def _path(location: str) -> bytes | None:
    """The root-relative path of a local location's canonical JSON, or None for another kind."""
    value = canonical_json.loads(location.encode("utf-8"))
    if not isinstance(value, dict):
        return None
    path, path_hex = value.get("path"), value.get("path_hex")
    if value.get("kind") == "local" and isinstance(path, str):
        raw = path.encode("utf-8")
    elif value.get("kind") == "local_raw" and isinstance(path_hex, str):
        try:
            raw = bytes.fromhex(path_hex)
        except ValueError:
            return None
    else:
        return None
    parts = raw.split(b"/")
    if b"\x00" in raw or any(part in (b"", b".", b"..") for part in parts):
        return None  # the compiler never states such a path; never look outside a store
    return raw


def _digest(store: SourceStore, path: bytes) -> tuple[int, str] | None:
    stream = store.open(path)
    if stream is None:
        return None
    digest, size = hashlib.sha256(), 0
    with stream:
        while block := stream.read(_READ_SIZE):
            digest.update(block)
            size += len(block)
    return size, "sha256:" + digest.hexdigest()
