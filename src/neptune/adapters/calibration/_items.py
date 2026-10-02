"""A calibration document as a tree of items, from YAML, JSON, TOML or OpenCV's XML.

Every format reader makes the same ``Item``: a name, a kind, the place it is written, its
children in source order and, for a scalar, its readings in the format's own schema (two where
YAML 1.1 and 1.2 differ). Nothing here knows what a camera matrix is; ``_formats`` and ``_emit``
do. Trees are built without recursion and never expand a YAML alias.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from xml.parsers import expat

from neptune.adapters.structured.tree import Alias, Collection, Document, Null, Unreadable
from neptune.adapters.structured.tree import Value as ReadValue
from neptune.model.configuration import CollectionType, ConfigScalar, ScalarType
from neptune.model.provenance import ByteRange, Locator, Span
from neptune.model.scalars import NonFinite, real

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
    """One value: ``name`` is its key (a sequence item's position); ``path`` the names from the
    root. ``where`` is where it is written; ``count`` is how many items a collection declares."""

    name: str
    path: tuple[str, ...]
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
        path = (*parent.path, name) if parent is not None else ()
        value = node.value
        item = Item(name, path, Kind.SCALAR, where, text=node.text, tag=node.tag)
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
    text. XML has no types of its own; the declared text is kept beside it by the caller."""
    token = text.strip()
    if _INT.fullmatch(token):
        return ConfigScalar(ScalarType.INT, int(token))
    if _FLOAT.fullmatch(token):
        return ConfigScalar(ScalarType.FLOAT, real(float(token)))
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
    elements: int = 0


def read_xml(data: bytes, max_depth: int, max_items: int, max_array: int) -> Item:
    """The XML document's root element as an item, with byte-range locators.

    An element with child elements is a mapping (a sequence if every child is ``_``, which is how
    OpenCV writes one); one without is a scalar of its text. The ``data`` of an
    ``opencv-matrix`` is a sequence of its whitespace-separated numbers, all cited by the
    element. A DTD is refused (no entity is ever expanded). Raises ``XmlRefused`` or
    ``expat.ExpatError``.
    """
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.buffer_text = True
    stack: list[_Open] = []
    root: list[Item] = []
    seen = [0]

    def refuse_doctype(*_: object) -> None:
        raise XmlRefused("dtd_refused", "the XML declares a DTD or an entity, which is never read")

    def start(name: str, attributes: dict[str, str]) -> None:
        if len(stack) >= max_depth:
            raise XmlRefused("too_deep", f"elements nest deeper than max_depth ({max_depth})")
        seen[0] += 1
        if seen[0] > max_items:
            raise XmlRefused("too_many_values", f"over max_items ({max_items}) elements")
        offset = parser.CurrentByteIndex
        parent = stack[-1].item if stack else None
        path = (*parent.path, name) if parent is not None else ()
        item = Item(name, path, Kind.SCALAR, ByteRange(offset, 0))
        item.order = stack[-1].elements if stack else 0
        if attributes.get("type_id") == OPENCV_MATRIX:
            item.tag = OPENCV_MATRIX
        stack.append(_Open(item, offset, []))
        if parent is not None:
            stack[-2].elements += 1
        else:
            root.append(item)

    def end(name: str) -> None:
        opened = stack.pop()
        item = opened.item
        close = data.find(b">", parser.CurrentByteIndex)
        item.where = ByteRange(
            opened.start, (close if close >= 0 else len(data) - 1) + 1 - opened.start
        )
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
                    kid.path = (*item.path, kid.name)
        elif name == "data" and parent is not None and parent.item.tag == OPENCV_MATRIX:
            _matrix_data(item, text, max_array)
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


def _matrix_data(item: Item, text: str, max_array: int) -> None:
    tokens = text.split()
    item.text = None
    if len(tokens) > max_array:
        item.kind, item.why, item.count = Kind.UNREAD, "array_too_large", len(tokens)
        return
    item.kind, item.count = Kind.SEQUENCE, len(tokens)
    item.children = [
        Item(
            str(i), (*item.path, str(i)), Kind.SCALAR, item.where, readings=(xml_scalar(t),), text=t
        )
        for i, t in enumerate(tokens)
    ]
