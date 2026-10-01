"""Archive inspection within limits (ADR 0029 §2): every bomb, lie and defect is a finding."""

import functools
import gzip
import io
import struct
import tarfile
import tracemalloc
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import IO

import pytest

from neptune.discovery.archive import (
    COMPRESSION_RATIO_EXCEEDED,
    CORRUPT,
    HEADER_TOO_LARGE,
    MEMBER_CORRUPT,
    MEMBER_COUNT_EXCEEDED,
    MEMBER_ENCRYPTED,
    MEMBER_LINK,
    MEMBER_PATH_UNSAFE,
    MEMBER_SIZE_EXCEEDED,
    MEMBER_SPECIAL,
    MEMBER_TRUNCATED,
    NESTING_DEPTH_EXCEEDED,
    TOTAL_SIZE_EXCEEDED,
    TRUNCATED,
    UNRECOGNISED,
    ArchiveKind,
    ArchiveLimits,
    ArchiveReport,
    MemberKind,
    inspect_archive,
    limits_from_json,
    sniff,
)
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.model.finding import FindingCategory, Severity, ingest_finding_from_json
from neptune.model.provenance import ByteRange, EvidenceRef

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "hostile"
MiB = 1 << 20


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def inspect(
    data: bytes,
    tmp_path: Path,
    limits: ArchiveLimits | None = None,
    stream: IO[bytes] | None = None,
) -> ArchiveReport:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    report = inspect_archive(
        stream or io.BytesIO(data),
        source=content_id(data),
        size=len(data),
        scratch=scratch,
        limits=limits,
    )
    assert list(scratch.iterdir()) == []  # no spool outlives the inspection
    return report


def codes(report: ArchiveReport) -> list[str]:
    return [finding.code for finding in report.findings]


def with_checksum(block: bytearray) -> bytes:
    """A tar header block with its checksum recomputed after bytes were patched."""
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\x00 " % sum(block)
    return bytes(block)


def pax_tar(pax: dict[str, str], data: bytes) -> bytes:
    """One member of ``data`` behind a pax header holding ``pax`` verbatim."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("m")
        info.size = len(data)
        info.pax_headers = pax
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def gnu_long_names(links: int, name_size: int) -> Iterator[bytes]:
    """``links`` chained GNU long-name headers of ``name_size`` bytes each, then one member."""
    for _ in range(links):
        info = tarfile.TarInfo("././@LongLink")
        info.type = tarfile.GNUTYPE_LONGNAME
        info.size = name_size
        yield info.tobuf(tarfile.GNU_FORMAT)
        yield b"a" * name_size + bytes(-name_size % 512)
    yield tarfile.TarInfo("final").tobuf(tarfile.GNU_FORMAT) + bytes(1024)


def gnu_sparse_tar(chunk: bytes, expanded: int) -> bytes:
    """An old GNU sparse ``S`` member: ``chunk`` stored at offset 0, expanding to ``expanded``."""
    info = tarfile.TarInfo("s")
    info.type = tarfile.GNUTYPE_SPARSE
    info.size = len(chunk)
    block = bytearray(info.tobuf(tarfile.GNU_FORMAT))
    block[386:398] = b"%011o\x00" % 0
    block[398:410] = b"%011o\x00" % len(chunk)
    block[483:495] = b"%011o\x00" % expanded
    return with_checksum(block) + chunk + bytes(-len(chunk) % 512) + bytes(1024)


def gzip_chunks(chunks: Iterable[bytes]) -> bytes:
    """A gzip stream of ``chunks``, compressed as they come, so the input is never held whole."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return b"".join(compressor.compress(chunk) for chunk in chunks) + compressor.flush()


def at_depth(frames: int, run: Callable[[], ArchiveReport]) -> ArchiveReport:
    """``run()`` called ``frames`` stack frames deeper than the caller."""
    return run() if frames == 0 else at_depth(frames - 1, run)


ERROR_CODES = {
    "recursion_limit",
    "out_of_memory",
    "unsupported",
    "end_of_data",
    "bad_zip",
    "bad_tar",
    "bad_gzip",
    "bad_deflate",
    "bad_xz",
    "bad_struct",
    "bad_encoding",
    "bad_value",
    "bad_index",
    "bad_number",
    "os_error",
    "other",
}


def peak_memory(run: Callable[[], object]) -> int:
    tracemalloc.start()
    try:
        run()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# --- sniffing ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (b"PK\x03\x04" + bytes(28), ArchiveKind.ZIP),
        (b"PK\x05\x06" + bytes(18), ArchiveKind.ZIP),
        (b"\x1f\x8b\x08", ArchiveKind.GZIP),
        (b"BZh91AY", ArchiveKind.BZIP2),
        (b"\xfd7zXZ\x00", ArchiveKind.XZ),
        (bytes(257) + b"ustar\x00", ArchiveKind.TAR),
        (b"\x89MCAP0\r\n", None),
        (b"", None),
    ],
)
def test_sniff(head: bytes, kind: ArchiveKind | None) -> None:
    assert sniff(head) == kind


