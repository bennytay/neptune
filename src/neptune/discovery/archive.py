"""Archive inspection within limits (ADR 0029 §2).

Zip and tar archives, plain or gzip/bzip2/xz compressed, and single compressed streams are
inspected without extracting anything. ``ArchiveLimits`` bounds what one source may cost: how many
members, how many bytes per member and in total once inflated, the compression ratio, and how
deep archives nest. Declared sizes are checked before a byte is inflated; actual bytes are counted
while inflating, because declared sizes can lie. Every exceeded limit, every unsafe member name,
every link or special member and every truncation is an ``IngestFinding`` citing the member's
bytes, never an exception, so the rest of a job is untouched.

The transform is ``neptune.archive`` at one version with the limits as its config: a different
limit is a different lineage. Member names are recorded exactly as the archive declares them.
Nested archives are read through a spool in scratch space (``neptune.discovery.scratch``), never
into the source tree; the spool is deleted when inspection ends. One full pass over an archive
inflates every member once; an archive adapter may later fuse this check with its own read.

What this module does **not** do: extract, resolve links, or decide what the archive means.
"""

import bz2
import gzip
import io
import lzma
import stat
import struct
import tarfile
import tempfile
import zipfile
import zlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Final, Protocol

from neptune.discovery.policy import bytes_field, path_problem, text_field
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, Locator, TransformRecord

ARCHIVE_ADAPTER_ID: Final = "neptune.archive"
ARCHIVE_VERSION: Final = "1.0.0"

PROBE_SIZE: Final = 512  # covers every magic, including ``ustar`` at offset 257
MAX_HEADER_SIZE: Final = 1 << 20  # headers and link targets are read whole: pax, GNU long names
GiB: Final = 1 << 30
_BLOCK: Final = 1 << 20
_SPOOL_MEMORY: Final = 1 << 20
_TAR_BLOCK: Final = 512

# Finding codes; each is documented in ADR 0029 §2.
UNRECOGNISED: Final = "neptune.archive.unrecognised"
TRUNCATED: Final = "neptune.archive.truncated"
CORRUPT: Final = "neptune.archive.corrupt"
MEMBER_COUNT_EXCEEDED: Final = "neptune.archive.member_count_exceeded"
MEMBER_SIZE_EXCEEDED: Final = "neptune.archive.member_size_exceeded"
TOTAL_SIZE_EXCEEDED: Final = "neptune.archive.total_size_exceeded"
COMPRESSION_RATIO_EXCEEDED: Final = "neptune.archive.compression_ratio_exceeded"
NESTING_DEPTH_EXCEEDED: Final = "neptune.archive.nesting_depth_exceeded"
HEADER_TOO_LARGE: Final = "neptune.archive.header_too_large"
MEMBER_PATH_UNSAFE: Final = "neptune.archive.member_path_unsafe"
MEMBER_LINK: Final = "neptune.archive.member_link"
MEMBER_SPECIAL: Final = "neptune.archive.member_special"
MEMBER_ENCRYPTED: Final = "neptune.archive.member_encrypted"
MEMBER_UNSUPPORTED: Final = "neptune.archive.member_unsupported"
MEMBER_TRUNCATED: Final = "neptune.archive.member_truncated"
MEMBER_CORRUPT: Final = "neptune.archive.member_corrupt"


@dataclass(frozen=True)
class ArchiveLimits:
    """What one archive, nested archives included, may cost. Sizes are uncompressed bytes."""

    max_members: int = 10_000
    max_member_size: int = 8 * GiB
    max_total_size: int = 64 * GiB
    max_compression_ratio: int = 100
    max_depth: int = 3

    def __post_init__(self) -> None:
        for name, value in self.to_json().items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def to_json(self) -> JsonObject:
        return {
            "max_compression_ratio": self.max_compression_ratio,
            "max_depth": self.max_depth,
            "max_member_size": self.max_member_size,
            "max_members": self.max_members,
            "max_total_size": self.max_total_size,
        }

    def transform(self) -> TransformRecord:
        return transform_record(
            adapter_id=ARCHIVE_ADAPTER_ID, adapter_version=ARCHIVE_VERSION, config=self.to_json()
        )


