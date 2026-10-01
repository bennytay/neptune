"""The small subset of YAML that a rosbag2 ``metadata.yaml`` is written in, with byte spans.

rosbag2 writes its metadata with yaml-cpp: block mappings and sequences, plain and quoted
scalars, the odd empty flow collection. PyYAML is not a dependency (ADR 0045 §3) and would not give
the byte span a citation needs, so this parser reads that subset and refuses the rest with an
error naming the line: anchors and aliases, tags, directives, complex keys, flow collections that
nest or span lines, scalars that continue on the next line, tabs as indentation.

Every scalar and key carries the exact bytes it was read from. Nothing is converted: ``Scalar.text``
is the text with its quotes and escapes resolved, and whether a scalar is a number or a null is
the reader's business, since YAML's own typing rules are what a bag's writer does not rely on.
The input is hostile: its size, depth and node count are bounded, and a line the subset cannot read
costs the entry it belongs to, not the file.
"""

from dataclasses import dataclass, field, replace
from typing import Final

MAX_BYTES: Final = 4 * 1024 * 1024
MAX_DEPTH: Final = 32
MAX_NODES: Final = 200_000
BOM: Final = "\N{ZERO WIDTH NO-BREAK SPACE}"

_ESCAPES: Final = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_HEX: Final = {"x": 2, "u": 4, "U": 8}


class YamlError(ValueError):
    """A construct outside the subset, or malformed: the message and the line it was found on."""

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.message = message
        self.line = line


class YamlLimitError(YamlError):
    """The document is larger, deeper or wider than the parser reads; nothing more is read."""


@dataclass(frozen=True)
class Scalar:
    """A scalar and the bytes it was read from (quotes included). ``length`` 0 is an empty value."""

    text: str
    start: int
    length: int
    quoted: bool = False

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass
class Entry:
    key: Scalar
    value: "Node"


@dataclass
class Mapping:
    entries: list[Entry]
    start: int
    end: int
    duplicates: list[str] = field(default_factory=list)

    def get(self, key: str) -> "Node | None":
        for entry in self.entries:
            if entry.key.text == key:
                return entry.value
        return None


@dataclass
class Sequence:
    items: list["Node"]
    start: int
    end: int


Node = Scalar | Mapping | Sequence


def span(node: Node) -> tuple[int, int]:
    """The first byte of a node and the byte after its last."""
    if isinstance(node, Scalar):
        return node.start, node.end
    return node.start, node.end


@dataclass(frozen=True)
class _Line:
    no: int  # 1-based, for messages
    raw: int  # index among all lines
    start: int  # byte offset of the line's first byte
    text: str
    indent: int
    col: int  # where the content starts (equals indent unless a sequence item was unwrapped)
    content: str  # from col, comment and trailing blanks removed


@dataclass
class Document:
    root: Node | None
    errors: list[YamlError]
    lines: list[tuple[int, int]] = field(default_factory=list)  # (offset, length) of each line


def _strip_comment(text: str) -> str:
    """``text`` without a trailing comment (a ``#`` at the start or after a blank, unquoted)."""
    quote = ""
    for i, char in enumerate(text):
        if quote:
            if char == quote and not (quote == '"' and text[i - 1] == "\\"):
                quote = ""
        elif char in "\"'" and (i == 0 or text[i - 1] in " \t[{,:-"):
            quote = char
        elif char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def parse(data: bytes) -> Document:
    """Read ``data``; a document that is not UTF-8 or exceeds the limits has no root."""
    if len(data) > MAX_BYTES:
        return Document(None, [YamlLimitError(f"{len(data)} bytes exceed {MAX_BYTES}", 1)])
    try:
        return _Parser(data).run()
    except YamlLimitError as exc:
        return Document(None, [exc])
    except YamlError as exc:
        return Document(None, [exc])


