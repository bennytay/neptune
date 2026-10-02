"""A workbook as a bounded zip of XML parts: opening it, reading a part, and the small parts.

An XLSX is an OOXML package: a zip whose parts are XML (ECMA-376 Part 2, 3). Everything here is
standard library and nothing is ever extracted: ``zipfile`` reads the directory after the same
bounds ``neptune.discovery.archive`` puts on an archive (ADR 0029 §2), a part is inflated in
pieces and counted as it goes (a declared size can lie), and XML is read by expat with the
protections done by hand: a document type declaration (so every entity, internal or external) is
refused, only UTF-8 is read, and nothing named by a part is ever opened.

Nothing in this module raises for hostile input: damage is a ``Problem``, which the caller turns
into a finding. A ``ShortReadError`` is not damage and passes through (ADR 0033 §3).
"""

import io
import posixpath
import re
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import IO, Final
from urllib.parse import unquote
from xml.parsers import expat

from neptune.adapters.contract import AdapterConfig, ShortReadError, SourceReader
from neptune.model.jsonvalue import JsonObject
from neptune.model.provenance import ByteRange, Locator

# The main namespaces of SpreadsheetML: ECMA-376 transitional and ISO 29500 strict.
MAIN_NS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "http://purl.oclc.org/ooxml/spreadsheetml/main",
    }
)
PIECE: Final = 64 * 1024
# What a sheet may hold before its first row, and how deep an XML part may nest: constants of the
# adapter's version (a different bound is a different version, never a setting).
MAX_PROLOGUE: Final = 1024 * 1024
MAX_DEPTH: Final = 64
_UTF16: Final = (b"\xff\xfe", b"\xfe\xff", b"<\x00", b"\x00<")
_ZIP_FLAG_ENCRYPTED: Final = 0x1
# The end of the tag that starts at an offset: quoted attribute values may hold '>'.
_TAG: Final = re.compile(rb"""(?:[^>"']++|"[^"]*+"|'[^']*+')*+>""")


@dataclass(frozen=True)
class XlsxLimits:
    """The settings that bound what a workbook, a part, a sheet or a table may cost."""

    max_cells: int
    max_compression_ratio: int
    max_gap_ratio: int
    max_part_bytes: int
    max_parts: int
    max_shared_string_bytes: int
    max_shared_strings: int
    max_sheets: int
    max_styles: int
    max_total_bytes: int

    @staticmethod
    def of(config: AdapterConfig) -> "XlsxLimits":
        return XlsxLimits(
            max_cells=config.integer("xlsx_max_cells"),
            max_compression_ratio=config.integer("xlsx_max_compression_ratio"),
            max_gap_ratio=config.integer("xlsx_max_gap_ratio"),
            max_part_bytes=config.integer("xlsx_max_part_bytes"),
            max_parts=config.integer("xlsx_max_parts"),
            max_shared_string_bytes=config.integer("xlsx_max_shared_string_bytes"),
            max_shared_strings=config.integer("xlsx_max_shared_strings"),
            max_sheets=config.integer("xlsx_max_sheets"),
            max_styles=config.integer("xlsx_max_styles"),
            max_total_bytes=config.integer("xlsx_max_total_bytes"),
        )


