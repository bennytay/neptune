"""A page's content as the file draws it: text runs and painted images, in content order.

The interpreter runs a page's content stream, and the form XObjects it draws, in order. It keeps
the graphics state (``q``/``Q``, ``cm``), the text state (``Tf``, ``Tc``, ``Tw``, ``Tz``, ``TL``,
``Ts``) and the text and line matrices, and the marked-content stack (``BMC``/``BDC``/``EMC``).
Everything else (paths, colours, clipping, shading) is skipped as not text.

- **A run** is what one text-showing operator (``Tj``, ``TJ``, ``'``, ``"``) shows: its codes
  decoded by the current font (``_fonts``), unmapped codes as U+FFFD and counted. Inside ``TJ``, a
  displacement of at least ``space_threshold`` thousandths of an em to the right puts one space
  between the strings around it. Its box is where its glyphs are placed in default user space:
  each glyph's advance (width, ``Tc``, ``Tw``, ``Tz``) and every ``TJ`` displacement, from the
  font's descent to its ascent, through the text matrix and the CTM. A run whose font lacks a
  width or an ascent, or that follows one whose advance was unknown on the same line, has none.
- **A paint** is an image drawn (``Do`` of an image XObject, an inline image): the unit square
  through the CTM.
- Each item carries the innermost marked-content id (MCID) around it and the outermost
  ``Artifact`` sequence around it, if any. MCIDs inside a form XObject are not the page's and are
  not read: the form's content belongs to the marked content that draws it.

Bounds: at most ``max_operations`` operators per page (forms included), ``max_content_bytes``
decoded bytes of content (the page's and each form's, once), forms nested ``MAX_FORM_DEPTH`` deep
and ``q`` nested ``MAX_STATE_DEPTH`` deep. Past an operator or byte bound the page stops there.
"""

import contextlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from pypdf import PdfReader
from pypdf.generic import ContentStream, DictionaryObject

from neptune.adapters.contract import ShortReadError
from neptune.adapters.pdf._fonts import Font, load_font
from neptune.adapters.pdf._objects import (
    array,
    dictionary,
    entry,
    integer,
    name,
    number,
    reference,
    resolve,
    stream,
    string_bytes,
)

MAX_FORM_DEPTH: Final = 8
MAX_STATE_DEPTH: Final = 1024
MAX_MARKED_DEPTH: Final = 1024
REPLACEMENT: Final = "�"

Matrix = tuple[float, float, float, float, float, float]
Point = tuple[float, float]
IDENTITY: Final[Matrix] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(m: Matrix, n: Matrix) -> Matrix:
    """``m`` then ``n``, in PDF's row-vector convention."""
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (
        a * p + b * r,
        a * q + b * s,
        c * p + d * r,
        c * q + d * s,
        e * p + f * r + t,
        e * q + f * s + u,
    )


def transform(m: Matrix, x: float, y: float) -> Point:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


@dataclass(frozen=True)
class Box:
    """An axis-aligned box in default user space."""

    x0: float
    y0: float
    x1: float
    y1: float

    @staticmethod
    def around(points: Sequence[Point]) -> "Box | None":
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        box = Box(min(xs), min(ys), max(xs), max(ys))
        return box if all(math.isfinite(v) for v in (box.x0, box.y0, box.x1, box.y1)) else None

    def union(self, other: "Box") -> "Box":
        return Box(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )


@dataclass(frozen=True)
class Line:
    """Where a run's baseline starts and ends, its direction and its font size, in user space."""

    origin: Point
    end: Point
    direction: Point
    height: float


@dataclass(frozen=True)
class Item:
    """A run (``text`` set) or a paint (``text`` is ``None``), in content order."""

    sequence: int
    text: str | None
    unmapped: int
    box: Box | None
    line: Line | None
    mcid: int | None
    artifact: int | None
    artifact_kind: str


@dataclass
class PageContent:
    items: list[Item] = field(default_factory=list)
    skipped: int = 0  # operators skipped: wrong operands, missing resources, unbalanced state
    missing_fonts: int = 0  # runs shown with no usable font
    limited: str | None = None  # "max_operations" or "max_content_bytes" when a bound stopped it
    content_bytes: int = 0
    operations: int = 0


@dataclass(frozen=True)
class GraphicsState:
    ctm: Matrix = IDENTITY
    font: Font | None = None
    size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0


class _Stop(Exception):
    """A page bound was reached: stop interpreting the page."""


@dataclass
class _Marked:
    mcid: int | None
    artifact: int | None
    kind: str


