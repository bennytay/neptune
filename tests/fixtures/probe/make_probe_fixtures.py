"""Build the probe engine's fixtures: signature-only files and small real containers.

Run ``uv run python tests/fixtures/probe/make_probe_fixtures.py`` to rewrite everything under
``tests/fixtures/probe/``. The output is byte-for-byte deterministic (fixed timestamps, no
host data), and ``tests/unit/discovery/test_probe_engine.py`` checks that the committed files are
exactly what ``build()`` returns.

Signature files hold a format's magic and a few plausible header bytes: enough to sniff, not
valid files. Each has a copy under a name that says nothing (``renamed/``), for the acceptance
criterion that a renamed or extensionless file is still detected. Containers are built with the
standard library and then trimmed to their exact end, so they stay small.
"""

import bz2
import gzip
import io
import lzma
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent
TEXT: Final = HERE.parent / "text"

# name -> (sniffed signature name, bytes). The names carry the usual extension.
SIGNATURE_FILES: Final[dict[str, tuple[str, bytes]]] = {
    "recording.mcap": ("MCAP", b"\x89MCAP0\r\n\x01" + bytes(8) + b"mcap-profile"),
    "drive.bag": ("ROS bag 2.0", b"#ROSBAG V2.0\n" + bytes(16)),
    "rosbag2.db3": ("SQLite 3 database", b"SQLite format 3\x00" + bytes(84)),
    "flight.ulg": ("ULog", b"ULog\x01\x12\x35\x01" + bytes(8)),
    "capture.pcap": ("pcap", b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00" + bytes(16)),
    "capture.pcapng": ("pcapng", b"\x0a\x0d\x0d\x0a\x1c\x00\x00\x00\x4d\x3c\x2b\x1a"),
    "scan.las": ("LAS point cloud", b"LASF\x00\x00" + bytes(20)),
    "scan.pcd": ("PCD point cloud", b"# .PCD v0.7 - Point Cloud Data file format\nVERSION 0.7\n"),
    "mesh.ply": ("PLY", b"ply\nformat binary_little_endian 1.0\nelement vertex 0\nend_header\n"),
    "scan.e57": ("E57", b"ASTM-E57\x01\x00\x00\x00" + bytes(16)),
    "model.glb": ("glTF binary", b"glTF\x02\x00\x00\x00\x20\x00\x00\x00"),
    "robot.urdf": ("XML", b'<?xml version="1.0"?>\n<robot name="x"/>\n'),
    "manual.pdf": ("PDF", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"),
    "photo.png": ("PNG", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(17)),
    "photo.jpg": ("JPEG", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + bytes(8)),
    "photo.tif": ("TIFF", b"II*\x00\x08\x00\x00\x00" + bytes(8)),
    "clip.gif": ("GIF", b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"),
    "photo.webp": ("WebP", b"RIFF\x1a\x00\x00\x00WEBPVP8 " + bytes(8)),
    "audio.wav": ("WAVE audio", b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(8)),
    "clip.avi": ("AVI", b"RIFF\x24\x00\x00\x00AVI LIST" + bytes(8)),
    "clip.mp4": ("ISO base media (MP4, MOV, HEIF)", b"\x00\x00\x00\x18ftypisom" + bytes(12)),
    "clip.mkv": ("Matroska / WebM", b"\x1a\x45\xdf\xa3\x93\x42\x82\x88matroska"),
    "audio.ogg": ("Ogg", b"OggS\x00\x02" + bytes(20)),
    "audio.flac": ("FLAC", b"fLaC\x00\x00\x00\x22" + bytes(16)),
    "data.h5": ("HDF5", b"\x89HDF\r\n\x1a\n\x00\x00\x00\x00\x00\x08\x08\x00"),
    "table.parquet": ("Parquet", b"PAR1\x15\x04\x15\x08" + bytes(8) + b"PAR1"),
    "array.npy": ("NumPy array", b"\x93NUMPY\x01\x00\x76\x00{'descr': '<f8', }"),
    "node": ("ELF executable", b"\x7fELF\x02\x01\x01\x00" + bytes(8)),
    "cloud.zst": ("zstd", b"\x28\xb5\x2f\xfd\x00\x58\x00\x00"),
    "cloud.7z": ("7-Zip archive", b"7z\xbc\xaf\x27\x1c\x00\x04" + bytes(24)),
}

TALLY: Final = b"TALLY1\n10 1\n20 2\n"
NOTES: Final = (TEXT / "notes.txt").read_bytes()
EPOCH: Final = (1980, 1, 1, 0, 0, 0)  # the earliest time a zip can state


def _zip_bytes(entries: list[tuple[str, bytes, int]], comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, EPOCH)
            info.compress_type = method
            info.external_attr = 0o040755 << 16 if name.endswith("/") else 0o100644 << 16
            archive.writestr(info, data)
        archive.comment = comment
    return buffer.getvalue()


def _tar_bytes(fmt: int, entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """A tar with its padding trimmed to the two end-of-archive blocks."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode() if data else ""
                archive.addfile(info)
            else:
                assert data is not None
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue().rstrip(b"\x00")
    return data + bytes(-len(data) % 512) + bytes(1024)


def _bomb_zip() -> bytes:
    """One deflated member that declares 4 MiB from a few KiB: a ratio over the default limit."""
    zeros = bytes(4 * 1024 * 1024)
    return _zip_bytes([("zeros.bin", zeros, zipfile.ZIP_DEFLATED)])


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for index, (name, (_, data)) in enumerate(SIGNATURE_FILES.items()):
        files[f"signatures/{name}"] = data
        files[f"renamed/{index:02d}_{Path(name).stem}"] = data

    members = _zip_bytes(
        [
            ("notes.txt", NOTES, zipfile.ZIP_DEFLATED),
            ("logs/", b"", zipfile.ZIP_STORED),
            ("logs/lift.tally", TALLY, zipfile.ZIP_STORED),
            ("logs/renamed", TALLY, zipfile.ZIP_DEFLATED),
            ("../escape.txt", b"outside\n", zipfile.ZIP_STORED),
            ("recording.mcap", SIGNATURE_FILES["recording.mcap"][1], zipfile.ZIP_DEFLATED),
        ],
        comment=b"neptune probe fixture",
    )
    files["containers/members.zip"] = members
    files["containers/truncated.zip"] = members[: len(members) // 2]
    files["containers/bomb.zip"] = _bomb_zip()
    files["containers/empty.zip"] = _zip_bytes([])

    tar = _tar_bytes(
        tarfile.USTAR_FORMAT,
        [
            ("notes.txt", NOTES, "file"),
            ("logs/", None, "dir"),
            ("logs/lift.tally", TALLY, "file"),
            ("latest", b"logs/lift.tally", "symlink"),
            ("drive.bag", SIGNATURE_FILES["drive.bag"][1], "file"),
        ],
    )
    files["containers/members.tar"] = tar
    files["containers/members.tar.gz"] = gzip.compress(tar, mtime=0)
    files["containers/longnames.tar"] = _tar_bytes(
        tarfile.PAX_FORMAT,
        [("deep/" * 30 + "flight.ulg", SIGNATURE_FILES["flight.ulg"][1], "file")],
    )
    files["containers/gnu_longname.tar"] = _tar_bytes(
        tarfile.GNU_FORMAT,
        [("gnu/" * 30 + "notes.txt", NOTES, "file")],
    )
    files["containers/notes.txt.gz"] = gzip.compress(NOTES, mtime=0)
    files["containers/notes.txt.bz2"] = bz2.compress(NOTES)
    files["containers/notes.txt.xz"] = lzma.compress(NOTES, format=lzma.FORMAT_XZ)
    files["containers/tally.xz"] = lzma.compress(TALLY, format=lzma.FORMAT_XZ)
    # A gzip whose deflate stream is damaged after its first bytes.
    good = gzip.compress(NOTES, mtime=0)
    files["containers/corrupt.gz"] = good[:14] + bytes(b ^ 0xFF for b in good[14:-8]) + good[-8:]
    # A tar whose first header fails its checksum.
    files["containers/badsum.tar"] = tar[:148] + b"0000000\x00" + tar[156:]
    # Nested three deep: a tar inside a gzip inside a zip.
    files["containers/nested.zip"] = _zip_bytes(
        [("members.tar.gz", files["containers/members.tar.gz"], zipfile.ZIP_STORED)]
    )
    return files


def main() -> None:
    for name, data in build().items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        sys.stdout.write(f"{len(data):>8}  {name}\n")


if __name__ == "__main__":
    main()
