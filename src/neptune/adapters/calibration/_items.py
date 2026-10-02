"""A calibration document as a tree of items, from YAML, JSON, TOML or OpenCV's XML.

Every format reader makes the same ``Item``: a name, a kind, the place it is written, its
children in source order and, for a scalar, its readings in the format's own schema (two where
YAML 1.1 and 1.2 differ). Nothing here knows what a camera matrix is; ``_formats`` and ``_emit``
do. Trees are built without recursion and never expand a YAML alias.
"""

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from xml.parsers import expat

from neptune.adapters.structured.tree import Alias, Collection, Document, Null, Unreadable
from neptune.adapters.structured.tree import Value as ReadValue
from neptune.model.configuration import CollectionType, ConfigScalar, ScalarType
from neptune.model.provenance import ByteRange, Locator, Span
from neptune.model.scalars import NonFinite

OPENCV_MATRIX: Final = "opencv-matrix"
# The YAML tag OpenCV's FileStorage writes a matrix with (``!!opencv-matrix``, expanded).
OPENCV_MATRIX_TAG: Final = "tag:yaml.org,2002:opencv-matrix"


class Kind(StrEnum):
    MAPPING = "mapping"
    SEQUENCE = "sequence"
    SCALAR = "scalar"
    NULL = "null"  # the format defines the scalar as null
    ALIAS = "alias"  # a YAML alias: never expanded
    UNREAD = "unread"  # a value no record can hold: why says so


@dataclass
class Item:
    """One value: ``name`` is its key (a sequence item's position), ``where`` where it is written,
    ``count`` how many items a collection declares. A name is cited in parameters as the path
    from the entry, built when they are flattened."""

    name: str
    kind: Kind
    where: Locator
    children: list["Item"] = field(default_factory=list)
    readings: tuple[ConfigScalar, ...] = ()
    text: str | None = None
    tag: str | None = None
    why: str | None = None
    repeated: bool = False
    count: int = 0
    order: int = 0  # its position among its parent's children

    def child(self, name: str) -> "Item | None":
        """The first entry named ``name`` of a mapping."""
        if self.kind is not Kind.MAPPING:
            return None
        return next((c for c in self.children if c.name == name), None)

    @property
    def is_matrix(self) -> bool:
        return self.tag in (OPENCV_MATRIX, OPENCV_MATRIX_TAG)


def from_document(document: Document) -> Item | None:
    """The document's tree; ``None`` if it holds no node."""
    items: list[Item] = []
    for node in document.nodes:
        parent = items[node.parent] if node.parent >= 0 else None
        where: Locator = Span(*node.span) if node.span is not None else _inherit(parent, document)
        name = str(node.path[-1]) if node.path else ""
        value = node.value
        item = Item(name, Kind.SCALAR, where, text=node.text, tag=node.tag)
        item.repeated, item.order = node.repeated, node.order
        match value:
            case Collection(type=kind, length=length):
                item.kind = Kind.MAPPING if kind is CollectionType.MAPPING else Kind.SEQUENCE
                item.count = length
            case Alias(anchor=anchor):
                item.kind, item.why = Kind.ALIAS, f"an alias to the anchor {anchor!r}"
            case Null():
                item.kind = Kind.NULL
            case ReadValue(readings=readings):
                item.readings = readings
            case Unreadable(reason=reason):
                item.kind, item.why = Kind.UNREAD, reason
        if parent is not None:
            parent.children.append(item)
        items.append(item)
    return items[0] if items else None


def _inherit(parent: Item | None, document: Document) -> Locator:
    return parent.where if parent is not None else Span(*document.extent)


def count_items(root: Item) -> int:
    total, stack = 0, [root]
    while stack:
        item = stack.pop()
        total += 1
        stack.extend(item.children)
    return total


# --- OpenCV FileStorage XML --------------------------------------------------------------------

MAX_DIGITS: Final = 400  # past binary64 and far below Python's own int-from-text limit
_INT: Final = re.compile(r"[+-]?[0-9]+")
_FLOAT: Final = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NON_FINITE: Final = {
    ".nan": NonFinite.NAN,
    ".inf": NonFinite.POSITIVE_INFINITY,
    "+.inf": NonFinite.POSITIVE_INFINITY,
    "-.inf": NonFinite.NEGATIVE_INFINITY,
}


def xml_scalar(text: str) -> ConfigScalar:
    """OpenCV's reading of an XML value: an integer, a real (``.nan``, ``.inf`` included) or the
    text. XML has no types of its own. A number beyond what a record holds (an integer of more
    than ``MAX_DIGITS`` digits, a real past binary64) is the text as written, never a guessed
    value."""
    token = text.strip()
    if _INT.fullmatch(token) and len(token) <= MAX_DIGITS:
        return ConfigScalar(ScalarType.INT, int(token))
    if _FLOAT.fullmatch(token) and not _INT.fullmatch(token):
        number = float(token)
        if math.isfinite(number):
            return ConfigScalar(ScalarType.FLOAT, number)
        return ConfigScalar(ScalarType.STRING, text)
    special = _NON_FINITE.get(token.lower())
    if special is not None:
        return ConfigScalar(ScalarType.FLOAT, special)
    return ConfigScalar(ScalarType.STRING, text)


