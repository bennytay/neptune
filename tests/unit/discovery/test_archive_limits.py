"""Archive inspection within limits (ADR 0028 §2): every bomb, lie and defect is a finding."""

import gzip
import io
import tarfile
import tracemalloc
import zipfile
from collections.abc import Callable
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


# --- headers, truncation and corruption --------------------------------------------------------


def test_a_pax_header_bomb_is_refused(tmp_path: Path) -> None:
    data = fixture("pax_bomb.tar.gz")
    report = inspect(data, tmp_path)
    assert codes(report) == [HEADER_TOO_LARGE]
    assert report.findings[0].details["declared_size"] == 2 * MiB
    assert not report.complete
    assert peak_memory(lambda: inspect(data, tmp_path)) < 2 * MiB


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
