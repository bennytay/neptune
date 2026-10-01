"""XMP packets: their RDF properties, one row per value, as the packet declares them.

A packet becomes one ``StructuredTable`` named ``XMP`` citing its exact bytes, with columns
``namespace`` (the property's namespace URI), ``path`` and ``value``, and one ``StructuredRecord``
per value in document order, cited ``[packet, Row(r)]``. Paths follow the XMP specification's
path syntax with the prefixes the packet declares: ``tiff:Make``, a struct field
``Iptc4xmpCore:CreatorContactInfo/Iptc4xmpCore:CiCity``, an array item ``dc:subject[2]``, a
qualifier ``dc:title[1]/?xml:lang``. Values are the text as written; blank text is ``Unknown``.
Nothing is converted: a date stays its text and a number its digits.

The packet is parsed by expat with namespaces on and no DTD: a ``<!DOCTYPE`` (the way to entity
expansion bombs and external entities) refuses the packet, as do malformed XML and nesting past
64 elements, each an ``image.xmp_unreadable`` finding. Every element spends the structure budget
and every row the entry budget.
"""

from dataclasses import dataclass, field
from typing import Final
from xml.parsers import expat

from neptune.adapters.image._context import Context, NotCopied, Skipped
from neptune.adapters.image._emit import XMP_UNREADABLE, CellInput
from neptune.adapters.image._space import LimitHit, Space
from neptune.model.ids import RecordId
from neptune.model.knowledge import AssertionKind, Known, NotCovered
from neptune.model.provenance import Locator

RDF: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XML: Final = "http://www.w3.org/XML/1998/namespace"
XMP_HEADER: Final = ("namespace", "path", "value")
MAX_DEPTH: Final = 64
_ARRAYS: Final = frozenset({"Bag", "Seq", "Alt"})


@dataclass(frozen=True)
class _Name:
    uri: str
    local: str
    prefix: str

    @property
    def qualified(self) -> str:
        return f"{self.prefix}:{self.local}" if self.prefix else self.local

    def is_rdf(self, local: str) -> bool:
        return self.uri == RDF and self.local == local


def _name(text: str) -> _Name:
    parts = text.split(" ")
    if len(parts) == 3:
        return _Name(parts[0], parts[1], parts[2])
    if len(parts) == 2:
        return _Name(parts[0], parts[1], "")
    return _Name("", text, "")


