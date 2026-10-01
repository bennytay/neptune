"""Opening a PDF with pypdf safely: bounded, quiet, never trusting the file's own account.

- The source is read through ``SourceStream``, a seekable view of the ``SourceReader``; pypdf
  never sees a path. A read the source cannot serve is recorded and re-raised as the
  ``ShortReadError`` it is, even where pypdf would have swallowed the exception.
- pypdf runs under ``pypdf.apply_configuration``: every stream inflates to at most
  ``max_stream_bytes``, the page tree is bounded, and no external decoder (``jbig2dec``) is ever
  called. Its log warnings (repairs it made, streams it could not inflate) are captured per
  phase and become findings; nothing is printed.
- A file pypdf cannot open as written (no ``%%EOF``, a broken cross-reference table, no trailer)
  is opened once more with a tail of our own appended in memory: ``trailer <</Root n g R>>
  startxref 0 %%EOF``, naming the last object the file declares as ``/Type /Catalog``. pypdf
  then rebuilds its cross-reference table by scanning the file's objects. The source is never
  changed; the repair is reported (``pdf.repaired``) and every citation stays in its bytes.
- Encryption: pypdf tries the empty user password itself. The adapter reads an encrypted file
  only when that succeeded and the file uses RC4 (``/V`` 1 or 2, or 4 with ``V2`` crypt
  filters), which pypdf decrypts in pure Python. AES needs a native library whose presence
  would make output depend on the host, so it is never decrypted.
"""

import io
import logging
import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

from pypdf import PdfReader, apply_configuration, get_configuration
from pypdf.generic import DictionaryObject

from neptune.adapters.contract import ShortReadError, SourceReader, read_pieces

from ._objects import array, dictionary, entry, integer, name

BUFFER_SIZE: Final = 64 * 1024
_OBJECT: Final = re.compile(
    rb"(?<![0-9])([0-9]{1,10})[ \t\r\n\f\x00]+([0-9]{1,5})[ \t\r\n\f\x00]+obj\b"
)
_CATALOG: Final = re.compile(rb"/Type[ \t\r\n\f\x00]*/Catalog\b")
_CATALOG_WINDOW: Final = 4096
RC4_VERSIONS: Final = (1, 2)


class SourceStream(io.RawIOBase):
    """A seekable, read-only view of a ``SourceReader``, plus ``suffix`` bytes after its end."""

    def __init__(self, source: SourceReader, suffix: bytes = b"") -> None:
        super().__init__()
        self._source = source
        self._suffix = suffix
        self._size = source.size + len(suffix)
        self._position = 0
        self.short_read: ShortReadError | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        if base + offset < 0:
            raise ValueError("negative seek position")
        self._position = base + offset
        return self._position

    def readinto(self, buffer: "memoryview | bytearray") -> int:  # type: ignore[override]
        view = memoryview(buffer).cast("B")
        if self._position >= self._size or not len(view):
            return 0
        if self._position < self._source.size:
            wanted = min(len(view), self._source.size - self._position)
            data = self._source.read(self._position, wanted)
            if not data:
                self.short_read = ShortReadError(self._source.content_id, self._position, wanted)
                raise self.short_read
        else:
            start = self._position - self._source.size
            data = self._suffix[start : start + len(view)]
        view[: len(data)] = data
        self._position += len(data)
        return len(data)

    def check(self) -> None:
        """Re-raise a short read pypdf may have caught and hidden."""
        if self.short_read is not None:
            raise self.short_read