# --- bombs -------------------------------------------------------------------------------------


def test_a_zip_bomb_is_refused_before_a_byte_is_inflated(tmp_path: Path) -> None:
    data = fixture("bomb.zip")
    report = inspect(data, tmp_path)
    assert codes(report) == [COMPRESSION_RATIO_EXCEEDED]
    [finding] = report.findings
    assert finding.details["uncompressed"] == 48 * MiB
    assert finding.details["compressed"] == len(data)
    assert finding.subject == EvidenceRef(content_id(data), (ByteRange(0, len(data)),))
    assert (report.members, report.complete) == ((), False)
    assert peak_memory(lambda: inspect(data, tmp_path)) < 2 * MiB


def test_a_tar_gz_bomb_is_refused_from_its_first_header(tmp_path: Path) -> None:
    data = fixture("bomb.tar.gz")
    report = inspect(data, tmp_path)
    assert codes(report) == [COMPRESSION_RATIO_EXCEEDED]
    [finding] = report.findings
    assert isinstance(finding.subject, EvidenceRef)
    assert finding.subject.locator == (ByteRange(0, len(data)), ByteRange(0, 48 * MiB + 512))
    assert finding.details["name"] == "zeros.bin"
    assert report.kind is ArchiveKind.GZIP
    assert peak_memory(lambda: inspect(data, tmp_path)) < 2 * MiB


def test_declared_member_size_limit_skips_the_member(tmp_path: Path) -> None:
    limits = ArchiveLimits(max_compression_ratio=10_000, max_member_size=MiB)
    report = inspect(fixture("bomb.zip"), tmp_path, limits)
    assert codes(report) == [MEMBER_SIZE_EXCEEDED]
    [member] = report.members
    assert (member.name, member.declared_size, member.read_bytes) == ("zeros.bin", 48 * MiB, 0)
    assert report.complete  # a zip can skip a member without inflating it
    report = inspect(fixture("bomb.tar.gz"), tmp_path, limits)
    assert codes(report) == [MEMBER_SIZE_EXCEEDED]
    assert not report.complete  # a compressed tar cannot


def test_declared_total_budget_stops_everything(tmp_path: Path) -> None:
    limits = ArchiveLimits(max_compression_ratio=10_000, max_total_size=2 * MiB)
    report = inspect(fixture("bomb.zip"), tmp_path, limits)
    assert codes(report) == [TOTAL_SIZE_EXCEEDED]
    assert (report.members, report.complete) == ((), False)


def test_actual_bytes_are_bounded_when_nothing_is_declared(tmp_path: Path) -> None:
    data = gzip.compress(bytes(8 * MiB), mtime=0)  # a single stream: no declared size at all
    limits = ArchiveLimits(max_compression_ratio=1_000_000, max_member_size=MiB)
    report = inspect(data, tmp_path, limits)
    assert codes(report) == [MEMBER_SIZE_EXCEEDED]
    [member] = report.members
    assert (member.kind, member.declared_size, member.read_bytes) == (
        MemberKind.STREAM,
        None,
        MiB + 1,
    )
    assert peak_memory(lambda: inspect(data, tmp_path, limits)) < 4 * MiB

    report = inspect(data, tmp_path)  # default limits: the ratio cap stops the read
    assert codes(report) == [COMPRESSION_RATIO_EXCEEDED]
    assert report.members[0].read_bytes == 100 * len(data) + 1
    assert report.findings[0].details["compressed"] == len(data)


def test_member_count_is_checked_before_the_directory_is_parsed(tmp_path: Path) -> None:
    data = fixture("many_members.zip")
    report = inspect(data, tmp_path)
    assert (codes(report), len(report.members), report.complete) == ([], 1200, True)
    report = inspect(data, tmp_path, ArchiveLimits(max_members=1000))
    assert codes(report) == [MEMBER_COUNT_EXCEEDED]
    assert report.findings[0].details["members"] == 1200
    assert report.members == ()


def lying_count(data: bytes, entries: int) -> bytes:
    """``data`` with its end-of-central-directory record declaring ``entries`` members."""
    patched = bytearray(data)
    struct.pack_into("<HH", patched, patched.rfind(b"PK\x05\x06") + 8, entries, entries)
    return bytes(patched)


