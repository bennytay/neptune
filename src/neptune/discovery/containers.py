"""Bounded inspection of the containers sniffing finds: zip, tar, gzip, bzip2 and xz (ADR 0027).

What it does: lists members with their names, kinds and sizes exactly as the container states
them, cites each member's stored bytes, hands each regular member's head to the engine's prober
so the report says what the container holds, and opens a member that is itself a container down
to a fixed depth. Nothing is extracted, no name is resolved against a filesystem, and every read
is bounded by ``ProbePolicy``: members listed, decoded bytes per compressed stream, nesting depth,
and the declared compression ratio above which a member is not decoded at all.

What it does not do: ingest members. An archive adapter would (none exists yet); this report is
for selection, explanation and the receipt. Problems are reported as findings, never raised: a
corrupt directory, a listing cut short by a limit, an encrypted or undecodable member are each
reported and the inspection carries on with what it can see.

Citations follow ADR 0016: a member's stored bytes are a ``ByteRange`` in its container's scope,
compressed if the member is compressed; a member of a nested container adds a step inside what
the engine decoded. Headers are metadata, not part of a citation; a gzip member is its whole
stream. A member's size the container does not state is hinted to adapters as the head's length
when the stream ended inside the head, and as one more than the head's length otherwise.
"""

import bz2
import lzma
import stat
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
from typing import Final, Protocol

from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints, SourceReader
from neptune.adapters.registry import Selection, SelectionStatus
from neptune.discovery.sniff import ContainerKind, Sniff
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, Locator

_READ: Final = 64 * 1024
_ZIP_TAIL: Final = 64 * 1024 + 22  # the end record plus the longest comment it can carry
_HEADER_LIMIT: Final = 64 * 1024  # the most a gzip header, a tar long name or a pax block may hold
_MAX_INDEX: Final = 1 << 62  # a declared count, size or offset at or over this is not believed


class MemberKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    OTHER = "other"  # a device, a FIFO, a format-specific entry


@dataclass(frozen=True)
class ProbePolicy:
    """The engine's limits. Its JSON is the engine's transform config, so a change re-lineages.

    - ``max_members``: members listed per container before the listing stops.
    - ``max_depth``: containers opened along one path; 2 opens a tar inside a gzip, 0 opens none.
    - ``scan_bytes``: decoded bytes examined per compressed stream, and central-directory bytes
      read per zip. At least ``PROBE_HEAD_SIZE``, so every head can be decoded.
    - ``max_ratio``: a member declaring more than this many decoded bytes per compressed byte is
      not decoded: it is reported instead.
    """

    max_members: int = 1000
    max_depth: int = 2
    scan_bytes: int = 1024 * 1024
    max_ratio: int = 1000

    def __post_init__(self) -> None:
        for name, least in (
            ("max_members", 1),
            ("max_depth", 0),
            ("scan_bytes", PROBE_HEAD_SIZE),
            ("max_ratio", 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < least:
                raise ValueError(f"{name} must be an integer of at least {least}, got {value!r}")

    def to_json(self) -> JsonObject:
        return {
            "max_depth": self.max_depth,
            "max_members": self.max_members,
            "max_ratio": self.max_ratio,
            "scan_bytes": self.scan_bytes,
        }


@dataclass(frozen=True)
class MemberProbe:
    """What the engine made of a member's head: its sniff and every adapter's claim, ranked."""

    sniff: Sniff
    selection: Selection

    def to_json(self) -> JsonObject:
        return {"selection": selection_to_json(self.selection), "sniff": self.sniff.to_json()}


def selection_to_json(selection: Selection) -> JsonObject:
    out: dict[str, JsonValue] = {
        "candidates": [
            {"adapter": c.adapter, "result": c.result.to_json(), "version": c.version}
            for c in selection.candidates
        ],
        "status": str(selection.status),
    }
    if selection.status is SelectionStatus.SELECTED:
        out["adapter"] = selection.candidates[0].adapter
    return out


# The engine probes a member's head (crash-isolated, findings cited to the subject) and builds
# a finding of ``<engine id>.<name>`` about a subject.
Prober = Callable[[bytes, ProbeHints, EvidenceRef], MemberProbe]
Reporter = Callable[[str, FindingCategory, Severity, EvidenceRef, str, JsonObject], None]


def _name_json(key: str, name: bytes) -> tuple[str, JsonValue]:
    """A name as text when it is UTF-8, else as ``<key>_hex``: names are kept byte-exact."""
    try:
        return key, name.decode("utf-8")
    except UnicodeDecodeError:
        return f"{key}_hex", name.hex()


@dataclass(frozen=True)
class Member:
    """One entry of a container, as the container states it. ``entry`` cites its stored bytes."""

    index: int
    name: bytes
    kind: MemberKind
    entry: EvidenceRef
    size: int | None = None  # decoded bytes, as declared
    compressed_size: int | None = None
    method: str | None = None
    link_target: bytes | None = None
    probe: MemberProbe | None = None  # the head's probe; None when the head was not decoded
    nested: "ContainerReport | None" = None

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "entry": self.entry.to_json(),
            "index": self.index,
            "kind": str(self.kind),
        }
        key, value = _name_json("name", self.name)
        out[key] = value
        for field in ("size", "compressed_size", "method"):
            if getattr(self, field) is not None:
                out[field] = getattr(self, field)
        if self.link_target is not None:
            key, value = _name_json("link_target", self.link_target)
            out[key] = value
        if self.probe is not None:
            out["probe"] = self.probe.to_json()
        if self.nested is not None:
            out["nested"] = self.nested.to_json()
        return out


@dataclass(frozen=True)
class ContainerReport:
    """What one container holds, as far as the policy let the engine look.

    ``declared_count`` is the member count the container states (a zip's end record), if any.
    ``complete`` is false when a limit, a corrupt structure or a missing decoder cut the listing or
    the inspection of a member short; the findings say which.
    """

    kind: ContainerKind
    members: tuple[Member, ...]
    declared_count: int | None
    complete: bool

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "complete": self.complete,
            "kind": str(self.kind),
            "members": [member.to_json() for member in self.members],
        }
        if self.declared_count is not None:
            out["declared_count"] = self.declared_count
        return out


