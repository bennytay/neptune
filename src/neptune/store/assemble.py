"""Assembling an ingest package from a workspace, and exporting a portable copy (ADR 0026).

``assemble`` gathers what an ingest committed (each source's plan under its transform, every
chunk's records and findings, every stream's runs), merges each stream's runs into its series
file, and writes the package beside its destination before renaming it into place: a package
appears whole or not at all. Sources stay where they are unless asked for.

``export`` copies a package with every source materialised into ``blobs/``: the portable form,
readable anywhere. Its records and receipt are the original's; only its manifest differs, because
it now holds the bytes. It is written the same way, whole or not at all.
"""

import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from neptune.discovery.source import LocalSource, SourceAccessError
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ContentId, RecordId
from neptune.model.run import Stream
from neptune.model.source import LocalPath, RawLocalPath, SourceAbsence, SourceRevision
from neptune.store.package import (
    Content,
    PackageError,
    package_contents,
    package_id,
    read_package,
)
from neptune.store.series import SERIES_SETTINGS, merge_runs
from neptune.store.workspace import Workspace


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


@contextmanager
def _staged(destination: Path) -> Iterator[Path]:
    """A hidden sibling of ``destination`` that becomes it on success and is removed otherwise.

    ``destination`` must not exist: a package is written once. Renaming the finished directory is
    one step, so the package appears whole or not at all.
    """
    if destination.exists():
        raise PackageError(f"{destination} exists; a package is written once")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=f".{destination.name}."))
    try:
        staging.chmod(0o777 & ~_umask())  # mkdtemp makes it private; a package is ordinary
        yield staging
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _lay_out(
    staging: Path, contents: Mapping[str, Content], *, movable: Path | None = None
) -> bool:
    """Write ``contents`` under ``staging``: bytes as given, paths under ``movable`` moved, the
    rest copied as streams. Returns whether anything was copied, since a copied file can have
    changed since it was hashed and what landed must then be read back.
    """
    copied = False
    for relative, data in sorted(contents.items()):
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        elif movable is not None and data.parent == movable:
            data.rename(target)
        else:
            shutil.copyfile(data, target)
            copied = True
    return copied


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

    with _staged(destination) as staging:
        scratch = staging / ".series"
        scratch.mkdir()
        series: dict[RecordId, Content] = {}
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
        copied = _lay_out(staging, contents, movable=scratch)
        scratch.rmdir()  # every merged series was moved into place
        if copied:  # a source can change between hashing and copying: check what landed
            read_package(staging)
    return package_id(contents)


def _sources_under(
    records: Iterable[Any], wanted: Iterable[ContentId], root: Path
) -> dict[ContentId, Path]:
    """Where each wanted source lies under ``root``: a location whose latest revision holds it.

    Only the head of each location's chain is tried, since a superseded revision's location holds
    other bytes, or none, by now. Each location is opened through ``LocalSource`` first, so the
    walk's policy applies: a symlink or special file there is not a source. The bytes are checked
    against the content id as the package is written, so a changed file fails the export.
    """
    chain = [r for r in records if isinstance(r, (SourceRevision, SourceAbsence))]
    superseded = {previous for entry in chain for previous in entry.supersedes}
    source, wanted, found = LocalSource(root), set(wanted), {}
    for revision in sorted(chain, key=lambda entry: entry.id):
        if not isinstance(revision, SourceRevision) or revision.id in superseded:
            continue
        if revision.content_id not in wanted or revision.content_id in found:
            continue
        location = revision.location
        if not isinstance(location, (LocalPath, RawLocalPath)):
            continue  # an external object: fetching it is a connector's (MVL-45)
        try:
            source.open(location).close()
        except SourceAccessError:
            continue
        found[revision.content_id] = Path(
            os.fsdecode(os.path.join(os.fsencode(root), location.raw))
        )
    if missing := sorted(wanted - set(found)):
        raise PackageError(f"sources not under {root}: {missing}")
    return found


def export(package_root: Path, destination: Path, source_root: Path) -> ContentId:
    """Copy a package with every source materialised, read from its locations under the root.

    Each referenced source is found at a location its revisions record, relative to
    ``source_root``, and its bytes are checked against its content id while copied. The export
    appears at ``destination``, which must not exist, whole or not at all.
    """
    package = read_package(package_root)
    blobs: dict[ContentId, Content] = dict(package.blobs)
    if wanted := [h.content_id for h in package.manifest.sources if h.content_id not in blobs]:
        blobs.update(_sources_under(package.records, wanted, source_root))
    contents = package_contents(
        package.records, series=package.series, blobs=blobs, store=package.manifest.store
    )
    with _staged(destination) as staging:
        _lay_out(staging, contents)
        read_package(staging)  # the sources were hashed, then copied: check what landed
    return package_id(contents)