@functools.cache
def lying_directory_zip() -> bytes:
    """30,000 empty members (a 1.5 MB directory) whose end record declares one."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(30_000):
            archive.writestr(zipfile.ZipInfo(f"{index:05d}", (1980, 1, 1, 0, 0, 0)), b"")
    return lying_count(buffer.getvalue(), 1)


@pytest.mark.parametrize("nested", [False, True])
def test_a_directory_that_outnumbers_its_end_record_is_refused_before_it_is_parsed(
    tmp_path: Path, hostile: ModuleType, nested: bool
) -> None:
    """zipfile builds every entry before a caller sees one; the count is walked first, unheld."""
    data = lying_directory_zip()
    if nested:  # a directory bounded by the member limit, not the file on disk
        data = hostile.make_zip([("inner.zip", data)], compression=zipfile.ZIP_DEFLATED)
    report = inspect(data, tmp_path)
    assert codes(report) == [MEMBER_COUNT_EXCEEDED]
    assert report.findings[0].details["members"] == 10_001
    assert peak_memory(lambda: inspect(data, tmp_path)) < 4 * MiB


def test_a_lying_count_is_caught_at_the_exact_limit(tmp_path: Path) -> None:
    data = lying_count(fixture("many_members.zip"), 1)
    report = inspect(data, tmp_path, ArchiveLimits(max_members=1200))
    assert (codes(report), len(report.members)) == ([], 1200)
    report = inspect(data, tmp_path, ArchiveLimits(max_members=1199))
    assert codes(report) == [MEMBER_COUNT_EXCEEDED]
    assert (report.findings[0].details["members"], report.members) == (1200, ())


def test_a_directory_larger_than_its_member_limit_allows_is_refused_unread(
    tmp_path: Path,
) -> None:
    report = inspect(lying_directory_zip(), tmp_path, ArchiveLimits(max_members=100))
    assert codes(report) == [HEADER_TOO_LARGE]
    details = report.findings[0].details
    assert (details["directory_size"], details["max_directory_size"]) == (51 * 30_000, MiB)


def test_a_directory_larger_than_what_precedes_it_is_corrupt(
    tmp_path: Path, hostile: ModuleType
) -> None:
    data = bytearray(hostile.make_zip([("a.txt", b"x")]))
    struct.pack_into("<I", data, data.rfind(b"PK\x05\x06") + 12, 0x7FFFFFFF)
    report = inspect(bytes(data), tmp_path)
    assert codes(report) == [CORRUPT]
    assert report.findings[0].details == {"size": len(data), "directory_size": 0x7FFFFFFF}


def test_tar_member_count_stops_the_stream(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for index in range(5):
            archive.addfile(tarfile.TarInfo(f"m{index}"))
    report = inspect(buffer.getvalue(), tmp_path, ArchiveLimits(max_members=3))
    assert codes(report) == [MEMBER_COUNT_EXCEEDED]
    assert (len(report.members), report.complete) == (3, False)


# --- nesting -----------------------------------------------------------------------------------


def test_nesting_stops_at_the_depth_limit(tmp_path: Path) -> None:
    report = inspect(fixture("nested.zip"), tmp_path)
    assert codes(report) == [NESTING_DEPTH_EXCEEDED]
    [finding] = report.findings
    assert (finding.details["name"], finding.details["depth"]) == ("level4.zip", 4)
    assert isinstance(finding.subject, EvidenceRef) and len(finding.subject.locator) == 3
    level2 = report.members[0].nested
    assert level2 is not None and level2.depth == 2
    level3 = level2.members[0].nested
    assert level3 is not None and level3.depth == 3
    assert level3.findings == report.findings  # a nested report carries what was found below it
    assert level3.members[0].name == "level4.zip" and level3.members[0].nested is None
    assert report.complete  # nothing was lost at this level; the member was simply not opened


def test_nesting_within_the_limit_reaches_the_leaf(tmp_path: Path) -> None:
    report = inspect(fixture("nested.zip"), tmp_path, ArchiveLimits(max_depth=10))
    assert codes(report) == []
    depth = 1
    while report.members[0].nested is not None:
        report = report.members[0].nested
        depth += 1
    assert depth == 6
    assert (report.members[0].name, report.members[0].read_bytes) == ("leaf.txt", 5)


def test_mixed_formats_nest_within_the_default_depth(tmp_path: Path) -> None:
    report = inspect(fixture("mixed.zip"), tmp_path)
    assert codes(report) == []
    tgz = report.members[0].nested
    assert tgz is not None and tgz.kind is ArchiveKind.GZIP and tgz.depth == 2
    inner = tgz.members[0].nested
    assert inner is not None and inner.kind is ArchiveKind.ZIP and inner.depth == 3
    assert [m.name for m in inner.members] == ["leaf.txt"]
    report = inspect(fixture("mixed.zip"), tmp_path, ArchiveLimits(max_depth=2))
    assert codes(report) == [NESTING_DEPTH_EXCEEDED]
    assert report.findings[0].details["name"] == "inner.zip"


# --- names, links and specials -----------------------------------------------------------------


def test_unsafe_member_names_are_recorded_and_not_read(tmp_path: Path) -> None:
    report = inspect(fixture("traversal.zip"), tmp_path)
    unsafe = [f for f in report.findings if f.code == MEMBER_PATH_UNSAFE]
    assert [(f.details["name"], f.details["problem"]) for f in unsafe] == [
        ("../../etc/passwd", "parent_reference"),
        ("/etc/shadow", "absolute"),
        ("a/../../b.txt", "parent_reference"),
        ("nul\x00hidden.txt", "nul"),
        ("", "empty"),
    ]
    assert all(
        f.severity is Severity.ERROR and f.category is FindingCategory.SKIPPED for f in unsafe
    )
    read = {m.name: m.read_bytes for m in report.members}
    assert read["../../etc/passwd"] == 0 and read["ok/fine.txt"] == 5
    assert read["a//b"] == 13 and read["C:\\win\\x"] == 12  # tolerated: characters, not escapes
    [link] = [f for f in report.findings if f.code == MEMBER_LINK]
    assert link.details == {"link": "symlink", "name": "link", "target": "/etc/passwd"}
    [special] = [f for f in report.findings if f.code == MEMBER_SPECIAL]
    assert (special.details["name"], special.severity) == ("pipe", Severity.INFO)
    assert report.complete


def test_members_cpython_writes_are_files_and_directories(tmp_path: Path) -> None:
    """``ZipFile.writestr`` stores ``0o600 << 16``: permissions and no file type, a regular file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plain.txt", b"hello")
        archive.writestr("sub/", b"")
        archive.mkdir("made")
        archive.writestr("sub/data.bin", bytes(10))
    report = inspect(buffer.getvalue(), tmp_path)
    assert (codes(report), report.complete) == ([], True)
    assert [(m.name, m.kind, m.read_bytes) for m in report.members] == [
        ("plain.txt", MemberKind.FILE, 5),
        ("sub/", MemberKind.DIRECTORY, 0),
        ("made/", MemberKind.DIRECTORY, 0),
        ("sub/data.bin", MemberKind.FILE, 10),
    ]


