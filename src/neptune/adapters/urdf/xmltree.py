"""A hardened XML reader that keeps every element's exact bytes, and a writer that records them.

``parse`` reads UTF-8 XML with the standard library's expat into ``Element``s, each knowing the
half-open byte range ``[start, end)`` of its markup in the parsed bytes, so a record can cite the
element exactly (ADR 0039 §5). Nothing a document declares is ever expanded: a DOCTYPE, and so
every entity declaration and external reference, stops the parse (``XmlError``), as do a declared
encoding other than UTF-8, nesting deeper than ``max_depth``, more than ``max_elements``
elements and an attribute or text run longer than ``MAX_VALUE_CHARS``. Expat's own
amplification protection never comes into play, because no entity is ever defined.

``serialize`` writes a tree back as canonical XML and fills in each element's range in the bytes
it wrote: that is how an expansion's records cite the expansion (ADR 0039 §4). Given a budget, it
stops with ``TooLarge`` as soon as it has written more, so it never holds much more than that.
"""

import xml.parsers.expat
from dataclasses import dataclass, field
from typing import Final

# The longest attribute value or text run read, in characters. Real descriptions stay far below.
MAX_VALUE_CHARS: Final = 64 * 1024
_UTF8_NAMES: Final = frozenset({"utf-8", "utf8", "us-ascii", "ascii"})


@dataclass(eq=False)
class Element:
    """One element: its name, attributes in document order, children, and its bytes' range.

    ``children`` holds elements and text runs in order (comments and processing instructions are
    not kept). ``start`` and ``end`` locate the element's markup, start tag to end tag, in the
    bytes it was parsed from or serialised to; ``-1`` before it has any. ``unresolved`` names
    the attributes (and ``#text`` for its text) an expansion could not resolve, with the state
    they take: ``not_covered`` or ``unknown``.
    """

    tag: str
    attributes: list[tuple[str, str]]
    children: list["Element | str"] = field(default_factory=list)
    start: int = -1
    end: int = -1
    unresolved: dict[str, str] = field(default_factory=dict)

    def attribute(self, name: str) -> str | None:
        for key, value in self.attributes:
            if key == name:
                return value
        return None

    def elements(self) -> list["Element"]:
        return [child for child in self.children if isinstance(child, Element)]

    def text(self) -> str:
        return "".join(child for child in self.children if isinstance(child, str))