class ArchiveKind(StrEnum):
    ZIP = "zip"
    TAR = "tar"
    GZIP = "gzip"  # a compressed stream; a tar inside is inspected as one archive
    BZIP2 = "bzip2"
    XZ = "xz"


class MemberKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    SPECIAL = "special"
    STREAM = "stream"  # the one member of a single compressed stream


def sniff(head: bytes) -> ArchiveKind | None:
    """The archive kind the first bytes declare, or ``None``. Needs up to ``PROBE_SIZE`` bytes."""
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return ArchiveKind.ZIP
    if head[:2] == b"\x1f\x8b":
        return ArchiveKind.GZIP
    if head[:3] == b"BZh":
        return ArchiveKind.BZIP2
    if head[:6] == b"\xfd7zXZ\x00":
        return ArchiveKind.XZ
    if head[257:262] == b"ustar":
        return ArchiveKind.TAR
    return None


@dataclass(frozen=True)
class ArchiveMember:
    """One member as the archive declares it, and what inspection read of it."""

    name: str
    kind: MemberKind
    declared_size: int | None  # a single compressed stream declares nothing trustworthy
    locator: tuple[Locator, ...]  # from the top-level source to this member's bytes
    read_bytes: int
    nested: "ArchiveReport | None"


@dataclass(frozen=True)
class ArchiveReport:
    """What one archive holds. ``findings`` covers this archive and everything nested in it."""

    kind: ArchiveKind | None
    depth: int
    members: tuple[ArchiveMember, ...]
    findings: tuple[IngestFinding, ...]
    complete: bool  # False when a limit or a defect stopped the inspection early


def inspect_archive(
    stream: IO[bytes],
    *,
    source: ContentId,
    size: int,
    scratch: Path,
    limits: ArchiveLimits | None = None,
) -> ArchiveReport:
    """Inspect ``stream`` (the bytes of ``source``, ``size`` long) within ``limits``.

    ``scratch`` is a directory from ``neptune.discovery.scratch.scratch_space``; nested archives
    larger than 1 MiB are spooled there while they are inspected. The stream must be seekable.
    """
    limits = limits or ArchiveLimits()
    state = _State(source, limits, limits.transform(), Path(scratch), limits.max_total_size)
    return _inspect(state, stream, size, (), 1)


# --- shared machinery --------------------------------------------------------------------------


class _Readable(Protocol):
    """What a member's file object must offer: zip, tar and compressed streams all do."""

    def read(self, n: int = ..., /) -> bytes: ...


@dataclass
class _State:
    source: ContentId
    limits: ArchiveLimits
    transform: TransformRecord
    scratch: Path
    total_remaining: int
    findings: list[IngestFinding] = field(default_factory=list)
    stopped: bool = False  # the total budget is spent: every level stops

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        locator: tuple[Locator, ...],
        message: str,
        details: JsonObject,
    ) -> None:
        self.findings.append(
            ingest_finding(
                code=code,
                category=category,
                severity=severity,
                subject=EvidenceRef(self.source, locator),
                transform=self.transform,
                message=message,
                details=details,
            )
        )

    def limit(
        self, code: str, locator: tuple[Locator, ...], message: str, details: JsonObject
    ) -> None:
        self.finding(code, FindingCategory.LIMIT, Severity.ERROR, locator, message, details)

    def charge(self, amount: int) -> None:
        self.total_remaining = max(self.total_remaining - amount, 0)


@dataclass(frozen=True)
class _Pumped:
    read: int
    nested: "ArchiveReport | None" = None
    stopped: bool = False  # a limit, a truncation or a defect ended the read before its end


def _inspect(
    state: _State, stream: IO[bytes], size: int, prefix: tuple[Locator, ...], depth: int
) -> ArchiveReport:
    start = len(state.findings)
    stream.seek(0)
    head = _read_up_to(stream, PROBE_SIZE)
    kind = sniff(head)
    if kind is None:
        state.finding(
            UNRECOGNISED,
            FindingCategory.UNSUPPORTED,
            Severity.ERROR,
            (*prefix, ByteRange(0, size)),
            "not a zip, tar or compressed stream; not inspected",
            {"head_hex": head[:8].hex()},
        )
        members: list[ArchiveMember] = []
        complete = False
    elif kind is ArchiveKind.ZIP:
        members, complete = _zip(state, stream, size, head, prefix, depth)
    elif kind is ArchiveKind.TAR:
        members, complete = _tar(state, stream, size, kind, prefix, depth)
    else:
        members, complete = _compressed(state, stream, size, kind, prefix, depth)
    return ArchiveReport(kind, depth, tuple(members), tuple(state.findings[start:]), complete)