# --- Views: the bytes a parser sees, whole or a bounded decoded prefix ------------------------


class _View(Protocol):
    @property
    def size(self) -> int: ...

    @property
    def complete(self) -> bool:
        """True if these are all the bytes; false for a decoded prefix cut by a budget."""
        ...

    def read(self, offset: int, length: int) -> bytes: ...


class _Whole:
    def __init__(self, reader: SourceReader) -> None:
        self._reader = reader

    @property
    def size(self) -> int:
        return self._reader.size

    @property
    def complete(self) -> bool:
        return True

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self._reader.size or length <= 0:
            return b""
        return self._reader.read(offset, min(length, self._reader.size - offset))


class _Window:
    """``length`` bytes of a view from ``offset``: a stored member, read in place."""

    def __init__(self, view: _View, offset: int, length: int) -> None:
        self._view, self._offset = view, offset
        self._size = max(0, min(length, view.size - offset))
        self._complete = view.complete and offset + length <= view.size

    @property
    def size(self) -> int:
        return self._size

    @property
    def complete(self) -> bool:
        return self._complete

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self._size or length <= 0:
            return b""
        return self._view.read(self._offset + offset, min(length, self._size - offset))


class _Prefix:
    """The decoded bytes of a compressed member, as many as the budget and the stream gave."""

    def __init__(self, data: bytes, complete: bool) -> None:
        self._data, self._complete = data, complete

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def complete(self) -> bool:
        return self._complete

    def read(self, offset: int, length: int) -> bytes:
        return self._data[offset : offset + length] if length > 0 else b""


# --- Decoding compressed streams, bounded --------------------------------------------------------


class _Decompressor(Protocol):
    def decompress(self, data: bytes, max_length: int) -> bytes: ...

    @property
    def needs_input(self) -> bool: ...

    @property
    def eof(self) -> bool: ...


class _Inflate:
    """``zlib.decompressobj`` with the ``needs_input`` / ``eof`` surface of bz2 and lzma."""

    def __init__(self) -> None:
        self._inflate = zlib.decompressobj(-15)
        self._tail = b""

    def decompress(self, data: bytes, max_length: int) -> bytes:
        out = self._inflate.decompress(self._tail + data, max_length)
        self._tail = self._inflate.unconsumed_tail
        return out

    @property
    def needs_input(self) -> bool:
        return not self._tail

    @property
    def eof(self) -> bool:
        return self._inflate.eof


