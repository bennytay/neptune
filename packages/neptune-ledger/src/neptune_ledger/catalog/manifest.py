"""The registry manifest: the registration log as a file, so a rebuild needs only it and the
packages (Ledger ADR 0012 §1).

The manifest is one canonical JSON document. It lists every registration of one tenant in
``tx_seq`` order: the package id, the root it was registered from, the Ledger version that indexed
it and its transaction key. It is a function of the registration log and nothing else, so the same
catalog always writes the same bytes. ``write_manifest`` rewrites it after a registration under a
per-tenant advisory lock, reading the log after taking the lock, so the last writer always writes
the newest committed log, and replaces the file atomically. ``Manifest.from_bytes`` treats the file
as hostile input: anything but the exact shape this module writes is ``ManifestError``.
"""

import contextlib
import itertools
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg import sql

from neptune.identity import canonical_json
from neptune.store.durable import fsync_directory
from neptune_ledger.catalog.migrate import tenant_schema

FORMAT: Final = "neptune-ledger/registry-manifest"
FORMAT_VERSION: Final = 1
_KEYS: Final = frozenset({"format", "format_version", "registrations", "tenant_id"})
_ENTRY_KEYS: Final = frozenset(
    {"ledger_version", "package_id", "root_locator", "tx_seq", "tx_time"}
)
# The shapes migration 0001 gives these columns, so a manifest cannot carry a value the
# registration log would refuse.
_PACKAGE_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
_TX_TIME: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_VERSION: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_MAX_SEQ: Final = 2**63 - 1

Conn = psycopg.Connection[tuple[Any, ...]]


class ManifestError(ValueError):
    """The bytes are not a registry manifest this Ledger writes."""


class ManifestNotWritten(RuntimeError):
    """A registration committed, but the manifest could not be rewritten (ADR 0012 §1).

    ``registration`` is the committed answer. Registering the same package again, or ``ledger
    manifest``, rewrites the file.
    """

    def __init__(self, registration: Any, cause: BaseException) -> None:
        super().__init__(f"the registry manifest was not rewritten: {cause}")
        self.registration = registration


@dataclass(frozen=True)
class Entry:
    """One registration, as ``registration_log`` holds it (ADR 0002 §4)."""

    tx_seq: int
    tx_time: str
    package_id: str
    root_locator: str
    ledger_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "ledger_version": self.ledger_version,
            "package_id": self.package_id,
            "root_locator": self.root_locator,
            "tx_seq": self.tx_seq,
            "tx_time": self.tx_time,
        }


@dataclass(frozen=True)
class Manifest:
    """Every registration of one tenant, in ``tx_seq`` order."""

    tenant_id: str
    registrations: tuple[Entry, ...]

    def to_bytes(self) -> bytes:
        """The file's bytes: canonical JSON and one newline."""
        document: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "registrations": [entry.to_json() for entry in self.registrations],
            "tenant_id": self.tenant_id,
        }
        return canonical_json.dumps(document) + b"\n"

    @classmethod
    def from_bytes(cls, data: bytes) -> "Manifest":
        """Parse a manifest file, refusing anything but the shape ``to_bytes`` writes."""
        if not data.endswith(b"\n"):
            raise ManifestError("a registry manifest ends with one newline")
        try:
            value = canonical_json.loads(data[:-1])
        except canonical_json.CanonicalJsonError as exc:
            raise ManifestError(f"not a registry manifest: {exc}") from exc
        if not isinstance(value, dict) or set(value) != _KEYS:
            raise ManifestError(f"a registry manifest has exactly the keys {sorted(_KEYS)}")
        if value["format"] != FORMAT or value["format_version"] != FORMAT_VERSION:
            raise ManifestError(f"not a {FORMAT} version {FORMAT_VERSION} document")
        tenant = value["tenant_id"]
        try:
            tenant_schema(tenant if isinstance(tenant, str) else "")
        except ValueError as exc:
            raise ManifestError(f"tenant_id: {exc}") from exc
        assert isinstance(tenant, str)
        registrations = value["registrations"]
        if not isinstance(registrations, list):
            raise ManifestError("registrations is a list")
        entries = tuple(_entry(item, index) for index, item in enumerate(registrations))
        for before, after in itertools.pairwise(entries):
            if after.tx_seq <= before.tx_seq or after.tx_time < before.tx_time:
                raise ManifestError(
                    f"registration {after.tx_seq} does not follow {before.tx_seq}: tx_seq rises"
                    " and tx_time never falls (ADR 0002 §4)"
                )
        if len({entry.package_id for entry in entries}) != len(entries):
            raise ManifestError("a package is listed twice; a package registers once")
        return cls(tenant, entries)


