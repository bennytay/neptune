"""What a font declares about its codes: the text each one stands for and its advance width.

A shown string is a sequence of codes. A simple font (Type1, TrueType, Type3) uses one byte per
code; a composite font (Type0) uses the code lengths its CMap declares (two bytes for
``Identity-H``). For each code this module answers, from the font dictionary alone:

- **Text**: the font's ``ToUnicode`` CMap, if it maps the code; otherwise, for a simple font, the
  glyph its encoding names (base encoding plus ``Differences``), read through the Adobe Glyph
  List. A composite font without ``ToUnicode`` maps nothing. Anything else is unmapped: ``None``,
  never a guess.
- **Width**: ``Widths`` (simple fonts, ``MissingWidth`` outside its range) or ``W``/``DW``
  (composite fonts), in glyph space scaled to one unit of font size; for the 14 standard fonts
  without ``Widths``, the Adobe Core 14 metrics. Otherwise ``None``.
- **Ascent and descent**: the font descriptor's, the Core 14 metrics', or a Type3 font's
  ``FontBBox``; otherwise ``None``.

The encoding tables, the glyph list and the Core 14 metrics are pypdf's (``pypdf._codecs``),
pinned with it; nothing here decodes a font program.

Cost: a CMap is read for at most ``MAX_CMAP_BYTES`` and ``MAX_TABLE_ENTRIES`` entries, a ``W``
table for ``MAX_TABLE_ENTRIES``; a font cut there says so (``Font.limited``). Ranges (code space,
``bfrange``, ``W``) are indexed once into disjoint, sorted segments, so each code is a binary
search, never a scan of every range; where declared ranges overlap the first declared wins, as
a scan in declaration order would give. A font caches the glyphs it has decoded.
"""

import bisect
import heapq
import itertools
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final, Generic, TypeVar

from pypdf._codecs import (
    _mac_encoding,
    _pdfdoc_encoding,
    _std_encoding,
    _symbol_encoding,
    _win_encoding,
    _zapfding_encoding,
    adobe_glyphs,
)
from pypdf._codecs.core_font_metrics import CORE_FONT_METRICS
from pypdf.generic import DictionaryObject

from ._objects import (
    array,
    dictionary,
    entry,
    integer,
    name,
    number,
    stream,
)

# A CMap or a width table larger than this is not read: its codes stay unmapped or unmeasured.
MAX_CMAP_BYTES: Final = 4 * 1024 * 1024
MAX_TABLE_ENTRIES: Final = 1 << 17
MAX_CODE_BYTES: Final = 4  # ISO 32000-1 §9.7.6.2: a code is one to four bytes
MAX_CACHED_GLYPHS: Final = 1 << 16
GLYPH_SPACE: Final = 1.0 / 1000.0  # glyph space units per unit of font size, but in Type3
_ENCODINGS: Final[dict[str, list[str]]] = {
    "StandardEncoding": _std_encoding,
    "WinAnsiEncoding": _win_encoding,
    "MacRomanEncoding": _mac_encoding,
    "PDFDocEncoding": _pdfdoc_encoding,
}
_SYMBOLIC_FLAG: Final = 4
_SUBSET: Final = re.compile(r"[A-Z]{6}\+")
_CMAP_TOKEN: Final = re.compile(rb"<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/<>\[\]()]+|[A-Za-z_][\w.]*|-?\d+")


V = TypeVar("V")


class Ranges(Generic[V]):
    """Closed ranges of codes with a value each, indexed once for binary search.

    Built in O(n log n) into disjoint sorted segments, each owned by the first declared range
    covering it: ``find`` answers what a scan of the ranges in declaration order would, in
    O(log n).
    """

    def __init__(self, declared: Sequence[tuple[int, int, V]]) -> None:
        self._declared = list(declared)
        self._starts: list[int] = []
        self._ends: list[int] = []
        self._owners: list[int] = []
        bounds = sorted({point for low, high, _ in declared for point in (low, high + 1)})
        by_start = sorted(range(len(declared)), key=lambda i: (declared[i][0], i))
        active: list[tuple[int, int]] = []  # (declaration index, last code), first declared on top
        upcoming = 0
        for point, following in itertools.pairwise(bounds):
            while upcoming < len(by_start) and declared[by_start[upcoming]][0] <= point:
                index = by_start[upcoming]
                heapq.heappush(active, (index, declared[index][1]))
                upcoming += 1
            while active and active[0][1] < point:
                heapq.heappop(active)
            if not active:
                continue
            owner = active[0][0]
            if self._owners and self._owners[-1] == owner and self._ends[-1] == point - 1:
                self._ends[-1] = following - 1
            else:
                self._starts.append(point)
                self._ends.append(following - 1)
                self._owners.append(owner)

    def find(self, code: int) -> tuple[int, int, V] | None:
        """The first declared range holding ``code``, or ``None``."""
        at = bisect.bisect_right(self._starts, code) - 1
        if at < 0 or code > self._ends[at]:
            return None
        return self._declared[self._owners[at]]


