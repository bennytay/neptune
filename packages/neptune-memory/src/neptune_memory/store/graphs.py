"""A tenant's graph on disk: the graph document and one record per consolidated snapshot.

The reference persistence behind ``memory consolidate``, ``rebuild`` and ``dump`` (ADR 0016 §4),
until G3 maps ``schema.Claim`` onto ``PostgresStore``. One directory per tenant under a root:

- ``graph.json``: the tenant's graph document (``schema.codec``) as canonical JSON;
- ``snapshots/<ledger snapshot>.json``: the ``MemorySnapshot`` of each consolidation and every
  finding it produced, as canonical JSON.

Everything is derived from the Ledger, so a rebuild replaces it; nothing here reads or writes a
package. Each file is written to a temporary name, synced and renamed, so a crash leaves the old
file or the new one, never a mix. Paths are hostile input: a tenant is a token, never a path, and a
symlink anywhere in a tenant's directory is refused.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune_memory.schema.codec import GraphDocument, graph_from_json

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.snapshot import ConsolidationRun

GRAPH_FILE: Final = "graph.json"
SNAPSHOTS: Final = "snapshots"
TENANT: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SNAPSHOT_FILE: Final = re.compile(r"(0|[1-9][0-9]{0,18})\.json")
TEMPORARY: Final = ".tmp"  # a write interrupted before its rename: Memory's, never read


class GraphStoreError(ValueError):
    """A tenant directory that cannot be used as is: a bad name, a symlink, an unreadable file."""


def _plain(path: Path) -> Path:
    if path.is_symlink():
        raise GraphStoreError(f"refusing a symlink: {path}")
    return path


def _write(path: Path, value: JsonValue) -> None:
    """Write, flush, rename over ``path`` and flush the directory: a rename is atomic but not
    durable until its directory is synced, and a power cut must leave the old file or the new."""
    temporary = _plain(path.with_name(path.name + TEMPORARY))
    with temporary.open("wb") as handle:
        handle.write(canonical_json.dumps(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(_plain(path))
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path: Path) -> JsonValue:
    try:
        return canonical_json.loads(_plain(path).read_bytes().rstrip(b"\n"))
    except ValueError as exc:
        raise GraphStoreError(f"{path}: not canonical JSON: {exc}") from exc


class TenantGraphs:
    """Every tenant's graph under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _tenant(self, tenant: str) -> Path:
        if not isinstance(tenant, str) or not TENANT.fullmatch(tenant):
            raise GraphStoreError(f"a tenant is a token matching {TENANT.pattern}: {tenant!r}")
        return _plain(_plain(self.root) / tenant)

    def load(self, tenant: str) -> GraphDocument | None:
        """The tenant's graph, or ``None`` if it has none yet."""
        path = _plain(self._tenant(tenant) / GRAPH_FILE)
        if not path.exists():
            return None
        data = _read(path)
        try:
            return graph_from_json(data)
        except (TypeError, ValueError) as exc:
            raise GraphStoreError(f"{path}: not a graph document: {exc}") from exc

    def snapshot(self, tenant: str, ledger_snapshot: int) -> JsonValue | None:
        """The record of the tenant's consolidation of ``ledger_snapshot``, if it has one."""
        path = _plain(self._tenant(tenant) / SNAPSHOTS / f"{ledger_snapshot}.json")
        return _read(path) if path.exists() else None

    def snapshots(self, tenant: str) -> list[JsonValue]:
        """Every snapshot record the tenant holds, in Ledger snapshot order."""
        folder = _plain(self._tenant(tenant) / SNAPSHOTS)
        if not folder.exists():
            return []
        names = []
        for path in folder.iterdir():
            _plain(path)
            if path.name.endswith(TEMPORARY) and SNAPSHOT_FILE.fullmatch(
                path.name.removesuffix(TEMPORARY)
            ):
                continue
            match = SNAPSHOT_FILE.fullmatch(path.name)
            if match is None:
                raise GraphStoreError(f"unexpected file in {folder}: {path.name}")
            names.append((int(match.group(1)), path))
        return [_read(p) for _, p in sorted(names)]

    def save(
        self, tenant: str, document: GraphDocument, run: ConsolidationRun, *, fresh: bool = False
    ) -> None:
        """Record ``run``'s snapshot, then the graph it produced (the graph names the head).

        ``fresh`` (a rebuild): ``document`` replaces the tenant's graph, and every other snapshot
        record goes once the new graph is in place. A directory holding files Memory did not
        write is refused before anything is written.
        """
        directory = self._tenant(tenant)
        if fresh:
            self._ours(directory)
        folder = _plain(directory / SNAPSHOTS)
        folder.mkdir(parents=True, exist_ok=True)
        record: JsonValue = {
            "findings": run.findings_json(),
            "snapshot": run.snapshot.to_json(),
        }
        name = f"{run.snapshot.ledger_snapshot}.json"
        _write(folder / name, record)
        _write(directory / GRAPH_FILE, document.to_json())
        if fresh:
            for path in sorted(folder.iterdir()):
                if path.name != name:
                    path.unlink()

    def _ours(self, directory: Path) -> None:
        """Refuse a tenant directory holding anything Memory did not write, or a symlink."""
        if not directory.exists():
            return
        ours = {GRAPH_FILE, GRAPH_FILE + TEMPORARY, SNAPSHOTS}
        unexpected = []
        for path in sorted(directory.iterdir()):
            if path.name not in ours or _plain(path).is_dir() != (path.name == SNAPSHOTS):
                unexpected.append(path.name)
        folder = _plain(directory / SNAPSHOTS)
        if folder.is_dir():
            for path in folder.iterdir():
                name = _plain(path).name.removesuffix(TEMPORARY)
                if not path.is_file() or not SNAPSHOT_FILE.fullmatch(name):
                    unexpected.append(f"{SNAPSHOTS}/{path.name}")
        if unexpected:
            raise GraphStoreError(f"{directory} holds files Memory did not write: {unexpected}")