class XmlRefused(Exception):
    """The XML is not read, and why: the reason names a finding."""

    def __init__(self, name: str, message: str, offset: int = 0) -> None:
        super().__init__(message)
        self.name = name
        self.message = message
        self.offset = offset


@dataclass
class _Open:
    item: Item
    start: int
    texts: list[str]
    cost: int  # characters of this element's path from the root
    elements: int = 0


@dataclass(frozen=True)
class XmlLimits:
    max_depth: int
    max_items: int
    max_array: int
    max_scalar: int
    max_path_cost: int  # characters every element's path may total


def _tag_end(data: bytes, start: int) -> int:
    """One past the ``>`` that closes the tag at ``start``, skipping quoted attribute values."""
    quote = 0
    for i in range(start, len(data)):
        byte = data[i]
        if quote:
            quote = 0 if byte == quote else quote
        elif byte in (0x22, 0x27):
            quote = byte
        elif byte == 0x3E:
            return i + 1
    return len(data)


def read_xml(data: bytes, limits: XmlLimits) -> Item:
    """The XML document's root element as an item, with byte-range locators.

    An element with child elements is a mapping (a sequence if every child is ``_``, which is how
    OpenCV writes one); one without is a scalar of its text. The ``data`` of an
    ``opencv-matrix`` is a sequence of its whitespace-separated numbers, all cited by the
    element. A DTD is refused (no entity is ever expanded). Raises ``XmlRefused`` or
    ``expat.ExpatError`` as ``XmlRefused``; ASCII-compatible encodings only.
    """
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.buffer_text = True
    stack: list[_Open] = []
    root: list[Item] = []
    seen = [0, 0]  # elements and data numbers read; characters of every path

    def refuse_doctype(*_: object) -> None:
        raise XmlRefused("dtd_refused", "the XML declares a DTD or an entity, which is never read")

    def start(name: str, attributes: dict[str, str]) -> None:
        if len(stack) >= limits.max_depth:
            raise XmlRefused(
                "too_deep", f"elements nest deeper than max_depth ({limits.max_depth})"
            )
        seen[0] += 1
        if seen[0] > limits.max_items:
            raise XmlRefused("too_many_values", f"over max_items ({limits.max_items}) values")
        cost = (stack[-1].cost if stack else 0) + len(name) + 1
        seen[1] += cost
        if seen[1] > limits.max_path_cost:
            raise XmlRefused("paths_too_long", "the elements' paths total too many characters")
        offset = parser.CurrentByteIndex
        item = Item(name, Kind.SCALAR, ByteRange(offset, 0))
        item.order = stack[-1].elements if stack else 0
        if attributes.get("type_id") == OPENCV_MATRIX:
            item.tag = OPENCV_MATRIX
        if stack:
            stack[-1].elements += 1
        else:
            root.append(item)
        stack.append(_Open(item, offset, [], cost))

    def end(name: str) -> None:
        opened = stack.pop()
        item = opened.item
        index = parser.CurrentByteIndex
        closing = data[index : index + 2] == b"</"  # else an empty element: <x/> ends as it starts
        stop = _tag_end(data, index if closing else opened.start)
        item.where = ByteRange(opened.start, stop - opened.start)
        text = "".join(opened.texts)
        parent = stack[-1] if stack else None
        if opened.elements:
            kids = item.children
            item.kind = (
                Kind.SEQUENCE if kids and all(kid.name == "_" for kid in kids) else Kind.MAPPING
            )
            item.count = len(kids)
            if item.kind is Kind.SEQUENCE:
                for position, kid in enumerate(kids):
                    kid.name = str(position)
        elif name == "data" and parent is not None and parent.item.tag == OPENCV_MATRIX:
            _matrix_data(item, text, limits, seen)
        elif len(text) > limits.max_scalar:
            item.kind, item.why = Kind.UNREAD, "a scalar over max_scalar_length"
        else:
            item.text = text.strip()
            item.readings = (xml_scalar(text),)
        if parent is not None:
            parent.item.children.append(item)

    def characters(text: str) -> None:
        if stack and not stack[-1].elements:
            stack[-1].texts.append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.EntityDeclHandler = refuse_doctype
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise XmlRefused("syntax_error", str(exc), exc.offset) from exc
    if not root:
        raise XmlRefused("no_document", "the XML holds no element")
    return root[0]


def _matrix_data(item: Item, text: str, limits: XmlLimits, seen: list[int]) -> None:
    tokens = text.split()
    item.text = None
    if len(tokens) > limits.max_array:
        item.kind, item.why, item.count = Kind.UNREAD, "array_too_large", len(tokens)
        return
    seen[0] += len(tokens)
    if seen[0] > limits.max_items:
        raise XmlRefused("too_many_values", f"over max_items ({limits.max_items}) values")
    item.kind, item.count = Kind.SEQUENCE, len(tokens)
    item.children = [
        Item(str(i), Kind.SCALAR, item.where, readings=(xml_scalar(t),), text=t)
        for i, t in enumerate(tokens)
    ]