@dataclass(frozen=True)
class Glyph:
    """One code of a shown string: its bytes' length, its text and its advance per unit size."""

    code: int
    length: int
    text: str | None
    width: float | None


def _is_control(char: str) -> bool:
    return len(char) == 1 and (ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0)


def _base_table(table: list[str]) -> list[str | None]:
    return [None if _is_control(char) else char for char in table]


def _scalar(value: int) -> str | None:
    if 0 <= value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF:
        return chr(value)
    return None


def _glyph_component(component: str) -> str | None:
    known = adobe_glyphs.get("/" + component)
    if known is not None:
        return known
    if component.startswith("uni") and len(component) > 3 and (len(component) - 3) % 4 == 0:
        digits = component[3:]
        if all(c in "0123456789ABCDEF" for c in digits):
            chars = [_scalar(int(digits[i : i + 4], 16)) for i in range(0, len(digits), 4)]
            if all(char is not None for char in chars):
                return "".join(char for char in chars if char is not None)
        return None
    if component.startswith("u") and 5 <= len(component) <= 7:
        digits = component[1:]
        if all(c in "0123456789ABCDEF" for c in digits):
            return _scalar(int(digits, 16))
    return None


def glyph_text(glyph: str) -> str | None:
    """A glyph name's text by the Adobe Glyph List rules: ``f_i`` is ``fi``, ``A.sc`` is ``A``."""
    base = glyph.split(".", 1)[0]
    if not base:
        return None
    parts = [_glyph_component(part) for part in base.split("_")]
    if any(part is None for part in parts):
        return None
    return "".join(part for part in parts if part is not None)


def _utf16(data: bytes) -> str | None:
    try:
        return data.decode("utf-16-be")
    except UnicodeDecodeError:
        return None


def _by_length(entries: Sequence[tuple[int, int, int, V]]) -> dict[int, Ranges[V]]:
    """``(length, low, high, value)`` entries as one ``Ranges`` per code length."""
    grouped: dict[int, list[tuple[int, int, V]]] = {}
    for length, low, high, value in entries:
        grouped.setdefault(length, []).append((low, high, value))
    return {length: Ranges(found) for length, found in grouped.items()}


@dataclass
class ToUnicode:
    """A ToUnicode CMap: code space ranges, single codes, and code ranges mapped to text.

    ``limited`` is set when the CMap is larger than ``MAX_CMAP_BYTES`` or ``MAX_TABLE_ENTRIES``
    and was read only up to there. The range indexes are built on the first lookup.
    """

    spaces: list[tuple[int, int, int]] = field(default_factory=list)  # (length, low, high)
    chars: dict[tuple[int, int], str] = field(default_factory=dict)  # (length, code) -> text
    ranges: list[tuple[int, int, int, str]] = field(default_factory=list)  # (length, lo, hi, base)
    limited: bool = False
    _ranges: dict[int, Ranges[str]] | None = field(default=None, repr=False, compare=False)
    _spaces: dict[int, Ranges[None]] | None = field(default=None, repr=False, compare=False)

    @property
    def entries(self) -> int:
        return len(self.spaces) + len(self.chars) + len(self.ranges)

    def lookup(self, code: int, length: int) -> str | None:
        found = self.chars.get((length, code))
        if found is not None:
            return found
        if self._ranges is None:
            self._ranges = _by_length(self.ranges)
        ranges = self._ranges.get(length)
        hit = ranges.find(code) if ranges is not None else None
        if hit is None:
            return None
        low, _, base = hit
        if not base:
            return None
        last = _scalar(ord(base[-1]) + code - low)
        return None if last is None else base[:-1] + last

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        """``(code, length)`` for each code of ``data`` by the declared code space ranges."""
        if self._spaces is None:
            self._spaces = _by_length([(size, low, high, None) for size, low, high in self.spaces])
        spaces = self._spaces
        lengths = sorted(spaces) or [2]
        position = 0
        while position < len(data):
            for size in lengths:
                piece = data[position : position + size]
                if len(piece) < size:
                    continue
                code = int.from_bytes(piece, "big")
                if spaces[size].find(code) is not None:
                    yield code, size
                    position += size
                    break
            else:
                size = min(lengths[0], len(data) - position)
                yield int.from_bytes(data[position : position + size], "big"), size
                position += size