def _declared_within(
    state: _State,
    name: str,
    locator: tuple[Locator, ...],
    declared: int,
    *,
    compressed: int | None,
) -> bool:
    """Check a member's declared size against the limits before inflating anything."""
    limits = state.limits
    if declared > limits.max_member_size:
        state.limit(
            MEMBER_SIZE_EXCEEDED,
            locator,
            f"member declares {declared} bytes, more than the {limits.max_member_size} allowed;"
            " not read",
            {**text_field("name", name), "declared_size": declared, **limits.to_json()},
        )
        return False
    if declared > state.total_remaining:
        state.limit(
            TOTAL_SIZE_EXCEEDED,
            locator,
            f"member declares {declared} bytes, beyond the {state.total_remaining} left of the"
            f" {limits.max_total_size} byte budget; inspection stopped",
            {**text_field("name", name), "declared_size": declared, **limits.to_json()},
        )
        state.stopped = True
        return False
    if compressed is not None and declared > limits.max_compression_ratio * max(compressed, 1):
        state.limit(
            COMPRESSION_RATIO_EXCEEDED,
            locator,
            f"member declares {declared} bytes from {compressed} compressed, over"
            f" {limits.max_compression_ratio}:1; not read",
            {
                **text_field("name", name),
                "uncompressed": declared,
                "compressed": compressed,
                **limits.to_json(),
            },
        )
        return False
    return True


def _pump(
    state: _State,
    fileobj: _Readable,
    name: str,
    locator: tuple[Locator, ...],
    declared: int | None,
    depth: int,
    *,
    compressed: int | None = None,
) -> _Pumped:
    """Read a member to its end within the limits, inspecting it as an archive if it is one.

    ``compressed`` is the member's compressed size when no declared size could be checked against
    the ratio beforehand (a single compressed stream): the read then stops at the ratio cap too.
    """
    limits = state.limits
    caps: dict[str, int] = {"member": limits.max_member_size, "total": state.total_remaining}
    if compressed is not None:
        caps["ratio"] = limits.max_compression_ratio * max(compressed, 1)
    allowed = min(caps.values())
    read = 0
    spool: IO[bytes] | None = None
    try:
        try:
            head = _read_up_to(fileobj, min(PROBE_SIZE, allowed + 1))
            read = len(head)
            inner = sniff(head)
            if inner is not None and read <= allowed:
                if depth + 1 > limits.max_depth:
                    state.limit(
                        NESTING_DEPTH_EXCEEDED,
                        locator,
                        f"member is a {inner} archive nested {depth + 1} deep, more than the"
                        f" {limits.max_depth} allowed; not inspected",
                        {**text_field("name", name), "depth": depth + 1, **limits.to_json()},
                    )
                    state.charge(read)
                    return _Pumped(read, stopped=True)
                spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115 (closed in ``finally``)
                    max_size=_SPOOL_MEMORY, dir=str(state.scratch)
                )
                spool.write(head)
            while read <= allowed:
                block = fileobj.read(min(_BLOCK, allowed - read + 1))
                if not block:
                    break
                read += len(block)
                if spool is not None and read <= allowed:
                    spool.write(block)
        except (
            EOFError,
            tarfile.TarError,
            zipfile.BadZipFile,
            zlib.error,
            lzma.LZMAError,
            OSError,
        ) as exc:
            state.charge(read)
            code = MEMBER_TRUNCATED if _is_truncation(exc) else MEMBER_CORRUPT
            _member_defect(state, code, name, locator, read, declared, exc)
            return _Pumped(read, stopped=True)
        state.charge(read)
        if read > allowed:
            binding = min(caps, key=caps.__getitem__)
            if binding == "ratio":
                assert compressed is not None  # the ratio cap exists only when it is known
                state.limit(
                    COMPRESSION_RATIO_EXCEEDED,
                    locator,
                    f"member inflates to more than {limits.max_compression_ratio} times its"
                    f" {compressed} compressed bytes; reading stopped at {read}",
                    {
                        **text_field("name", name),
                        "read_bytes": read,
                        "compressed": compressed,
                        **limits.to_json(),
                    },
                )
            elif binding == "member":
                state.limit(
                    MEMBER_SIZE_EXCEEDED,
                    locator,
                    f"member inflates past the {limits.max_member_size} bytes allowed; reading"
                    f" stopped at {read}",
                    {**text_field("name", name), "read_bytes": read, **limits.to_json()},
                )
            else:
                state.limit(
                    TOTAL_SIZE_EXCEEDED,
                    locator,
                    f"member inflates past the {limits.max_total_size} byte budget; inspection"
                    f" stopped at {read}",
                    {**text_field("name", name), "read_bytes": read, **limits.to_json()},
                )
                state.stopped = True
            return _Pumped(read, stopped=True)
        if declared is not None and read < declared:
            state.finding(
                MEMBER_TRUNCATED,
                FindingCategory.CORRUPT,
                Severity.ERROR,
                locator,
                f"member declares {declared} bytes but only {read} are present; truncated",
                {**text_field("name", name), "declared_size": declared, "read_bytes": read},
            )
            return _Pumped(read, stopped=True)
        if spool is None:
            return _Pumped(read)
        spool.seek(0)
        nested = _inspect(state, spool, read, locator, depth + 1)
        return _Pumped(read, nested, stopped=state.stopped)
    finally:
        if spool is not None:
            spool.close()