def _exception_name(exc: BaseException) -> str:
    """``zlib.error``, ``OSError``: the class, qualified unless built in."""
    kind = type(exc)
    return kind.__name__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__name__}"


class _Decoded:
    """Up to ``budget`` decoded bytes of the compressed range ``[offset, offset + length)``.

    Bytes are produced on demand and kept, so the head can be taken first and the rest only if
    the head turns out to be a container. ``error`` names the exception a corrupt stream raised.
    """

    def __init__(
        self, view: _View, offset: int, length: int, decompressor: _Decompressor, budget: int
    ) -> None:
        self._view, self._pos, self._end = view, offset, min(offset + length, view.size)
        self._decompressor, self._budget = decompressor, budget
        self._pieces: list[bytes] = []
        self._have = 0
        self.error: str | None = None

    def prefix(self, want: int) -> bytes:
        """The first ``min(want, budget)`` decoded bytes, or fewer if the stream or input ends."""
        want = min(want, self._budget)
        stream = self._decompressor
        while self._have < want and not stream.eof and self.error is None:
            piece = b""
            if stream.needs_input and self._pos < self._end:
                piece = self._view.read(self._pos, min(_READ, self._end - self._pos))
                self._pos += len(piece)
            try:
                out = stream.decompress(piece, want - self._have)
            except (zlib.error, OSError, EOFError, lzma.LZMAError, ValueError) as exc:
                self.error = _exception_name(exc)
                break
            self._pieces.append(out)
            self._have += len(out)
            if not out and not piece:
                break  # the input is spent and the stream has nothing more to give
        return b"".join(self._pieces)[:want]

    @property
    def eof(self) -> bool:
        return self._decompressor.eof


# --- The inspection -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scope:
    """Where a parser is: the source, the steps to this container's bytes, and the depth."""

    source: ContentId
    steps: tuple[Locator, ...]
    depth: int
    policy: ProbePolicy
    prober: Prober
    report: Reporter

    def cite(self, offset: int, length: int) -> EvidenceRef:
        return EvidenceRef(self.source, (*self.steps, ByteRange(offset, length)))

    def nested(self, entry: EvidenceRef) -> "_Scope":
        return replace(self, steps=entry.locator, depth=self.depth + 1)

    def corrupt(self, subject: EvidenceRef, message: str, **details: JsonValue) -> None:
        self.report(
            "container_corrupt",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            subject,
            message,
            details,
        )

    def limit(self, which: str, subject: EvidenceRef, message: str, **details: JsonValue) -> None:
        self.report(
            "container_limit",
            FindingCategory.LIMIT,
            Severity.WARNING,
            subject,
            message,
            {"limit": which, **details},
        )

    def not_inspected(self, subject: EvidenceRef, message: str, **details: JsonValue) -> None:
        self.report(
            "container_not_inspected",
            FindingCategory.UNSUPPORTED,
            Severity.INFO,
            subject,
            message,
            details,
        )


def inspect_container(
    reader: SourceReader,
    kind: ContainerKind,
    *,
    policy: ProbePolicy,
    prober: Prober,
    report: Reporter,
) -> ContainerReport:
    """List and probe the members of ``reader``, a container of ``kind``, within ``policy``.

    ``prober`` probes a member's head and ``report`` records a finding; both are the engine's.
    Nothing raises for what the bytes hold: a hostile container yields a short report and findings.
    """
    scope = _Scope(reader.content_id, (), 1, policy, prober, report)
    return _inspect(_Whole(reader), kind, scope)


def _inspect(view: _View, kind: ContainerKind, scope: _Scope) -> ContainerReport:
    match kind:
        case ContainerKind.ZIP:
            return _zip(view, scope)
        case ContainerKind.TAR:
            return _tar(view, scope)
        case ContainerKind.GZIP:
            return _gzip(view, scope)
        case ContainerKind.BZIP2:
            return _stream(view, scope, kind, bz2.BZ2Decompressor())
        case ContainerKind.XZ:
            return _stream(view, scope, kind, lzma.LZMADecompressor(lzma.FORMAT_XZ))
        case _:
            scope.not_inspected(
                scope.cite(0, view.size),
                f"a {kind} container is recognised but not opened: no decoder is available",
                container=str(kind),
            )
            return ContainerReport(kind, (), None, False)