def _hex(token: bytes) -> bytes | None:
    digits = re.sub(rb"\s", b"", token[1:-1])
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return None


def parse_to_unicode(data: bytes) -> ToUnicode:
    """Read the parts of a CMap that map codes to text; anything malformed is skipped.

    Reading stops at ``MAX_CMAP_BYTES``, ``4 * MAX_TABLE_ENTRIES`` tokens or
    ``MAX_TABLE_ENTRIES`` entries, and the CMap is then ``limited``.
    """
    cmap = ToUnicode(limited=len(data) > MAX_CMAP_BYTES)
    section = b""
    operands: list[bytes | list[bytes]] = []
    stack: list[bytes] | None = None
    for count, match in enumerate(_CMAP_TOKEN.finditer(data, 0, MAX_CMAP_BYTES)):
        token = match.group()
        # At the entry bound the CMap is cut only if more of it follows: a section's closing
        # keyword is not another entry, so a table of exactly the bound is read whole.
        more = section and token[:1] in (b"<", b"/", b"[")
        if count > MAX_TABLE_ENTRIES * 4 or (cmap.entries >= MAX_TABLE_ENTRIES and more):
            cmap.limited = True
            break
        if token == b"[":
            stack = []
        elif token == b"]":
            if stack is not None and section:
                operands.append(stack)
                _apply(cmap, section, operands)
            stack = None
        elif stack is not None:
            stack.append(token)
        elif token in (b"begincodespacerange", b"beginbfchar", b"beginbfrange"):
            section, operands = token, []
        elif token.startswith(b"end"):
            section, operands = b"", []
        elif section:
            operands.append(token)
            _apply(cmap, section, operands)
    return cmap


def _destination(token: bytes) -> str | None:
    if token.startswith(b"<"):
        raw = _hex(token)
        return None if raw is None else _utf16(raw)
    if token.startswith(b"/"):
        return glyph_text(token[1:].decode("latin-1"))
    return None


def _apply(cmap: ToUnicode, section: bytes, operands: list[bytes | list[bytes]]) -> None:
    """Consume ``operands`` once they hold a whole entry of ``section``."""
    if section == b"begincodespacerange" and len(operands) == 2:
        low, high = (_hex(t) if isinstance(t, bytes) else None for t in operands)
        if low is not None and high is not None and len(low) == len(high) and low:
            bottom, top = int.from_bytes(low, "big"), int.from_bytes(high, "big")
            if len(low) <= MAX_CODE_BYTES and bottom <= top:
                cmap.spaces.append((len(low), bottom, top))
        operands.clear()
    elif section == b"beginbfchar" and len(operands) == 2:
        source, target = operands
        code = _hex(source) if isinstance(source, bytes) else None
        text = _destination(target) if isinstance(target, bytes) else None
        if code and text is not None:
            cmap.chars[(len(code), int.from_bytes(code, "big"))] = text
        operands.clear()
    elif section == b"beginbfrange" and len(operands) == 3:
        first, last, target = operands
        low = _hex(first) if isinstance(first, bytes) else None
        high = _hex(last) if isinstance(last, bytes) else None
        operands.clear()
        if not low or high is None or len(low) != len(high):
            return
        lo, hi = int.from_bytes(low, "big"), int.from_bytes(high, "big")
        if hi < lo:
            return
        if isinstance(target, list):
            for offset, token in enumerate(target[: hi - lo + 1]):
                text = _destination(token)
                if text is not None and cmap.entries < MAX_TABLE_ENTRIES:
                    cmap.chars[(len(low), lo + offset)] = text
        else:
            base = _destination(target)
            if base is not None:
                cmap.ranges.append((len(low), lo, hi, base))


def _core_metrics(base_font: str) -> object | None:
    return CORE_FONT_METRICS.get(_SUBSET.sub("", base_font, count=1))