class Problem(Exception):
    """Damage or a limit: ``code`` is a finding name, ``scope`` the steps to the bytes it cites
    (nothing means the whole source)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: JsonObject | None = None,
        scope: tuple[Locator, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: JsonObject = details or {}
        self.scope = scope


def error_code(exc: BaseException) -> str:
    """A defect's stable class, never the library's message (which varies with versions)."""
    for kind, code in (
        (EOFError, "end_of_data"),
        (zipfile.BadZipFile, "bad_zip"),
        (zlib.error, "bad_deflate"),
        (NotImplementedError, "unsupported"),
        (UnicodeError, "bad_encoding"),
        (ValueError, "bad_value"),
        (LookupError, "bad_index"),
        (OSError, "os_error"),
    ):
        if isinstance(exc, kind):
            return code
    return "other"


class _SourceFile(io.RawIOBase):
    """A read-only, seekable file over a source, for ``zipfile``."""

    def __init__(self, source: SourceReader) -> None:
        self._source = source
        self._at = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._at

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._at, io.SEEK_END: self._source.size}[whence]
        self._at = max(0, base + offset)
        return self._at

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        size = self._source.size
        if self._at >= size:
            return 0
        data = self._source.read(self._at, min(len(buffer), size - self._at))
        if not data:
            raise ShortReadError(self._source.content_id, self._at, size - self._at)
        buffer[: len(data)] = data
        self._at += len(data)
        return len(data)


@dataclass(frozen=True)
class _Directory:
    entries: int  # as the end record declares them
    start: int  # where the central directory starts: its end record's place less its size
    size: int


_END: Final = b"PK\x05\x06"
_END64: Final = b"PK\x06\x06"
_LOCATOR64: Final = b"PK\x06\x07"
_CENTRAL: Final = b"PK\x01\x02"
_CENTRAL_FIXED: Final = 46
_DIRECTORY_BYTES_PER_PART: Final = 1024
_MIN_DIRECTORY_CAP: Final = 1024 * 1024


def _directory(stream: IO[bytes], size: int) -> _Directory | None:
    """The central directory as the end records (zip64 included) declare it, or ``None`` if the
    file has no end record. Only the end records are read."""
    tail_at = max(0, size - (22 + 65535))
    stream.seek(tail_at)
    tail = stream.read(size - tail_at)
    at = tail.rfind(_END)
    if at < 0 or at + 22 > len(tail):
        return None
    entries, length, offset = struct.unpack_from("<HII", tail, at + 10)
    end = tail_at + at
    wide = entries == 0xFFFF or length == 0xFFFFFFFF or offset == 0xFFFFFFFF
    if wide and at >= 20 and tail[at - 20 : at - 16] == _LOCATOR64:
        (record_at,) = struct.unpack_from("<Q", tail, at - 12)
        stream.seek(record_at)
        record = stream.read(56)
        if len(record) == 56 and record.startswith(_END64):
            entries, length = struct.unpack_from("<QQ", record, 32)
            end = record_at
    return _Directory(entries, end - length, length)


def _walk(stream: IO[bytes], directory: _Directory, stop: int) -> int:
    """How many entries the directory holds, counting no further than ``stop``: each fixed part is
    read and its name, extra field and comment are seeked over, so nothing is held. The end
    record's count can lie low, and ``zipfile`` builds every entry it finds."""
    stream.seek(directory.start)
    count = walked = 0
    while walked < directory.size and count < stop:
        fixed = stream.read(_CENTRAL_FIXED)
        if len(fixed) < _CENTRAL_FIXED or not fixed.startswith(_CENTRAL):
            break  # zipfile refuses the directory here, and its finding says so
        variable = sum(struct.unpack_from("<3H", fixed, 28))
        stream.seek(variable, io.SEEK_CUR)
        walked += _CENTRAL_FIXED + variable
        count += 1
    return count


class GapBudget:
    """How many blank cells a row may make for its real cells (``xlsx_max_gap_ratio``).

    A row's cells are by column, so a real cell far to the right of the others is a run of blank
    cells before it. The blanks a row makes up to and including the gap before a real cell may be
    at most ``ratio`` times the real cells kept so far plus one (the whole row's, not each gap's,
    so the allowance does not compound): a row makes at most ``(ratio + 1) * real + ratio`` cells.
    The first real cell that would exceed it, and every one after it, is not covered. ``add``
    takes the columns in order and says whether the cell is kept; ``width`` is the cells the row
    makes (its blanks, its kept cells, and one not-covered cell where the rest was cut). The scan
    in ``plan`` and the reader in ``ingest`` both use it, so a block's cell count is exact.
    """

    def __init__(self, ratio: int) -> None:
        self.ratio = ratio
        self.real = 0
        self.next = 0
        self.cut = False

    def add(self, column: int) -> bool:
        if self.cut or column - self.real > self.ratio * (self.real + 1):
            self.cut = True
            return False
        self.real += 1
        self.next = column + 1
        return True

    @property
    def width(self) -> int:
        return self.next + (1 if self.cut else 0)