def _hint(name: bytes) -> str:
    """The last path component of a member's name, for ``ProbeHints``; names are advisory."""
    return name.rstrip(b"/").rsplit(b"/", 1)[-1].decode("utf-8", errors="replace")


def _finish(
    scope: _Scope,
    member: Member,
    head: bytes | None,
    hint_size: int | None,
    view: Callable[[], _View] | None,
) -> Member:
    """Probe a member's complete head and open it if it is a container the depth allows."""
    if member.kind is not MemberKind.FILE or head is None or hint_size is None:
        return member
    probe = scope.prober(head, ProbeHints(_hint(member.name), hint_size), member.entry)
    member = replace(member, probe=probe)
    inner = probe.sniff.container
    if inner is None or view is None:
        return member
    if scope.depth >= scope.policy.max_depth:
        scope.limit(
            "depth",
            member.entry,
            f"member {member.index} is a {inner} container {scope.depth} deep;"
            f" not opened (max_depth {scope.policy.max_depth})",
            container=str(inner),
            depth=scope.depth,
            max_depth=scope.policy.max_depth,
            member=member.index,
        )
        return member
    return replace(member, nested=_inspect(view(), inner, scope.nested(member.entry)))


def _decoded_head(
    scope: _Scope, member: Member, decoded: _Decoded, declared: int | None
) -> tuple[bytes | None, int | None, Callable[[], _View]]:
    """The head of a compressed member, or ``None`` with a finding if it cannot be decoded whole.

    Returns the head, the size to hint to adapters, and how to view the decoded bytes.
    """
    want = PROBE_HEAD_SIZE if declared is None else min(declared, PROBE_HEAD_SIZE)
    head = decoded.prefix(want)
    whole = decoded.eof

    def view() -> _View:
        return _Prefix(decoded.prefix(scope.policy.scan_bytes), decoded.eof)

    if decoded.error is not None:
        scope.corrupt(
            member.entry,
            f"member {member.index}: the {member.method} stream is corrupt ({decoded.error});"
            " not probed",
            error=decoded.error,
            member=member.index,
        )
        return None, None, view
    if len(head) < want and not whole:
        scope.corrupt(
            member.entry,
            f"member {member.index}: the {member.method} stream ends after {len(head)} decoded"
            f" bytes, before its end marker; not probed",
            decoded=len(head),
            member=member.index,
        )
        return None, None, view
    if declared is not None:
        return head, declared, view
    return head, len(head) if whole else len(head) + 1, view


# --- zip -----------------------------------------------------------------------------------------

_EOCD: Final = b"PK\x05\x06"
_EOCD64_LOCATOR: Final = b"PK\x06\x07"
_EOCD64: Final = b"PK\x06\x06"
_CENTRAL: Final = b"PK\x01\x02"
_LOCAL: Final = b"PK\x03\x04"
_ZIP_METHODS: Final = {0: "stored", 8: "deflate"}


def _u16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 2], "little")


def _u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "little")


def _u64(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 8], "little")


def _find_eocd(tail: bytes) -> int | None:
    """The end-of-central-directory record whose comment runs exactly to the end of the file."""
    at = tail.rfind(_EOCD)
    while at >= 0:
        if at + 22 <= len(tail) and at + 22 + _u16(tail, at + 20) == len(tail):
            return at
        at = tail.rfind(_EOCD, 0, at)
    return None


def _zip64_directory(view: _View, eocd_at: int) -> tuple[int, int, int] | None:
    """``(count, size, offset)`` of the central directory from the zip64 end record."""
    locator_at = eocd_at - 20
    if locator_at < 0:
        return None
    locator = view.read(locator_at, 20)
    if locator[:4] != _EOCD64_LOCATOR:
        return None
    record_at = _u64(locator, 8)
    if record_at + 56 > locator_at:
        return None
    record = view.read(record_at, 56)
    if len(record) < 56 or record[:4] != _EOCD64:
        return None
    count, size, offset = _u64(record, 32), _u64(record, 40), _u64(record, 48)
    if max(count, size, offset) >= _MAX_INDEX:
        return None
    return count, size, offset