@pytest.mark.parametrize(
    ("system", "attributes", "kind"),
    [
        (0, 0o010644 << 16, MemberKind.FILE),  # a DOS host's high bits are not a mode
        (0, 0x10, MemberKind.DIRECTORY),  # the MS-DOS directory attribute
        (3, 0x20, MemberKind.FILE),  # a Unix host, no file type: the archive attribute only
        (3, 0o010644 << 16, MemberKind.SPECIAL),  # a FIFO
        (3, 0o040755 << 16, MemberKind.DIRECTORY),  # a directory by its mode, not its name
        (19, 0o120777 << 16, MemberKind.SYMLINK),  # OS X stores a mode too
    ],
)
def test_zip_member_kinds_follow_the_host_conventions(
    tmp_path: Path, system: int, attributes: int, kind: MemberKind
) -> None:
    info = zipfile.ZipInfo("entry", (1980, 1, 1, 0, 0, 0))
    info.create_system = system
    info.external_attr = attributes
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, b"x")
    [member] = inspect(buffer.getvalue(), tmp_path).members
    assert member.kind is kind


def test_tar_links_specials_and_escapes(tmp_path: Path) -> None:
    report = inspect(fixture("links.tar"), tmp_path)
    assert codes(report) == [
        MEMBER_LINK,
        MEMBER_LINK,
        MEMBER_LINK,
        MEMBER_SPECIAL,
        MEMBER_PATH_UNSAFE,
        MEMBER_PATH_UNSAFE,
    ]
    links = {
        f.details["name"]: (f.details["link"], f.details["target"]) for f in report.findings[:3]
    }
    assert links == {
        "latest": ("symlink", "/etc/passwd"),
        "rel": ("symlink", "../outside/canary.txt"),
        "hard": ("hardlink", "ok.txt"),
    }
    assert report.findings[3].details["type"] == "fifo"
    kinds = {m.name: m.kind for m in report.members}
    assert kinds["dir"] is MemberKind.DIRECTORY and kinds["hard"] is MemberKind.HARDLINK
    assert {m.name: m.read_bytes for m in report.members}["ok.txt"] == 63
    assert report.complete
    for finding in report.findings:
        assert isinstance(finding.subject, EvidenceRef)
        [step] = finding.subject.locator  # an uncompressed tar: the member's own bytes
        assert isinstance(step, ByteRange) and step.offset % 512 == 0