def _member_defect(
    state: _State,
    code: str,
    name: str,
    locator: tuple[Locator, ...],
    read: int,
    declared: int | None,
    exc: BaseException,
) -> None:
    what = "ends before its declared size" if code == MEMBER_TRUNCATED else "cannot be decoded"
    details: dict[str, JsonValue] = {
        **text_field("name", name),
        "read_bytes": read,
        **_detail(exc),
    }
    if declared is not None:
        details["declared_size"] = declared
    state.finding(
        code,
        FindingCategory.CORRUPT,
        Severity.ERROR,
        locator,
        f"member {what}; {read} bytes were read",
        details,
    )


def _detail(exc: BaseException) -> JsonObject:
    return text_field("detail", str(exc) or type(exc).__name__)


def _read_up_to(fileobj: _Readable, count: int) -> bytes:
    """Up to ``count`` bytes, tolerating short reads; shorter only at EOF."""
    pieces: list[bytes] = []
    got = 0
    while got < count:
        block = fileobj.read(count - got)
        if not block:
            break
        pieces.append(block)
        got += len(block)
    return b"".join(pieces)


# --- zip ---------------------------------------------------------------------------------------

_EOCD: Final = b"PK\x05\x06"
_EOCD_SIZE: Final = 22
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE: Final = 20
_ZIP64_EOCD: Final = b"PK\x06\x06"
_ZIP64_EOCD_SIZE: Final = 56


def _zip_entry_count(stream: IO[bytes], size: int) -> int | None:
    """The member count the end-of-central-directory record declares, before parsing anything."""
    tail_length = min(size, _EOCD_SIZE + 0xFFFF)
    stream.seek(size - tail_length)
    tail = _read_up_to(stream, tail_length)
    position = tail.rfind(_EOCD)
    if position < 0 or len(tail) - position < _EOCD_SIZE:
        return None
    entries, directory_size, directory_offset = struct.unpack_from("<HII", tail, position + 10)
    count: int = entries
    if entries == 0xFFFF or 0xFFFFFFFF in (directory_size, directory_offset):
        locator_at = position - _ZIP64_LOCATOR_SIZE
        if locator_at >= 0 and tail[locator_at : locator_at + 4] == _ZIP64_LOCATOR:
            (record_offset,) = struct.unpack_from("<Q", tail, locator_at + 8)
            if record_offset + _ZIP64_EOCD_SIZE <= size:
                stream.seek(record_offset)
                record = _read_up_to(stream, _ZIP64_EOCD_SIZE)
                if record[:4] == _ZIP64_EOCD:
                    (count,) = struct.unpack_from("<Q", record, 32)
    return count


