"""Generate the hostile fixtures: archives that lie, overflow, nest, traverse or end early.

Run ``uv run python tests/fixtures/hostile/make_hostile.py`` to rewrite the committed files. Every
file is built from constants here, with no clock, randomness or host path, so ``build()`` is
deterministic; ``tests/integration/test_hostile_fixtures.py`` checks the committed files against it.
Each file is small on disk and large, wrong or dangerous only as declared. ``README.md`` lists them.

Symlinks and odd file names cannot be committed portably, so ``build_tree`` creates that part of
the suite in a directory the tests give it: loops, escapes into a sibling canary directory, names
that look like traversal, a FIFO, a deep tree, and a benign file that must still ingest.
"""

import gzip
import io
import os
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent
DOS_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
MiB: Final = 1 << 20
BOMB_SIZE: Final = 48 * MiB  # inflates 1000:1 from about 48 KB
MANY_MEMBERS: Final = 1_200
NESTING: Final = 6  # outer zip is depth 1; the zip holding leaf.txt is depth 6
PAX_HEADER_SIZE: Final = 2 * MiB  # above neptune.discovery.archive.MAX_HEADER_SIZE
HUGE_DECLARED: Final = 4 << 30  # a 4 GiB member whose data is absent
TEXT: Final = b"alpha bravo charlie delta echo foxtrot golf hotel india juliet\n"
CANARY: Final = b"CANARY: bytes outside the ingest root; never to be read\n"


# --- builders ----------------------------------------------------------------------------------


def zip_info(name: str, mode: int = 0o100644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=DOS_EPOCH)
    info.create_system = 3  # unix, so external_attr carries a mode
    info.external_attr = mode << 16
    return info