def test_a_zip_symlink_target_is_capped_like_a_header(tmp_path: Path, hostile: ModuleType) -> None:
    """A link's target is read whole to record it; a 2 MiB target is refused, not held in memory."""
    link = hostile.zip_info("latest", 0o120777)
    members = [(link, bytes(2 * MiB)), ("ok.txt", b"ok\n")]
    data = hostile.make_zip(members, compression=zipfile.ZIP_DEFLATED)
    limits = ArchiveLimits(max_compression_ratio=10_000)  # so the ratio does not refuse it first
    report = inspect(data, tmp_path, limits)
    assert codes(report) == [HEADER_TOO_LARGE]
    assert report.findings[0].details["declared_size"] == 2 * MiB
    assert [(m.name, m.read_bytes) for m in report.members] == [("latest", 0), ("ok.txt", 3)]
    assert report.complete
    assert peak_memory(lambda: inspect(data, tmp_path, limits)) < MiB


def test_a_tar_link_with_an_unsafe_name_is_unsafe_before_it_is_a_link(
    tmp_path: Path, hostile: ModuleType
) -> None:
    data = hostile.make_tar(
        [
            (hostile.tar_info("../../evil", kind=tarfile.SYMTYPE, linkname="/etc/passwd"), b""),
            (hostile.tar_info("/abs-hard", kind=tarfile.LNKTYPE, linkname="ok.txt"), b""),
            (hostile.tar_info("fine", kind=tarfile.SYMTYPE, linkname="ok.txt"), b""),
        ]
    )
    report = inspect(data, tmp_path)
    assert codes(report) == [MEMBER_PATH_UNSAFE, MEMBER_PATH_UNSAFE, MEMBER_LINK]
    assert [f.details["problem"] for f in report.findings[:2]] == ["parent_reference", "absolute"]
    assert report.complete


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_a_corrupt_zip_symlink_is_a_member_finding(
    tmp_path: Path, hostile: ModuleType, compression: int
) -> None:
    """A bad CRC or a broken stream in a link's target is that member's finding, not a crash."""
    target = b"../" * 40 + b"etc/passwd"
    link = hostile.zip_info("latest", 0o120777)
    data = bytearray(
        hostile.make_zip([(link, target), ("ok.txt", b"ok\n")], compression=compression)
    )
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("latest")
    data[info.header_offset + 30 + len("latest") + len(info.extra) + 2] ^= 0xFF
    report = inspect(bytes(data), tmp_path)
    assert codes(report) == [MEMBER_CORRUPT]
    assert report.findings[0].details["name"] == "latest"
    assert [(m.name, m.read_bytes) for m in report.members] == [("latest", 0), ("ok.txt", 3)]
    assert report.complete


# --- headers, truncation and corruption --------------------------------------------------------


def test_a_pax_header_bomb_is_refused(tmp_path: Path) -> None:
    data = fixture("pax_bomb.tar.gz")
    report = inspect(data, tmp_path)
    assert codes(report) == [HEADER_TOO_LARGE]
    assert report.findings[0].details["declared_size"] == 2 * MiB
    assert not report.complete
    assert peak_memory(lambda: inspect(data, tmp_path)) < 2 * MiB


def test_chained_long_names_are_capped_at_one_mebibyte_in_total(tmp_path: Path) -> None:
    """tarfile holds every link of a long-name chain until the member's last header parses."""
    data = gzip_chunks(gnu_long_names(40, MiB - 512))  # 40 MiB of names in about 40 KB
    assert len(data) < 100_000
    report = inspect(data, tmp_path)
    assert codes(report) == [HEADER_TOO_LARGE]
    assert report.findings[0].details == {"header_offset": 0, "max_header_size": MiB}
    assert not report.complete
    assert peak_memory(lambda: inspect(data, tmp_path)) < 4 * MiB


def test_one_members_headers_are_capped_however_they_are_split(
    tmp_path: Path, hostile: ModuleType
) -> None:
    """Two long names under the per-header cap still add up past it; the member before is kept."""
    first = hostile.make_tar([(hostile.tar_info("ok.txt", 3), b"ok\n")])[:1024]
    data = first + b"".join(gnu_long_names(2, 700_000))
    report = inspect(data, tmp_path)
    assert codes(report) == [HEADER_TOO_LARGE]
    assert report.findings[0].details == {"header_offset": 1024, "max_header_size": MiB}
    assert [(m.name, m.read_bytes) for m in report.members] == [("ok.txt", 3)]