def small_int(text: str | None, digits: int = 9) -> int | None:
    """A non-negative integer an attribute states in at most ``digits`` ASCII digits, else
    ``None``: hostile text never reaches ``int`` unbounded."""
    if text is not None and text.isascii() and text.isdecimal() and len(text) <= digits:
        return int(text)
    return None


def resolve(base: str, target: str) -> str | None:
    """The part name a relationship ``target`` of part ``base`` names, or ``None`` if it leaves the
    package. Only the name is computed; nothing is opened."""
    target = unquote(target)
    path = target[1:] if target.startswith("/") else posixpath.join(posixpath.dirname(base), target)
    path = posixpath.normpath(path)
    if not path or path == ".." or path.startswith(("../", "/")):
        return None
    return path


class Package:
    """An open workbook: its parts by name, each with the bytes it occupies in the source."""

    def __init__(
        self,
        source: SourceReader,
        limits: XlsxLimits,
        archive: zipfile.ZipFile,
        infos: dict[str, zipfile.ZipInfo],
        spans: dict[str, ByteRange],
    ) -> None:
        self.source = source
        self.limits = limits
        self.names = tuple(infos)
        self._archive = archive
        self._infos = infos
        self._spans = spans

    @staticmethod
    def open(source: SourceReader, limits: XlsxLimits) -> "Package":
        """Open ``source`` as a zip within ``limits``; a ``Problem`` says why not."""
        size = source.size
        stream = io.BufferedReader(_SourceFile(source))
        try:
            directory = _directory(stream, size)
        except ShortReadError:
            raise
        except Exception as exc:
            raise Problem(
                "xlsx_corrupt",
                "the zip's end-of-central-directory records cannot be read",
                {"error": error_code(exc), "size": size},
            ) from exc
        if directory is None:
            raise Problem(
                "xlsx_corrupt",
                "the workbook has no zip end-of-central-directory record: it is truncated or not"
                " a zip",
                {"error": "no_end_record", "size": size},
            )
        if directory.start < 0:
            raise Problem(
                "xlsx_corrupt",
                f"the zip's central directory declares {directory.size} bytes, more than precede"
                " its end record",
                {"error": "bad_directory", "size": size},
            )
        cap = max(_MIN_DIRECTORY_CAP, limits.max_parts * _DIRECTORY_BYTES_PER_PART)
        if directory.entries > limits.max_parts or directory.size > cap:
            raise Problem(
                "xlsx_limit",
                f"the zip declares {directory.entries} parts in a directory of {directory.size}"
                f" bytes, over xlsx_max_parts ({limits.max_parts}); not read",
                {"limit": "xlsx_max_parts", "parts": directory.entries, "max": limits.max_parts},
            )
        try:
            walked = _walk(stream, directory, limits.max_parts + 1)
        except ShortReadError:
            raise
        except Exception as exc:
            raise Problem(
                "xlsx_corrupt",
                "the zip's central directory cannot be walked",
                {"error": error_code(exc), "size": size},
            ) from exc
        if walked > limits.max_parts:
            raise Problem(
                "xlsx_limit",
                f"the zip's directory holds more than xlsx_max_parts ({limits.max_parts}) parts;"
                " not read",
                {"limit": "xlsx_max_parts", "max": limits.max_parts},
            )
        try:
            stream.seek(0)
            archive = zipfile.ZipFile(stream)
            listed = archive.infolist()
        except ShortReadError:
            raise
        except Exception as exc:
            raise Problem(
                "xlsx_corrupt",
                "the zip's central directory cannot be read",
                {"error": error_code(exc), "size": size},
            ) from exc
        declared = sum(info.file_size for info in listed)
        if declared > limits.max_total_bytes:
            raise Problem(
                "xlsx_limit",
                f"the parts declare {declared} bytes uncompressed, over xlsx_max_total_bytes"
                f" ({limits.max_total_bytes}); not read",
                {"limit": "xlsx_max_total_bytes", "declared": declared},
            )
        if declared > limits.max_compression_ratio * max(size, 1):
            raise Problem(
                "xlsx_limit",
                f"the parts declare {declared} bytes uncompressed from {size} in the file, over"
                f" {limits.max_compression_ratio}:1; not read",
                {
                    "limit": "xlsx_max_compression_ratio",
                    "declared": declared,
                    "size": size,
                    "max": limits.max_compression_ratio,
                },
            )
        ordered = sorted(listed, key=lambda info: info.header_offset)
        infos: dict[str, zipfile.ZipInfo] = {}
        spans: dict[str, ByteRange] = {}
        for index, info in enumerate(ordered):
            end = (
                ordered[index + 1].header_offset if index + 1 < len(ordered) else archive.start_dir
            )
            end = max(min(end, size), min(info.header_offset, size))
            start = min(info.header_offset, size)
            infos[info.filename] = info
            spans[info.filename] = ByteRange(start, end - start)
        return Package(source, limits, archive, infos, spans)

    def has(self, name: str) -> bool:
        return name in self._infos

    def span(self, name: str) -> ByteRange:
        """The bytes part ``name`` occupies in the source: its header, data and descriptor."""
        return self._spans[name]

    def pieces(self, name: str, cap: int | None = None) -> Iterator[bytes]:
        """The inflated bytes of part ``name`` in order, at most ``PIECE`` at a time.

        The declared size and ratio are checked before a byte is inflated and the bytes are
        counted as they come, because a declared size can lie. A ``Problem`` says what stopped it.
        """
        limits, info = self.limits, self._infos[name]
        cap = limits.max_part_bytes if cap is None else cap
        scope = (self._spans[name],)
        if info.flag_bits & _ZIP_FLAG_ENCRYPTED:
            raise Problem(
                "xlsx_part_refused",
                f"part {name!r} is encrypted; it is not read",
                {"part": name, "reason": "encrypted"},
                scope,
            )
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise Problem(
                "xlsx_part_refused",
                f"part {name!r} uses compression method {info.compress_type}; it is not read",
                {"part": name, "reason": "compression", "method": info.compress_type},
                scope,
            )
        if info.file_size > cap:
            raise Problem(
                "xlsx_limit",
                f"part {name!r} declares {info.file_size} bytes, over xlsx_max_part_bytes"
                f" ({cap}); it is not read",
                {"limit": "xlsx_max_part_bytes", "part": name, "declared": info.file_size},
                scope,
            )
        if info.file_size > limits.max_compression_ratio * max(info.compress_size, 1):
            raise Problem(
                "xlsx_limit",
                f"part {name!r} declares {info.file_size} bytes from {info.compress_size}, over"
                f" {limits.max_compression_ratio}:1; it is not read",
                {
                    "limit": "xlsx_max_compression_ratio",
                    "part": name,
                    "declared": info.file_size,
                    "compressed": info.compress_size,
                },
                scope,
            )
        total = 0
        try:
            handle = self._archive.open(info)
        except ShortReadError:
            raise
        except Exception as exc:
            raise Problem(
                "xlsx_corrupt",
                f"part {name!r} cannot be opened",
                {"part": name, "error": error_code(exc)},
                scope,
            ) from exc
        with handle:
            while True:
                try:
                    piece = handle.read(PIECE)
                except ShortReadError:
                    raise
                except Exception as exc:
                    raise Problem(
                        "xlsx_corrupt",
                        f"part {name!r} is damaged {total} bytes in: what precedes is read",
                        {"part": name, "error": error_code(exc), "read": total},
                        scope,
                    ) from exc
                if not piece:
                    return
                total += len(piece)
                if total > cap:
                    raise Problem(
                        "xlsx_limit",
                        f"part {name!r} inflates to more than xlsx_max_part_bytes ({cap});"
                        f" reading stopped at {total}",
                        {"limit": "xlsx_max_part_bytes", "part": name, "read": total},
                        scope,
                    )
                yield piece


