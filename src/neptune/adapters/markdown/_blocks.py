"""CommonMark's blocks with exact spans: markdown-it-py's block parser, its leaf rules recorded.

markdown-it-py (CommonMark, plus the GFM table rule) finds the blocks. Its tokens carry line
ranges, not offsets, so each leaf rule is wrapped: when one matches, the wrapper records every line
it consumed as markdown-it saw it inside its containers, ``[content start, line end)``, the
content start being past any blockquote marker, list marker and indentation. A block's span is
then exact, in the parser's own terms, without re-implementing container prefixes:

- paragraph, setext heading: from the first content character of the first line to the last
  non-blank character of the last content line;
- ATX heading: its text, after the opening ``#`` run and before an optional closing run;
- fenced and indented code: its content lines whole, from where the first one begins inside its
  containers (indentation included) to the last one's end;
- HTML block: from the first line's content start to the last non-blank character;
- link reference definition: from its first character to its last non-blank one; CommonMark
  declares it, the model has no role for it, so the block's role is ``Unknown``;
- table: from the header row's first character to the last row's last non-blank character, and
  each row and cell by the GFM rule (cells split at unescaped pipes, then trimmed).

Inline content is never parsed (the ``inline`` core rule is off): emphasis, links and code spans
stay in the block's text as written. Offsets are into the parser's normalized text (line endings
as LF, NUL as U+FFFD), which has the source's lines and columns; ``Lines`` maps them back.
"""

import bisect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from markdown_it import MarkdownIt
from markdown_it.rules_block import (
    StateBlock,
    code,
    fence,
    heading,
    html_block,
    lheading,
    paragraph,
    reference,
    table,
)
from markdown_it.parser_block import _rules as _BLOCK_RULES
from markdown_it.token import Token

from neptune.model.world import BlockRole

MAX_NESTING: Final = 64
_LEAVES: Final = (
    "paragraph",
    "heading",
    "table",
    "fence",
    "code_block",
    "html_block",
    "definition",
)
NEWLINES: Final = re.compile(r"\r\n?|\n")
# Which blocks each rule may interrupt: ``Ruler.at`` resets these, so the wrapper restores them.
_INTERRUPTS: Final = {name: {"alt": list(alt)} for name, _, alt in _BLOCK_RULES}
_ROLES: Final[dict[str, BlockRole]] = {
    "fence": BlockRole.CODE,
    "code_block": BlockRole.CODE,
    "heading": BlockRole.HEADING,
    "table": BlockRole.TABLE,
}


@dataclass(frozen=True)
class Recorded:
    """One leaf rule's match: its name and each line's ``[content start, end)`` in parser text,
    with where each line begins inside its containers (before its indentation)."""

    rule: str
    first: int
    lines: tuple[tuple[int, int], ...]
    begins: tuple[int, ...]


@dataclass(frozen=True)
class Cell:
    start: int
    end: int
    value: str  # trimmed, an escaped pipe unescaped (the GFM table rule's reading)


@dataclass(frozen=True)
class Row:
    start: int
    end: int
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class Block:
    """A leaf block: what CommonMark says it is, and its span in the source text."""

    start: int
    end: int
    role: BlockRole | None
    level: int | None
    leveled: bool
    rows: tuple[Row, ...] = ()  # tables: header first, then body rows; the delimiter row is not one
    lines: tuple[int, int] = (0, 0)
    definition: bool = False  # a link reference definition


@dataclass
class Parsed:
    blocks: list[Block] = field(default_factory=list)
    nesting_cut: list[tuple[int, int]] = field(default_factory=list)  # line ranges never parsed


class Lines:
    """Line starts of the source text and of the parser's text, to map offsets between them.

    Both texts have the same lines: the parser's has LF line endings, NUL as U+FFFD, and front
    matter lines emptied. A column within a line is the same in both.
    """

    def __init__(self, source: str, parsed: str) -> None:
        self.source = [0, *(match.end() for match in NEWLINES.finditer(source))]
        self.parser = [0, *(index + 1 for index, char in enumerate(parsed) if char == "\n")]
        if len(self.source) != len(self.parser):
            raise ValueError("the parser's text must have the source's lines")

    def to_source(self, offset: int) -> int:
        line = bisect.bisect_right(self.parser, offset) - 1
        return self.source[line] + offset - self.parser[line]


def normalize(text: str) -> str:
    """The text as markdown-it parses it (its ``normalize`` core rule)."""
    return NEWLINES.sub("\n", text).replace("\0", "\ufffd")


def _recording(
    name: str, rule: Callable[[StateBlock, int, int, bool], bool], into: dict[int, Recorded]
) -> Callable[[StateBlock, int, int, bool], bool]:
    def run(state: StateBlock, start: int, end: int, silent: bool) -> bool:
        matched = rule(state, start, end, silent)
        if matched and not silent:
            consumed = range(start, state.line)
            lines = tuple(
                (state.bMarks[line] + state.tShift[line], state.eMarks[line]) for line in consumed
            )
            into[start] = Recorded(
                name, start, lines, tuple(state.bMarks[line] for line in consumed)
            )
        return matched

    return run