@dataclass
class Font:
    """What one font dictionary declares; built by ``load_font``."""

    name: str
    composite: bool = False
    vertical: bool = False
    identity: bool = False
    to_unicode: ToUnicode | None = None
    encoding: list[str | None] = field(default_factory=lambda: [None] * 256)
    widths: dict[int, float] = field(default_factory=dict)
    width_ranges: list[tuple[int, int, float]] = field(default_factory=list)
    default_width: float | None = None
    core_widths: dict[str, float] | None = None
    ascent: float | None = None
    descent: float | None = None
    limited: bool = False  # its ToUnicode or W table was read only up to a bound
    _width_index: Ranges[float] | None = field(default=None, repr=False, compare=False)
    _cache: dict[tuple[int, int], Glyph] = field(default_factory=dict, repr=False, compare=False)

    def glyphs(self, data: bytes) -> list[Glyph]:
        if not self.composite:
            return [self._glyph(code, 1) for code in data]
        if self.to_unicode is not None and self.to_unicode.spaces:
            return [self._glyph(code, size) for code, size in self.to_unicode.split(data)]
        found = []
        for position in range(0, len(data), 2):
            piece = data[position : position + 2]
            found.append(self._glyph(int.from_bytes(piece, "big"), len(piece)))
        return found

    def _glyph(self, code: int, length: int) -> Glyph:
        cached = self._cache.get((code, length))
        if cached is not None:
            return cached
        text: str | None = None
        if self.to_unicode is not None:
            text = self.to_unicode.lookup(code, length)
        if text is None and not self.composite and code < 256:
            text = self.encoding[code]
        glyph = Glyph(code, length, text, self._width(code, length))
        if len(self._cache) < MAX_CACHED_GLYPHS:
            self._cache[(code, length)] = glyph
        return glyph

    def _width(self, code: int, length: int) -> float | None:
        # A composite font's widths are by CID, which is the code only under an Identity CMap.
        if self.composite and not self.identity:
            return self.default_width if not self.widths and not self.width_ranges else None
        if code in self.widths:
            return self.widths[code]
        if self.width_ranges:
            if self._width_index is None:
                self._width_index = Ranges(self.width_ranges)
            hit = self._width_index.find(code)
            if hit is not None:
                return hit[2]
        if self.core_widths is not None and not self.composite and code < 256:
            char = self.encoding[code]
            if char is not None and char in self.core_widths:
                return self.core_widths[char]
            return None
        return self.default_width


def _scaled(value: float, scale: float) -> float:
    """Glyph-space units to units of font size; dividing by 1000 is exact more often."""
    return value / 1000.0 if scale == GLYPH_SPACE else value * scale


def _descriptor_metrics(font: Font, descriptor: DictionaryObject | None, scale: float) -> None:
    if descriptor is None:
        return
    ascent, descent = number(entry(descriptor, "/Ascent")), number(entry(descriptor, "/Descent"))
    if ascent is not None and descent is not None:
        font.ascent, font.descent = _scaled(ascent, scale), _scaled(descent, scale)


def _simple_encoding(font_dict: DictionaryObject, base_font: str, flags: int) -> list[str | None]:
    declared = entry(font_dict, "/Encoding")
    core = _SUBSET.sub("", base_font, count=1)
    builtin = (
        _symbol_encoding
        if core == "Symbol"
        else _zapfding_encoding
        if core == "ZapfDingbats"
        else None
    )
    table: list[str | None]
    if builtin is not None:
        table = _base_table(builtin)
    elif flags & _SYMBOLIC_FLAG:
        table = [None] * 256  # a symbolic font's own encoding is in its program
    else:
        table = _base_table(_std_encoding)
    base = name(declared)
    if base in _ENCODINGS:
        table = _base_table(_ENCODINGS[base])
    encoding = dictionary(declared)
    if encoding is not None:
        inner = name(entry(encoding, "/BaseEncoding"))
        if inner in _ENCODINGS:
            table = _base_table(_ENCODINGS[inner])
        code = None
        for item in array(entry(encoding, "/Differences")) or ():
            value = integer(item)
            if value is not None:
                code = value
                continue
            glyph = name(item)
            if glyph is not None and code is not None:
                if 0 <= code < 256:
                    table[code] = glyph_text(glyph)
                code += 1
    return table