class XmlError(Exception):
    """The bytes are not a document this reader accepts. ``code`` names the finding to emit."""

    def __init__(self, code: str, message: str, details: dict[str, int | str]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _Refused(Exception):
    def __init__(self, error: XmlError) -> None:
        super().__init__(error.message)
        self.error = error


def _start_tag_end(data: bytes, start: int) -> int:
    """The offset just past the ``>`` ending the start tag at ``start``; quotes may hold ``>``."""
    quote = 0
    for index in range(start + 1, len(data)):
        byte = data[index]
        if quote:
            if byte == quote:
                quote = 0
        elif byte in (0x22, 0x27):  # " and '
            quote = byte
        elif byte == 0x3E:  # >
            return index + 1
    return len(data)  # unreachable for markup expat accepted


def parse(data: bytes, *, max_depth: int, max_elements: int) -> Element:
    """The document's root element, or ``XmlError`` saying why the bytes are refused."""
    parser = xml.parsers.expat.ParserCreate(encoding="UTF-8")
    parser.ordered_attributes = True
    parser.buffer_text = True
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[Element] = []
    tag_ends: list[int] = []
    roots: list[Element] = []
    count = 0

    def refuse(code: str, message: str, **details: int | str) -> None:
        raise _Refused(XmlError(code, message, {"offset": parser.CurrentByteIndex, **details}))

    def declaration(version: str | None, encoding: str | None, standalone: int) -> None:
        if encoding is not None and encoding.lower() not in _UTF8_NAMES:
            refuse(
                "encoding_unsupported",
                "the document declares an encoding other than UTF-8; it is not read",
                encoding=encoding[:64],
            )

    def doctype(name: str, system: str | None, public: str | None, internal: bool) -> None:
        refuse(
            "doctype_refused",
            "the document has a DOCTYPE: entity and DTD declarations are never processed,"
            " so it is not read",
        )

    def start(tag: str, attributes: list[str]) -> None:
        nonlocal count
        count += 1
        if count > max_elements:
            refuse("limit_exceeded", "more elements than max_elements", limit=max_elements)
        if len(stack) >= max_depth:
            refuse("limit_exceeded", "elements nest deeper than max_depth", limit=max_depth)
        pairs = list(zip(attributes[::2], attributes[1::2], strict=True))
        if any(len(value) > MAX_VALUE_CHARS for _, value in pairs):
            refuse("limit_exceeded", "an attribute is longer than the limit", limit=MAX_VALUE_CHARS)
        offset = parser.CurrentByteIndex
        element = Element(tag, pairs, start=offset)
        if stack:
            stack[-1].children.append(element)
        else:
            roots.append(element)
        stack.append(element)
        tag_ends.append(_start_tag_end(data, offset))

    def end(tag: str) -> None:
        element, tag_end = stack.pop(), tag_ends.pop()
        if data[tag_end - 2 : tag_end] == b"/>":
            element.end = tag_end
        else:
            closing = data.find(b">", parser.CurrentByteIndex)
            element.end = closing + 1

    def characters(text: str) -> None:
        if not stack:
            return
        children = stack[-1].children
        last = children[-1] if children else None
        if isinstance(last, str):  # expat delivers one run of text in pieces: join them
            children.pop()
            text = last + text
        if len(text) > MAX_VALUE_CHARS:
            refuse("limit_exceeded", "a text run is longer than the limit", limit=MAX_VALUE_CHARS)
        children.append(text)

    def entity(*_: object) -> None:
        refuse("doctype_refused", "the document declares an entity; it is not read")

    def external(context: str, base: str | None, system: str | None, public: str | None) -> int:
        refuse("doctype_refused", "the document refers to an external entity; it is not read")
        return 0

    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.UnparsedEntityDeclHandler = entity
    parser.ExternalEntityRefHandler = external
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(data, True)
    except _Refused as refused:
        raise refused.error from None
    except xml.parsers.expat.ExpatError as exc:
        # The message names no expat text, so the output does not depend on expat's version.
        raise XmlError(
            "xml_malformed",
            f"the bytes are not well-formed XML from line {exc.lineno}; nothing is read",
            {"column": exc.offset, "expat_error": exc.code, "line": exc.lineno},
        ) from None
    return roots[0]


# --- Writing -----------------------------------------------------------------------------------


def _escape(text: str, attribute: bool) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\r", "&#13;")
    if attribute:
        text = text.replace('"', "&quot;").replace("\t", "&#9;").replace("\n", "&#10;")
    return text


class TooLarge(Exception):
    """The serialisation passed its budget; what was written is dropped."""


class _Writer:
    def __init__(self, budget: int | None) -> None:
        self.parts: list[bytes] = []
        self.size = 0
        self.budget = budget

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        self.size += len(data)
        if self.budget is not None and self.size > self.budget:
            raise TooLarge(self.budget)
        self.parts.append(data)

    def element(self, element: Element, depth: int) -> None:
        indent = "  " * depth
        element.start = self.size
        attributes = "".join(f' {k}="{_escape(v, True)}"' for k, v in element.attributes)
        children = [c for c in element.children if not (isinstance(c, str) and not c.strip())]
        if not children:
            self.write(f"<{element.tag}{attributes}/>")
        elif all(isinstance(child, Element) for child in children):
            self.write(f"<{element.tag}{attributes}>")
            for child in children:
                assert isinstance(child, Element)
                self.write("\n" + indent + "  ")
                self.element(child, depth + 1)
            self.write(f"\n{indent}</{element.tag}>")
        else:  # text, or text mixed with elements: written exactly, with no added whitespace
            self.write(f"<{element.tag}{attributes}>")
            for child in element.children:
                if isinstance(child, str):
                    self.write(_escape(child, False))
                else:
                    self.element(child, depth + 1)
            self.write(f"</{element.tag}>")
        element.end = self.size


XML_DECLARATION: Final = '<?xml version="1.0" encoding="UTF-8"?>\n'


def serialize(root: Element, budget: int | None = None) -> bytes:
    """``root`` as canonical UTF-8 XML; sets every element's ``start`` and ``end`` in it.

    Attributes keep their order, whitespace-only text between elements is replaced by one newline
    and two spaces per level, any other text is written exactly, and nothing else is added. The
    same tree always gives the same bytes. ``TooLarge`` once more than ``budget`` bytes are written.
    """
    writer = _Writer(budget)
    writer.write(XML_DECLARATION)
    writer.element(root, 0)
    writer.write("\n")
    return b"".join(writer.parts)