def _zip(
    state: _State,
    stream: IO[bytes],
    size: int,
    head: bytes,
    prefix: tuple[Locator, ...],
    depth: int,
) -> tuple[list[ArchiveMember], bool]:
    limits = state.limits
    scope = (*prefix, ByteRange(0, size))
    declared = _zip_entry_count(stream, size)
    if declared is None:
        if head.startswith(b"PK\x03\x04"):
            state.finding(
                TRUNCATED,
                FindingCategory.CORRUPT,
                Severity.ERROR,
                scope,
                "zip has members but no end-of-central-directory record; it is truncated",
                {"size": size},
            )
        else:
            state.finding(
                CORRUPT,
                FindingCategory.CORRUPT,
                Severity.ERROR,
                scope,
                "zip end-of-central-directory record is missing or malformed",
                {"size": size},
            )
        return [], False
    if declared > limits.max_members:
        state.limit(
            MEMBER_COUNT_EXCEEDED,
            scope,
            f"zip declares {declared} members, more than the {limits.max_members} allowed;"
            " not inspected",
            {"members": declared, **limits.to_json()},
        )
        return [], False
    stream.seek(0)
    try:
        archive = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, struct.error) as exc:
        state.finding(
            CORRUPT,
            FindingCategory.CORRUPT,
            Severity.ERROR,
            scope,
            "zip central directory cannot be read",
            {"size": size, **_detail(exc)},
        )
        return [], False
    with archive:
        infos = sorted(archive.infolist(), key=lambda info: info.header_offset)
        if len(infos) > limits.max_members:
            state.limit(
                MEMBER_COUNT_EXCEEDED,
                scope,
                f"zip holds {len(infos)} members, more than the {limits.max_members} allowed;"
                " not inspected",
                {"members": len(infos), **limits.to_json()},
            )
            return [], False
        total_declared = sum(info.file_size for info in infos)
        if total_declared > state.total_remaining:
            state.limit(
                TOTAL_SIZE_EXCEEDED,
                scope,
                f"members declare {total_declared} bytes uncompressed, beyond the"
                f" {state.total_remaining} left of the {limits.max_total_size} byte budget;"
                " not inspected",
                {"declared_total": total_declared, **limits.to_json()},
            )
            state.stopped = True
            return [], False
        if total_declared > limits.max_compression_ratio * max(size, 1):
            state.limit(
                COMPRESSION_RATIO_EXCEEDED,
                scope,
                f"members declare {total_declared} bytes from {size} compressed, over"
                f" {limits.max_compression_ratio}:1; not inspected",
                {"uncompressed": total_declared, "compressed": size, **limits.to_json()},
            )
            return [], False
        members: list[ArchiveMember] = []
        for index, info in enumerate(infos):
            end = infos[index + 1].header_offset if index + 1 < len(infos) else archive.start_dir
            locator = (*prefix, ByteRange(info.header_offset, max(end - info.header_offset, 0)))
            members.append(_zip_member(state, archive, info, locator, depth))
            if state.stopped:
                return members, False
        return members, True