@dataclass
class _Node:
    name: _Name
    attributes: list[tuple[_Name, str]]
    children: list["_Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


class _Refused(Exception):
    pass


def _parse(ctx: Context, data: bytes) -> _Node:
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.namespace_prefixes = True
    parser.ordered_attributes = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    root = _Node(_Name("", "", ""), [])
    stack = [root]

    def start(name: str, attributes: list[str]) -> None:
        if len(stack) > MAX_DEPTH:
            raise _Refused(f"elements nest deeper than {MAX_DEPTH}")
        ctx.budget.structure()
        pairs = [(_name(attributes[i]), attributes[i + 1]) for i in range(0, len(attributes), 2)]
        node = _Node(_name(name), pairs)
        stack[-1].children.append(node)
        stack.append(node)

    def end(name: str) -> None:
        stack.pop()

    def text(data: str) -> None:
        stack[-1].text.append(data)

    def doctype(*_: object) -> None:
        raise _Refused("it declares a DTD")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = doctype
    parser.Parse(data.rstrip(b"\x00"), True)
    return root


def _find_rdf(node: _Node) -> _Node | None:
    for child in node.children:
        if child.name.is_rdf("RDF"):
            return child
        found = _find_rdf(child)
        if found is not None:
            return found
    return None


def read(ctx: Context, space: Space, what: str) -> RecordId | None:
    """The table of the XMP packet that is all of ``space``; ``None`` if it is unreadable."""
    out = ctx.out
    whole = space.whole()
    if not out.first("XMP packet", whole):
        return None
    try:
        root = _parse(ctx, space.read(0, space.size))
    except LimitHit as hit:
        ctx.stopped(hit)
        return None
    except (expat.ExpatError, _Refused, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, _Refused) else f"it is not well-formed XML ({exc})"
        out.finding(XMP_UNREADABLE, whole, f"the XMP packet in {what} is not read: {reason}")
        return None
    rdf = _find_rdf(root)
    if rdf is None:
        out.finding(XMP_UNREADABLE, whole, f"the XMP packet in {what} has no rdf:RDF element")
        return None
    table = out.table(
        whole, Known("XMP"), XMP_HEADER, f"the XMP packet in {what}", AssertionKind.STATED
    )
    if table is None:
        return None
    rows = _Rows(ctx, table, whole)
    try:
        for description in rdf.children:
            if description.name.is_rdf("Description"):
                rows.fields(description, "")
    except LimitHit as hit:
        ctx.stopped(hit)
    ctx.not_copied(whole, XMP_HEADER, rows.skipped)
    return table


class _Rows:
    def __init__(self, ctx: Context, table: RecordId, locator: tuple[Locator, ...]) -> None:
        self.ctx = ctx
        self.table = table
        self.locator = locator
        self.count = 0
        self.keep = ctx.max_value_bytes
        self.skipped = Skipped()

    def emit(self, namespace: str, path: str | None, value: str) -> None:
        """One row. A text over ``max_value_bytes`` (a path is ``None`` once it is) is not copied:
        its cell is ``NotCovered`` and one finding for the packet names the first such cells."""
        self.ctx.budget.entry()
        cells: list[CellInput] = []
        for column, text in enumerate((namespace, path, value)):
            if text is None:
                self.skipped.add(self.count, column, NotCopied(None))
                cells.append(NotCovered())
                continue
            size = _bytes_over(text, self.keep)
            if size is None:
                cells.append(text)
            else:
                self.skipped.add(self.count, column, NotCopied(size))
                cells.append(NotCovered())
        self.ctx.out.row(self.table, self.locator, self.count, cells)
        self.count += 1

    def _add(self, path: str | None, piece: str) -> str | None:
        """``path`` followed by ``piece``, or ``None`` once it would pass ``max_value_bytes``.

        The longer path is never built, so a deep tree of long names costs no more than its
        names; characters are at most bytes, so a path of more characters is over.
        """
        if path is None or len(path) + len(piece) > self.keep:
            return None
        joined = path + piece
        return joined if _bytes_over(joined, self.keep) is None else None

    def _join(self, path: str | None, name: str) -> str | None:
        return self._add(path, name if path == "" else f"/{name}")

    def fields(self, node: _Node, path: str | None) -> None:
        """The fields of a struct (or of a top-level ``rdf:Description``): attributes, children."""
        for name, value in node.attributes:
            if name.uri not in (RDF, XML):
                self.emit(name.uri, self._join(path, name.qualified), value)
        for child in node.children:
            self.value(child, self._join(path, child.name.qualified), child.name.uri)

    def value(self, node: _Node, path: str | None, namespace: str) -> None:
        """One property element: a simple value, a URI, a struct or an array."""
        rdf = {name.local: value for name, value in node.attributes if name.uri == RDF}
        for name, value in node.attributes:
            if name.uri == XML:
                self.emit(namespace, self._add(path, f"/?xml:{name.local}"), value)
        resource = rdf.get("resource")
        if resource is not None:
            self.emit(namespace, path, resource)
            return
        fields = [(n, v) for n, v in node.attributes if n.uri not in (RDF, XML)]
        if rdf.get("parseType") == "Resource" or fields:
            for name, value in fields:
                self.emit(name.uri, self._join(path, name.qualified), value)
            for child in node.children:
                self.value(child, self._join(path, child.name.qualified), child.name.uri)
            return
        if not node.children:
            self.emit(namespace, path, "".join(node.text))
            return
        for child in node.children:
            if child.name.uri == RDF and child.name.local in _ARRAYS:
                items = [item for item in child.children if item.name.is_rdf("li")]
                for index, item in enumerate(items, 1):
                    self.value(item, self._add(path, f"[{index}]"), namespace)
            elif child.name.is_rdf("Description"):
                self.fields(child, path)
            else:
                self.value(child, self._join(path, child.name.qualified), child.name.uri)


def _bytes_over(text: str, keep: int) -> int | None:
    """The UTF-8 size of ``text`` if it is more than ``keep`` bytes, else ``None``."""
    if len(text) * 4 <= keep:
        return None
    size = len(text.encode())
    return size if size > keep else None