class Warnings(logging.Handler):
    """pypdf's log warnings since the last ``take``: those about inflating streams apart."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.streams = 0
        self.other = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "pypdf.filters":
            self.streams += 1
        else:
            self.other += 1

    def take(self) -> tuple[int, int]:
        found = (self.streams, self.other)
        self.streams = self.other = 0
        return found


@contextmanager
def pypdf_session(max_stream_bytes: int) -> Iterator[Warnings]:
    """pypdf bounded and silenced for the duration; its warnings collected, not printed."""
    captured = Warnings()
    logger = logging.getLogger("pypdf")
    saved = (logger.propagate, logger.level)
    logger.addHandler(captured)
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    configuration = get_configuration().with_overwrites(
        maximum_declared_stream_length=max_stream_bytes,
        array_based_stream_maximum_output_length=max_stream_bytes,
        jbig2_maximum_output_length=max_stream_bytes,
        lzw_maximum_output_length=max_stream_bytes,
        run_length_maximum_output_length=max_stream_bytes,
        zlib_maximum_output_length=max_stream_bytes,
        jbig2dec_binary=None,
    )
    try:
        with apply_configuration(configuration), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield captured
    finally:
        logger.removeHandler(captured)
        logger.propagate, logger.level = saved


@dataclass
class Opened:
    """A document pypdf opened: its reader, the stream it reads, and how it was opened."""

    reader: PdfReader
    stream: SourceStream
    repaired: bool
    encryption: str  # "none", "rc4" (read with the empty password) or "unreadable"
    catalog: DictionaryObject
    open_warnings: tuple[int, int] = field(default=(0, 0))


class Unreadable(Exception):
    """pypdf could not open the file even after the repair; ``cause`` is the exception class."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def find_catalog(source: SourceReader) -> tuple[int, int] | None:
    """The last object the bytes declare as ``/Type /Catalog``: the latest revision's root."""
    data = b"".join(read_pieces(source, 0, source.size))
    found = None
    for match in _OBJECT.finditer(data):
        window = data[match.end() : match.end() + _CATALOG_WINDOW]
        end = window.find(b"endobj")
        if _CATALOG.search(window if end < 0 else window[:end]):
            found = (int(match.group(1)), int(match.group(2)))
    return found


def _encryption(reader: PdfReader) -> str:
    encrypt = dictionary(entry(reader.trailer, "/Encrypt"))
    if encrypt is None:
        return "none"
    version = integer(entry(encrypt, "/V"))
    rc4 = name(entry(encrypt, "/Filter")) == "Standard" and version in RC4_VERSIONS
    if version == 4:
        filters: dict[object, object] = dict(dictionary(entry(encrypt, "/CF")) or {})
        methods = {name(entry(value, "/CFM")) for value in filters.values()}
        rc4 = name(entry(encrypt, "/Filter")) == "Standard" and methods <= {"V2", "None", None}
    decrypted = reader._encryption is not None and reader._encryption.is_decrypted()
    return "rc4" if rc4 and decrypted else "unreadable"


def _attempt(source: SourceReader, suffix: bytes) -> tuple[PdfReader, SourceStream]:
    stream = SourceStream(source, suffix)
    try:
        reader = PdfReader(io.BufferedReader(stream, BUFFER_SIZE), strict=False)
        if _encryption(reader) == "unreadable":
            reader._override_encryption = True  # numbers in the page tree are not encrypted
        reader.root_object  # noqa: B018 - validates the catalog now, not on first use
    finally:
        stream.check()
    return reader, stream


def open_document(source: SourceReader, captured: Warnings) -> Opened:
    """Open ``source``; ``Unreadable`` if pypdf cannot, even repaired. Never another exception
    for bad bytes, except ``MemoryError`` and ``ShortReadError``, which the runtime owns."""
    repaired = False
    try:
        reader, stream = _attempt(source, b"")
    except (MemoryError, ShortReadError):
        raise
    except Exception as first:
        catalog = find_catalog(source)
        if catalog is None:
            raise Unreadable(type(first).__name__) from first
        tail = b"\ntrailer\n<</Root %d %d R>>\nstartxref\n0\n%%%%EOF\n" % catalog
        try:
            reader, stream = _attempt(source, tail)
        except (MemoryError, ShortReadError):
            raise
        except Exception as second:
            raise Unreadable(type(second).__name__) from second
        repaired = True
    root = dictionary(reader.root_object)
    if root is None:
        raise Unreadable("PdfReadError")
    return Opened(reader, stream, repaired, _encryption(reader), root, captured.take())


def page_list(opened: Opened) -> list[DictionaryObject]:
    """Every page in document order; ``Unreadable`` if the page tree cannot be walked."""
    try:
        pages = list(opened.reader.pages)
    except (MemoryError, ShortReadError):
        raise
    except Exception as exc:
        raise Unreadable(type(exc).__name__) from exc
    finally:
        opened.stream.check()
    return [page for page in pages if isinstance(page, DictionaryObject)]


def has_entries(container: object, key: str) -> bool:
    """Whether ``key`` names a non-empty value: an array, a dictionary or a name tree."""
    value = entry(container, key)
    return bool(array(value)) or bool(dictionary(value))