def _zip64_fields(extra: bytes, sizes: tuple[int, int, int]) -> tuple[int, int, int] | None:
    """``(uncompressed, compressed, local offset)`` with 0xFFFFFFFF fields read from the extra."""
    at = 0
    while at + 4 <= len(extra):
        tag, length = _u16(extra, at), _u16(extra, at + 2)
        body = extra[at + 4 : at + 4 + length]
        if tag == 1:
            out, cursor = [], 0
            for value in sizes:
                if value == 0xFFFFFFFF:
                    if cursor + 8 > len(body):
                        return None
                    value = _u64(body, cursor)
                    cursor += 8
                    if value >= _MAX_INDEX:
                        return None
                out.append(value)
            return out[0], out[1], out[2]
        at += 4 + length
    return None


def _zip(view: _View, scope: _Scope) -> ContainerReport:
    kind, size = ContainerKind.ZIP, view.size
    whole = scope.cite(0, size)
    if not view.complete:
        scope.limit(
            "bytes",
            whole,
            f"a zip inside a compressed member is not opened: its directory lies at its end,"
            f" beyond the {size} decoded bytes examined",
            decoded=size,
        )
        return ContainerReport(kind, (), None, False)
    tail_length = min(size, _ZIP_TAIL)
    tail = view.read(size - tail_length, tail_length)
    eocd = _find_eocd(tail)
    if eocd is None:
        scope.corrupt(
            scope.cite(size - tail_length, tail_length),
            "zip: no end-of-central-directory record ends the file; it is truncated or not a zip",
        )
        return ContainerReport(kind, (), None, False)
    eocd_at = size - tail_length + eocd
    count, cd_size, cd_offset = _u16(tail, eocd + 10), _u32(tail, eocd + 12), _u32(tail, eocd + 16)
    if count == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        zip64 = _zip64_directory(view, eocd_at)
        if zip64 is None:
            scope.corrupt(
                scope.cite(eocd_at, size - eocd_at),
                "zip: the end record defers to a zip64 record that is missing or malformed",
            )
            return ContainerReport(kind, (), None, False)
        count, cd_size, cd_offset = zip64
    if cd_offset + cd_size > eocd_at:
        scope.corrupt(
            scope.cite(eocd_at, size - eocd_at),
            f"zip: the central directory ({cd_size} bytes at {cd_offset}) runs past its end record",
            directory_offset=cd_offset,
            directory_size=cd_size,
        )
        return ContainerReport(kind, (), None, False)

    budget = min(cd_size, scope.policy.scan_bytes)
    directory = view.read(cd_offset, budget)
    members: list[Member] = []
    complete = True
    at = 0

    def cut(index: int) -> None:
        if budget < cd_size:
            scope.limit(
                "bytes",
                whole,
                f"zip: the central directory holds {cd_size} bytes; {budget} were read"
                f" (scan_bytes), listing {index} of {count} members",
                directory_size=cd_size,
                listed=index,
                scan_bytes=scope.policy.scan_bytes,
            )
        else:
            scope.corrupt(
                scope.cite(cd_offset, cd_size),
                f"zip: central directory entry {index} is cut short",
                entry=index,
            )

    while at < len(directory):
        index = len(members)
        if index >= scope.policy.max_members:
            scope.limit(
                "members",
                whole,
                f"zip: {count} members declared; the first {index} are listed (max_members)",
                declared=count,
                listed=index,
                max_members=scope.policy.max_members,
            )
            complete = False
            break
        if directory[at : at + 4] != _CENTRAL:
            scope.corrupt(
                scope.cite(cd_offset + at, min(4, len(directory) - at)),
                f"zip: central directory entry {index} has a bad signature",
                entry=index,
            )
            complete = False
            break
        if at + 46 > len(directory):
            cut(index)
            complete = False
            break
        name_length, extra_length, comment_length = (
            _u16(directory, at + 28),
            _u16(directory, at + 30),
            _u16(directory, at + 32),
        )
        end = at + 46 + name_length + extra_length + comment_length
        if end > len(directory):
            cut(index)
            complete = False
            break
        sizes = (_u32(directory, at + 24), _u32(directory, at + 20), _u32(directory, at + 42))
        name = directory[at + 46 : at + 46 + name_length]
        if 0xFFFFFFFF in sizes:
            extra = directory[at + 46 + name_length : at + 46 + name_length + extra_length]
            zip64 = _zip64_fields(extra, sizes)
            if zip64 is None:
                scope.corrupt(
                    scope.cite(cd_offset + at, end - at),
                    f"zip: member {index} needs zip64 sizes its extra field does not hold",
                    member=index,
                )
                complete = False
                break
            sizes = zip64
        members.append(
            _zip_member(
                view,
                scope,
                index,
                name,
                flags=_u16(directory, at + 8),
                method=_u16(directory, at + 10),
                sizes=sizes,
                attributes=_u32(directory, at + 38),
            )
        )
        at = end
    if complete and len(members) < count:
        cut(len(members))
        complete = False
    return ContainerReport(kind, tuple(members), count, complete)