def _zip_member(
    state: _State,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    locator: tuple[Locator, ...],
    depth: int,
) -> ArchiveMember:
    name = info.orig_filename  # ``filename`` is cut at the first NUL; the declared name is not
    mode = info.external_attr >> 16
    if info.is_dir():
        kind = MemberKind.DIRECTORY
    elif stat.S_ISLNK(mode):
        kind = MemberKind.SYMLINK
    elif mode == 0 or stat.S_ISREG(mode):
        kind = MemberKind.FILE
    else:
        kind = MemberKind.SPECIAL

    def member(read: int, nested: ArchiveReport | None = None) -> ArchiveMember:
        return ArchiveMember(name, kind, info.file_size, locator, read, nested)

    if _skip_by_policy(state, name, kind, locator, mode=mode):
        return member(0)
    if info.flag_bits & 0x1:
        state.finding(
            MEMBER_ENCRYPTED,
            FindingCategory.UNSUPPORTED,
            Severity.ERROR,
            locator,
            "member is encrypted; not read",
            text_field("name", name),
        )
        return member(0)
    if kind is MemberKind.SYMLINK and info.file_size > MAX_HEADER_SIZE:
        # A link's target is its member data, read whole to record it: capped like a header.
        state.limit(
            HEADER_TOO_LARGE,
            locator,
            f"symlink target declares {info.file_size} bytes, more than the {MAX_HEADER_SIZE}"
            " allowed; not read",
            {
                **text_field("name", name),
                "declared_size": info.file_size,
                "max_header_size": MAX_HEADER_SIZE,
            },
        )
        return member(0)
    if not _declared_within(state, name, locator, info.file_size, compressed=info.compress_size):
        return member(0)
    try:
        fileobj = archive.open(info)
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, struct.error) as exc:
        _member_defect(state, MEMBER_CORRUPT, name, locator, 0, info.file_size, exc)
        return member(0)
    except (NotImplementedError, RuntimeError) as exc:
        state.finding(
            MEMBER_UNSUPPORTED,
            FindingCategory.UNSUPPORTED,
            Severity.ERROR,
            locator,
            "member uses a compression method or feature Neptune does not decode; not read",
            {**text_field("name", name), "method": info.compress_type, **_detail(exc)},
        )
        return member(0)
    with fileobj:
        if kind is MemberKind.SYMLINK:
            target = _read_up_to(fileobj, info.file_size)
            state.charge(len(target))
            _link_finding(state, name, locator, "symlink", target)
            return member(len(target))
        pumped = _pump(state, fileobj, name, locator, info.file_size, depth)
    return member(pumped.read, pumped.nested)


# --- tar and compressed streams ----------------------------------------------------------------

_EXTENDED_HEADERS: Final = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)
_TRUNCATION_MESSAGES: Final = ("unexpected end of data", "truncated header", "empty file")
_SPECIAL_TYPES: Final = {
    tarfile.FIFOTYPE: "fifo",
    tarfile.CHRTYPE: "character_device",
    tarfile.BLKTYPE: "block_device",
}


class _HeaderTooLarge(tarfile.TarError):
    def __init__(self, size: int) -> None:
        super().__init__(f"extended header declares {size} bytes")
        self.size = size


class _GuardedTarInfo(tarfile.TarInfo):
    """Refuses extended headers above ``MAX_HEADER_SIZE``: tarfile reads them whole into memory."""

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.type in _EXTENDED_HEADERS and self.size > MAX_HEADER_SIZE:
            raise _HeaderTooLarge(self.size)
        # Not in typeshed, but the hook tarfile dispatches every header through since 2.5.
        processed: tarfile.TarInfo = super()._proc_member(archive)  # type: ignore[misc]
        return processed


def _compressed(
    state: _State,
    stream: IO[bytes],
    size: int,
    kind: ArchiveKind,
    prefix: tuple[Locator, ...],
    depth: int,
) -> tuple[list[ArchiveMember], bool]:
    """A gzip, bzip2 or xz stream: a tar inside is one archive; anything else is one member."""
    scope = (*prefix, ByteRange(0, size))
    try:
        with _open_compressed(kind, stream) as inflater:
            head = _read_up_to(inflater, PROBE_SIZE)
    except EOFError as exc:
        _stream_defect(state, TRUNCATED, kind, scope, size, exc)
        return [], False
    except (OSError, zlib.error, lzma.LZMAError) as exc:
        _stream_defect(state, CORRUPT, kind, scope, size, exc)
        return [], False
    if sniff(head) is ArchiveKind.TAR:
        return _tar(state, stream, size, kind, prefix, depth)
    stream.seek(0)
    with _open_compressed(kind, stream) as inflater:
        pumped = _pump(state, inflater, "", scope, None, depth, compressed=size)
    member = ArchiveMember("", MemberKind.STREAM, None, scope, pumped.read, pumped.nested)
    return [member], not pumped.stopped


def _open_compressed(kind: ArchiveKind, stream: IO[bytes]) -> io.BufferedIOBase:
    stream.seek(0)
    if kind is ArchiveKind.GZIP:
        return gzip.GzipFile(fileobj=stream, mode="rb")
    if kind is ArchiveKind.BZIP2:
        return bz2.BZ2File(stream, mode="rb")
    return lzma.LZMAFile(stream, mode="rb")