class Interpreter:
    """Runs one page's content; ``run`` returns what it drew. A fresh instance per page."""

    def __init__(
        self,
        reader: PdfReader,
        *,
        max_operations: int,
        max_content_bytes: int,
        space_threshold: int,
    ) -> None:
        self._reader = reader
        self._max_operations = max_operations
        self._max_content_bytes = max_content_bytes
        self._space = space_threshold / 1000.0
        self._content = PageContent()
        self._fonts: dict[object, Font | None] = {}
        self._forms: dict[object, list[tuple[object, bytes]]] = {}
        self._state = GraphicsState()
        self._stack: list[GraphicsState] = []
        self._marked: list[_Marked] = []
        self._artifacts = 0
        self._tm: Matrix = IDENTITY
        self._tlm: Matrix = IDENTITY
        self._tm_known = True
        self._form_depth = 0

    def run(self, page: DictionaryObject) -> PageContent:
        contents = resolve(dict.get(page, "/Contents"))
        if contents is None:
            return self._content
        resources = dictionary(entry(page, "/Resources"))
        with contextlib.suppress(_Stop):
            self._execute(self._operations(ContentStream(contents, self._reader)), resources)
        return self._content

    # --- Bounds -----------------------------------------------------------------------------

    def _operations(self, content: ContentStream) -> list[tuple[object, bytes]]:
        self._content.content_bytes += len(content.get_data())
        if self._content.content_bytes > self._max_content_bytes:
            self._content.limited = "max_content_bytes"
            raise _Stop
        return list(content.operations)

    def _count(self) -> None:
        self._content.operations += 1
        if self._content.operations > self._max_operations:
            self._content.limited = "max_operations"
            raise _Stop

    # --- The interpreter loop ---------------------------------------------------------------

    def _execute(
        self, operations: list[tuple[object, bytes]], resources: DictionaryObject | None
    ) -> None:
        for operands, operator in operations:
            self._count()
            handler = _HANDLERS.get(operator)
            if handler is None:
                continue
            try:
                handler(self, operands, resources)
            except (_Stop, MemoryError, ShortReadError, RecursionError):
                raise
            except Exception:
                self._content.skipped += 1

    # --- Graphics and text state ------------------------------------------------------------

    def _push(self, operands: object, resources: DictionaryObject | None) -> None:
        if len(self._stack) >= MAX_STATE_DEPTH:
            self._content.skipped += 1
            return
        self._stack.append(self._state)

    def _pop(self, operands: object, resources: DictionaryObject | None) -> None:
        if self._stack:
            self._state = self._stack.pop()

    def _cm(self, operands: object, resources: DictionaryObject | None) -> None:
        self._state = replace(self._state, ctm=multiply(_matrix(operands), self._state.ctm))

    def _bt(self, operands: object, resources: DictionaryObject | None) -> None:
        self._tm = self._tlm = IDENTITY
        self._tm_known = True

    def _tf(self, operands: object, resources: DictionaryObject | None) -> None:
        values = _operands(operands, 2)
        size = number(values[1])
        font_name = name(values[0])
        if size is None or font_name is None:
            raise ValueError("Tf takes a font name and a size")
        font_dict = entry(entry(resources, "/Font"), "/" + font_name)
        self._state = replace(self._state, font=self._font(font_dict), size=size)

    def _font(self, font_dict: object) -> Font | None:
        found = dictionary(font_dict)
        if found is None:
            return None
        key = reference(found) or id(found)
        if key not in self._fonts:
            try:
                self._fonts[key] = load_font(found)
            except (MemoryError, ShortReadError, RecursionError):
                raise
            except Exception:
                self._fonts[key] = None
        return self._fonts[key]

    def set_text_state(self, field_name: str, value: float) -> None:
        state = self._state
        match field_name:
            case "char_spacing":
                self._state = replace(state, char_spacing=value)
            case "word_spacing":
                self._state = replace(state, word_spacing=value)
            case "scale":
                self._state = replace(state, scale=value)
            case "leading":
                self._state = replace(state, leading=value)
            case "rise":
                self._state = replace(state, rise=value)

    def _td(self, operands: object, resources: DictionaryObject | None) -> None:
        tx, ty = (_require(number(v)) for v in _operands(operands, 2))
        self._move(tx, ty)

    def _td_leading(self, operands: object, resources: DictionaryObject | None) -> None:
        tx, ty = (_require(number(v)) for v in _operands(operands, 2))
        self._state = replace(self._state, leading=-ty)
        self._move(tx, ty)

    def _move(self, tx: float, ty: float) -> None:
        self._tlm = multiply((1.0, 0.0, 0.0, 1.0, tx, ty), self._tlm)
        self._tm = self._tlm
        self._tm_known = True

    def _tm_set(self, operands: object, resources: DictionaryObject | None) -> None:
        self._tm = self._tlm = _matrix(operands)
        self._tm_known = True

    def _next_line(self, operands: object, resources: DictionaryObject | None) -> None:
        self._move(0.0, -self._state.leading)

    # --- Showing text -----------------------------------------------------------------------

    def _tj(self, operands: object, resources: DictionaryObject | None) -> None:
        data = string_bytes(_operands(operands, 1)[0])
        if data is None:
            raise ValueError("Tj takes a string")
        self._show([data])

    def _quote(self, operands: object, resources: DictionaryObject | None) -> None:
        data = string_bytes(_operands(operands, 1)[0])
        if data is None:
            raise ValueError("' takes a string")
        self._next_line(operands, resources)
        self._show([data])

    def _double_quote(self, operands: object, resources: DictionaryObject | None) -> None:
        values = _operands(operands, 3)
        word, char, data = number(values[0]), number(values[1]), string_bytes(values[2])
        if word is None or char is None or data is None:
            raise ValueError('" takes two numbers and a string')
        self._state = replace(self._state, word_spacing=word, char_spacing=char)
        self._next_line(operands, resources)
        self._show([data])

    def _tj_array(self, operands: object, resources: DictionaryObject | None) -> None:
        elements = array(_operands(operands, 1)[0])
        if elements is None:
            raise ValueError("TJ takes an array")
        parts: list[bytes | float] = []
        for element in elements:
            data = string_bytes(element)
            value = number(element) if data is None else None
            if data is not None:
                parts.append(data)
            elif value is not None:
                parts.append(value)
        self._show(parts)

    def _show(self, parts: Sequence[bytes | float]) -> None:
        state = self._state
        font = state.font
        start, ctm = self._tm, state.ctm
        known = self._tm_known
        text: list[str] = []
        unmapped, x, low, high = 0, 0.0, 0.0, 0.0
        pending_space = False
        for part in parts:
            if isinstance(part, float):
                shift = -part / 1000.0 * state.size * state.scale
                x += shift
                low, high = min(low, x), max(high, x)
                if -part / 1000.0 >= self._space and text:
                    pending_space = True
                continue
            if font is None:
                self._content.missing_fonts += 1
                text.append(REPLACEMENT * len(part))
                unmapped += len(part)
                known = False
                continue
            shown: list[str] = []
            for glyph in font.glyphs(part):
                if glyph.text is None:
                    shown.append(REPLACEMENT)
                    unmapped += 1
                else:
                    shown.append(glyph.text)
                if glyph.width is None or font.vertical:
                    known = False
                    continue
                spacing = state.char_spacing
                if glyph.length == 1 and glyph.code == 32:
                    spacing += state.word_spacing
                x += (glyph.width * state.size + spacing) * state.scale
                low, high = min(low, x), max(high, x)
            piece = "".join(shown)
            if pending_space and piece and not piece[0].isspace() and not text[-1][-1:].isspace():
                text.append(" ")
            pending_space = False
            text.append(piece)
        self._tm = multiply((1.0, 0.0, 0.0, 1.0, x, 0.0), start)
        self._tm_known = known
        shown_text = "".join(text)
        if not shown_text:
            return
        box: Box | None = None
        line: Line | None = None
        if known and font is not None:
            matrix = multiply(start, ctm)
            origin = transform(matrix, 0.0, state.rise)
            end = transform(matrix, x, state.rise)
            up = transform(matrix, 0.0, state.rise + state.size)
            unit = transform(matrix, 1.0, state.rise)
            dx, dy = unit[0] - origin[0], unit[1] - origin[1]
            length = math.hypot(dx, dy)
            height = math.hypot(up[0] - origin[0], up[1] - origin[1])
            if length > 0 and math.isfinite(length) and math.isfinite(height):
                line = Line(origin, end, (dx / length, dy / length), height)
            if font.ascent is not None and font.descent is not None:
                top = state.rise + font.ascent * state.size
                bottom = state.rise + font.descent * state.size
                corners = [transform(matrix, cx, cy) for cx in (low, high) for cy in (bottom, top)]
                box = Box.around(corners)
        self._emit(shown_text, unmapped, box, line)

    def _emit(self, text: str | None, unmapped: int, box: Box | None, line: Line | None) -> None:
        mcid = next((m.mcid for m in reversed(self._marked) if m.mcid is not None), None)
        artifact = next((m for m in self._marked if m.artifact is not None), None)
        self._content.items.append(
            Item(
                sequence=len(self._content.items),
                text=text,
                unmapped=unmapped,
                box=box,
                line=line,
                mcid=mcid,
                artifact=None if artifact is None else artifact.artifact,
                artifact_kind="" if artifact is None else artifact.kind,
            )
        )

    # --- Images and forms -------------------------------------------------------------------

    def _paint(self) -> None:
        corners = [transform(self._state.ctm, x, y) for x in (0.0, 1.0) for y in (0.0, 1.0)]
        self._emit(None, 0, Box.around(corners), None)

    def _inline_image(self, operands: object, resources: DictionaryObject | None) -> None:
        self._paint()

    def _do(self, operands: object, resources: DictionaryObject | None) -> None:
        xobject_name = name(_operands(operands, 1)[0])
        if xobject_name is None:
            raise ValueError("Do takes a name")
        xobject = stream(entry(entry(resources, "/XObject"), "/" + xobject_name))
        if xobject is None:
            raise ValueError(f"no XObject {xobject_name}")
        subtype = name(entry(xobject, "/Subtype"))
        if subtype == "Image":
            self._paint()
            return
        if subtype != "Form":
            return
        if self._form_depth >= MAX_FORM_DEPTH:
            raise ValueError("forms nested too deep")
        key = reference(xobject) or id(xobject)
        if key not in self._forms:
            self._forms[key] = self._operations(ContentStream(xobject, self._reader))
        matrix = _matrix(array(entry(xobject, "/Matrix")) or list(IDENTITY))
        saved = (self._state, len(self._stack), len(self._marked))
        self._state = replace(self._state, ctm=multiply(matrix, self._state.ctm))
        self._form_depth += 1
        try:
            form_resources = dictionary(entry(xobject, "/Resources")) or resources
            self._execute(self._forms[key], form_resources)
        finally:
            self._form_depth -= 1
            self._state = saved[0]
            del self._stack[saved[1] :]
            del self._marked[saved[2] :]

    # --- Marked content ---------------------------------------------------------------------

    def _bmc(self, operands: object, resources: DictionaryObject | None) -> None:
        self._begin(name(_operands(operands, 1)[0]), None, resources)

    def _bdc(self, operands: object, resources: DictionaryObject | None) -> None:
        values = _operands(operands, 2)
        properties = values[1]
        if name(properties) is not None:
            properties = entry(entry(resources, "/Properties"), "/" + (name(properties) or ""))
        self._begin(name(values[0]), dictionary(properties), resources)

    def _begin(
        self,
        tag: str | None,
        properties: DictionaryObject | None,
        resources: DictionaryObject | None,
    ) -> None:
        if len(self._marked) >= MAX_MARKED_DEPTH:
            raise ValueError("marked content nested too deep")
        mcid = integer(entry(properties, "/MCID")) if properties is not None else None
        if self._form_depth:
            mcid = None  # a form's MCIDs belong to the form's own structure parents
        artifact, kind = None, ""
        if tag == "Artifact" and not any(m.artifact is not None for m in self._marked):
            artifact = self._artifacts
            self._artifacts += 1
            if properties is not None:
                kind = name(entry(properties, "/Subtype")) or ""
        self._marked.append(
            _Marked(mcid if mcid is not None and mcid >= 0 else None, artifact, kind)
        )

    def _emc(self, operands: object, resources: DictionaryObject | None) -> None:
        if self._marked:
            self._marked.pop()


