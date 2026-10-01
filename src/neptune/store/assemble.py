"""Assembling an ingest package from a workspace, and exporting a portable copy (ADR 0026).

``stage`` gathers what an ingest committed (each source's plan under its transform, every
chunk's records and findings, every stream's runs), takes each stream's series file from the
workspace (merging its runs the first time a package needs it, ADR 0031 §4), and builds the
package in a hidden directory beside its destination. ``publish`` flushes it
to disk and renames it into place: a package appears whole or not at all, and survives a crash
once it has appeared. ``assemble`` is the two in one call; the runtime (MVL-6) verifies the staged
package and writes its envelope between them. Sources stay where they are unless asked for; one
asked for is read as ``export`` reads it.

``export`` copies a package with every source it can reach materialised into ``blobs/``: the
portable form, readable anywhere. Each source is read through a ``Source``
(``neptune.discovery.source``), so the walk's policy applies, and checked against its content id
as it lands. Its records and receipt are the original's; only its manifest differs, because it now
holds the bytes. It is written the same way, whole or not at all.
"""

import hashlib
import secrets
import shutil
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, Protocol

from neptune.identity import canonical_json
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.package import package_manifest_from_json
from neptune.model.run import Stream
from neptune.model.source import LocalPath, RawLocalPath, SourceAbsence, SourceRevision
from neptune.store.durable import fsync_directory, fsync_tree
from neptune.store.package import (
    MANIFEST,
    Content,
    PackageError,
    copy_file,
    open_file,
    package_contents,
    package_id,
    read_package,
)
from neptune.store.receipt import cited_sources
from neptune.store.series import SERIES_SETTINGS, merge_runs
from neptune.store.workspace import DerivativeKey, Held, Owner, Workspace, WorkspaceError

_COPY_SIZE: Final = 1024 * 1024
# A stream's series file, as a derivative of the runs of the chunks it merges (ADR 0031 §4). The
# version changes whenever the merge would write other bytes from the same runs and settings.
SERIES_RECIPE: Final = "neptune.store.series/1"
SERIES_FILE: Final = "series.parquet"


def series_key(stream: RecordId, chunks: Iterable[str], owners: Iterable[Owner]) -> DerivativeKey:
    """The key of ``stream``'s series file: the chunks whose runs it merges, and the settings.

    Committed chunks never change (their id is their content), so these are everything the file
    is a function of. A new adapter version or config is new chunk ids, so a new key; a new
    pyarrow is new settings, so a new key.
    """
    inputs: JsonObject = {
        "chunks": sorted(set(chunks)),
        "settings": SERIES_SETTINGS,
        "stream": stream,
    }
    return DerivativeKey(SERIES_RECIPE, inputs, tuple(sorted(set(owners))))


@dataclass(frozen=True)
class DerivativeUse:
    """A derivative a package needed, and how the workspace came by it."""

    key: DerivativeKey
    held: Held


def _copy_checked(source: Path, target: Path, expected: tuple[int, ContentId]) -> bool:
    """Copy ``source`` to ``target``, hashing as it goes; whether it is the file that was kept.

    A copy that is not is removed: what a package holds is what the workspace recorded. The kept
    file is opened as every file the store reads is (``open_file``): never through a symlink.
    """
    digest, size = hashlib.sha256(), 0
    with open_file(source) as data, target.open("xb") as copy:
        while block := data.read(_COPY_SIZE):
            digest.update(block)
            size += len(block)
            copy.write(block)
    if (size, "sha256:" + digest.hexdigest()) == expected:
        return True
    target.unlink()
    return False


@contextmanager
def _workspace_io() -> Iterator[None]:
    """An ``OSError`` reading or writing the workspace is the workspace's: ``WorkspaceError``.

    So a caller tells a workspace that will not read or write from a package that cannot be
    written (ADR 0035 §6); the ``OSError`` is the cause.
    """
    try:
        yield
    except OSError as exc:
        raise WorkspaceError(f"the workspace cannot be read or written: {exc}") from exc


