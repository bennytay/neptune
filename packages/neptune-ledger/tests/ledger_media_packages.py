"""Packages that hold the media fixtures as sources, referenced or materialised (MVL-96).

A package here is what an ingest of those files would register before any adapter ran: one
``source_artifact`` and one ``source_revision`` per file, written by the compiler's own package
writer, so registration verifies it like any other. Materialised sources go into the package's
``blobs/``; referenced ones stay in an ingest root the test names as a source store. A small
``chunk_size`` makes a read span several chunks, so chunk verification is exercised.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ledger_series_packages import read
from ledger_thread_packages import artifact, revision
from neptune.store.package import package_files, write_package


def content_id(data: bytes) -> str:
    made = artifact(data)["content_id"]
    assert isinstance(made, str)
    return made


def source_package(
    root: Path,
    sources: Mapping[str, bytes],
    *,
    materialise: frozenset[str] = frozenset(),
    chunk_size: int = 8388608,
) -> str:
    """Write a package at ``root`` stating ``sources`` (ingest-root path -> bytes); return its id.

    Paths in ``materialise`` are stored in the package; the others are only referenced.
    """
    records: list[Any] = []
    blobs: dict[Any, bytes] = {}
    for path, data in sorted(sources.items()):
        made = artifact(data, chunk_size)
        records += [made, revision({"kind": "local", "path": path}, made["content_id"])]
        if path in materialise:
            blobs[made["content_id"]] = data
    return write_package(root, package_files([read(r) for r in records], blobs=blobs))


def ingest_root(root: Path, sources: Mapping[str, bytes]) -> Path:
    """An ingest root holding ``sources`` at their paths."""
    for path, data in sources.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    return root