def test_a_long_header_chain_is_a_finding_at_any_stack_depth(tmp_path: Path) -> None:
    """1,200 chained long names: a fixed chain cap, not a RecursionError that moves with depth."""
    data = b"".join(gnu_long_names(1200, 1))
    shallow = inspect(data, tmp_path)
    assert codes(shallow) == [HEADER_TOO_LARGE]
    assert shallow.findings[0].details == {
        "header_chain": 33,
        "max_header_chain": 32,
        "max_header_size": MiB,
    }
    assert at_depth(600, lambda: inspect(data, tmp_path)) == shallow


def test_pax_global_headers_are_capped_across_the_archive(tmp_path: Path) -> None:
    """Global headers accumulate in tarfile for the archive's life, so their total is capped."""
    pieces = []
    for index in range(3):
        pax = {f"key{index}": "v" * 600_000}
        pieces.append(tarfile.TarInfo.create_pax_global_header(pax))
        pieces.append(tarfile.TarInfo(f"m{index}").tobuf(tarfile.USTAR_FORMAT))
    report = inspect(b"".join(pieces) + bytes(1024), tmp_path)
    assert codes(report) == [HEADER_TOO_LARGE]
    declared = [
        tarfile.TarInfo.frombuf(header[:512], "utf-8", "surrogateescape").size
        for header in pieces[::2]
    ]
    assert report.findings[0].details["global_header_bytes"] == declared[0] + declared[1]
    assert declared[0] < MiB < declared[0] + declared[1]
    assert [m.name for m in report.members] == ["m0"]


@pytest.mark.parametrize("form", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_ordinary_extended_headers_pass(tmp_path: Path, form: int) -> None:
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w", format=form, pax_headers={"comment": "a git archive"}
    ) as archive:
        info = tarfile.TarInfo("d/" * 150 + "f.txt")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"ok\n"))
        link = tarfile.TarInfo("l" * 120)
        link.type = tarfile.SYMTYPE
        link.linkname = "t/" * 100
        archive.addfile(link)
    report = inspect(buffer.getvalue(), tmp_path)
    assert codes(report) == [MEMBER_LINK]
    assert report.findings[0].details["target"] == "t/" * 100
    assert [(m.name, m.read_bytes) for m in report.members] == [
        ("d/" * 150 + "f.txt", 3),
        ("l" * 120, 0),
    ]
    assert report.complete


SPARSE_BOMB = 200 * MiB
SPARSE_CHUNK = b"PK\x03\x04" + bytes(508)  # a stored chunk that sniffs as a nested zip


@pytest.mark.parametrize(
    "data",
    [
        pax_tar({"GNU.sparse.map": "0,512", "GNU.sparse.size": str(SPARSE_BOMB)}, SPARSE_CHUNK),
        pax_tar(
            {
                "GNU.sparse.map": "0,512",
                "GNU.sparse.realsize": str(SPARSE_BOMB),
                "size": str(SPARSE_BOMB),  # moves tarfile's idea of where the next header is
            },
            SPARSE_CHUNK,
        ),
        gnu_sparse_tar(SPARSE_CHUNK, SPARSE_BOMB),
    ],
    ids=["pax-0.1", "pax-size-override", "gnu-old"],
)
def test_a_sparse_member_is_held_to_the_ratio_before_it_is_read(
    tmp_path: Path, data: bytes
) -> None:
    """tarfile fills sparse holes with zeros: 512 stored bytes would spool 200 MiB to scratch."""
    assert len(data) < 16_000
    report = inspect(data, tmp_path)
    assert codes(report) == [COMPRESSION_RATIO_EXCEEDED]
    details = report.findings[0].details
    assert (details["uncompressed"], details["compressed"]) == (SPARSE_BOMB, 512)
    [member] = report.members
    assert (member.declared_size, member.read_bytes, member.nested) == (SPARSE_BOMB, 0, None)
    [step] = member.locator
    assert isinstance(step, ByteRange) and step.offset + step.length <= len(data)
    assert peak_memory(lambda: inspect(data, tmp_path)) < MiB
    report = inspect(gzip.compress(data, mtime=0), tmp_path)  # compressed: the same, per member
    assert codes(report) == [COMPRESSION_RATIO_EXCEEDED]
    loose = 1_000_000  # the size limits hold the expanded size too, before a byte is read
    report = inspect(
        data, tmp_path, ArchiveLimits(max_compression_ratio=loose, max_member_size=MiB)
    )
    assert codes(report) == [MEMBER_SIZE_EXCEEDED]
    report = inspect(data, tmp_path, ArchiveLimits(max_compression_ratio=loose, max_total_size=MiB))
    assert codes(report) == [TOTAL_SIZE_EXCEEDED]
    assert [m.read_bytes for m in report.members] == [0]


