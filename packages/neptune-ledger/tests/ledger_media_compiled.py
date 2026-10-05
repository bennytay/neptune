"""A package the compiler itself wrote from two camera recordings (MVL-96), and its generator.

``tests/fixtures/media/compiled/sources/`` is an ingest root: a quadruped's head camera
(``sensor_msgs/msg/Image``, zstd chunks) and a manipulator's wrist camera
(``sensor_msgs/msg/CompressedImage``, lz4 chunks), rewritten from the frozen fixtures with small
chunks so a recording spans several. ``compiled/package/`` is what ``neptune ingest`` wrote from
it, run as a subprocess (a workspace member never imports the compiler's runtime), without
``volatile/``. Its series cite every message as the compiler does: the Chunk record's byte
range, then the Message record's byte range in the chunk's uncompressed records.

Regenerate with ``uv run python tests/ledger_media_compiled.py`` from this package; the tests
read the committed files.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

from ledger_media_fixtures import FIXTURES, fixture, rewrite_mcap

COMPILED: Final = FIXTURES / "compiled"
SOURCES: Final = COMPILED / "sources"
PACKAGE: Final = COMPILED / "package"
HEAD: Final = "legged/head_camera.mcap"
WRIST: Final = "arm/wrist_camera.mcap"


def sources() -> dict[str, bytes]:
    from mcap.writer import CompressionType

    return {
        HEAD: rewrite_mcap(
            fixture("head_camera.mcap"), compression=CompressionType.ZSTD, chunk_size=256
        ),
        WRIST: rewrite_mcap(
            fixture("wrist_camera.mcap"), compression=CompressionType.LZ4, chunk_size=512
        ),
    }


def build() -> None:
    shutil.rmtree(COMPILED, ignore_errors=True)
    for path, data in sources().items():
        (SOURCES / path).parent.mkdir(parents=True, exist_ok=True)
        (SOURCES / path).write_bytes(data)
    command = [str(Path(sys.executable).parent / "neptune"), "ingest", str(SOURCES)]
    flags = ["--isolation", "in_process", "--job", "ledger-media"]
    with tempfile.TemporaryDirectory() as workspace:
        subprocess.run(
            [*command, "--out", str(PACKAGE), "-w", workspace, *flags],
            check=True,
            capture_output=True,
        )
    shutil.rmtree(PACKAGE / "volatile", ignore_errors=True)


if __name__ == "__main__":
    build()