def parser(records: dict[int, Recorded]) -> MarkdownIt:
    """CommonMark plus GFM tables, inline parsing off, each leaf rule recording into ``records``."""
    options = {"maxNesting": MAX_NESTING, "inline_definitions": True}
    md = MarkdownIt("commonmark", options).enable("table")
    md.disable(["inline", "text_join"], ignoreInvalid=True)
    for name, rule in (
        ("table", table),
        ("code", code),
        ("fence", fence),
        ("reference", reference),
        ("html_block", html_block),
        ("heading", heading),
        ("lheading", lheading),
        ("paragraph", paragraph),
    ):
        md.block.ruler.at(name, _recording(name, rule, records), _INTERRUPTS[name])
    return md


def _trimmed_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in " \t":
        end -= 1
    return end


def _split_row(text: str, start: int, end: int) -> Row:
    """A table row's cells by the GFM rule: split at unescaped pipes, drop the enclosing pipes'
    empty ends, trim each cell."""
    while start < end and text[start] in " \t":
        start += 1
    end = _trimmed_end(text, start, end)
    bounds: list[tuple[int, int]] = []
    segment, escaped = start, False
    for position in range(start, end):
        char = text[position]
        if char == "|" and not escaped:
            bounds.append((segment, position))
            segment = position + 1
        escaped = char == "\\"
    bounds.append((segment, end))
    if bounds and bounds[0][0] == bounds[0][1]:
        bounds.pop(0)
    if bounds and bounds[-1][0] == bounds[-1][1]:
        bounds.pop()
    cells = []
    for low, high in bounds:
        while low < high and text[low] in " \t":
            low += 1
        high = _trimmed_end(text, low, high)
        raw = text[low:high]
        cells.append(Cell(low, high, raw.replace("\\|", "|")))
    return Row(start, end, tuple(cells))


def _closes(text: str, line: tuple[int, int], markup: str) -> bool:
    """Whether a recorded line is a closing fence for an opening ``markup``."""
    stripped = text[line[0] : line[1]].strip()
    return len(stripped) >= len(markup) and set(stripped) == {markup[0]}


def _span(
    leaf: str, token: Token, content: str, record: Recorded, text: str
) -> tuple[int, int] | None:
    """The block's ``[start, end)`` in the parser's text, or ``None`` if it holds no text."""
    lines, begins = record.lines, record.begins
    if leaf == "heading" and record.rule == "heading":  # ATX: the text between the markers
        start, end = lines[0]
        at = text.find(content, start + len(token.markup), end) if content else -1
        return (at, at + len(content)) if at >= 0 else None
    if leaf == "heading":  # setext: every line but the underline
        lines = lines[:-1]
    elif leaf == "fence":
        lines, begins = lines[1:], begins[1:]
        if lines and _closes(text, lines[-1], token.markup):
            lines = lines[:-1]
    if not lines:
        return None
    start = lines[0][0]
    if leaf in ("fence", "code_block"):
        start, end = begins[0], lines[-1][1]
        return (start, end) if text[start:end].strip() else None
    end = _trimmed_end(text, start, lines[-1][1])
    return (start, end) if end > start else None


def parse(text: str) -> Parsed:
    """The leaf blocks of ``text`` (already normalized) in document order, with their roles."""
    records: dict[int, Recorded] = {}
    tokens = parser(records).parse(text)
    parsed = Parsed()
    containers: list[str] = []
    lists = 0
    for index, token in enumerate(tokens):
        kind = token.type
        if kind in ("blockquote_open", "list_item_open"):
            containers.append("quote" if kind == "blockquote_open" else "item")
            if token.level + 1 >= MAX_NESTING and token.map:
                parsed.nesting_cut.append((token.map[0], token.map[1]))
            continue
        if kind in ("blockquote_close", "list_item_close"):
            containers.pop()
            continue
        if kind in ("bullet_list_open", "ordered_list_open"):
            lists += 1
            continue
        if kind in ("bullet_list_close", "ordered_list_close"):
            lists -= 1
            continue
        leaf = kind.removesuffix("_open")
        if leaf not in _LEAVES:
            continue
        if kind == leaf and leaf in ("paragraph", "heading", "table"):
            continue  # a close token
        if token.map is None or token.map[0] not in records:
            continue
        record = records[token.map[0]]
        inline = leaf in ("paragraph", "heading")
        content = tokens[index + 1].content if inline else token.content
        span = _span(leaf, token, content, record, text)
        if span is None:
            continue
        role, level, leveled = _role(leaf, token, containers, lists)
        rows: tuple[Row, ...] = ()
        if leaf == "table":
            body = [line for number, line in enumerate(record.lines) if number != 1]
            rows = tuple(_split_row(text, low, high) for low, high in body)
        parsed.blocks.append(
            Block(
                span[0],
                span[1],
                role,
                level,
                leveled,
                rows,
                (token.map[0], token.map[1]),
                leaf == "definition",
            )
        )
    return parsed


def _role(
    leaf: str, token: Token, containers: list[str], lists: int
) -> tuple[BlockRole | None, int | None, bool]:
    if leaf == "heading":
        return BlockRole.HEADING, int(token.tag[1]), True
    if leaf in _ROLES:
        return _ROLES[leaf], None, False
    if leaf in ("html_block", "definition"):
        return None, None, False
    innermost = containers[-1] if containers else None
    if innermost == "item":
        return BlockRole.LIST_ITEM, lists, True
    if innermost == "quote":
        return BlockRole.QUOTE, None, False
    return BlockRole.PARAGRAPH, None, False
