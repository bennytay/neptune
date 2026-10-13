"""The source index: where each cited source sits in the corpus (ADR 0016).

An answer cites a source by content id (``sha256:...``), which no person can open. The source
index maps each content id to the corpus-relative paths the Ledger's catalog states for it (a
referenced source's ``source_revision`` locations, read through ``CatalogApi.resolve``), so the
agent renderer can name the file beside each ``[E<k>]`` key. It is built where a catalog exists
(the harness's ledger stage, ``build_source_index``), written as one small JSON document
(``dumps_source_index``) next to the graph it serves, and read by the MCP server
(``--sources FILE``, ``read_source_index``) where no catalog is attached.

The document::

    {"kind": "neptune-context.source-index", "version": 1,
     "sources": {"sha256:<hex>": ["sites/PLANT-2/cmms/work_orders.csv"], ...}}

Paths are data. Only a relative POSIX path with no empty, ``.`` or ``..`` part, no backslash and
no control character is kept: a host path (absolute, a drive, a UNC share) is never written or
read, so an answer never prints where a host keeps its files. A source the catalog places at no
such path is left out, and the renderer says it is not in the index; nothing is guessed.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune.model.ids import parse_content_id

if TYPE_CHECKING:
    from pathlib import Path

    from neptune.model.provenance import EvidenceRef
    from neptune_ledger.api import CatalogApi

KIND: Final = "neptune-context.source-index"
VERSION: Final = 1
MAX_INDEX_BYTES: Final = 16 * 1024 * 1024
MAX_PATH_CHARS: Final = 4096
MAX_PATHS_PER_SOURCE: Final = 64


@dataclass(frozen=True)
class SourceIndex:
    """Content id -> the sorted, distinct corpus-relative paths the source sits at."""

    paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for source, found in self.paths.items():
            parse_content_id(source)
            if not found or len(found) > MAX_PATHS_PER_SOURCE:
                raise ValueError(f"{source}: 1 to {MAX_PATHS_PER_SOURCE} paths")
            if list(found) != sorted(set(found)):
                raise ValueError(f"{source}: paths must be sorted and distinct")
            for path in found:
                if not is_corpus_path(path):
                    raise ValueError(f"{source}: not a corpus-relative path: {path[:200]!r}")

    def get(self, source: object) -> tuple[str, ...]:
        """The paths of ``source``; empty when the index does not place it."""
        return self.paths.get(source, ()) if isinstance(source, str) else ()

    def to_json(self) -> dict[str, object]:
        return {
            "kind": KIND,
            "sources": {s: list(self.paths[s]) for s in sorted(self.paths)},
            "version": VERSION,
        }


def is_corpus_path(path: object) -> bool:
    """A relative POSIX path an answer may print: no host root, no climbing, no hidden control."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        return False
    if path.startswith("/") or "\\" in path or ":" in path.split("/", 1)[0]:
        return False  # absolute, a Windows separator, a drive or URL scheme
    if any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 or c in "  " for c in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def dumps_source_index(index: SourceIndex) -> str:
    """The index as sorted, indented JSON with a final newline: same index, same bytes."""
    return json.dumps(index.to_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def source_index_from_json(document: object) -> SourceIndex:
    """Read an index document strictly; ``ValueError`` on anything else."""
    if not isinstance(document, dict) or set(document) != {"kind", "sources", "version"}:
        raise ValueError("a source index has exactly kind, sources and version")
    if document["kind"] != KIND:
        raise ValueError(f"kind is not {KIND!r}")
    if document["version"] != VERSION or isinstance(document["version"], bool):
        raise ValueError(f"version {document['version']!r} is not {VERSION}")
    sources = document["sources"]
    if not isinstance(sources, dict):
        raise ValueError("sources is an object of content id -> paths")
    paths: dict[str, tuple[str, ...]] = {}
    for source, found in sources.items():
        if not isinstance(found, list) or not all(isinstance(p, str) for p in found):
            raise ValueError(f"{str(source)[:80]}: paths are a list of strings")
        paths[source] = tuple(found)
    return SourceIndex(paths)


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key[:80]!r}")
        out[key] = value
    return out


def _no_constant(token: str) -> object:
    raise ValueError(f"{token} is not JSON")


def read_source_index(path: Path) -> SourceIndex:
    """The index at ``path``: a regular file of at most ``MAX_INDEX_BYTES``, read strictly.
    Raises ``ValueError`` or ``OSError``."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"{path.name} is not a regular file")
    with path.open("rb") as handle:
        data = handle.read(MAX_INDEX_BYTES + 1)
    if len(data) > MAX_INDEX_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_INDEX_BYTES} bytes")
    try:
        document = json.loads(data, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(f"{path.name} is not a source index: {exc}") from exc
    return source_index_from_json(document)


def build_source_index(
    catalog: CatalogApi, refs: Iterable[EvidenceRef], *, as_of: int | None = None
) -> SourceIndex:
    """Where the catalog places each content-addressed source of ``refs``.

    One ``resolve`` per source (its first ref in canonical order). A resolved source's paths are
    the ``local`` locations its referenced copies state, kept only when they are corpus paths
    (``is_corpus_path``); a materialised copy states none. An unresolvable source, an external
    object and a source with no such path are left out."""
    from neptune.identity.canonical_json import dumps
    from neptune_ledger.api import EvidenceAnchor, from_json

    first: dict[str, tuple[bytes, EvidenceRef]] = {}
    for ref in refs:
        if not isinstance(ref.source, str):
            continue
        key = dumps(ref.to_json())
        if ref.source not in first or key < first[ref.source][0]:
            first[ref.source] = (key, ref)
    paths: dict[str, tuple[str, ...]] = {}
    for source in sorted(first):
        anchor = from_json(EvidenceAnchor, first[source][1].to_json())
        resolution = catalog.resolve(anchor, as_of=as_of)
        if resolution.status != "resolved":
            continue
        found = {
            str(location["path"])
            for copy in resolution.fetch
            for location in copy.locations
            if location.get("kind") == "local" and is_corpus_path(location.get("path"))
        }
        if found:
            paths[source] = tuple(sorted(found))[:MAX_PATHS_PER_SOURCE]
    return SourceIndex(paths)