def _entry(item: object, index: int) -> Entry:
    where = f"registrations[{index}]"
    if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
        raise ManifestError(f"{where} has exactly the keys {sorted(_ENTRY_KEYS)}")
    seq, at, package, root, version = (
        item["tx_seq"],
        item["tx_time"],
        item["package_id"],
        item["root_locator"],
        item["ledger_version"],
    )
    if not isinstance(seq, int) or isinstance(seq, bool) or not 1 <= seq <= _MAX_SEQ:
        raise ManifestError(f"{where}.tx_seq is an integer from 1 to 2^63 - 1")
    if not isinstance(at, str) or not _TX_TIME.fullmatch(at):
        raise ManifestError(f"{where}.tx_time is RFC 3339 UTC with six fractional digits")
    if not isinstance(package, str) or not _PACKAGE_ID.fullmatch(package):
        raise ManifestError(f"{where}.package_id is a sha256 content id")
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root:
        raise ManifestError(f"{where}.root_locator is an absolute path")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ManifestError(f"{where}.ledger_version is MAJOR.MINOR.PATCH")
    return Entry(seq, at, package, root, version)


def read_manifest(conn: Conn, tenant_id: str) -> Manifest:
    """The tenant's registration log as a manifest, read in one statement."""
    schema = tenant_schema(tenant_id)
    rows = conn.execute(
        sql.SQL(
            "SELECT tx_seq, tx_time, package_id, root_locator, ledger_version"
            " FROM {}.registration_log WHERE tenant_id = %s ORDER BY tx_seq"
        ).format(sql.Identifier(schema)),
        (tenant_id,),
    ).fetchall()
    return Manifest(
        tenant_id,
        tuple(
            Entry(int(seq), str(at), str(package), str(root), str(version))
            for seq, at, package, root, version in rows
        ),
    )


def write_manifest(conn: Conn, tenant_id: str, path: str | os.PathLike[str]) -> Manifest:
    """Rewrite the tenant's manifest at ``path`` from its registration log; return it.

    ``conn`` must not be inside a transaction. The log is read after taking the tenant's manifest
    lock, and the file is replaced while the lock is held, so concurrent writers leave the newest
    log on disk. An ``OSError`` writing the file propagates; the file is then unchanged.
    """
    lock = "manifest:" + tenant_schema(tenant_id)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
        manifest = read_manifest(conn, tenant_id)
        replace_file(Path(path), manifest.to_bytes())
    return manifest


def replace_file(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically and durably (``replacing``)."""
    with replacing(path) as fd:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]


@contextlib.contextmanager
def replacing(path: Path) -> Iterator[int]:
    """A descriptor to write ``path``'s new content to; on success it replaces ``path``.

    The content goes to a new sibling file (``O_EXCL``, so nothing planted at its name is
    written through), is fsynced and renamed over ``path``, and the directory is fsynced. On any
    failure the sibling is removed and ``path`` is unchanged.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.unlink(missing_ok=True)  # a leftover of a crashed writer with the same ids
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o644)
    try:
        try:
            yield fd
            os.fsync(fd)
        finally:
            os.close(fd)
        temporary.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