def _zip_member(
    view: _View,
    scope: _Scope,
    index: int,
    name: bytes,
    *,
    flags: int,
    method: int,
    sizes: tuple[int, int, int],
    attributes: int,
) -> Member:
    uncompressed, compressed, local_at = sizes
    mode = attributes >> 16
    if name.endswith(b"/") or stat.S_ISDIR(mode):
        kind = MemberKind.DIRECTORY
    elif stat.S_ISLNK(mode):
        kind = MemberKind.SYMLINK
    else:
        kind = MemberKind.FILE
    method_name = _ZIP_METHODS.get(method, f"method {method}")
    header = view.read(local_at, 30)
    member = Member(
        index,
        name,
        kind,
        scope.cite(min(local_at, view.size), 0),
        uncompressed,
        compressed,
        method_name,
    )
    if len(header) < 30 or header[:4] != _LOCAL:
        scope.corrupt(
            scope.cite(min(local_at, view.size), len(header)),
            f"zip: member {index}'s local header at {local_at} is missing or malformed",
            member=index,
            offset=local_at,
        )
        return member
    data_at = local_at + 30 + _u16(header, 26) + _u16(header, 28)
    if data_at + compressed > view.size:
        available = max(0, view.size - data_at)
        scope.corrupt(
            scope.cite(min(data_at, view.size), available),
            f"zip: member {index} declares {compressed} compressed bytes at {data_at};"
            f" {available} remain",
            available=available,
            member=index,
        )
        return replace(member, entry=scope.cite(min(data_at, view.size), available))
    member = replace(member, entry=scope.cite(data_at, compressed))
    if kind is not MemberKind.FILE:
        return member
    if flags & 1:
        scope.not_inspected(
            member.entry, f"zip: member {index} is encrypted; not decoded", member=index
        )
        return member
    if method == 0:
        stored = view.read(data_at, min(compressed, PROBE_HEAD_SIZE))
        return _finish(
            scope, member, stored, compressed, partial(_Window, view, data_at, compressed)
        )
    if method != 8:
        scope.not_inspected(
            member.entry,
            f"zip: member {index} uses compression method {method}, which is not decoded",
            member=index,
            method=method,
        )
        return member
    if uncompressed > scope.policy.max_ratio * max(compressed, 1):
        scope.limit(
            "ratio",
            member.entry,
            f"zip: member {index} declares {uncompressed} bytes from {compressed} compressed,"
            f" over max_ratio {scope.policy.max_ratio}; not decoded",
            compressed_size=compressed,
            max_ratio=scope.policy.max_ratio,
            member=index,
            size=uncompressed,
        )
        return member
    decoded = _Decoded(view, data_at, compressed, _Inflate(), scope.policy.scan_bytes)
    head, hint, nested = _decoded_head(scope, member, decoded, uncompressed)
    return _finish(scope, member, head, hint, nested)


# --- tar -----------------------------------------------------------------------------------------

_TAR_KINDS: Final = {
    b"0": MemberKind.FILE,
    b"\x00": MemberKind.FILE,
    b"7": MemberKind.FILE,  # contiguous file: read like a regular one
    b"5": MemberKind.DIRECTORY,
    b"2": MemberKind.SYMLINK,
    b"1": MemberKind.HARDLINK,
}
_BLOCK: Final = 512


def _tar_number(field: bytes) -> int | None:
    """A size field: octal text, or GNU base-256 when the top bit is set. ``None`` if neither."""
    if field and field[0] & 0x80:
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    text = field.split(b"\x00", 1)[0].strip(b" ")
    if not text:
        return 0
    try:
        return int(text, 8)
    except ValueError:
        return None


def _tar_checksum_ok(header: bytes) -> bool:
    declared = _tar_number(header[148:156])
    if declared is None:
        return False
    body = header[:148] + b" " * 8 + header[156:]
    unsigned = sum(body)
    signed = sum(b - 256 if b > 127 else b for b in body)
    return declared in (unsigned, signed)