def _simple_widths(font: Font, font_dict: DictionaryObject, scale: float) -> None:
    widths = array(entry(font_dict, "/Widths"))
    first = integer(entry(font_dict, "/FirstChar"))
    descriptor = dictionary(entry(font_dict, "/FontDescriptor"))
    if widths is not None and first is not None:
        for offset, item in enumerate(widths[:256]):
            width = number(item)
            if width is not None:
                font.widths[first + offset] = _scaled(width, scale)
        missing = number(entry(descriptor, "/MissingWidth")) if descriptor is not None else None
        font.default_width = _scaled(missing if missing is not None else 0.0, scale)
        return
    metrics = _core_metrics(font.name)
    if metrics is not None:
        widths_by_char = getattr(metrics, "character_widths", {})
        font.core_widths = {
            char: float(width) / 1000.0
            for char, width in widths_by_char.items()
            if char != "default"
        }


def _cid_widths(font: Font, descendant: DictionaryObject) -> None:
    default = number(entry(descendant, "/DW"))
    font.default_width = (default if default is not None else 1000.0) / 1000.0
    table: list[object] = list(array(entry(descendant, "/W")) or [])
    position, entries = 0, 0
    while position < len(table):
        if entries >= MAX_TABLE_ENTRIES:
            font.limited = True
            break
        first = integer(table[position])
        if first is None:
            break
        following = array(table[position + 1]) if position + 1 < len(table) else None
        if following is not None:
            room = MAX_TABLE_ENTRIES - entries
            font.limited = font.limited or len(following) > room
            for offset, item in enumerate(following[:room]):
                width = number(item)
                if width is not None:
                    font.widths[first + offset] = width / 1000.0
            entries += len(following)
            position += 2
            continue
        last = integer(table[position + 1]) if position + 1 < len(table) else None
        width = number(table[position + 2]) if position + 2 < len(table) else None
        if last is None or width is None:
            break
        if first <= last:
            font.width_ranges.append((first, last, width / 1000.0))
        entries += 1
        position += 3


def load_font(font_dict: DictionaryObject) -> Font:
    """Everything this module reads from one font dictionary; parts that do not parse stay
    unknown (``None``) rather than failing the font."""
    subtype = name(entry(font_dict, "/Subtype")) or ""
    base_font = name(entry(font_dict, "/BaseFont")) or ""
    font = Font(name=base_font)
    to_unicode = stream(entry(font_dict, "/ToUnicode"))
    if to_unicode is not None:
        font.to_unicode = parse_to_unicode(to_unicode.get_data())
        font.limited = font.to_unicode.limited
    if subtype == "Type0":
        font.composite = True
        encoding = name(entry(font_dict, "/Encoding")) or ""
        font.identity = encoding in ("Identity-H", "Identity-V")
        font.vertical = encoding.endswith("-V")
        descendants: list[object] = list(array(entry(font_dict, "/DescendantFonts")) or [])
        descendant = dictionary(descendants[0]) if descendants else None
        if descendant is not None:
            _cid_widths(font, descendant)
            descriptor = dictionary(entry(descendant, "/FontDescriptor"))
            _descriptor_metrics(font, descriptor, GLYPH_SPACE)
        return font
    descriptor = dictionary(entry(font_dict, "/FontDescriptor"))
    flags = (integer(entry(descriptor, "/Flags")) or 0) if descriptor is not None else 0
    font.encoding = _simple_encoding(font_dict, base_font, flags)
    scale = GLYPH_SPACE
    if subtype == "Type3":
        matrix = [number(v) for v in array(entry(font_dict, "/FontMatrix")) or []]
        if len(matrix) == 6 and matrix[0] is not None and matrix[3] is not None:
            scale = matrix[0]
            bbox = [number(v) for v in array(entry(font_dict, "/FontBBox")) or []]
            if len(bbox) == 4 and bbox[1] is not None and bbox[3] is not None:
                font.ascent, font.descent = bbox[3] * matrix[3], bbox[1] * matrix[3]
        else:
            scale = 0.0  # no usable matrix: no width is known
    _simple_widths(font, font_dict, scale)
    if subtype == "Type3" and scale == 0.0:
        font.widths, font.default_width, font.core_widths = {}, None, None
    if font.ascent is None:
        _descriptor_metrics(font, descriptor, scale if subtype == "Type3" else GLYPH_SPACE)
    if font.ascent is None:
        metrics = _core_metrics(base_font)
        core = getattr(metrics, "font_descriptor", None)
        if core is not None:
            font.ascent, font.descent = core.ascent / 1000.0, core.descent / 1000.0
    return font