Handler = Callable[[Interpreter, object, DictionaryObject | None], None]


def _setter(field_name: str, *, percent: bool = False) -> Handler:
    """A handler setting one text-state parameter from its operand (``Tz`` is a percentage)."""

    def handle(self: Interpreter, operands: object, resources: DictionaryObject | None) -> None:
        value = _require(number(_operands(operands, 1)[0]))
        self.set_text_state(field_name, value / 100.0 if percent else value)

    return handle


def _operands(operands: object, count: int) -> list[object]:
    if not isinstance(operands, list) or len(operands) < count:
        raise ValueError(f"expected {count} operands")
    return operands[-count:] if count else []


def _require(value: float | None) -> float:
    if value is None:
        raise ValueError("expected a number")
    return value


def _matrix(operands: object) -> Matrix:
    values = [number(v) for v in _operands(operands, 6)]
    if any(v is None for v in values):
        raise ValueError("a matrix is six numbers")
    a, b, c, d, e, f = (v for v in values if v is not None)
    return (a, b, c, d, e, f)


_HANDLERS: Final[dict[bytes, Handler]] = {
    b"q": Interpreter._push,
    b"Q": Interpreter._pop,
    b"cm": Interpreter._cm,
    b"BT": Interpreter._bt,
    b"Tf": Interpreter._tf,
    b"Tc": _setter("char_spacing"),
    b"Tw": _setter("word_spacing"),
    b"Tz": _setter("scale", percent=True),
    b"TL": _setter("leading"),
    b"Ts": _setter("rise"),
    b"Td": Interpreter._td,
    b"TD": Interpreter._td_leading,
    b"Tm": Interpreter._tm_set,
    b"T*": Interpreter._next_line,
    b"Tj": Interpreter._tj,
    b"'": Interpreter._quote,
    b'"': Interpreter._double_quote,
    b"TJ": Interpreter._tj_array,
    b"Do": Interpreter._do,
    b"INLINE IMAGE": Interpreter._inline_image,
    b"BMC": Interpreter._bmc,
    b"BDC": Interpreter._bdc,
    b"EMC": Interpreter._emc,
}