# --- XML ---------------------------------------------------------------------------------------


class Stop(Exception):
    """Raised by a handler to end the parse early."""


class Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _refuse_doctype(*_: object) -> None:
    raise Refused("doctype")


def new_parser() -> expat.XMLParserType:
    """An expat parser that reads names with their namespaces (``"<uri> <local>"``) and refuses
    any document type declaration, so no entity is ever declared, expanded or fetched."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = False
    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.EntityDeclHandler = _refuse_doctype
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    # Handlers must see every complete token as it is fed, not when more bytes arrive.
    deferral: Callable[[bool], None] | None = getattr(parser, "SetReparseDeferralEnabled", None)
    if deferral is not None:
        deferral(False)
    return parser


def guarded(
    package: Package,
    name: str,
    parser: expat.XMLParserType,
    start: Callable[[str, dict[str, str]], None],
) -> None:
    """Install ``start`` as the parser's element handler with a nesting bound: a part nested past
    ``MAX_DEPTH`` is a limit ``Problem`` at that element, before expat holds any more of it."""
    depth = [0]

    def opened(tag: str, attrs: dict[str, str]) -> None:
        depth[0] += 1
        if depth[0] > MAX_DEPTH:
            raise Problem(
                "xlsx_limit",
                f"part {name!r} nests deeper than {MAX_DEPTH} elements; it is not read further",
                {"limit": "depth", "part": name, "max": MAX_DEPTH},
                (package.span(name),),
            )
        start(tag, attrs)

    def closed(tag: str) -> None:
        depth[0] -= 1

    parser.StartElementHandler = opened
    parser.EndElementHandler = closed


def local(name: str) -> str | None:
    """The local name of a SpreadsheetML element, or ``None`` for any other namespace."""
    namespace, _, tail = name.rpartition(" ")
    return tail if namespace in MAIN_NS else None


class Window:
    """The last two pieces fed to a parser, so the end of a tag whose start was in the piece
    before can still be found. One ``push`` of a whole document makes it a plain buffer."""

    def __init__(self, keep: int = PIECE) -> None:
        self._keep = keep
        self.buffer = b""
        self.base = 0

    def push(self, piece: bytes) -> None:
        kept = self.buffer[-self._keep :] if len(self.buffer) > self._keep else self.buffer
        self.base += len(self.buffer) - len(kept)
        self.buffer = kept + piece

    def tag_end(self, at: int) -> int | None:
        """The offset just past the ``>`` of the tag starting at ``at``, or ``None`` if the tag is
        not wholly in the window (a tag longer than a piece)."""
        if at < self.base:
            return None
        found = _TAG.match(self.buffer, at - self.base)
        return None if found is None else self.base + found.end()

    def is_empty_tag(self, at: int, end: int) -> bool:
        """Whether the tag at ``[at, end)`` is a self-closing ``<x/>``."""
        return end - at >= 2 and self.buffer[end - self.base - 2 : end - self.base] == b"/>"


def drive(
    package: Package, name: str, parser: expat.XMLParserType, window: Window | None = None
) -> None:
    """Feed part ``name`` to ``parser``. What the handlers kept before a ``Problem`` is theirs.

    ``Stop`` from a handler ends the parse quietly. Anything wrong with the part is a ``Problem``
    citing the part's bytes: not UTF-8, a document type declaration, XML that is not well formed.
    """
    scope = (package.span(name),)
    first = True
    try:
        for piece in package.pieces(name):
            if first:
                first = False
                if piece.startswith(_UTF16):
                    raise Problem(
                        "xlsx_part_refused",
                        f"part {name!r} is not UTF-8 (a UTF-16 byte-order mark or null bytes);"
                        " it is not read",
                        {"part": name, "reason": "encoding"},
                        scope,
                    )
            if window is not None:
                window.push(piece)
            parser.Parse(piece, False)
        parser.Parse(b"", True)
    except Stop:
        return
    except Refused as refused:
        raise Problem(
            "xlsx_part_refused",
            f"part {name!r} declares a document type; it is not read (entities are never expanded)",
            {"part": name, "reason": refused.reason},
            scope,
        ) from refused
    except expat.ExpatError as exc:
        raise Problem(
            "xlsx_corrupt",
            f"part {name!r} is not well formed XML {parser.ErrorByteIndex} bytes in: what"
            " precedes is read",
            {"part": name, "error": "bad_xml", "offset": parser.ErrorByteIndex, "code": exc.code},
            scope,
        ) from exc


def relationship_id(attrs: dict[str, str]) -> str | None:
    """The ``r:id`` attribute, in whichever relationships namespace the workbook declares."""
    for key, value in attrs.items():
        namespace, _, tail = key.rpartition(" ")
        if tail == "id" and namespace.endswith("/relationships"):
            return value
    return None


# --- Small parts -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Relationship:
    id: str
    type: str  # the last segment of the relationship type URI: "worksheet", "sharedStrings"
    target: str
    external: bool
    start: int
    length: int  # of the Relationship tag


def read_relationships(package: Package, name: str) -> list[Relationship]:
    """The relationships a part declares. A ``Problem`` loses all of them: they are small."""
    found: list[Relationship] = []
    window = Window()
    parser = new_parser()

    def start(tag: str, attrs: dict[str, str]) -> None:
        if tag.rpartition(" ")[2] != "Relationship" or not tag.endswith(
            "relationships Relationship"
        ):
            return
        at = parser.CurrentByteIndex
        end = window.tag_end(at)
        found.append(
            Relationship(
                attrs.get("Id", ""),
                attrs.get("Type", "").rpartition("/")[2],
                attrs.get("Target", ""),
                attrs.get("TargetMode", "").lower() == "external",
                at,
                0 if end is None else end - at,
            )
        )
        if len(found) > 100_000:
            raise Stop

    guarded(package, name, parser, start)
    drive(package, name, parser, window)
    return found


@dataclass(frozen=True)
class SheetDecl:
    """A ``<sheet>`` of the workbook: its name as declared, its state, its relationship id and
    where its tag is in the workbook part."""

    name: str
    state: str | None
    rid: str | None
    start: int
    length: int


@dataclass
class Workbook:
    date1904: bool | None = None  # None: the workbook does not say (the 1900 system applies)
    epoch: tuple[int, int] | None = None  # the workbookPr tag's [start, length)
    sheets: list[SheetDecl] = field(default_factory=list)
    sheet_total: int = 0


def read_workbook(package: Package, name: str) -> Workbook:
    """The workbook part: its date system and its sheets in order, up to ``xlsx_max_sheets``."""
    book = Workbook()
    window = Window()
    parser = new_parser()
    cap = package.limits.max_sheets

    def start(tag: str, attrs: dict[str, str]) -> None:
        kind = local(tag)
        at = parser.CurrentByteIndex
        if kind == "workbookPr" and book.epoch is None:
            end = window.tag_end(at)
            book.epoch = (at, 0 if end is None else end - at)
            value = attrs.get("date1904")
            if value is not None:
                book.date1904 = {"1": True, "true": True, "0": False, "false": False}.get(value)
        elif kind == "sheet":
            book.sheet_total += 1
            if len(book.sheets) < cap:
                end = window.tag_end(at)
                if end is None:  # a tag longer than a read piece cannot be cited
                    raise Problem(
                        "xlsx_limit",
                        f"a sheet tag of {name!r} is longer than a read piece; not read",
                        {"limit": "tag", "part": name, "max": PIECE},
                        (package.span(name),),
                    )
                book.sheets.append(
                    SheetDecl(
                        attrs.get("name", ""),
                        attrs.get("state"),
                        relationship_id(attrs),
                        at,
                        end - at,
                    )
                )

    guarded(package, name, parser, start)
    drive(package, name, parser, window)
    return book


@dataclass
class SharedStrings:
    """The workbook's shared strings. ``complete`` is False when a limit or damage stopped the
    table: an index past ``strings`` is then not covered rather than missing."""

    strings: list[str] = field(default_factory=list)
    complete: bool = True


def read_shared_strings(package: Package, name: str | None) -> tuple[SharedStrings, Problem | None]:
    """The shared strings: each ``<si>`` as the concatenation of its text runs (phonetic runs
    excluded), up to ``xlsx_max_shared_strings`` and ``xlsx_max_shared_string_bytes``."""
    table = SharedStrings()
    if name is None or not package.has(name):
        return table, None
    limits = package.limits
    parser = new_parser()
    state = {"item": -1, "phonetic": 0, "text": -1, "bytes": 0, "current": 0}
    parts: list[str] = []
    stack: list[str | None] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        kind = local(tag)
        stack.append(kind)
        if len(stack) > MAX_DEPTH:
            raise Problem(
                "xlsx_limit",
                "XML nests too deep",
                {"limit": "depth", "part": name},
                (package.span(name),),
            )
        if kind == "si" and len(stack) == 2:
            state["item"] = 1
            state["current"] = 0
            parts.clear()
        elif kind == "rPh" and state["item"] > 0:
            state["phonetic"] += 1
        elif kind == "t" and state["item"] > 0 and state["phonetic"] == 0:
            state["text"] = 1

    def end(tag: str) -> None:
        kind = stack.pop()
        if kind == "t":
            state["text"] = -1
        elif kind == "rPh" and state["phonetic"]:
            state["phonetic"] -= 1
        elif kind == "si" and len(stack) == 1 and state["item"] > 0:
            text = "".join(parts)
            state["item"] = -1
            state["bytes"] += len(text.encode("utf-8", "surrogatepass"))
            state["current"] = 0
            if len(table.strings) >= limits.max_shared_strings:
                table.complete = False
                raise Stop
            table.strings.append(text)

    def text(data: str) -> None:
        if state["text"] > 0:
            parts.append(data)
            state["current"] += len(data)
            if state["bytes"] + state["current"] > limits.max_shared_string_bytes:
                table.complete = False
                raise Stop

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        drive(package, name, parser)
    except Problem as problem:
        table.complete = False
        return table, problem
    if not table.complete:
        scope = (package.span(name),)
        return table, Problem(
            "xlsx_limit",
            f"the shared strings exceed xlsx_max_shared_strings ({limits.max_shared_strings}) or"
            f" xlsx_max_shared_string_bytes ({limits.max_shared_string_bytes}); strings from"
            f" {len(table.strings)} on are not covered",
            {
                "limit": "xlsx_max_shared_strings",
                "max_strings": limits.max_shared_strings,
                "max_bytes": limits.max_shared_string_bytes,
                "read": len(table.strings),
            },
            scope,
        )
    return table, None


@dataclass
class Styles:
    """The cell formats a workbook declares: ``formats[s]`` is the ``numFmtId`` of cell format
    ``s``; ``codes`` holds the format codes the workbook defines itself (id 164 and up)."""

    formats: list[int] = field(default_factory=list)
    codes: dict[int, str] = field(default_factory=dict)
    complete: bool = True


def read_styles(package: Package, name: str | None) -> tuple[Styles, Problem | None]:
    """The number formats and the format of each cell style (``cellXfs``), up to
    ``xlsx_max_styles`` entries."""
    styles = Styles()
    if name is None or not package.has(name):
        return styles, None
    cap = package.limits.max_styles
    parser = new_parser()
    stack: list[str | None] = []
    seen = {"entries": 0}

    def start(tag: str, attrs: dict[str, str]) -> None:
        kind = local(tag)
        stack.append(kind)
        if len(stack) > MAX_DEPTH:
            raise Problem(
                "xlsx_limit",
                "XML nests too deep",
                {"limit": "depth", "part": name},
                (package.span(name),),
            )
        if kind not in ("numFmt", "xf") or len(stack) != 3:
            return
        parent = stack[1]
        if (kind, parent) not in (("numFmt", "numFmts"), ("xf", "cellXfs")):
            return
        seen["entries"] += 1
        if seen["entries"] > cap:
            styles.complete = False
            raise Stop
        if kind == "numFmt":
            ident, code = small_int(attrs.get("numFmtId")), attrs.get("formatCode")
            if ident is not None and code:
                styles.codes[ident] = code
        else:
            styles.formats.append(small_int(attrs.get("numFmtId", "0")) or 0)

    def end(tag: str) -> None:
        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        drive(package, name, parser)
    except Problem as problem:
        styles.complete = False
        return styles, problem
    if not styles.complete:
        return styles, Problem(
            "xlsx_limit",
            f"the styles hold more than xlsx_max_styles ({cap}) entries; formats beyond are not"
            " covered",
            {"limit": "xlsx_max_styles", "max": cap},
            (package.span(name),),
        )
    return styles, None
