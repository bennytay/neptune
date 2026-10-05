"""A tenant's graph on disk: the graph document and one record per consolidated snapshot.

The reference persistence behind ``memory consolidate``, ``rebuild`` and ``dump`` (ADR 0016 §4),
until G3 maps ``schema.Claim`` onto ``PostgresStore``. One directory per tenant under a root:

- ``graph.json``: the tenant's graph document (``schema.codec``) as canonical JSON;
- ``snapshots/<ledger snapshot>.json``: the ``MemorySnapshot`` of each consolidation and every
  finding it produced, as canonical JSON.

Everything is derived from the Ledger, so ``drop`` deletes it; nothing here reads or writes a
package. Each file is written to a temporary name and renamed, so a crash leaves the old file or
the new one, never a mix. Paths are hostile input: a tenant is a token, never a path, and a
symlink anywhere in a tenant's directory is refused.
"""

from __future__ import annotations

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
    temporary = path.with_name(path.name + ".tmp")
    _plain(temporary).write_bytes(canonical_json.dumps(value) + b"\n")
    temporary.replace(_plain(path))


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
        try:
            return graph_from_json(canonical_json.loads(path.read_bytes().rstrip(b"\n")))
        except ValueError as exc:
            raise GraphStoreError(f"{path}: not a graph document: {exc}") from exc

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
        return [canonical_json.loads(p.read_bytes().rstrip(b"\n")) for _, p in sorted(names)]

    def save(self, tenant: str, document: GraphDocument, run: ConsolidationRun) -> None:
        """Record ``run``'s snapshot, then the graph it produced (the graph names the head)."""
        directory = self._tenant(tenant)
        folder = _plain(directory / SNAPSHOTS)
        folder.mkdir(parents=True, exist_ok=True)
        record: JsonValue = {
            "findings": run.findings_json(),
            "snapshot": run.snapshot.to_json(),
        }
        _write(folder / f"{run.snapshot.ledger_snapshot}.json", record)
        _write(directory / GRAPH_FILE, document.to_json())

    def drop(self, tenant: str) -> None:
        """Delete the tenant's graph and snapshot records; other files are refused, not deleted."""
        directory = self._tenant(tenant)
        if not directory.exists():
            return
        folder = _plain(directory / SNAPSHOTS)
        ours = {GRAPH_FILE, GRAPH_FILE + TEMPORARY, SNAPSHOTS}
        unexpected = sorted(p.name for p in directory.iterdir() if p.name not in ours)
        if folder.exists():
            for path in folder.iterdir():
                _plain(path)
                name = path.name.removesuffix(TEMPORARY)
                if not SNAPSHOT_FILE.fullmatch(name):
                    unexpected.append(f"{SNAPSHOTS}/{path.name}")
        if unexpected:
            raise GraphStoreError(f"{directory} holds files Memory did not write: {unexpected}")
        if folder.exists():
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
        for name in (GRAPH_FILE, GRAPH_FILE + TEMPORARY):
            path = _plain(directory / name)
            if path.exists():
                path.unlink()