def test_a_sparse_member_within_the_ratio_is_read_to_its_expanded_size(tmp_path: Path) -> None:
    data = pax_tar({"GNU.sparse.map": "0,512", "GNU.sparse.size": "40000"}, b"x" * 512)
    report = inspect(data, tmp_path)
    assert (codes(report), report.complete) == ([], True)
    assert [(m.declared_size, m.read_bytes) for m in report.members] == [(40_000, 40_000)]


@pytest.mark.parametrize("kind", [tarfile.REGTYPE, tarfile.SYMTYPE])
def test_sizes_beyond_any_range_are_findings_not_crashes(tmp_path: Path, kind: bytes) -> None:
    """A base-256 size field can declare 2^70 bytes; the member's range stays inside the tar."""
    info = tarfile.TarInfo("huge")
    info.type = kind
    info.size = 1 << 70
    data = info.tobuf(tarfile.GNU_FORMAT) + bytes(1024)
    report = inspect(data, tmp_path)
    assert codes(report) == [MEMBER_SIZE_EXCEEDED]
    [step] = report.members[0].locator
    assert step == ByteRange(0, len(data))


def test_a_zip64_header_offset_beyond_the_archive_is_a_member_finding(
    tmp_path: Path, hostile: ModuleType
) -> None:
    data = bytearray(hostile.make_zip([("a.txt", b"x")]))
    entry = data.find(b"PK\x01\x02")
    name_length, extra_length = struct.unpack_from("<HH", data, entry + 28)
    struct.pack_into("<I", data, entry + 42, 0xFFFFFFFF)  # the offset lives in the zip64 extra
    struct.pack_into("<H", data, entry + 30, extra_length + 12)
    at = entry + 46 + name_length + extra_length
    data[at:at] = struct.pack("<HHQ", 1, 8, (1 << 64) - 1)
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<I", data, end + 12, struct.unpack_from("<I", data, end + 12)[0] + 12)
    report = inspect(bytes(data), tmp_path)
    assert codes(report) == [MEMBER_CORRUPT]
    assert report.members[0].locator == (ByteRange(len(data), 0),)


def test_a_huge_declared_member_with_no_data_is_truncation(tmp_path: Path) -> None:
    report = inspect(fixture("huge_member.tar"), tmp_path)
    assert codes(report) == [MEMBER_TRUNCATED]
    assert report.findings[0].details["declared_size"] == 4 << 30
    assert report.members[0].read_bytes == 0
    report = inspect(fixture("huge_member.tar"), tmp_path, ArchiveLimits(max_member_size=1 << 30))
    assert codes(report) == [MEMBER_SIZE_EXCEEDED]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("truncated.zip", [TRUNCATED]),
        ("truncated.tar", [MEMBER_TRUNCATED]),
        ("truncated.tar.gz", [TRUNCATED]),
        ("corrupt_member.zip", [MEMBER_CORRUPT]),
        ("encrypted.zip", [MEMBER_ENCRYPTED]),
    ],
)
def test_truncated_and_defective_archives(tmp_path: Path, name: str, expected: list[str]) -> None:
    report = inspect(fixture(name), tmp_path)
    assert codes(report) == expected
    assert all(f.severity is Severity.ERROR for f in report.findings)


def test_a_truncated_tar_still_reports_its_whole_members(tmp_path: Path) -> None:
    report = inspect(fixture("truncated.tar"), tmp_path)
    assert [(m.name, m.read_bytes) for m in report.members] == [("a.txt", 630), ("b.txt", 0)]
    assert report.findings[0].details["name"] == "b.txt"
    assert not report.complete


def test_not_an_archive_and_an_empty_zip(tmp_path: Path) -> None:
    report = inspect(b"\x89MCAP0\r\n" + bytes(64), tmp_path)
    assert codes(report) == [UNRECOGNISED] and report.kind is None
    report = inspect(b"PK\x05\x06" + bytes(18), tmp_path)
    assert (codes(report), report.members, report.complete) == ([], (), True)
    report = inspect(b"PK\x05\x06" + bytes(4), tmp_path)
    assert codes(report) == [CORRUPT]


