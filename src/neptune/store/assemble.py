"""Assembling an ingest package from a workspace, and exporting a portable copy (ADR 0026).

``assemble`` gathers what an ingest committed (each source's plan under its transform, every
chunk's records and findings, every stream's runs), merges each stream's runs into its series
file, and writes the package beside its destination before renaming it into place: a package
appears whole or not at all. Sources stay where they are unless asked for.

``export`` copies a package with every source materialised into ``blobs/``: the portable form,
readable anywhere. Its records and receipt are the original's; only its manifest differs, because
it now holds the bytes.
"""

import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ContentId, RecordId
from neptune.model.run import Stream
from neptune.model.source import LocalPath, RawLocalPath, SourceRevision
from neptune.store.package import (
    PackageError,
    package_contents,
    package_id,
    read_package,
    write_package,
)
from neptune.store.series import SERIES_SETTINGS, merge_runs
from neptune.store.workspace import Workspace


def assemble(
    destination: Path,
    workspace: Workspace,
    ledger: SourceLedger,
    ingested: Iterable[tuple[ContentId, RecordId]],
    *,
    materialise: Mapping[ContentId, Path] | None = None,
) -> ContentId:
    """Write the package of ``ingested`` sources, each a (content id, transform id) pair.

    Every chunk of each source's plan must be committed in ``workspace``. ``materialise`` names
    sources to copy into the package, with a path holding each one's bytes; every other source is
    referenced. ``destination`` must not exist. Returns the package id.
    """
    if destination.exists():
        raise PackageError(f"{destination} exists; a package is written once")
    records: dict[tuple[str, str], Any] = {}
    runs: dict[RecordId, list[Path]] = defaultdict(list)
    for source, transform in sorted(set(ingested)):
        plan = workspace.load_plan(source, transform)
        if plan is None:
            raise PackageError(f"no plan of {source} under transform {transform}")
        found: list[Any] = [plan.transform, *plan.findings]
        for chunk in plan.chunks:
            output = workspace.load(str(chunk["id"]))
            found += [*output.records, *output.findings]
            for stream, run in output.runs.items():
                runs[stream].append(run)
        for item in found:
            records[item.kind, item.id] = item
    streams = {r.id: r for r in records.values() if isinstance(r, Stream)}
    if strays := set(runs) - set(streams):
        raise PackageError(f"runs of streams the package does not hold: {sorted(strays)}")
    if silent := set(streams) - set(runs):
        raise PackageError(f"streams without a series run: {sorted(silent)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=f".{destination.name}."))
    scratch = staging / ".series"
    try:
        scratch.mkdir()
        series: dict[RecordId, Path] = {}
        for stream_id, stream_runs in sorted(runs.items()):
            merged = scratch / f"{stream_id.removeprefix('rec:sha256:')}.parquet"
            merge_runs(streams[stream_id], sorted(stream_runs), merged)
            series[stream_id] = merged
        ledger_records = (*ledger.artifacts(), *ledger.revisions(), *ledger.absences())
        contents = package_contents(
            [*ledger_records, *records.values()],
            series=series,
            blobs=dict(materialise or {}),
            store={"series": SERIES_SETTINGS} if series else {},
        )
        copied = False
        for relative, data in sorted(contents.items()):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                target.write_bytes(data)
            elif data.parent == scratch:
                data.rename(target)  # the merged series is already the file: move it into place
            else:
                shutil.copyfile(data, target)
                copied = True
        scratch.rmdir()
        if copied:  # a source can change between hashing and copying: check what landed
            read_package(staging)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return package_id(contents)


def _location_path(root: Path, revision: SourceRevision) -> Path | None:
    location = revision.location
    if isinstance(location, LocalPath):
        return root / location.path
    if isinstance(location, RawLocalPath):
        return Path(os.fsdecode(os.fsencode(root) + b"/" + location.path))
    return None  # an external object: fetching it is a connector's (MVL-45)


def export(package_root: Path, destination: Path, source_root: Path) -> ContentId:
    """Copy a package with every source materialised, read from its locations under the root.

    Each referenced source is found at a location its revisions record, relative to
    ``source_root``, and its bytes are checked against its content id while copied.
    """
    package = read_package(package_root)
    found: dict[ContentId, Path] = {}
    for record in package.records:
        if isinstance(record, SourceRevision) and record.content_id not in found:
            path = _location_path(source_root, record)
            if path is not None and path.is_file():
                found[record.content_id] = path
    blobs: dict[ContentId, Any] = dict(package.blobs)
    for handle in package.manifest.sources:
        if handle.content_id not in blobs:
            if handle.content_id not in found:
                raise PackageError(f"source {handle.content_id} is not under {source_root}")
            blobs[handle.content_id] = found[handle.content_id]
    contents = package_contents(
        package.records, series=package.series, blobs=blobs, store=package.manifest.store
    )
    write_package(destination, contents)
    return read_package(destination).id
