"""Finding and reading a folder's manifest, and the lineage it enters (ADR 0047 §3, §4).

A manifest is a file inside the folder it describes: ``neptune.yaml`` (or ``neptune.yml``, or
``neptune.json``) at the root, found automatically, or another file below the root named by the
caller. Being inside the root makes it evidence like any other file: walked, hashed and listed by
the package, so editing it is a new revision and a new lineage, and every finding about it cites
its own bytes by JSON pointer.

It is read through the walk's own safe ``open`` (never through a symlink, regular files only), at
most ``MAX_BYTES``, and parsed whole before anything is walked. One that cannot be used is a
``ManifestError``: a configuration error, never half-applied.
"""

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Final

from neptune.discovery.source import LocalSource, SkipReason, SourceAccessError
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.manifest.reader import MAX_BYTES, ManifestError
from neptune.manifest.schema import Manifest, parse_manifest
from neptune.model.ids import ContentId
from neptune.model.provenance import EvidenceRef, JsonPointer, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath, local_location

MANIFEST_ID: Final = "neptune.manifest"
MANIFEST_VERSION: Final = "0.1.0"
MANIFEST_NAMES: Final = ("neptune.yaml", "neptune.yml", "neptune.json")


@dataclass(frozen=True)
class LoadedManifest:
    """A manifest read from the root: its declarations, where it is and the bytes' content id.

    ``transform`` is the manifest's producer record: its config is the manifest's location, its
    content id and every declaration, so a package made under a manifest names exactly which one,
    and one made under an edited manifest is another lineage.
    """

    manifest: Manifest
    location: LocalPath
    content_id: ContentId
    size: int

    @cached_property
    def transform(self) -> TransformRecord:
        return transform_record(
            adapter_id=MANIFEST_ID,
            adapter_version=MANIFEST_VERSION,
            config={
                "declarations": self.manifest.to_json(),
                "location": self.location.to_json(),
                "source": self.content_id,
            },
        )

    def cite(self, pointer: str) -> EvidenceRef:
        """The manifest's bytes at ``pointer``: what a stated value or a finding about it cites."""
        return EvidenceRef(self.content_id, (JsonPointer(pointer),))


def parse_bytes(data: bytes, name: str) -> Manifest:
    """``data`` as a manifest, JSON when ``name`` ends ``.json``; errors name the file."""
    try:
        return parse_manifest(data, json_syntax=name.lower().endswith(".json"))
    except ManifestError as exc:
        raise ManifestError(f"{name}: {exc}") from None


def discover(source: LocalSource) -> LocalPath | None:
    """The root's manifest, if it has one; ``ManifestError`` if it has more than one name."""
    if source.is_file:
        return None
    found = [name for name in MANIFEST_NAMES if _exists(source, LocalPath(name))]
    if len(found) > 1:
        raise ManifestError(f"the folder has {' and '.join(found)}; keep one manifest")
    return LocalPath(found[0]) if found else None


def _exists(source: LocalSource, location: LocalPath) -> bool:
    try:
        stream = source.open(location)
    except SourceAccessError as exc:  # there, but not readable as a manifest: ``read`` says why
        return exc.reason is not SkipReason.MISSING
    except OSError:
        return True
    stream.close()
    return True


def locate(source: LocalSource, path: os.PathLike[str] | str) -> LocalPath:
    """The root-relative location of the manifest file ``path`` names, which must be in the root.

    ``path`` is as the caller typed it: relative to the working directory, or absolute. Only its
    directory is resolved (the file itself is opened without following a link).
    """
    if source.is_file:
        raise ManifestError("a single-file source has no manifest; ingest its folder")
    given = os.fspath(path)
    head, name = os.path.split(given)
    if not name or name in (".", ".."):
        raise ManifestError(f"{given} does not name a manifest file")
    try:
        directory = os.path.realpath(head or ".", strict=True)
        root = os.path.realpath(source.root, strict=True)
    except OSError as exc:
        raise ManifestError(f"{given} cannot be found: {exc.strerror}") from None
    relative = os.path.relpath(Path(directory) / name, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise ManifestError(
            f"{given} is outside the folder; a manifest is evidence about the folder it is in, "
            "so the package must hold it: put it in the folder"
        )
    location = local_location(os.fsencode(relative).replace(os.sep.encode(), b"/"))
    if isinstance(location, RawLocalPath):
        raise ManifestError(f"{given}: a manifest's path must be UTF-8")
    return location


def read(source: LocalSource, location: LocalPath) -> LoadedManifest:
    """The manifest at ``location``, opened as the walk opens any file, and parsed."""
    try:
        stream = source.open(location)
    except SourceAccessError as exc:
        raise ManifestError(f"{location.path} cannot be read: {exc.reason}") from None
    except OSError as exc:
        raise ManifestError(f"{location.path} cannot be read: {exc.strerror}") from None
    try:
        with stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ManifestError(f"{location.path} cannot be read: {exc.strerror}") from None
    if len(data) > MAX_BYTES:
        raise ManifestError(f"{location.path} is larger than {MAX_BYTES} bytes")
    manifest = parse_bytes(data, location.path)
    return LoadedManifest(manifest, location, content_id(data), len(data))