class _Parser:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.errors: list[YamlError] = []
        self.nodes = 0
        self.raw: list[tuple[int, str]] = []
        self.lines: list[_Line] = []
        self.i = 0
        offset = 0
        for index, piece in enumerate(data.split(b"\n")):
            try:
                text = piece.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise YamlError("the line is not UTF-8", index + 1) from exc
            text = text.removesuffix("\r")
            shift = 0
            if index == 0 and text.startswith(BOM):
                text, shift = text[1:], len(BOM.encode())
            self.raw.append((offset + shift, text))
            offset += len(piece) + 1
        ended = False
        for index, (start, text) in enumerate(self.raw):
            if text.startswith(BOM):
                raise YamlError("a byte order mark inside the document", index + 1)
            stripped = text.lstrip(" ")
            if not stripped.strip() or stripped.startswith("#"):
                continue
            indent = len(text) - len(stripped)
            if stripped.startswith("\t"):
                raise YamlError("a tab is not allowed as indentation", index + 1)
            if ended:
                raise YamlError("content after the document's end marker", index + 1)
            if text.rstrip() == "...":
                ended = True
                continue
            if text.rstrip() == "---":
                if self.lines:
                    raise YamlError("a second document is not supported", index + 1)
                continue
            if text.startswith("%"):
                raise YamlError("a directive is not supported", index + 1)
            self.lines.append(
                _Line(index + 1, index, start, text, indent, indent, _strip_comment(stripped))
            )

    # --- positions ---------------------------------------------------------------------------

    def byte(self, line: _Line, col: int) -> int:
        return line.start + len(line.text[:col].encode())

    def token(self, line: _Line, col: int, text: str) -> Scalar:
        """The plain scalar ``text`` that starts at character ``col`` of ``line``."""
        return Scalar(text, self.byte(line, col), len(text.encode()), False)

    def quoted(self, line: _Line, col: int, source: str) -> Scalar:
        """The quoted scalar written ``source`` (quotes included) at character ``col``."""
        return Scalar(_unquote(source, line.no), self.byte(line, col), len(source.encode()), True)

    # --- the document ------------------------------------------------------------------------

    def run(self) -> Document:
        if not self.lines:
            return Document(None, [])
        root = self.block(self.lines[0].indent, 0)
        if self.i < len(self.lines):
            line = self.lines[self.i]
            self.errors.append(YamlError("content after the document's end", line.no))
        return Document(root, self.errors, [(o, len(t.encode())) for o, t in self.raw])

    def count(self, line: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise YamlLimitError(f"more than {MAX_NODES} nodes", line)

    def block(self, indent: int, depth: int) -> Node:
        if depth > MAX_DEPTH:
            raise YamlLimitError(f"nested deeper than {MAX_DEPTH}", self.lines[self.i].no)
        line = self.lines[self.i]
        if self._is_item(line):
            return self.sequence(line.indent, depth)
        if _split_key(line.content) is not None:
            return self.mapping(line.indent, depth)
        self.i += 1
        return self.value(line.content, line.col, line, line.indent - 1, depth)

    @staticmethod
    def _is_item(line: _Line) -> bool:
        return line.content == "-" or line.content.startswith("- ")

    def _skip(self, indent: int, failed: int) -> None:
        """Move past the entry that failed (it started at index ``failed``) and what it owns."""
        self.i = max(self.i, failed + 1)
        while self.i < len(self.lines) and self.lines[self.i].indent > indent:
            self.i += 1

    def mapping(self, indent: int, depth: int) -> Mapping:
        first = self.lines[self.i]
        entries: list[Entry] = []
        seen: set[str] = set()
        duplicates: list[str] = []
        end = self.byte(first, first.col)
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent < indent or (line.indent == indent and self._is_item(line)):
                break
            at = self.i
            if line.indent > indent:
                self.errors.append(YamlError("unexpected indentation", line.no))
                self._skip(indent, at)
                continue
            split = _split_key(line.content)
            if split is None:
                self.errors.append(YamlError("a line that is not `key: value`", line.no))
                self._skip(indent, at)
                continue
            self.count(line.no)
            key_end, value_at = split
            try:
                if line.content[0] in "\"'":
                    key = self.quoted(line, line.col, line.content[:key_end])
                else:
                    key = self.token(line, line.col, line.content[:key_end])
                self.i += 1
                rest = line.content[value_at:].lstrip(" ")
                col = line.col + len(line.content) - len(rest)
                value = self.value(rest, col, line, indent, depth + 1)
            except YamlLimitError:
                raise
            except YamlError as exc:
                self.errors.append(exc)
                self._skip(indent, at)
                continue
            if key.text in seen:
                duplicates.append(key.text)
            seen.add(key.text)
            entries.append(Entry(key, value))
            end = max(end, span(value)[1], key.end)
        return Mapping(entries, self.byte(first, first.col), end, duplicates)

    def value(self, rest: str, col: int, line: _Line, parent: int, depth: int) -> Node:
        """The value after ``key:`` or ``-`` whose owner is indented ``parent``; ``self.i`` is
        already past the owner's line."""
        self.count(line.no)
        if not rest:
            if self.i < len(self.lines):
                following = self.lines[self.i]
                same_indent_item = (
                    following.indent == parent
                    and self._is_item(following)
                    and not self._is_item(line)
                )
                if following.indent > parent or same_indent_item:
                    return self.block(following.indent, depth)
            return Scalar("", self.byte(line, len(line.text.rstrip())), 0)
        head = rest[0]
        if head in "|>":
            return self.block_scalar(rest, line, parent)
        if head in "[{":
            return self.flow(rest, col, line)
        if head in "&*!%@`":
            raise YamlError(
                f"`{head}` (an anchor, alias, tag or reserved indicator) is not supported",
                line.no,
            )
        if head in "\"'":
            end = _quoted_end(rest, 0)
            if end is None:
                raise YamlError("a quoted scalar must close on its line", line.no)
            if rest[end:].strip():
                raise YamlError("text after a quoted scalar", line.no)
            return self.quoted(line, col, rest[:end])
        if head == "?" and rest[1:2] in ("", " "):
            raise YamlError("a complex key is not supported", line.no)
        return self.token(line, col, rest)

    def sequence(self, indent: int, depth: int) -> Sequence:
        first = self.lines[self.i]
        items: list[Node] = []
        end = self.byte(first, first.col)
        while self.i < len(self.lines):
            line = self.lines[self.i]
            at = self.i
            if line.indent > indent:
                self.errors.append(YamlError("unexpected indentation", line.no))
                self._skip(indent, at)
                continue
            if line.indent != indent or not self._is_item(line):
                break
            self.count(line.no)
            after = line.content[1:]
            rest = after.lstrip(" ")
            try:
                if not rest:
                    self.i += 1
                    node = self.value("", line.col + 1, line, indent, depth + 1)
                else:
                    shift = 1 + len(after) - len(rest)
                    inner = replace(line, indent=indent + shift, col=line.col + shift, content=rest)
                    self.lines[self.i] = inner
                    node = self.block(inner.indent, depth + 1)
            except YamlLimitError:
                raise
            except YamlError as exc:
                self.errors.append(exc)
                self._skip(indent, at)
                continue
            items.append(node)
            end = max(end, span(node)[1])
        return Sequence(items, self.byte(first, first.col), end)

    def flow(self, rest: str, col: int, line: _Line) -> Node:
        """A flow sequence or mapping of scalars that opens and closes on one line."""
        closing = "]" if rest[0] == "[" else "}"
        if not rest.endswith(closing) or len(rest) < 2:
            raise YamlError("a flow collection must close on its line", line.no)
        inner = rest[1:-1]
        if not _quoted_only(inner):
            raise YamlError("a nested flow collection is not supported", line.no)
        start = self.byte(line, col)
        stop = start + len(rest.encode())
        sequence = rest[0] == "["
        items: list[Node] = []
        entries: list[Entry] = []
        offset = col + 1
        for part in _split_commas(inner):
            piece = part.strip()
            lead = len(part) - len(part.lstrip())
            if piece and sequence:
                items.append(self._flow_scalar(piece, offset + lead, line))
            elif piece:
                colon = _colon(piece)
                if colon is None:
                    raise YamlError("a flow mapping entry is `key: value`", line.no)
                key = self._flow_scalar(piece[:colon].strip(), offset + lead, line)
                tail = piece[colon + 1 :]
                gap = len(tail) - len(tail.lstrip())
                value = self._flow_scalar(tail.strip(), offset + lead + colon + 1 + gap, line)
                entries.append(Entry(key, value))
            offset += len(part) + 1
        return Sequence(items, start, stop) if sequence else Mapping(entries, start, stop)

    def _flow_scalar(self, text: str, col: int, line: _Line) -> Scalar:
        if text and text[0] in "\"'":
            end = _quoted_end(text, 0)
            if end is None or text[end:].strip():
                raise YamlError("a malformed quoted scalar in a flow collection", line.no)
            return self.quoted(line, col, text[:end])
        if text and text[0] in "&*!%@`":
            raise YamlError("an anchor, alias or tag is not supported", line.no)
        return self.token(line, col, text)

    def block_scalar(self, header: str, line: _Line, parent: int) -> Scalar:
        """A ``|`` or ``>`` scalar: the lines below the owner that are indented deeper."""
        chomp = "-" if "-" in header else ""
        if header.strip("|>-+0123456789 "):
            raise YamlError("a block scalar header holds more than its indicators", line.no)
        index = line.raw + 1
        block: list[int] = []
        indent: int | None = None
        while index < len(self.raw):
            text = self.raw[index][1]
            stripped = text.lstrip(" ")
            if stripped.strip():
                width = len(text) - len(stripped)
                if width <= parent:
                    break
                indent = width if indent is None else min(indent, width)
            block.append(index)
            index += 1
        while block and not self.raw[block[-1]][1].strip():
            block.pop()
        last = block[-1] if block else line.raw
        while self.i < len(self.lines) and self.lines[self.i].raw <= last:
            self.i += 1
        if not block or indent is None:
            return Scalar("", self.byte(line, len(line.text.rstrip())), 0, True)
        pieces = [self.raw[k][1][indent:] if self.raw[k][1].strip() else "" for k in block]
        if header[0] == "|":
            text = "\n".join(pieces)
        else:
            text = ""
            for piece in pieces:
                joiner = "\n" if not piece else (" " if text and not text.endswith("\n") else "")
                text += joiner + piece
        if chomp != "-":
            text += "\n"
        start = self.raw[block[0]][0]
        stop = self.raw[last][0] + len(self.raw[last][1].encode())
        return Scalar(text, start, stop - start, True)


def _split_key(content: str) -> tuple[int, int] | None:
    """For a ``key: value`` line: where the key ends and where the text after its colon starts."""
    if not content or content[0] in "[{":
        return None
    if content[0] in "\"'":
        end = _quoted_end(content, 0)
        if end is None:
            return None
        rest = content[end:]
        stripped = rest.lstrip(" ")
        if stripped.startswith(":") and (len(stripped) == 1 or stripped[1] == " "):
            return end, end + (len(rest) - len(stripped)) + 1
        return None
    at = content.find(": ")
    if at >= 0:
        return len(content[:at].rstrip()), at + 1
    if content.endswith(":"):
        return len(content) - 1, len(content)
    return None


def _colon(piece: str) -> int | None:
    """The first colon of a flow mapping entry that is not inside a quoted key."""
    start = 0
    if piece[0] in "\"'":
        end = _quoted_end(piece, 0)
        if end is None:
            return None
        start = end
    at = piece.find(":", start)
    return at if at >= 0 else None


def _quoted_end(text: str, start: int) -> int | None:
    """The index after the quote that closes the quoted scalar starting at ``text[start]``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        char = text[i]
        if quote == '"' and char == "\\":
            i += 2
            continue
        if char == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return None


def _unquote(token: str, line: int) -> str:
    body = token[1:-1]
    if token[0] == "'":
        return body.replace("''", "'")
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise YamlError("a backslash ends a quoted scalar", line)
        code = body[i]
        if code in _HEX:
            width = _HEX[code]
            digits = body[i + 1 : i + 1 + width]
            try:
                out.append(chr(int(digits, 16)))
            except (ValueError, OverflowError) as exc:
                raise YamlError(f"a bad \\{code} escape", line) from exc
            if len(digits) != width:
                raise YamlError(f"a bad \\{code} escape", line)
            i += 1 + width
        elif code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 1
        else:
            raise YamlError(f"an unknown escape \\{code}", line)
    return "".join(out)


def _split_commas(text: str) -> list[str]:
    parts, start, quote = [], 0, ""
    for i, char in enumerate(text):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == ",":
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _quoted_only(text: str) -> bool:
    """Whether every bracket in ``text`` is inside a quoted scalar."""
    quote = ""
    for char in text:
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "[]{}":
            return False
    return True