def test_a_zip_version_zipfile_refuses_is_a_finding(tmp_path: Path) -> None:
    """zipfile raises ``NotImplementedError`` for "version needed 6.4" while listing members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", b"hi")
    data = bytearray(buffer.getvalue())
    data[data.find(b"PK\x01\x02") + 6] = 64
    report = inspect(bytes(data), tmp_path)
    assert codes(report) == [CORRUPT]
    assert report.findings[0].details == {"size": len(data), "error": "unsupported"}
    assert (report.members, report.complete) == ((), False)


def sparse_extended_then_eof() -> bytes:
    """A GNU sparse ``S`` header promising an extension block (byte 482), then the end."""
    info = tarfile.TarInfo("s")
    info.type = tarfile.GNUTYPE_SPARSE
    block = bytearray(info.tobuf(tarfile.GNU_FORMAT))
    block[482] = 1
    return with_checksum(block)


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (sparse_extended_then_eof, "bad_index"),
        (lambda: pax_tar({"GNU.sparse.map": "a,b"}, bytes(512)), "bad_value"),
        (
            lambda: pax_tar(
                {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}, b"zz\n" + bytes(509)
            ),
            "bad_value",
        ),
    ],
    ids=["gnu-sparse-extension-eof", "pax-sparse-map-not-numbers", "pax-sparse-1.0-bad-count"],
)
def test_tar_header_errors_tarfile_does_not_wrap_are_findings(
    tmp_path: Path, make: Callable[[], bytes], error: str
) -> None:
    """tarfile lets ``IndexError`` and ``ValueError`` escape from sparse headers."""
    data = make()
    report = inspect(data, tmp_path)
    assert codes(report) == [CORRUPT]
    assert report.findings[0].details == {"error": error}
    assert not report.complete
    report = inspect(gzip.compress(data, mtime=0), tmp_path)  # the same inside a compressed tar
    assert codes(report) == [CORRUPT]


def test_defects_record_a_stable_code_never_the_library_message(tmp_path: Path) -> None:
    """Library messages vary across Python and zlib versions; findings must not."""
    seen = set()
    for name in ("corrupt_member.zip", "huge_member.tar", "truncated.tar", "truncated.tar.gz"):
        for finding in inspect(fixture(name), tmp_path).findings:
            assert "detail" not in finding.details
            assert finding.details["error"] in ERROR_CODES
            seen.add(finding.details["error"])
    assert seen == {"bad_deflate", "end_of_data"}
    data = bytearray(fixture("bomb.tar.gz"))
    data[len(data) // 2 : len(data) // 2 + 64] = bytes(64)  # a broken deflate stream mid-tar
    report = inspect(bytes(data), tmp_path, ArchiveLimits(max_compression_ratio=10_000))
    assert [f.details.get("error") for f in report.findings] == ["bad_deflate"]


# --- provenance, determinism, config -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in FIXTURES.iterdir() if p.is_file() and p.suffix not in (".py", ".md")),
)
def test_every_finding_has_provenance_and_recomputes(tmp_path: Path, name: str) -> None:
    data = fixture(name)
    limits = ArchiveLimits(max_members=1000)
    first = inspect(data, tmp_path, limits)
    second = inspect(data, tmp_path, limits)
    assert first == second
    for finding in first.findings:
        assert isinstance(finding.subject, EvidenceRef)
        assert finding.subject.source == content_id(data)
        assert finding.transform == limits.transform().id
        line = canonical_json.dumps(finding.to_json())
        assert check_ingest_finding(ingest_finding_from_json(canonical_json.loads(line))) == finding
    assert name == "mixed.zip" or first.findings  # every hostile fixture says what is wrong


def test_different_limits_are_a_different_lineage(tmp_path: Path) -> None:
    data = fixture("bomb.zip")
    strict = inspect(data, tmp_path, ArchiveLimits(max_compression_ratio=50)).findings[0]
    default = inspect(data, tmp_path).findings[0]
    assert strict.transform != default.transform
    assert strict.id != default.id
    assert ArchiveLimits().transform().config == ArchiveLimits().to_json()


def test_limits_are_validated_and_round_trip() -> None:
    assert limits_from_json(ArchiveLimits(max_depth=7).to_json()) == ArchiveLimits(max_depth=7)
    for bad in ({"max_members": 0}, {"max_depth": True}, {"max_total_size": -1}):
        with pytest.raises(ValueError):
            ArchiveLimits(**bad)
    with pytest.raises(ValueError):
        limits_from_json({"max_members": 1})
    with pytest.raises(ValueError):
        limits_from_json({**ArchiveLimits().to_json(), "max_depth": "3"})


def test_inspection_reads_from_a_real_file_and_writes_only_to_scratch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name in ("nested.zip", "mixed.zip", "traversal.zip"):
        (root / name).write_bytes(fixture(name))
    before = sorted((p.name, p.read_bytes()) for p in root.iterdir())
    for name in ("nested.zip", "mixed.zip", "traversal.zip"):
        with (root / name).open("rb") as stream:
            inspect(fixture(name), tmp_path, ArchiveLimits(max_depth=10), stream=stream)
    assert sorted((p.name, p.read_bytes()) for p in root.iterdir()) == before
