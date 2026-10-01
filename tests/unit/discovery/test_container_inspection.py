"""Container inspection on real small archives: exact member citations, bounded by policy.

The oracle for every citation is the standard library: a member's cited bytes, decoded the way
its ``method`` says, must equal the bytes ``zipfile`` / ``tarfile`` / ``gzip`` hand back.
"""

import bz2
import gzip
import hashlib
import importlib.util
import io
import lzma
import sys
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    Resources,
    SourceReader,
)
from neptune.adapters.registry import AdapterRegistry, SelectionStatus
from neptune.discovery.containers import (
    ContainerReport,
    Member,
    MemberKind,
    ProbePolicy,
)
from neptune.discovery.probe import ProbeEngine, SourceProbe
from neptune.discovery.reader import BytesReader
from neptune.discovery.sniff import ContainerKind
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.provenance import ByteRange, Locator

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
CONTAINERS: Final = FIXTURES / "probe" / "containers"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter", FIXTURES / "adapters" / "tally_adapter.py")
GENERATOR: Final = _load("make_probe_fixtures", FIXTURES / "probe" / "make_probe_fixtures.py")
NOTES: Final = (FIXTURES / "text" / "notes.txt").read_bytes()


class Invariant:
    """An adapter that checks the ABI's head law on every probe and claims nothing."""

    descriptor = AdapterDescriptor(
        id="invariant",
        version="1.0.0",
        abi=ABI_VERSION,
        summary="Asserts len(head) == min(hints.size, PROBE_HEAD_SIZE) for every probe.",
        formats=(FormatSpec("Nothing"),),
        record_kinds=("document_record",),
        config=(),
        libraries=(),
        finding_codes=(),
        locator_steps=(),
        conventions=(),
        resources=Resources(0, True),
        security=(),
    )

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        assert len(head) == min(hints.size, PROBE_HEAD_SIZE), (len(head), hints.size)
        return ProbeResult(0.0, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        raise NotImplementedError

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        raise NotImplementedError

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        raise NotImplementedError


def registry() -> AdapterRegistry:
    return AdapterRegistry([*builtin_adapters(), TALLY.TallyAdapter(), Invariant()])


def probe(data: bytes, name: str = "", policy: ProbePolicy | None = None) -> SourceProbe:
    return ProbeEngine(registry(), policy).probe(BytesReader(data), name)


def fixture(name: str) -> bytes:
    return (CONTAINERS / name).read_bytes()


def report(name: str, policy: ProbePolicy | None = None) -> tuple[ContainerReport, SourceProbe]:
    probed = probe(fixture(name), name, policy)
    assert probed.container is not None
    return probed.container, probed


def codes(probed: SourceProbe) -> list[tuple[str, str]]:
    """``(code, limit)`` of every finding: the limit name for ``container_limit``, else ``""``."""
    return [
        (f.code.removeprefix("neptune.probe."), str(f.details.get("limit", "")))
        for f in probed.findings
    ]


def _same(data: bytes) -> bytes:
    return data


def resolve(
    data: bytes, steps: tuple[Locator, ...], decoders: list[Callable[[bytes], bytes]]
) -> bytes:
    """Follow a locator path into ``data``; ``decoders[i]`` decodes what step ``i`` cites."""
    for step, decode in zip(steps, [*decoders, _same], strict=False):
        assert isinstance(step, ByteRange)
        assert step.offset + step.length <= len(data), "a citation runs past its scope"
        data = data[step.offset : step.offset + step.length]
        if step is not steps[-1]:
            data = decode(data)
    return data


def inflate(data: bytes) -> bytes:
    return zlib.decompressobj(-15).decompress(data)


def by_name(container: ContainerReport) -> dict[bytes, Member]:
    return {member.name: member for member in container.members}


def selected(member: Member) -> str | None:
    assert member.probe is not None
    return member.probe.selection.adapter


# --- zip -----------------------------------------------------------------------------------------


def test_zip_members_are_listed_in_directory_order_with_their_kinds_and_methods() -> None:
    container, probed = report("members.zip")
    assert (container.kind, container.complete, container.declared_count) == (
        ContainerKind.ZIP,
        True,
        6,
    )
    assert [(m.name, m.kind, m.method) for m in container.members] == [
        (b"notes.txt", MemberKind.FILE, "deflate"),
        (b"logs/", MemberKind.DIRECTORY, "stored"),
        (b"logs/lift.tally", MemberKind.FILE, "stored"),
        (b"logs/renamed", MemberKind.FILE, "deflate"),
        (b"../escape.txt", MemberKind.FILE, "stored"),
        (b"recording.mcap", MemberKind.FILE, "deflate"),
    ]
    assert [f.code for f in probed.findings] == ["neptune.probe.unsupported"]


def test_zip_member_citations_resolve_to_the_members_bytes() -> None:
    data = fixture("members.zip")
    container, _ = report("members.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in container.members:
            info = archive.getinfo(member.name.decode())
            assert (member.size, member.compressed_size) == (info.file_size, info.compress_size)
            cited = resolve(data, member.entry.locator, [])
            decoded = inflate(cited) if member.method == "deflate" else cited
            assert decoded == archive.read(info)


def test_zip_members_are_probed_by_their_bytes_not_their_names() -> None:
    container, _ = report("members.zip")
    members = by_name(container)
    assert selected(members[b"notes.txt"]) == "text"
    assert selected(members[b"logs/lift.tally"]) == "tally"
    assert selected(members[b"logs/renamed"]) == "tally"  # deflated, extensionless
    assert selected(members[b"../escape.txt"]) == "text"  # listed verbatim, never resolved
    mcap = members[b"recording.mcap"]
    assert mcap.probe is not None
    assert mcap.probe.selection.status is SelectionStatus.UNSUPPORTED
    assert [s.name for s in mcap.probe.sniff.signatures] == ["MCAP"]
    assert members[b"logs/"].probe is None


def test_an_empty_zip_is_a_complete_container_with_no_members() -> None:
    container, probed = report("empty.zip")
    assert (container.members, container.declared_count, container.complete) == ((), 0, True)
    assert "holding 0 members" in probed.findings[-1].message


def test_a_truncated_zip_is_corrupt_not_an_exception() -> None:
    container, probed = report("truncated.zip")
    assert (container.members, container.complete) == ((), False)
    assert codes(probed) == [("container_corrupt", ""), ("unsupported", "")]
    assert "or more members" in probed.findings[-1].message


def test_a_zip_bomb_is_reported_and_never_decoded() -> None:
    container, probed = report("bomb.zip")
    (member,) = container.members
    assert (member.size, member.probe) == (4 * 1024 * 1024, None)
    limit = probed.findings[0]
    assert (limit.code, limit.category, limit.severity) == (
        "neptune.probe.container_limit",
        FindingCategory.LIMIT,
        Severity.WARNING,
    )
    assert limit.details == {
        "compressed_size": member.compressed_size,
        "limit": "ratio",
        "max_ratio": 1000,
        "member": 0,
        "size": 4 * 1024 * 1024,
    }
    assert limit.subject == member.entry


def test_the_member_limit_cuts_the_listing_and_says_so() -> None:
    container, probed = report("members.zip", ProbePolicy(max_members=2))
    assert [m.name for m in container.members] == [b"notes.txt", b"logs/"]
    assert (container.complete, container.declared_count) == (False, 6)
    assert codes(probed)[0] == ("container_limit", "members")
    assert probed.findings[0].details == {
        "declared": 6,
        "limit": "members",
        "listed": 2,
        "max_members": 2,
    }


def _nth(data: bytes, needle: bytes, n: int) -> int:
    at = -1
    for _ in range(n + 1):
        at = data.index(needle, at + 1)
    return at


def _patch_zip(data: bytes, field: int, value: bytes, member: int = 0) -> bytes:
    """Overwrite the ``field`` bytes after both headers of ``member`` (the CD lies 2 further)."""
    local = _nth(data, b"PK\x03\x04", member)
    central = _nth(data, b"PK\x01\x02", member)
    out = bytearray(data)
    out[local + field : local + field + len(value)] = value
    out[central + field + 2 : central + field + 2 + len(value)] = value
    return bytes(out)


def test_an_encrypted_member_is_not_decoded_and_said_so() -> None:
    data = _patch_zip(fixture("members.zip"), 6, b"\x01\x00")  # general purpose bit 0
    probed = probe(data)
    assert probed.container is not None
    assert probed.container.members[0].probe is None
    assert codes(probed)[0] == ("container_not_inspected", "")
    assert probed.findings[0].severity is Severity.INFO
    assert "encrypted" in probed.findings[0].message


def test_an_unknown_compression_method_is_not_decoded() -> None:
    data = _patch_zip(fixture("members.zip"), 8, b"\x63\x00")  # method 99
    probed = probe(data)
    assert probed.container is not None
    assert probed.container.members[0].method == "method 99"
    assert probed.container.members[0].probe is None
    assert "method 99" in probed.findings[0].message


def test_a_member_whose_local_header_is_missing_is_reported() -> None:
    data = fixture("members.zip")
    central = data.index(b"PK\x01\x02")
    out = bytearray(data)
    out[central + 42 : central + 46] = (len(data) - 1).to_bytes(4, "little")  # offset past the end
    probed = probe(bytes(out))
    assert probed.container is not None
    assert probed.container.members[0].probe is None
    assert codes(probed)[0] == ("container_corrupt", "")
    assert "local header" in probed.findings[0].message


def test_a_zip_inside_a_compressed_member_is_opened_only_when_all_of_it_was_decoded() -> None:
    whole = probe(gzip.compress(fixture("members.zip"), mtime=0))
    assert whole.container is not None
    (member,) = whole.container.members
    assert member.nested is not None and member.nested.complete
    assert [m.name for m in member.nested.members][:2] == [b"notes.txt", b"logs/"]
    assert all(len(m.entry.locator) == 2 for m in member.nested.members)
    big = _zip_bytes([("big.bin", bytes(range(256)) * 300, zipfile.ZIP_STORED)])
    cut = probe(gzip.compress(big, mtime=0), policy=ProbePolicy(scan_bytes=PROBE_HEAD_SIZE))
    assert cut.container is not None
    (member,) = cut.container.members
    assert member.nested is not None and member.nested.members == ()
    assert codes(cut)[0] == ("container_limit", "bytes")
    assert "directory lies at its end" in cut.findings[0].message


def _zip_bytes(entries: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in entries:
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, method)
    return buffer.getvalue()


# --- tar -----------------------------------------------------------------------------------------


def test_tar_members_kinds_links_and_citations() -> None:
    data = fixture("members.tar")
    container, probed = report("members.tar")
    assert (container.kind, container.complete) == (ContainerKind.TAR, True)
    assert [(m.name, m.kind) for m in container.members] == [
        (b"notes.txt", MemberKind.FILE),
        (b"logs/", MemberKind.DIRECTORY),
        (b"logs/lift.tally", MemberKind.FILE),
        (b"latest", MemberKind.SYMLINK),
        (b"drive.bag", MemberKind.FILE),
    ]
    assert by_name(container)[b"latest"].link_target == b"logs/lift.tally"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in container.members:
            if member.kind is MemberKind.FILE:
                extracted = archive.extractfile(member.name.decode())
                assert extracted is not None
                assert resolve(data, member.entry.locator, []) == extracted.read()
    assert selected(by_name(container)[b"notes.txt"]) == "text"
    assert selected(by_name(container)[b"logs/lift.tally"]) == "tally"
    assert [f.code for f in probed.findings] == ["neptune.probe.unsupported"]


def test_pax_and_gnu_long_names_are_read() -> None:
    container, _ = report("longnames.tar")
    (member,) = container.members
    assert member.name == b"deep/" * 30 + b"flight.ulg"
    assert member.probe is not None
    assert [s.name for s in member.probe.sniff.signatures] == ["ULog"]
    container, _ = report("gnu_longname.tar")
    (member,) = container.members
    assert member.name == b"gnu/" * 30 + b"notes.txt"
    assert selected(member) == "text"


def test_a_header_failing_its_checksum_stops_the_listing() -> None:
    container, probed = report("badsum.tar")
    assert (container.members, container.complete) == ((), False)
    assert codes(probed)[0] == ("container_corrupt", "")
    assert probed.findings[0].subject.locator == (ByteRange(0, 512),)  # type: ignore[union-attr]


def test_a_member_declaring_more_than_remains_is_corrupt() -> None:
    data = bytearray(fixture("members.tar"))
    # The first header's size field: claim far more than the archive holds, fixing the checksum.
    data[124:136] = b"77777777777\x00"
    body = bytes(data[:148]) + b" " * 8 + bytes(data[156:512])
    data[148:156] = f"{sum(body):06o}\x00 ".encode()
    probed = probe(bytes(data))
    assert probed.container is not None
    assert len(probed.container.members) == 1 and not probed.container.complete
    assert codes(probed)[0] == ("container_corrupt", "")
    assert "remain" in probed.findings[0].message


# --- gzip, bzip2, xz and nesting -----------------------------------------------------------------


def test_single_member_streams_are_decoded_and_the_member_probed() -> None:
    for name, method, size in (
        ("notes.txt.gz", "deflate", len(NOTES)),
        ("notes.txt.bz2", "bzip2", len(NOTES)),
        ("notes.txt.xz", "xz", len(NOTES)),
    ):
        container, probed = report(name)
        (member,) = container.members
        assert (member.method, member.size, member.compressed_size) == (
            method,
            size,
            len(fixture(name)) if method != "deflate" else len(fixture(name)) - 18,
        )
        assert member.entry.locator == (ByteRange(0, len(fixture(name))),)
        assert selected(member) == "text", name
        assert [f.code for f in probed.findings] == ["neptune.probe.unsupported"], name
    container, _ = report("tally.xz")
    assert selected(container.members[0]) == "tally"


def test_a_tar_inside_a_gzip_is_listed_with_two_step_citations_that_resolve() -> None:
    data = fixture("members.tar.gz")
    container, probed = report("members.tar.gz")
    (outer,) = container.members
    assert outer.probe is not None and outer.probe.sniff.container is ContainerKind.TAR
    assert outer.nested is not None and outer.nested.complete
    tar = gzip.decompress(data)
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        for member in outer.nested.members:
            assert len(member.entry.locator) == 2
            if member.kind is MemberKind.FILE:
                extracted = archive.extractfile(member.name.decode())
                assert extracted is not None
                assert resolve(data, member.entry.locator, [gzip.decompress]) == extracted.read()
    assert selected(by_name(outer.nested)[b"logs/lift.tally"]) == "tally"
    assert [f.code for f in probed.findings] == ["neptune.probe.unsupported"]


def test_nesting_stops_at_max_depth_with_a_finding() -> None:
    container, probed = report("nested.zip")  # zip > gzip > tar: three deep
    (outer,) = container.members
    assert outer.nested is not None and outer.nested.kind is ContainerKind.GZIP
    (inner,) = outer.nested.members
    assert inner.nested is None
    assert codes(probed)[0] == ("container_limit", "depth")
    assert probed.findings[0].details == {
        "container": "tar",
        "depth": 2,
        "limit": "depth",
        "max_depth": 2,
        "member": 0,
    }
    assert probed.findings[0].subject == inner.entry
    shallow = probe(fixture("members.tar.gz"), policy=ProbePolicy(max_depth=1))
    assert shallow.container is not None and shallow.container.members[0].nested is None
    assert codes(shallow)[0] == ("container_limit", "depth")
    closed = probe(fixture("members.tar.gz"), policy=ProbePolicy(max_depth=0))
    assert closed.container is None
    assert codes(closed)[0] == ("container_limit", "depth")


def test_a_corrupt_deflate_stream_is_reported_and_not_probed() -> None:
    container, probed = report("corrupt.gz")
    (member,) = container.members
    assert member.probe is None
    assert codes(probed)[0] == ("container_corrupt", "")
    assert probed.findings[0].details == {"error": "zlib.error", "member": 0}


def test_a_gzip_declaring_a_bomb_ratio_is_not_probed_once_it_runs_past_the_budget() -> None:
    # A gzip states its size only in a trailer, and its last bytes are one only if the stream
    # ends there. So the stream is decoded to the budget first; one still going is held to
    # max_ratio by what its trailer states.
    bomb = bytearray(gzip.compress(bytes(2 * 1024 * 1024), mtime=0))  # past the 1 MiB budget
    bomb[-4:] = (10**9).to_bytes(4, "little")  # claim a gigabyte from a few KiB
    probed = probe(bytes(bomb))
    assert probed.container is not None
    (member,) = probed.container.members
    assert (member.size, member.probe) == (10**9, None)
    assert codes(probed)[0] == ("container_limit", "ratio")
    assert probed.findings[0].details == {
        "compressed_size": len(bomb) - 18,
        "decoded": 1024 * 1024,
        "limit": "ratio",
        "max_ratio": 1000,
        "size": 10**9,
    }
    # A stream that ends within the budget settles its trailer: the claim is a contradiction.
    short = bytearray(gzip.compress(bytes(100), mtime=0))
    short[-4:] = (10**9).to_bytes(4, "little")
    probed = probe(bytes(short))
    assert probed.container is not None
    (member,) = probed.container.members
    assert member.probe is not None
    assert codes(probed)[0] == ("container_corrupt", "")
    assert probed.findings[0].details == {
        "declared": 10**9,
        "decoded": 100,
        "member": 0,
        "stream": 0,
    }


def test_the_byte_budget_bounds_a_compressed_tar_listing() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in (("big", b"x" * (PROBE_HEAD_SIZE + 4096)), ("after", NOTES)):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(payload), 0
            archive.addfile(info, io.BytesIO(payload))
    probed = probe(
        gzip.compress(buffer.getvalue(), mtime=0), policy=ProbePolicy(scan_bytes=PROBE_HEAD_SIZE)
    )
    assert probed.container is not None
    (outer,) = probed.container.members
    assert outer.nested is not None
    assert [m.name for m in outer.nested.members] == [b"big"]
    assert not outer.nested.complete
    assert codes(probed)[0] == ("container_limit", "bytes")
    assert probed.findings[0].details["listed"] == 1


def _ustar(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(payload), 0
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_a_compressed_tar_cut_by_the_budget_on_a_header_boundary_is_not_complete() -> None:
    # Tar blocks and both budgets are 512-aligned, so a first member of budget - 512 bytes puts
    # the second member's header exactly where the decoded bytes end: nothing of it is read, and
    # that silence must not pass for the end of the archive.
    for budget in (ProbePolicy().scan_bytes, PROBE_HEAD_SIZE):
        tar = _ustar([("first", b"x" * (budget - 512)), *((f"more{i}", NOTES) for i in range(5))])
        probed = probe(gzip.compress(tar, mtime=0), policy=ProbePolicy(scan_bytes=budget))
        assert probed.container is not None
        (outer,) = probed.container.members
        assert outer.nested is not None
        assert [m.name for m in outer.nested.members] == [b"first"]
        assert not outer.nested.complete
        assert codes(probed)[0] == ("container_limit", "bytes")
        assert probed.findings[0].details == {
            "decoded": budget,
            "limit": "bytes",
            "listed": 1,
            "scan_bytes": budget,
        }
        whole = probe(gzip.compress(tar, mtime=0), policy=ProbePolicy(scan_bytes=2 * budget))
        assert whole.container is not None
        nested = whole.container.members[0].nested
        assert nested is not None and nested.complete and len(nested.members) == 6


def test_unopenable_containers_are_recognised_and_said_so() -> None:
    for name in ("cloud.zst", "cloud.7z"):
        probed = probe((FIXTURES / "probe" / "signatures" / name).read_bytes(), name)
        assert probed.container is not None and probed.container.members == ()
        assert codes(probed) == [("container_not_inspected", ""), ("unsupported", "")]
        assert probed.findings[0].severity is Severity.INFO


def test_bzip2_and_xz_sizes_are_known_only_when_the_stream_ends_within_the_budget() -> None:
    big = b"y" * (PROBE_HEAD_SIZE * 2)
    for data in (bz2.compress(big), lzma.compress(big, format=lzma.FORMAT_XZ)):
        probed = probe(data)  # the default budget reaches the end: the size is what was decoded
        assert probed.container is not None
        (member,) = probed.container.members
        assert (member.size, selected(member)) == (len(big), "text")
        cut = probe(data, policy=ProbePolicy(scan_bytes=PROBE_HEAD_SIZE))
        assert cut.container is not None
        (member,) = cut.container.members
        assert member.size is None  # not stated, and the stream goes on past the budget
        assert selected(member) == "text"


def test_a_declared_size_the_stream_contradicts_is_a_finding_and_the_member_is_still_probed() -> (
    None
):
    for declared in (1000, 4):
        data = _patch_zip(fixture("members.zip"), 22, declared.to_bytes(4, "little"))
        probed = probe(data)
        assert probed.container is not None
        member = probed.container.members[0]
        assert member.size == declared  # reported as the container states it
        assert selected(member) == "text"  # probed with what the stream holds
        assert codes(probed)[0] == ("container_corrupt", "")
        assert probed.findings[0].details == {"declared": declared, "decoded": 232, "member": 0}


def test_a_short_declared_size_for_a_long_stream_is_a_finding() -> None:
    long = b"line of text\n" * 10000
    data = _patch_zip(
        _zip_bytes([("long.txt", long, zipfile.ZIP_DEFLATED)]), 22, (5).to_bytes(4, "little")
    )
    probed = probe(data)
    assert probed.container is not None
    assert selected(probed.container.members[0]) == "text"
    assert codes(probed)[0] == ("container_corrupt", "")
    assert "holds more than" in probed.findings[0].message


def test_a_stored_member_whose_two_sizes_disagree_is_a_finding() -> None:
    data = _patch_zip(fixture("members.zip"), 22, (99).to_bytes(4, "little"), member=2)
    probed = probe(data)
    assert probed.container is not None
    assert selected(probed.container.members[2]) == "tally"
    assert codes(probed)[0] == ("container_corrupt", "")
    assert probed.findings[0].details == {"compressed_size": 17, "member": 2, "size": 99}


def test_a_gzip_whose_stated_size_disagrees_with_its_stream_is_a_finding() -> None:
    data = bytearray(gzip.compress(NOTES, mtime=0))
    data[-4:] = (len(NOTES) + 1).to_bytes(4, "little")
    probed = probe(bytes(data))
    assert probed.container is not None
    assert selected(probed.container.members[0]) == "text"
    assert codes(probed)[0] == ("container_corrupt", "")
    assert probed.findings[0].details == {
        "declared": len(NOTES) + 1,
        "decoded": len(NOTES),
        "member": 0,
        "stream": 0,
    }


def test_a_stream_container_inside_a_member_cut_by_the_budget_is_not_opened() -> None:
    noise = b"".join(hashlib.sha256(i.to_bytes(4, "little")).digest() for i in range(3000))
    for inner in (gzip.compress(noise, mtime=0), bz2.compress(noise), lzma.compress(noise)):
        assert len(inner) > PROBE_HEAD_SIZE
        outer = _zip_bytes([("inner", inner, zipfile.ZIP_DEFLATED)])
        probed = probe(outer, policy=ProbePolicy(scan_bytes=PROBE_HEAD_SIZE))
        assert probed.container is not None
        (member,) = probed.container.members
        assert member.nested is not None and member.nested.members == ()
        assert codes(probed)[0] == ("container_limit", "bytes")
        assert "inside a compressed member is not opened" in probed.findings[0].message
        whole = probe(outer)  # the default budget covers it: opened, and its member probed
        assert whole.container is not None
        nested = whole.container.members[0].nested
        assert nested is not None and nested.members[0].probe is not None


def _tar_header(typeflag: bytes, size: int, name: bytes = b"x") -> bytes:
    header = bytearray(512)
    header[: len(name)] = name
    header[100:108] = b"0000644\x00"
    header[124:136] = f"{size:011o}\x00".encode()
    header[156:157] = typeflag
    header[257:265] = b"ustar\x0000"
    header[148:156] = b" " * 8
    header[148:156] = f"{sum(header):06o}\x00 ".encode()
    return bytes(header)


def test_a_flood_of_extended_headers_cannot_get_around_the_member_limit() -> None:
    first = fixture("members.tar")[:1024]  # the first member and its data
    data = first + _tar_header(b"x", 0) * 40 + bytes(1024)
    probed = probe(data, policy=ProbePolicy(max_members=5))
    assert probed.container is not None
    assert [m.name for m in probed.container.members] == [b"notes.txt"]
    assert not probed.container.complete
    assert codes(probed)[0] == ("container_limit", "members")
    assert probed.findings[0].details["headers"] == 16


def test_an_extended_header_declaring_more_than_remains_is_corrupt() -> None:
    data = fixture("members.tar")[:1024] + _tar_header(b"x", 1_000_000) + bytes(512)
    probed = probe(data)
    assert probed.container is not None
    assert not probed.container.complete
    assert codes(probed)[0] == ("container_corrupt", "")
    assert (
        "extended header" in probed.findings[0].message and "remain" in probed.findings[0].message
    )


# --- Streams laid end to end: gzip members, bzip2 and xz streams ---------------------------------

STREAMS: Final[dict[ContainerKind, tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]]] = {
    ContainerKind.GZIP: (lambda data: gzip.compress(data, mtime=0), gzip.decompress),
    ContainerKind.BZIP2: (bz2.compress, bz2.decompress),
    ContainerKind.XZ: (lambda data: lzma.compress(data, format=lzma.FORMAT_XZ), lzma.decompress),
}


def test_streams_laid_end_to_end_are_one_member_holding_their_concatenation() -> None:
    # `gzip -c >> log.gz`, pbzip2 and `cat a.xz b.xz` write streams end to end; their tools decode
    # the lot as one. A tar written across two streams must list whole, with citations that the
    # standard library's multi-stream readers resolve.
    tar = fixture("members.tar")
    cut = 1600  # inside the third member's header, so no stream boundary is a tar boundary
    for kind, (compress, decompress) in STREAMS.items():
        data = compress(tar[:cut]) + compress(tar[cut:])
        probed = probe(data)
        assert probed.container is not None and probed.container.kind is kind
        (member,) = probed.container.members
        assert (member.size, member.compressed_size) == (
            len(tar),
            len(data) if kind is not ContainerKind.GZIP else len(data) - 18,
        )
        assert member.nested is not None and member.nested.complete
        assert [m.name for m in member.nested.members] == [
            b"notes.txt",
            b"logs/",
            b"logs/lift.tally",
            b"latest",
            b"drive.bag",
        ]
        with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
            for inner in member.nested.members:
                if inner.kind is MemberKind.FILE:
                    extracted = archive.extractfile(inner.name.decode())
                    assert extracted is not None
                    assert resolve(data, inner.entry.locator, [decompress]) == extracted.read()
        assert selected(by_name(member.nested)[b"logs/lift.tally"]) == "tally"
        assert [f.code for f in probed.findings] == ["neptune.probe.unsupported"], kind


def test_each_gzip_member_states_its_own_size_and_each_statement_is_checked() -> None:
    first = bytearray(gzip.compress(NOTES, mtime=0))
    first[-4:] = (len(NOTES) + 1).to_bytes(4, "little")  # the first member's trailer lies
    probed = probe(bytes(first) + gzip.compress(NOTES, mtime=0))
    assert probed.container is not None
    (member,) = probed.container.members
    assert member.size == 2 * len(NOTES) + 1  # the sum of what the members state
    assert selected(member) == "text"
    assert codes(probed) == [("container_corrupt", ""), ("unsupported", "")]
    assert probed.findings[0].details == {
        "declared": len(NOTES) + 1,
        "decoded": len(NOTES),
        "member": 0,
        "stream": 0,
    }


def test_streams_the_budget_cuts_are_not_complete_and_state_no_total() -> None:
    tar = _ustar([("big", b"x" * PROBE_HEAD_SIZE), ("after", NOTES)])
    half = len(tar) // 2
    for kind, (compress, _) in STREAMS.items():
        data = compress(tar[:half]) + compress(tar[half:])
        probed = probe(data, policy=ProbePolicy(scan_bytes=PROBE_HEAD_SIZE))
        assert probed.container is not None
        (member,) = probed.container.members
        assert member.size is None, kind  # two streams seen, the end not reached: no total
        assert member.nested is not None and not member.nested.complete
        assert [m.name for m in member.nested.members] == [b"big"]
        assert codes(probed)[0] == ("container_limit", "bytes")
        whole = probe(data)
        assert whole.container is not None
        (member,) = whole.container.members
        assert member.size == len(tar)
        assert member.nested is not None and member.nested.complete


def test_bytes_after_the_last_stream_are_padding_if_null_and_a_finding_otherwise() -> None:
    # Null padding follows gzip members written to block devices; gzip and Python skip it. Other
    # bytes open no stream: they are cited as corrupt and the member keeps what was decoded.
    garbage = b"not a stream, nor a trailer"  # read as gzip's trailer, it would claim a bomb
    for kind, (compress, _) in STREAMS.items():
        padded = probe(compress(NOTES) + bytes(64))
        assert padded.container is not None
        (member,) = padded.container.members
        assert (member.size, selected(member)) == (len(NOTES), "text")
        assert codes(padded) == [("unsupported", "")], kind
        data = compress(NOTES) + garbage
        probed = probe(data)
        assert probed.container is not None
        (member,) = probed.container.members
        assert (member.size, selected(member)) == (len(NOTES), "text")
        assert codes(probed) == [("container_corrupt", ""), ("unsupported", "")], kind
        finding = probed.findings[0]
        assert finding.subject.locator == (ByteRange(len(data) - len(garbage), len(garbage)),)  # type: ignore[union-attr]
        assert finding.details == {
            "length": len(garbage),
            "member": 0,
            "offset": len(data) - len(garbage),
        }
        assert f"open no {kind} stream" in finding.message


def test_a_stream_the_input_cuts_short_is_a_finding_and_its_head_is_still_probed() -> None:
    long = NOTES * 300  # a first stream past the head, so the cut leaves a whole head to probe
    for kind, (compress, _) in STREAMS.items():
        second = compress(NOTES)
        cuts = [second[: len(second) // 2]]  # inside the second stream's data
        if kind is ContainerKind.GZIP:
            cuts += [second[:-3], second[:5]]  # inside the second member's trailer; its header
        for tail in cuts:
            probed = probe(compress(long) + tail)
            assert probed.container is not None
            (member,) = probed.container.members
            assert member.size is None, (kind, len(tail))  # the end was never reached
            assert selected(member) == "text"
            assert codes(probed)[0] == ("container_corrupt", ""), (kind, len(tail))
            finding = probed.findings[0]
            assert (
                "before its end marker" in finding.message and "not probed" not in finding.message
            )
            decoded = finding.details["decoded"]
            assert isinstance(decoded, int) and decoded >= len(long)
    short = compress(NOTES) + compress(NOTES)[:40]  # too little before the cut for a whole head
    probed = probe(short)
    assert probed.container is not None
    assert probed.container.members[0].probe is None
    assert "not probed" in probed.findings[0].message


# --- Hostile input: never an exception, always the same answer -----------------------------------

CORPUS: Final = sorted(p.name for p in CONTAINERS.iterdir())


@settings(max_examples=150, deadline=None)
@given(
    st.sampled_from(CORPUS),
    st.lists(st.tuples(st.integers(0, 5000), st.binary(min_size=1, max_size=4)), max_size=4),
    st.integers(0, 6000),
)
def test_mutated_containers_never_raise_and_probe_deterministically(
    name: str, edits: list[tuple[int, bytes]], cut: int
) -> None:
    data = bytearray(fixture(name))
    for at, value in edits:
        if at < len(data):
            data[at : at + len(value)] = value[: len(data) - at]
    mutated = bytes(data[:cut]) if cut < len(data) else bytes(data)
    first, second = probe(mutated, name), probe(mutated, name)
    assert canonical_json.dumps(first.to_json()) == canonical_json.dumps(second.to_json())
    for finding in first.findings:
        check_ingest_finding(finding)
        assert isinstance(finding, IngestFinding)
        subject = finding.subject
        assert getattr(subject, "source", None) == first.source


@given(st.binary(max_size=2000))
def test_random_bytes_never_raise(data: bytes) -> None:
    for prefix in (b"PK\x03\x04", b"\x1f\x8b\x08", b"BZh9", b"\xfd7zXZ\x00", bytes(257) + b"ustar"):
        probed = probe(prefix + data)
        assert probed.container is not None
        canonical_json.dumps(probed.to_json())