def make_zip(
    members: list[tuple[str, bytes] | tuple[zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression, compresslevel=9) as archive:
        for member, data in members:
            info = member if isinstance(member, zipfile.ZipInfo) else zip_info(member)
            info.compress_type = compression  # writestr takes the method from the ZipInfo
            archive.writestr(info, data)
    return buffer.getvalue()


def tar_info(
    name: str, size: int = 0, kind: bytes = tarfile.REGTYPE, linkname: str = ""
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.type = kind
    info.linkname = linkname
    info.mode = 0o644 if kind == tarfile.REGTYPE else 0o755
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def make_tar(entries: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


def gzip_bytes(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as stream:
        stream.write(data)
    return buffer.getvalue()


def text_tar() -> bytes:
    return make_tar(
        [
            (tar_info("a.txt", len(TEXT) * 10), TEXT * 10),
            (tar_info("b.txt", len(TEXT) * 30), TEXT * 30),
        ]
    )


# --- fixtures ----------------------------------------------------------------------------------


def bomb_zip() -> bytes:
    return make_zip([("zeros.bin", bytes(BOMB_SIZE))], compression=zipfile.ZIP_DEFLATED)


def bomb_tar_gz() -> bytes:
    return gzip_bytes(make_tar([(tar_info("zeros.bin", BOMB_SIZE), bytes(BOMB_SIZE))]))


def many_members_zip() -> bytes:
    return make_zip([(f"m{index:04d}", b"") for index in range(MANY_MEMBERS)])


def nested_zip() -> bytes:
    inner = make_zip([("leaf.txt", b"leaf\n")])
    for level in range(NESTING - 1, 0, -1):
        inner = make_zip([(f"level{level + 1}.zip", inner)])
    return inner


def mixed_zip() -> bytes:
    """Depth 3 with the default limits: a zip holding a tar.gz holding a zip holding a file."""
    innermost = make_zip([("leaf.txt", b"leaf\n")])
    tgz = gzip_bytes(make_tar([(tar_info("inner.zip", len(innermost)), innermost)]))
    return make_zip([("runs.tar.gz", tgz), ("notes.txt", TEXT)])


def traversal_zip() -> bytes:
    members: list[tuple[str, bytes] | tuple[zipfile.ZipInfo, bytes]] = [
        ("../../etc/passwd", b"root:x:0:0\n"),
        ("/etc/shadow", b"root:!:0\n"),
        ("a/../../b.txt", b"b\n"),
        ("nul_hidden.txt", b"after the nul\n"),  # the underscore becomes NUL below
        ("", b"unnamed\n"),
        ("a//b", b"double slash\n"),
        ("C:\\win\\x", b"backslashes\n"),
        ("ok/fine.txt", b"fine\n"),
        (zip_info("link", 0o120777), b"/etc/passwd"),
        (zip_info("pipe", 0o010644), b""),
    ]
    data = make_zip(members)
    assert data.count(b"nul_hidden.txt") == 2  # local header and central directory
    return data.replace(b"nul_hidden.txt", b"nul\x00hidden.txt")


def links_tar() -> bytes:
    return make_tar(
        [
            (tar_info("ok.txt", len(TEXT)), TEXT),
            (tar_info("latest", kind=tarfile.SYMTYPE, linkname="/etc/passwd"), b""),
            (tar_info("rel", kind=tarfile.SYMTYPE, linkname="../outside/canary.txt"), b""),
            (tar_info("hard", kind=tarfile.LNKTYPE, linkname="ok.txt"), b""),
            (tar_info("pipe", kind=tarfile.FIFOTYPE), b""),
            (tar_info("dir", kind=tarfile.DIRTYPE), b""),
            (tar_info("/abs/file", len(TEXT)), TEXT),
            (tar_info("../up.txt", len(TEXT)), TEXT),
        ]
    )


def pax_bomb_tar_gz() -> bytes:
    """A pax extended header declaring 2 MiB; tarfile would read it whole into memory."""
    header = tar_info("pax", PAX_HEADER_SIZE, kind=tarfile.XHDTYPE)
    return gzip_bytes(make_tar([(header, bytes(PAX_HEADER_SIZE))]))


def huge_member_tar() -> bytes:
    """A header declaring a 4 GiB member followed by nothing: large declared, 1 KB on disk."""
    header = tar_info("huge.bin", HUGE_DECLARED)
    return header.tobuf(tarfile.USTAR_FORMAT) + bytes(512)


def truncated_zip() -> bytes:
    data = make_zip([("a.txt", TEXT * 10), ("b.txt", TEXT * 20), ("c.txt", TEXT * 30)])
    return data[: len(data) * 6 // 10]


def truncated_tar() -> bytes:
    data = text_tar()
    assert data[2048:2560].rstrip(b"\x00")  # b.txt's data starts at 2048
    return data[: 2048 + 900]


def truncated_tar_gz() -> bytes:
    data = gzip_bytes(text_tar())
    return data[: len(data) * 6 // 10]


def corrupt_member_zip() -> bytes:
    data = bytearray(make_zip([("t.txt", TEXT * 100)], compression=zipfile.ZIP_DEFLATED))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        (info,) = archive.infolist()
    position = info.header_offset + 30 + len(info.filename) + len(info.extra) + 5
    data[position] ^= 0xFF
    return bytes(data)


def encrypted_zip() -> bytes:
    data = bytearray(make_zip([("secret.txt", TEXT)]))
    local = data.find(b"PK\x03\x04")
    central = data.find(b"PK\x01\x02")
    data[local + 6] |= 0x01  # general-purpose flag bit 0: encrypted
    data[central + 8] |= 0x01
    return bytes(data)


FIXTURES: Final[dict[str, Callable[[], bytes]]] = {
    "bomb.zip": bomb_zip,
    "bomb.tar.gz": bomb_tar_gz,
    "many_members.zip": many_members_zip,
    "nested.zip": nested_zip,
    "mixed.zip": mixed_zip,
    "traversal.zip": traversal_zip,
    "links.tar": links_tar,
    "pax_bomb.tar.gz": pax_bomb_tar_gz,
    "huge_member.tar": huge_member_tar,
    "truncated.zip": truncated_zip,
    "truncated.tar": truncated_tar,
    "truncated.tar.gz": truncated_tar_gz,
    "corrupt_member.zip": corrupt_member_zip,
    "encrypted.zip": encrypted_zip,
}

# Deflate and gzip output may differ between zlib builds; these are checked by content, not bytes.
COMPRESSED: Final = frozenset(
    {
        "bomb.zip",
        "bomb.tar.gz",
        "mixed.zip",
        "pax_bomb.tar.gz",
        "truncated.tar.gz",
        "corrupt_member.zip",
    }
)


def build() -> dict[str, bytes]:
    return {name: make() for name, make in FIXTURES.items()}


# --- the tree built at test time ---------------------------------------------------------------

BENIGN: Final = b"benign evidence that must still be ingested\n"
ODD_NAMES: Final = (
    "..hidden",
    "a..b",
    "..\\..\\etc\\passwd",
    "%2e%2e%2fetc%2fpasswd",
    " ..",
    "...",
    "x" * 255,
)
DEEP: Final = 64


def build_tree(root: Path, outside: Path) -> None:
    """Create the symlink, special-file and odd-name part of the suite under ``root``.

    ``outside`` is a sibling directory holding the canary that no read may reach.
    """
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "canary.txt").write_bytes(CANARY)
    (root / "real").mkdir(parents=True)
    (root / "real/run.bin").write_bytes(bytes(range(256)) * 4)
    (root / "benign.txt").write_bytes(BENIGN)
    (root / "names").mkdir()
    for name in ODD_NAMES:
        (root / "names" / name).write_bytes(name.encode())
    links = root / "links"
    links.mkdir()
    (links / "loop").symlink_to("loop")
    (links / "ping").symlink_to("pong")
    (links / "pong").symlink_to("ping")
    (links / "escape_abs").symlink_to(outside / "canary.txt")
    (links / "escape_rel").symlink_to(Path("..") / ".." / outside.name / "canary.txt")
    (links / "escape_dir").symlink_to(outside)
    (links / "inside").symlink_to(Path("..") / "real" / "run.bin")
    (links / "inside_abs").symlink_to(root / "real" / "run.bin")
    (links / "dangling").symlink_to("nowhere")
    (links / "chain").symlink_to("inside")
    (links / "dotdot_inside").symlink_to(Path("..") / "links" / ".." / "benign.txt")
    deep = root / "deep"
    for _ in range(DEEP):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_bytes(b"deep leaf\n")
    os.mkfifo(root / "fifo")
    archives = root / "archives"
    archives.mkdir()
    for name in FIXTURES:
        archives.joinpath(name).write_bytes((HERE / name).read_bytes())


if __name__ == "__main__":
    for name, data in build().items():
        HERE.joinpath(name).write_bytes(data)
        assert len(data) < 512 * 1024, (name, len(data))