def _series_file(
    workspace: Workspace, key: DerivativeKey, stream: Stream, runs: list[Path], target: Path
) -> Held:
    """Put ``stream``'s series file at ``target``, from the workspace, merging it if not kept.

    A kept file that no longer hashes as it was kept is discarded and merged again.
    """

    def build(directory: Path) -> None:
        merge_runs(stream, runs, directory / SERIES_FILE)

    with _workspace_io():
        derivative, held = workspace.materialise(key, build)
    kept = derivative.files.get(SERIES_FILE)
    if kept is not None and _copy_checked(derivative.file(SERIES_FILE), target, kept):
        return held
    with _workspace_io():
        workspace.discard(key)
        derivative, _ = workspace.materialise(key, build)
    if not _copy_checked(derivative.file(SERIES_FILE), target, derivative.files[SERIES_FILE]):
        raise PackageError(f"the series file of {stream.id} changed while it was copied")
    return Held.REBUILT


class SourceOpener(Protocol):
    """What materialising needs of a ``Source`` (``neptune.discovery.source``): to open a location.

    Opening applies the source's policy, so a symlink or special file at the location is refused,
    and raises the source's own error when the location cannot be opened; the export passes it on.
    """

    def open(self, location: LocalPath | RawLocalPath) -> BinaryIO: ...


def _sibling(destination: Path) -> Path:
    """A new hidden directory beside ``destination``, made like any directory: the umask applies."""
    while True:
        staging = destination.parent / f".{destination.name}.{secrets.token_hex(8)}"
        try:
            staging.mkdir()
        except FileExistsError:
            continue
        return staging


def _open_staging(destination: Path) -> Path:
    """A hidden sibling of ``destination`` to build a package in. ``destination`` must not exist."""
    if destination.exists():
        raise PackageError(f"{destination} exists; a package is written once")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return _sibling(destination)


class NotDurableError(PackageError):
    """The package was renamed into place, but flushing the directory that names it failed.

    It is whole at its destination, and a crash before the disk catches up may still lose its
    name. Nothing is removed: what failed is the guarantee, not the package. Every other failure
    of a publish happens before the rename, so the destination is not the publisher's.
    """


def _rename_into_place(staging: Path, destination: Path) -> None:
    """Flush ``staging`` to disk, rename it to ``destination`` in one step, flush where it landed.

    The package appears whole or not at all, and once it has appeared it stays. If the last
    flush fails, ``NotDurableError``: the package is in place, but may not survive a crash.
    """
    if destination.exists():
        raise PackageError(f"{destination} exists; a package is written once")
    fsync_tree(staging)
    staging.rename(destination)
    try:
        fsync_directory(destination.parent)
    except OSError as exc:
        raise NotDurableError(
            f"{destination} is in place, but its directory cannot be flushed: {exc}"
        ) from exc


