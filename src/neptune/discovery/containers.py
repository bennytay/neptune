"""Bounded inspection of the containers sniffing finds: zip, tar, gzip, bzip2 and xz (ADR 0027).

What it does: lists members with their names, kinds and sizes exactly as the container states
them, cites each member's stored bytes, hands each regular member's head to the engine's prober
so the report says what the container holds, and opens a member that is itself a container down
to a fixed depth. Nothing is extracted, no name is resolved against a filesystem, and every read
is bounded by ``ProbePolicy``: members listed, decoded bytes per compressed stream, nesting depth,
and the declared compression ratio above which a member is not probed.

What it does not do: ingest members. An archive adapter would (none exists yet); this report is
for selection, explanation and the receipt. Problems are reported as findings, never raised: a
corrupt directory, a listing cut short by a limit, an encrypted or undecodable member are each
reported and the inspection carries on with what it can see.

Citations follow ADR 0016: a member's stored bytes are a ``ByteRange`` in its container's scope,
compressed if the member is compressed; a member of a nested container adds a step inside what
the engine decoded. Headers are metadata, not part of a citation; a gzip member is its whole
stream. A member's size the container does not state is hinted to adapters as the head's length
when the stream ended inside the head, and as one more than the head's length otherwise.

Streams laid end to end (gzip members from ``gzip -c >>``, bzip2 streams from pbzip2, xz streams)
are one member whose content is their concatenation, as the formats' own tools decode them. They
are decoded to the budget, since their boundaries lie beyond the head; each counts against
``max_members``, since a stream that decodes to nothing spends no budget; each gzip member's
stated size is checked against its own stream; null padding between or after them is skipped
however long; and the content counts as complete only when every stream was decoded whole.
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
      reported instead of probed. A zip member is not decoded at all. A gzip states its size only
      in a trailer that is one only if the stream ends there, so it is decoded to ``scan_bytes``
      first and held to the ratio only if it is still going at the budget.
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

    @property
    def unused_data(self) -> bytes:
        """Input past the end of the stream, once ``eof``."""
        ...


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

    @property
    def unused_data(self) -> bytes:
        return self._inflate.unused_data


@dataclass(frozen=True)
class _Codec:
    """How a kind of compressed stream is opened, and how one stream follows another.

    gzip members, bzip2 streams and xz streams may lie end to end (``gzip -c >> log.gz``, pbzip2,
    ``cat a.xz b.xz``); their tools decode the lot as one. ``magic`` opens each further stream;
    null bytes before it are padding, as gzip tolerates. A gzip member also has a header before
    and a trailer (CRC32, size modulo 2^32) after its deflate stream. A zip member is one deflate
    stream and nothing follows it.
    """

    open: Callable[[], _Decompressor]
    magic: bytes | None = None
    gzip: bool = False


_GZIP_TRAILER: Final = 8
_DEFLATE: Final = _Codec(_Inflate)
_CODECS: Final = {
    ContainerKind.GZIP: _Codec(_Inflate, b"\x1f\x8b\x08", gzip=True),
    ContainerKind.BZIP2: _Codec(bz2.BZ2Decompressor, b"BZh"),
    ContainerKind.XZ: _Codec(partial(lzma.LZMADecompressor, lzma.FORMAT_XZ), b"\xfd7zXZ\x00"),
}


def _exception_name(exc: BaseException) -> str:
    """``zlib.error``, ``OSError``: the class, qualified unless built in."""
    kind = type(exc)
    return kind.__name__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__name__}"


class _Decoded:
    """Up to ``scan_bytes`` decoded bytes of the compressed range ``[offset, offset + length)``.

    Bytes are produced on demand and kept, so the head can be taken first and the rest only if
    the head turns out to be a container. Streams laid end to end are decoded as one, the way the
    formats' own tools do, and each counts against ``max_members``: a stream that decodes to
    nothing spends no budget, so without that limit a file of them would be walked to its end.
    Afterwards exactly one of these holds, or the decode is paused at the budget: ``ended``
    (every stream was decoded whole and the input is spent, bar padding or ``trailing`` bytes
    that open no stream), ``cut`` (the input ran out inside a stream, a trailer or a header),
    ``error`` (the exception a corrupt stream raised), ``limited`` (``max_members`` streams were
    decoded whole and another follows, unread). For gzip, ``sizes`` pairs each whole member's
    stated size with the length its stream held.
    """

    def __init__(
        self, view: _View, offset: int, length: int, codec: _Codec, policy: ProbePolicy
    ) -> None:
        self._view, self._pos, self._end = view, offset, min(offset + length, view.size)
        self._codec, self._budget, self._max_streams = codec, policy.scan_bytes, policy.max_members
        self._stream: _Decompressor | None = codec.open()
        self._stream_have = 0  # decoded bytes of the stream now open
        self._pieces: list[bytes] = []
        self._have = 0
        self.error: str | None = None
        self.streams = 1  # streams opened so far
        self.ended = False
        self.cut = False
        self.limited = False
        self.sizes: list[tuple[int, int]] = []  # gzip: (stated, decoded) per member decoded whole
        self.trailing: tuple[int, int] | None = None  # (offset, length) in the view

    def prefix(self, want: int) -> bytes:
        """The first ``min(want, budget)`` decoded bytes, or fewer if the streams or input end."""
        want = min(want, self._budget)
        while self._stream is not None and self.error is None:
            if self._stream.eof:
                self._advance()
                continue
            if self._have >= want:
                break
            piece = b""
            if self._stream.needs_input and self._pos < self._end:
                piece = self._view.read(self._pos, min(_READ, self._end - self._pos))
                self._pos += len(piece)
            try:
                out = self._stream.decompress(piece, want - self._have)
            except (zlib.error, OSError, EOFError, lzma.LZMAError, ValueError) as exc:
                self.error = _exception_name(exc)
                break
            self._pieces.append(out)
            self._have += len(out)
            self._stream_have += len(out)
            if not out and not piece and not self._stream.eof:
                self._stop(cut=True)  # the input is spent inside the stream
        if len(self._pieces) > 1:
            self._pieces = [b"".join(self._pieces)]
        return self._pieces[0][:want] if self._pieces else b""

    @property
    def size(self) -> int:
        """Decoded bytes so far."""
        return self._have

    def _stop(self, *, cut: bool = False, ended: bool = False) -> None:
        self._stream = None
        self.cut, self.ended = cut, ended

    def _advance(self) -> None:
        """A stream ended: take its trailer, then open the stream that follows or finish."""
        assert self._stream is not None
        at = self._pos - len(self._stream.unused_data)  # where the stream's compressed bytes end
        codec = self._codec
        if codec.gzip:
            if at + _GZIP_TRAILER > self._end:
                self._stop(cut=True)
                return
            trailer = self._view.read(at, _GZIP_TRAILER)
            self.sizes.append((_u32(trailer, 4), self._stream_have))
            at += _GZIP_TRAILER
        if codec.magic is None:
            self._stop(ended=True)
            return
        rest = self._view.read(at, min(_HEADER_LIMIT, self._end - at))
        while rest[:1] == b"\x00":  # null padding, however long: skip it, as gzip does
            at += len(rest) - len(rest.lstrip(b"\x00"))
            rest = self._view.read(at, min(_HEADER_LIMIT, self._end - at))
        if not rest:
            self._stop(ended=True)
            return
        if not rest.startswith(codec.magic):
            self.trailing = (at, self._end - at)
            self._stop(ended=True)
            return
        if self.streams >= self._max_streams:
            self._stream = None  # another stream follows, unread: neither ended nor cut
            self.limited = True
            return
        if codec.gzip:
            parsed = _gzip_header(rest)
            if isinstance(parsed, str) or at + parsed[0] + _GZIP_TRAILER > self._end:
                self._stop(cut=True)  # a header cut short, or one that leaves no room for a stream
                return
            at += parsed[0]
        self._pos = at
        self._stream = codec.open()
        self._stream_have = 0
        self.streams += 1


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
        case ContainerKind.BZIP2 | ContainerKind.XZ:
            return _stream(view, scope, kind)
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
    scope: _Scope,
    member: Member,
    decoded: _Decoded,
    declared: int | None,
    *,
    compare: bool = True,
) -> tuple[bytes | None, int | None, Callable[[], _View]]:
    """The head of a compressed member, or ``None`` with a finding if it cannot be decoded whole.

    Returns the head, the size to hint to adapters, and how to view the decoded bytes. The head is
    decoded to ``PROBE_HEAD_SIZE`` whatever the container declares, so the hint is what the stream
    holds: its length when the stream ended or was cut, the declared size when that is at least a
    head, else "more than a head". A declared size the stream contradicts is a finding and the
    member is still probed, unless ``compare`` is off (gzip checks each member's own statement).
    The view is complete once the engine has seen all the stream will give: ended or cut, not
    paused at the budget.
    """
    head = decoded.prefix(PROBE_HEAD_SIZE)

    def view() -> _View:
        data = decoded.prefix(scope.policy.scan_bytes)
        return _Prefix(data, decoded.ended or decoded.cut)

    if decoded.error is not None:
        scope.corrupt(
            member.entry,
            f"member {member.index}: the {member.method} stream is corrupt ({decoded.error});"
            " not probed",
            error=decoded.error,
            member=member.index,
        )
        return None, None, view
    if decoded.cut:
        short = len(head) < PROBE_HEAD_SIZE
        scope.corrupt(
            member.entry,
            f"member {member.index}: the {member.method} stream ends after {decoded.size} decoded"
            f" bytes, before its end marker" + ("; not probed" if short else ""),
            decoded=decoded.size,
            member=member.index,
        )
        return (None, None, view) if short else (head, decoded.size, view)
    if decoded.ended:
        actual = decoded.size
        if compare and declared is not None and actual != declared:
            scope.corrupt(
                member.entry,
                f"member {member.index} declares {declared} decoded bytes; its stream holds"
                f" {actual}",
                declared=declared,
                decoded=actual,
                member=member.index,
            )
        return head, actual, view
    # Paused at the budget, which is at least a head, or stopped at the stream limit, which
    # _stream_findings reported: the head is whole unless that limit came first.
    if len(head) < PROBE_HEAD_SIZE:
        return None, None, view
    if declared is not None and declared >= PROBE_HEAD_SIZE:
        return head, declared, view
    if compare and declared is not None:
        scope.corrupt(
            member.entry,
            f"member {member.index} declares {declared} decoded bytes; its stream holds more"
            f" than {PROBE_HEAD_SIZE}",
            declared=declared,
            member=member.index,
        )
    return head, PROBE_HEAD_SIZE + 1, view


def _unopened(view: _View, scope: _Scope, kind: ContainerKind) -> ContainerReport:
    """A stream container inside a compressed member cut by the budget: not opened, said so."""
    scope.limit(
        "bytes",
        scope.cite(0, view.size),
        f"a {kind} stream inside a compressed member is not opened: only its first {view.size}"
        f" decoded bytes were examined (scan_bytes)",
        container=str(kind),
        decoded=view.size,
        scan_bytes=scope.policy.scan_bytes,
    )
    return ContainerReport(kind, (), None, False)


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
        if uncompressed != compressed:
            scope.corrupt(
                member.entry,
                f"zip: stored member {index} declares {uncompressed} bytes but holds {compressed}",
                compressed_size=compressed,
                member=index,
                size=uncompressed,
            )
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
    decoded = _Decoded(view, data_at, compressed, _DEFLATE, scope.policy)
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
    headers = 0
    # Extended headers (long names, pax) precede a member, normally one or two of them; a flood
    # of them must not get around max_members, so every header read counts against this.
    header_cap = 3 * scope.policy.max_members

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
            if len(header) < _BLOCK and not view.complete:
                budget_hit()  # the budget ended here or in a block; nothing says the archive did
                complete = False
            break  # end-of-archive blocks, or nothing left
        if len(header) < _BLOCK:
            cut_short(f"tar: the header at {offset} is cut short")
            complete = False
            break
        headers += 1
        if headers > header_cap:
            scope.limit(
                "members",
                whole,
                f"tar: {headers} headers read for {index} members; the listing stops"
                f" (max_members {scope.policy.max_members})",
                headers=headers,
                listed=index,
                max_members=scope.policy.max_members,
            )
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
            available = max(0, view.size - data_at)
            if size > available:
                cut_short(
                    f"tar: the extended header at {offset} declares {size} bytes;"
                    f" {available} remain"
                )
                complete = False
                break
            block = view.read(data_at, min(size, _HEADER_LIMIT))
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
        if offset >= view.size:  # the next header, if any, lies at or beyond the bytes examined
            if not view.complete:
                budget_hit()
                complete = False
            break

    return ContainerReport(kind, tuple(members), None, complete)


# --- gzip, bzip2, xz -----------------------------------------------------------------------------


def _gzip_header(header: bytes) -> tuple[int, bytes] | str:
    """The length of the gzip member header opening ``header`` and the name it stores, or what is
    wrong with it. ``header`` is the member's first bytes, at most ``_HEADER_LIMIT`` of them."""
    if len(header) < 10:
        return "the header is cut short"
    flags, at, name = header[3], 10, b""
    if flags & 4:
        at += 2 + _u16(header, at)
    if flags & 8:
        end = header.find(b"\x00", at)
        if end < 0:
            return "the stored name is not terminated within 64 KiB"
        name, at = header[at:end], end + 1
    if flags & 16:
        end = header.find(b"\x00", at)
        if end < 0:
            return "the comment is not terminated within 64 KiB"
        at = end + 1
    if flags & 2:
        at += 2
    return at, name


def _stream_findings(scope: _Scope, member: Member, kind: ContainerKind, decoded: _Decoded) -> None:
    """What decoding a stream member's streams showed: sizes gzip members state, trailing bytes,
    and the stream limit."""
    if decoded.limited:
        scope.limit(
            "members",
            member.entry,
            f"member {member.index}: {decoded.streams} {kind} streams laid end to end were decoded;"
            f" the rest are not (max_members {scope.policy.max_members})",
            max_members=scope.policy.max_members,
            member=member.index,
            streams=decoded.streams,
        )
    for stream, (stated, actual) in enumerate(decoded.sizes):
        if stated != actual % (1 << 32):  # gzip stores a member's size modulo 2^32
            scope.corrupt(
                member.entry,
                f"member {member.index}: stream {stream} declares {stated} decoded bytes; it holds"
                f" {actual}",
                declared=stated,
                decoded=actual,
                member=member.index,
                stream=stream,
            )
    if decoded.trailing is not None:
        at, length = decoded.trailing
        scope.corrupt(
            scope.cite(at, length),
            f"member {member.index}: {length} bytes after stream {decoded.streams - 1} open no"
            f" {kind} stream; not decoded",
            length=length,
            member=member.index,
            offset=at,
        )


def _gzip(view: _View, scope: _Scope) -> ContainerReport:
    """One member: the concatenation of every gzip member in the stream, as ``gzip -d`` yields.

    The member's ``size`` is what the stream states: the sum of every member's stated size once
    all were reached; the trailer's statement (the last member's, so the whole for the usual
    single member) while the first member is still being decoded; nothing when more than one
    member was seen but not the end, or the input was cut, since the total is stated nowhere.
    ``compressed_size`` is the bytes between the first header and the last trailer.

    The trailer is the file's last eight bytes only if the stream ends there; in a file cut short
    they are deflate data. So the stream is decoded to the budget before the trailer is believed:
    an end, a cut or a corrupt stream settles what those bytes are, and only a stream still
    going at the budget is held to ``max_ratio`` by what its trailer states.
    """
    kind, size = ContainerKind.GZIP, view.size
    if not view.complete:
        return _unopened(view, scope, kind)  # the trailer, and so the sizes, lie beyond the budget
    whole = scope.cite(0, size)
    parsed = _gzip_header(view.read(0, min(size, _HEADER_LIMIT)))
    if isinstance(parsed, str):
        scope.corrupt(whole, f"gzip: {parsed}")
        return ContainerReport(kind, (), None, False)
    at, name = parsed
    if at + _GZIP_TRAILER > size:
        scope.corrupt(whole, "gzip: no room for a deflate stream and the trailer after the header")
        return ContainerReport(kind, (), None, False)
    compressed = size - at - _GZIP_TRAILER
    stated = _u32(view.read(size - _GZIP_TRAILER, _GZIP_TRAILER), 4)  # modulo 2^32, as stored
    member = Member(0, name, MemberKind.FILE, whole, stated, compressed, "deflate")
    decoded = _Decoded(view, at, size - at, _CODECS[kind], scope.policy)
    decoded.prefix(scope.policy.scan_bytes)  # to the budget: member boundaries lie beyond the head
    _stream_findings(scope, member, kind, decoded)
    if decoded.ended:
        member = replace(member, size=sum(stated for stated, _ in decoded.sizes))
    elif decoded.cut or decoded.streams > 1:
        member = replace(member, size=None)
    paused = not (decoded.ended or decoded.cut or decoded.error is not None)
    if paused and stated > scope.policy.max_ratio * max(compressed, 1):
        scope.limit(
            "ratio",
            whole,
            f"gzip: the trailer declares {stated} bytes from {compressed} compressed, over"
            f" max_ratio {scope.policy.max_ratio}, and the stream runs past the"
            f" {decoded.size} decoded bytes examined; not probed",
            compressed_size=compressed,
            decoded=decoded.size,
            max_ratio=scope.policy.max_ratio,
            size=stated,
        )
        return ContainerReport(kind, (member,), 1, True)
    head, hint, nested = _decoded_head(scope, member, decoded, member.size, compare=False)
    return ContainerReport(kind, (_finish(scope, member, head, hint, nested),), 1, True)


def _stream(view: _View, scope: _Scope, kind: ContainerKind) -> ContainerReport:
    """One member, the concatenation of every bzip2 or xz stream; neither names nor sizes it.

    Its ``size`` is the decoded length once every stream was decoded whole within the budget.
    """
    if not view.complete:
        return _unopened(view, scope, kind)  # a cut stream would read as corrupt, which it is not
    whole = scope.cite(0, view.size)
    member = Member(0, b"", MemberKind.FILE, whole, None, view.size, str(kind))
    decoded = _Decoded(view, 0, view.size, _CODECS[kind], scope.policy)
    decoded.prefix(scope.policy.scan_bytes)  # to the budget: the size is known only at the end
    _stream_findings(scope, member, kind, decoded)
    if decoded.ended:
        member = replace(member, size=decoded.size)
    head, hint, nested = _decoded_head(scope, member, decoded, None)
    return ContainerReport(kind, (_finish(scope, member, head, hint, nested),), 1, True)