def _stream_defect(
    state: _State,
    code: str,
    kind: ArchiveKind,
    scope: tuple[Locator, ...],
    size: int,
    exc: BaseException,
) -> None:
    what = "ends before its end-of-stream marker" if code == TRUNCATED else "cannot be decoded"
    state.finding(
        code,
        FindingCategory.CORRUPT,
        Severity.ERROR,
        scope,
        f"{kind} stream {what}",
        {"size": size, **_detail(exc)},
    )


def _tar(
    state: _State,
    stream: IO[bytes],
    size: int,
    kind: ArchiveKind,
    prefix: tuple[Locator, ...],
    depth: int,
) -> tuple[list[ArchiveMember], bool]:
    """Tar in stream mode: headers and data are read once, in order, and never seeked back.

    A compressed tar is inflated by this module's own reader, not by tarfile's stream layer,
    which inflates each 10 KiB input block whole: bzip2 and xz turn such a block into gigabytes.
    A member that cannot be skipped without inflating it (every member of a compressed tar) ends
    the inspection as soon as a limit stops its read; the finding says so.
    """
    scope = (*prefix, ByteRange(0, size))
    compressed = kind is not ArchiveKind.TAR
    tar_prefix = scope if compressed else prefix  # member ranges are in the inflated tar stream
    members: list[ArchiveMember] = []
    stream.seek(0)
    with _open_compressed(kind, stream) if compressed else nullcontext(stream) as fileobj:
        try:
            archive = tarfile.open(  # noqa: SIM115 (the ``with`` below owns it once it exists)
                fileobj=fileobj, mode="r|", tarinfo=_GuardedTarInfo
            )
        except _HeaderTooLarge as exc:
            _header_too_large(state, scope, exc)
            return members, False
        except (tarfile.TarError, EOFError, zlib.error, lzma.LZMAError, OSError) as exc:
            _tar_error(state, scope, exc)
            return members, False
        with archive:
            return _tar_members(state, archive, size, scope, tar_prefix, compressed, depth)


def _tar_members(
    state: _State,
    archive: tarfile.TarFile,
    size: int,
    scope: tuple[Locator, ...],
    tar_prefix: tuple[Locator, ...],
    compressed: bool,
    depth: int,
) -> tuple[list[ArchiveMember], bool]:
    limits = state.limits
    members: list[ArchiveMember] = []
    count = 0
    while True:
        try:
            info = archive.next()
        except _HeaderTooLarge as exc:
            _header_too_large(state, scope, exc)
            return members, False
        except (tarfile.TarError, EOFError, zlib.error, lzma.LZMAError, OSError) as exc:
            _tar_error(state, scope, exc)
            return members, False
        if info is None:
            return members, True
        count += 1
        if count > limits.max_members:
            state.limit(
                MEMBER_COUNT_EXCEEDED,
                scope,
                f"tar holds more than the {limits.max_members} members allowed; inspection stopped",
                {"members": count, **limits.to_json()},
            )
            return members, False
        length = info.offset_data - info.offset + _round_up(info.size)
        locator = (*tar_prefix, ByteRange(info.offset, length))
        inflated = info.offset_data + info.size
        if compressed and inflated > limits.max_compression_ratio * max(size, 1):
            state.limit(
                COMPRESSION_RATIO_EXCEEDED,
                locator,
                f"tar stream inflates to at least {inflated} bytes from {size} compressed,"
                f" over {limits.max_compression_ratio}:1; inspection stopped",
                {
                    **text_field("name", info.name),
                    "uncompressed": inflated,
                    "compressed": size,
                    **limits.to_json(),
                },
            )
            return members, False
        member, proceed = _tar_member(state, archive, info, locator, depth)
        members.append(member)
        if not proceed:
            return members, False