@contextmanager
def _staged(destination: Path) -> Iterator[Path]:
    """A hidden sibling of ``destination`` that becomes it on success and is removed otherwise.

    ``destination`` must not exist: a package is written once.
    """
    staging = _open_staging(destination)
    try:
        yield staging
        _rename_into_place(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _lay_out(
    staging: Path, contents: Mapping[str, Content], *, movable: Path | None = None
) -> list[str]:
    """Write ``contents`` under ``staging``: bytes as given, paths under ``movable`` moved, the
    rest copied as streams (``copy_file``: no symlink followed, no special file opened). Returns
    the paths it copied: a copied file can have changed since it was hashed, so what landed must
    be checked (``_check_copies``).
    """
    copied = []
    for relative, data in sorted(contents.items()):
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        elif movable is not None and data.parent == movable:
            data.rename(target)
        else:
            copy_file(data, target)
            copied.append(relative)
    return copied


def _check_copies(staging: Path, contents: Mapping[str, Content], copied: Iterable[str]) -> None:
    """Each copied file, as it landed, has the size and hash the manifest lists for it.

    Only copies are read back: bytes were written as given, and moved files were hashed where
    they lie. The rest of the package was checked when its contents were computed.
    """
    manifest = contents[MANIFEST]
    assert isinstance(manifest, bytes)
    listed = {
        file.path: (file.size, file.sha256)
        for file in package_manifest_from_json(canonical_json.loads(manifest)).files
    }
    for relative in copied:
        with open_file(staging / relative) as landed:
            artifact = digest_stream(landed)
        if (artifact.size, artifact.content_id) != listed[relative]:
            raise PackageError(f"{relative} changed while it was copied; it is not what was hashed")


@dataclass(frozen=True)
class StagedPackage:
    """A package built whole beside ``destination`` and not yet renamed into place.

    ``derivatives`` are the series files it holds, each as the workspace kept or built it.
    """

    path: Path
    destination: Path
    id: ContentId
    derivatives: tuple[DerivativeUse, ...] = ()

    def discard(self) -> None:
        """Remove the staged package. Nothing was published."""
        if self.path.exists():
            shutil.rmtree(self.path)


def stage(
    destination: Path,
    workspace: Workspace,
    ledger: SourceLedger,
    ingested: Iterable[tuple[ContentId, RecordId]],
    *,
    materialise: Iterable[ContentId] = (),
    source: SourceOpener | None = None,
    extra: Iterable[Any] = (),
    derived: Mapping[str, Iterable[JsonObject]] | None = None,
) -> StagedPackage:
    """Build the package of ``ingested`` sources, each a (content id, transform id) pair.

    ``ledger`` is every artifact, revision and absence the package lists, as given: the job
    passes its own scan's, never the workspace's history (ADR 0035 §9), since every entry is
    hashed into the package and its receipt.
    Every ingested source must be in ``ledger``, since the package lists the sources it cites,
    and every chunk of its plan must be committed in ``workspace``. ``extra`` adds records that
    are no adapter's output: the runtime's own transform and findings, and the transforms that
    ``derived`` tables (session proposals, ADR 0036) name. ``materialise`` names
    sources to copy into the package. Each is read as ``export`` reads one: from the head of a
    location chain in ``ledger`` that holds it, opened through ``source`` so its policy applies,
    and hashed where it lands. Every other source is referenced. ``destination`` must not exist.
    The package waits in a hidden sibling directory until ``publish`` renames it into place.

    Each stream's series file is a derivative (``series_key``): merged from its runs the first
    time a package needs it, kept in the workspace, and copied, checked against its hash, into
    every later package that holds the stream.
    """
    if destination.exists():
        raise PackageError(f"{destination} exists; a package is written once")
    wanted = set(materialise)
    if wanted and source is None:
        raise PackageError("materialising sources needs the Source to read them through")
    locations = _head_locations((*ledger.revisions(), *ledger.absences()), wanted)
    if lost := sorted(wanted - set(locations)):
        raise PackageError(f"no local location holds sources to materialise: {lost}")
    records: dict[tuple[str, str], Any] = {}
    for item in extra:
        records[item.kind, item.id] = item
    runs: dict[RecordId, list[tuple[str, Path, Owner]]] = defaultdict(list)
    for content, transform in sorted(set(ingested)):
        if ledger.artifact(content) is None:
            raise PackageError(f"source {content} was ingested but the ledger does not hold it")
        with _workspace_io():
            plan = workspace.load_plan(content, transform)
        if plan is None:
            raise PackageError(f"no plan of {content} under transform {transform}")
        found: list[Any] = [plan.transform, *plan.findings]
        for chunk in plan.chunks:
            chunk_id = str(chunk["id"])
            with _workspace_io():
                output = workspace.load(chunk_id)
            found += [*output.records, *output.findings]
            for stream, run in output.runs.items():
                runs[stream].append((chunk_id, run, (content, transform)))
        for item in found:
            records[item.kind, item.id] = item
    streams = {r.id: r for r in records.values() if isinstance(r, Stream)}
    if strays := set(runs) - set(streams):
        raise PackageError(f"runs of streams the package does not hold: {sorted(strays)}")
    if silent := set(streams) - set(runs):
        raise PackageError(f"streams without a series run: {sorted(silent)}")

    staging = _open_staging(destination)
    try:
        scratch = staging / ".scratch"
        scratch.mkdir()
        series: dict[RecordId, Content] = {}
        uses: list[DerivativeUse] = []
        for stream_id, stream_runs in sorted(runs.items()):
            key = series_key(
                stream_id, (chunk for chunk, _, _ in stream_runs), (o for _, _, o in stream_runs)
            )
            merged = scratch / f"{stream_id.removeprefix('rec:sha256:')}.parquet"
            paths = sorted(run for _, run, _ in stream_runs)
            held = _series_file(workspace, key, streams[stream_id], paths, merged)
            series[stream_id] = merged
            uses.append(DerivativeUse(key, held))
        ledger_records = (*ledger.artifacts(), *ledger.revisions(), *ledger.absences())
        contents = package_contents(
            [*ledger_records, *records.values()],
            series=series,
            blobs=_land(scratch, locations, source) if source is not None else {},
            store={"series": SERIES_SETTINGS} if series else {},
            derived=derived,
        )
        copied = _lay_out(staging, contents, movable=scratch)
        scratch.rmdir()  # every merged series and landed source was moved into place
        _check_copies(staging, contents, copied)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return StagedPackage(staging, destination, package_id(contents), tuple(uses))


def publish(staged: StagedPackage) -> ContentId:
    """Flush a staged package and rename it into place. Its destination must still not exist.

    ``NotDurableError`` means the rename happened and only the flush after it failed; any other
    error, that nothing was renamed.
    """
    _rename_into_place(staged.path, staged.destination)
    return staged.id


def assemble(
    destination: Path,
    workspace: Workspace,
    ledger: SourceLedger,
    ingested: Iterable[tuple[ContentId, RecordId]],
    *,
    materialise: Iterable[ContentId] = (),
    source: SourceOpener | None = None,
    extra: Iterable[Any] = (),
    derived: Mapping[str, Iterable[JsonObject]] | None = None,
) -> ContentId:
    """``stage`` then ``publish``: write the package of ``ingested`` sources at ``destination``.

    Returns the package id; the package appears whole or not at all.
    """
    staged = stage(
        destination,
        workspace,
        ledger,
        ingested,
        materialise=materialise,
        source=source,
        extra=extra,
        derived=derived,
    )
    try:
        return publish(staged)
    except BaseException:
        staged.discard()
        raise


def _head_locations(
    records: Iterable[Any], wanted: Iterable[ContentId]
) -> dict[ContentId, LocalPath | RawLocalPath]:
    """For each wanted source, the first local location whose latest revision holds it, if any.

    Only the head of each location's chain counts: a superseded revision's location holds other
    bytes, or none, by now. An external location is a connector's to fetch (MVL-45), so it is not
    one here.
    """
    chain = [r for r in records if isinstance(r, (SourceRevision, SourceAbsence))]
    superseded = {previous for entry in chain for previous in entry.supersedes}
    missing, found = set(wanted), {}
    for revision in sorted(chain, key=lambda entry: entry.id):
        if not isinstance(revision, SourceRevision) or revision.id in superseded:
            continue
        location = revision.location
        if revision.content_id in missing and isinstance(location, (LocalPath, RawLocalPath)):
            found[revision.content_id] = location
            missing.remove(revision.content_id)
    return found


def _land(
    scratch: Path, locations: Mapping[ContentId, LocalPath | RawLocalPath], source: SourceOpener
) -> dict[ContentId, Content]:
    """Stream each source from its location, opened through ``source``, into ``scratch``.

    Opening applies the source's policy, so a symlink or special file where a source was is
    refused, not followed. What lands is what ``package_contents`` hashes against the source's
    content id, and what is moved into ``blobs/``: what is checked is what ships.
    """
    landed: dict[ContentId, Content] = {}
    for content, location in sorted(locations.items()):
        target = scratch / content.removeprefix("sha256:")
        with source.open(location) as data, target.open("xb") as copy:
            shutil.copyfileobj(data, copy, _COPY_SIZE)
        landed[content] = target
    return landed


def export(package_root: Path, destination: Path, source: SourceOpener) -> ContentId:
    """Copy a package with its sources materialised, each read through ``source``.

    A referenced source is read from the location its revisions record, opened through ``source``
    so its policy applies, streamed into the export, and checked against its content id as it
    lands: a changed source fails the export. A source with no local location left (its last
    known state is absence) fails the export if the package's records cite it, and otherwise
    stays referenced: nothing in the package was read from it. The export appears at
    ``destination``, which must not exist, whole or not at all.
    """
    package = read_package(package_root)
    wanted = [h.content_id for h in package.manifest.sources if h.content_id not in package.blobs]
    locations = _head_locations(package.records, wanted)
    if lost := sorted(cited_sources(package.records).intersection(wanted) - set(locations)):
        raise PackageError(f"no local location holds sources the package cites: {lost}")
    with _staged(destination) as staging:
        scratch = staging / ".scratch"
        scratch.mkdir()
        blobs = {**package.blobs, **_land(scratch, locations, source)}
        contents = package_contents(
            package.records,
            series=package.series,
            blobs=blobs,
            store=package.manifest.store,
            derived=package.derived,
        )
        copied = _lay_out(staging, contents, movable=scratch)
        scratch.rmdir()  # every landed source was moved into place
        _check_copies(staging, contents, copied)  # the original's series, hashed then copied
    return package_id(contents)
