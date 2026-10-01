"""Measure the two archive passes on a generated gzip-compressed tar (MVL-57, ADR 0032).

Usage: ``python stress_archive_passes.py WORKDIR MIB [MIB ...]``

For each size, writes ``WORKDIR/archive-<MIB>.tar.gz``: a tar of 4 MiB members of seeded random
(incompressible) bytes, ``MIB`` MiB in all, gzip level 1, so nothing large is committed and the
same sizes give the same bytes. Then it measures, and prints one JSON object keyed by size:

- ``listing_bytes`` and ``listing_seconds``: what the probe engine (``ProbeEngine.probe``, every
  built-in adapter, the container listing included) reads of the archive and how long it takes,
  through a reader that counts what it serves; ``listing_members`` and ``listing_complete``: what
  the listing reports (incomplete once it reaches its budget);
- ``inspect_seconds`` and ``inflated_bytes``: the hardening inspector (``inspect_archive``)
  inflating the whole archive, and the bytes it read out of it; ``inspect_complete``;
- ``archive_bytes``: the compressed size.

Each archive is removed once measured. Times are this host's and vary; byte counts do not.
"""

import io
import json
import os
import random
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Final

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.archive import inspect_archive
from neptune.discovery.probe import ProbeEngine
from neptune.identity.hashing import digest_stream
from neptune.model.ids import ContentId

MIB: Final = 1024 * 1024
MEMBER: Final = 4 * MIB


class CountingFile:
    """A file's bytes as a ``SourceReader``, counting every byte it serves."""

    def __init__(self, path: Path, content_id: ContentId) -> None:
        self._fd = os.open(path, os.O_RDONLY)
        self._size = os.fstat(self._fd).st_size
        self._content_id = content_id
        self.served = 0

    @property
    def content_id(self) -> ContentId:
        return self._content_id

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        piece = os.pread(self._fd, min(length, max(0, self._size - offset)), offset)
        self.served += len(piece)
        return piece

    def close(self) -> None:
        os.close(self._fd)


def generate(path: Path, mib: int) -> None:
    """A gzip-compressed tar of ``mib`` MiB of incompressible members, from a fixed seed."""
    rng = random.Random(57)
    with tarfile.open(path, "w:gz", compresslevel=1, format=tarfile.PAX_FORMAT) as archive:
        left, index = mib * MIB, 0
        while left > 0:
            size = min(MEMBER, left)
            info = tarfile.TarInfo(f"frames/{index:04d}.bin")
            info.size, info.mtime, info.mode = size, 0, 0o644
            archive.addfile(info, io.BytesIO(rng.randbytes(size)))
            left, index = left - size, index + 1


def measure_one(workdir: Path, mib: int) -> dict[str, Any]:
    path = workdir / f"archive-{mib}.tar.gz"
    generate(path, mib)
    try:
        with path.open("rb") as stream:
            content_id = digest_stream(stream).content_id
        reader = CountingFile(path, content_id)
        try:
            engine = ProbeEngine(AdapterRegistry(builtin_adapters()))
            started = time.perf_counter()
            probed = engine.probe(reader, path.name)
            listing_seconds = time.perf_counter() - started
        finally:
            reader.close()
        assert probed.container is not None, "the head did not sniff as a container"
        with tempfile.TemporaryDirectory(dir=workdir) as scratch, path.open("rb") as stream:
            started = time.perf_counter()
            report = inspect_archive(
                stream, source=content_id, size=reader.size, scratch=Path(scratch)
            )
            inspect_seconds = time.perf_counter() - started
        return {
            "archive_bytes": reader.size,
            "inflated_bytes": sum(member.read_bytes for member in report.members),
            "inspect_complete": report.complete,
            "inspect_seconds": round(inspect_seconds, 3),
            "listing_bytes": reader.served,
            "listing_complete": probed.container.complete,
            "listing_members": len(probed.container.members),
            "listing_seconds": round(listing_seconds, 4),
        }
    finally:
        path.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    workdir = Path(argv[0])
    workdir.mkdir(parents=True, exist_ok=True)
    measured = {str(mib): measure_one(workdir, mib) for mib in (int(arg) for arg in argv[1:])}
    sys.stdout.write(json.dumps(measured, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