def _tar_member(
    state: _State,
    archive: tarfile.TarFile,
    info: tarfile.TarInfo,
    locator: tuple[Locator, ...],
    depth: int,
) -> tuple[ArchiveMember, bool]:
    name = info.name
    if info.isdir():
        kind = MemberKind.DIRECTORY
    elif info.issym():
        kind = MemberKind.SYMLINK
    elif info.islnk():
        kind = MemberKind.HARDLINK
    elif info.isreg():
        kind = MemberKind.FILE
    else:
        kind = MemberKind.SPECIAL

    def member(read: int, nested: ArchiveReport | None = None) -> ArchiveMember:
        return ArchiveMember(name, kind, info.size, locator, read, nested)

    # Skipping a member still inflates its data to reach the next header, so its declared size
    # must fit the limits whatever its kind.
    if not _declared_within(state, name, locator, info.size, compressed=None):
        return member(0), False
    if kind in (MemberKind.SYMLINK, MemberKind.HARDLINK):
        target = info.linkname.encode("utf-8", "surrogateescape")
        _link_finding(state, name, locator, str(kind), target)
        state.charge(info.size)
        return member(0), True
    if _skip_by_policy(state, name, kind, locator, tar_type=info.type):
        state.charge(info.size)
        return member(0), True
    fileobj = archive.extractfile(info)
    if fileobj is None:
        state.charge(info.size)
        return member(0), True
    with fileobj:
        pumped = _pump(state, fileobj, name, locator, info.size, depth)
    return member(pumped.read, pumped.nested), not pumped.stopped and not state.stopped


def _skip_by_policy(
    state: _State,
    name: str,
    kind: MemberKind,
    locator: tuple[Locator, ...],
    *,
    mode: int | None = None,
    tar_type: bytes | None = None,
) -> bool:
    """Record why a member is not read, if policy says so: an unsafe name or a special file."""
    problem = path_problem(name)
    if problem is not None:
        state.finding(
            MEMBER_PATH_UNSAFE,
            FindingCategory.SKIPPED,
            Severity.ERROR,
            locator,
            f"member name could escape an extraction root ({problem}); not read",
            {**text_field("name", name), "problem": problem},
        )
        return True
    if kind is MemberKind.DIRECTORY:
        return True
    if kind is MemberKind.SPECIAL:
        details: dict[str, JsonValue] = dict(text_field("name", name))
        if mode is not None:
            details["mode"] = stat.filemode(mode)
        if tar_type is not None:
            details["type"] = _SPECIAL_TYPES.get(tar_type, "type_" + tar_type.hex())
        state.finding(
            MEMBER_SPECIAL,
            FindingCategory.SKIPPED,
            Severity.INFO,
            locator,
            "member is not a regular file; not read",
            details,
        )
        return True
    return False


def _link_finding(
    state: _State, name: str, locator: tuple[Locator, ...], link: str, target: bytes
) -> None:
    state.finding(
        MEMBER_LINK,
        FindingCategory.SKIPPED,
        Severity.INFO,
        locator,
        f"member is a {link}; recorded and not followed",
        {**text_field("name", name), "link": link, **bytes_field("target", target)},
    )


def _header_too_large(state: _State, scope: tuple[Locator, ...], exc: _HeaderTooLarge) -> None:
    state.limit(
        HEADER_TOO_LARGE,
        scope,
        f"tar extended header declares {exc.size} bytes, more than the {MAX_HEADER_SIZE} allowed;"
        " inspection stopped",
        {"declared_size": exc.size, "max_header_size": MAX_HEADER_SIZE},
    )


def _is_truncation(exc: BaseException) -> bool:
    """EOF inside a compressed stream, or tarfile's words for data that ends early."""
    return isinstance(exc, EOFError) or (
        isinstance(exc, tarfile.TarError) and any(t in str(exc) for t in _TRUNCATION_MESSAGES)
    )


def _tar_error(state: _State, scope: tuple[Locator, ...], exc: BaseException) -> None:
    truncated = _is_truncation(exc)
    code = TRUNCATED if truncated else CORRUPT
    what = "ends before its declared structure does" if truncated else "cannot be decoded"
    state.finding(
        code,
        FindingCategory.CORRUPT,
        Severity.ERROR,
        scope,
        f"tar {what}; inspection stopped",
        _detail(exc),
    )


def _round_up(size: int) -> int:
    return -(-size // _TAR_BLOCK) * _TAR_BLOCK


def limits_from_json(data: JsonValue) -> ArchiveLimits:
    """Rebuild limits from a transform's config (the inverse of ``ArchiveLimits.to_json``)."""
    if not isinstance(data, dict) or set(data) != set(ArchiveLimits().to_json()):
        raise ValueError(f"not an archive-limits config: {data!r}")
    values = {name: data[name] for name in ArchiveLimits().to_json()}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, got {value!r}")
    return ArchiveLimits(**{name: int(value) for name, value in values.items()})