def _cstring(field: bytes) -> bytes:
    return field.split(b"\x00", 1)[0]


def _pax(block: bytes) -> dict[bytes, bytes]:
    """``path``, ``linkpath`` and ``size`` records of a pax extended header, as written."""
    out: dict[bytes, bytes] = {}
    at = 0
    while at < len(block):
        space = block.find(b" ", at)
        if space < 0:
            break
        try:
            length = int(block[at:space])
        except ValueError:
            break
        record = block[at : at + length]
        if length <= 0 or not record.endswith(b"\n") or b"=" not in record:
            break
        key, _, value = record[space - at + 1 : -1].partition(b"=")
        if key in (b"path", b"linkpath", b"size"):
            out[key] = value
        at += length
    return out


def _blocks(size: int) -> int:
    """The bytes ``size`` bytes of member data occupy: whole 512-byte blocks."""
    return -(-size // _BLOCK) * _BLOCK


def _tar(view: _View, scope: _Scope) -> ContainerReport:
    kind = ContainerKind.TAR
    whole = scope.cite(0, view.size)
    members: list[Member] = []
    complete = True
    offset = 0
    long_name: bytes | None = None
    long_link: bytes | None = None
    pax: dict[bytes, bytes] = {}

    def budget_hit() -> None:
        scope.limit(
            "bytes",
            whole,
            f"tar: listing stopped at {view.size} decoded bytes (scan_bytes);"
            f" {len(members)} members listed",
            decoded=view.size,
            listed=len(members),
            scan_bytes=scope.policy.scan_bytes,
        )

    def cut_short(message: str) -> None:
        if view.complete:
            scope.corrupt(scope.cite(offset, view.size - offset), message, offset=offset)
        else:
            budget_hit()

    while True:
        index = len(members)
        if index >= scope.policy.max_members:
            scope.limit(
                "members",
                whole,
                f"tar: the first {index} members are listed (max_members); the archive continues",
                listed=index,
                max_members=scope.policy.max_members,
            )
            complete = False
            break
        header = view.read(offset, _BLOCK)
        if not header.rstrip(b"\x00"):
            if header and len(header) < _BLOCK and not view.complete:
                budget_hit()
                complete = False
            break  # end-of-archive blocks, or nothing left
        if len(header) < _BLOCK:
            cut_short(f"tar: the header at {offset} is cut short")
            complete = False
            break
        if header[257:262] != b"ustar":
            scope.corrupt(
                scope.cite(offset, _BLOCK),
                f"tar: the header at {offset} has no ustar magic; the listing stops",
                offset=offset,
            )
            complete = False
            break
        if not _tar_checksum_ok(header):
            scope.corrupt(
                scope.cite(offset, _BLOCK),
                f"tar: the header at {offset} fails its checksum; the listing stops",
                offset=offset,
            )
            complete = False
            break
        size = _tar_number(header[124:136])
        if size is None or size >= _MAX_INDEX:
            scope.corrupt(
                scope.cite(offset, _BLOCK),
                f"tar: the header at {offset} has an unreadable size; the listing stops",
                offset=offset,
            )
            complete = False
            break
        typeflag = header[156:157]
        data_at = offset + _BLOCK
        if typeflag in (b"L", b"K", b"x", b"g"):
            want = min(size, _HEADER_LIMIT)
            block = view.read(data_at, want)
            if len(block) < want:
                cut_short(f"tar: the extended header at {offset} is cut short")
                complete = False
                break
            if typeflag == b"L":
                long_name = _cstring(block)
            elif typeflag == b"K":
                long_link = _cstring(block)
            elif typeflag == b"x":
                pax = _pax(block)
            offset = data_at + _blocks(size)
            continue
        name = _cstring(header[:100])
        if header[257:265] == b"ustar\x0000":
            prefix = _cstring(header[345:500])
            if prefix:
                name = prefix + b"/" + name
        link = _cstring(header[157:257])
        if long_name is not None:
            name = long_name
        if long_link is not None:
            link = long_link
        if b"path" in pax:
            name = pax[b"path"]
        if b"linkpath" in pax:
            link = pax[b"linkpath"]
        if b"size" in pax:
            try:
                size = int(pax[b"size"])
            except ValueError:
                size = -1
            if not 0 <= size < _MAX_INDEX:
                scope.corrupt(
                    scope.cite(offset, _BLOCK),
                    f"tar: member {index}'s pax size is unreadable; the listing stops",
                    member=index,
                )
                complete = False
                break
        long_name = long_link = None
        pax = {}
        member_kind = _TAR_KINDS.get(typeflag, MemberKind.OTHER)
        target = link if member_kind in (MemberKind.SYMLINK, MemberKind.HARDLINK) else None
        available = max(0, view.size - data_at)
        if size > available and view.complete:
            scope.corrupt(
                scope.cite(min(data_at, view.size), available),
                f"tar: member {index} declares {size} bytes; {available} remain",
                available=available,
                member=index,
                size=size,
            )
            members.append(
                Member(index, name, member_kind, scope.cite(min(data_at, view.size), available))
            )
            complete = False
            break
        member = Member(
            index, name, member_kind, scope.cite(data_at, size), size, None, None, target
        )
        head: bytes | None = None
        if member_kind is MemberKind.FILE:
            want = min(size, PROBE_HEAD_SIZE)
            head = view.read(data_at, want)
            if len(head) < want:  # only a decoded prefix cut by its budget gets here
                members.append(member)
                budget_hit()
                complete = False
                break
        members.append(_finish(scope, member, head, size, partial(_Window, view, data_at, size)))
        offset = data_at + _blocks(size)
        if offset > view.size:
            if not view.complete:
                budget_hit()
                complete = False
            break

    return ContainerReport(kind, tuple(members), None, complete)


# --- gzip, bzip2, xz -----------------------------------------------------------------------------


def _gzip(view: _View, scope: _Scope) -> ContainerReport:
    kind, size = ContainerKind.GZIP, view.size
    whole = scope.cite(0, size)
    header = view.read(0, min(size, _HEADER_LIMIT))
    flags = header[3] if len(header) >= 10 else 0
    at = 10
    name = b""
    bad: str | None = None
    if len(header) < 10:
        bad = "gzip: the header is cut short"
    if bad is None and flags & 4:
        at += 2 + _u16(header, at)
    if bad is None and flags & 8:
        end = header.find(b"\x00", at)
        if end < 0:
            bad = "gzip: the stored name is not terminated within 64 KiB"
        else:
            name, at = header[at:end], end + 1
    if bad is None and flags & 16:
        end = header.find(b"\x00", at)
        if end < 0:
            bad = "gzip: the comment is not terminated within 64 KiB"
        else:
            at = end + 1
    if bad is None and flags & 2:
        at += 2
    if bad is None and at + 8 > size:
        bad = "gzip: no room for a deflate stream and the trailer after the header"
    if bad is not None:
        scope.corrupt(whole, bad)
        return ContainerReport(kind, (), None, False)
    compressed = size - at - 8
    declared = _u32(view.read(size - 8, 8), 4)  # the size modulo 2^32, as gzip stores it
    member = Member(0, name, MemberKind.FILE, whole, declared, compressed, "deflate")
    if declared > scope.policy.max_ratio * max(compressed, 1):
        scope.limit(
            "ratio",
            whole,
            f"gzip: the member declares {declared} bytes from {compressed} compressed, over"
            f" max_ratio {scope.policy.max_ratio}; not decoded",
            compressed_size=compressed,
            max_ratio=scope.policy.max_ratio,
            size=declared,
        )
        return ContainerReport(kind, (member,), 1, True)
    decoded = _Decoded(view, at, compressed, _Inflate(), scope.policy.scan_bytes)
    head, hint, nested = _decoded_head(scope, member, decoded, declared)
    return ContainerReport(kind, (_finish(scope, member, head, hint, nested),), 1, True)


def _stream(
    view: _View, scope: _Scope, kind: ContainerKind, decompressor: _Decompressor
) -> ContainerReport:
    """A single-member stream that states neither a name nor a size: bzip2, xz."""
    whole = scope.cite(0, view.size)
    member = Member(0, b"", MemberKind.FILE, whole, None, view.size, str(kind))
    decoded = _Decoded(view, 0, view.size, decompressor, scope.policy.scan_bytes)
    head, hint, nested = _decoded_head(scope, member, decoded, None)
    if head is not None and decoded.eof:
        member = replace(member, size=len(head))
    return ContainerReport(kind, (_finish(scope, member, head, hint, nested),), 1, True)
