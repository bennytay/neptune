"""A page's blocks and its extracted text: the units every citation into the page counts in.

**Blocks.** On an untagged page, every run is a block, and every painted image is a figure
block. On a tagged page (``_structure``), content whose MCID the structure owns is grouped by its
owner: one block per owner per page, its items ordered by their structure paths, then by
content order. Each ``Artifact`` sequence is one block. Content in neither is one block per run
or image, as on an untagged page. Owner blocks come first, in the structure's order; the rest
follow in content order.

**Text.** A block's runs are joined in order. Between two runs, on a new line (the next run's
origin is more than half a font size off the previous run's baseline) the transform puts a line
feed; on the same line, a gap of at least ``space_threshold`` thousandths of an em puts one
space, unless either side already has whitespace there; otherwise nothing. Runs whose geometry is
unknown are joined by one space. A table block's text is its rows on this page, in row order, a
line feed between rows and a tab between the cells of a row (an empty cell is empty text). A
block with no runs, such as an image, is U+FFFC OBJECT REPLACEMENT CHARACTER in the page text and
has no text of its own.

**The page text** is every block's text in reading order, each followed by a line feed. A block's
span is its text's code points in it; a table cell's and row's spans are inside its block's.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.pdf._content import Box, Item
from neptune.adapters.pdf._structure import Key, Owner, PageStructure, Placement, Table
from neptune.model.world import BlockRole

PLACEHOLDER: Final = "￼"
_HEADER_FOOTER: Final = {"Header": BlockRole.HEADER, "Footer": BlockRole.FOOTER}


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    start: int
    end: int
    text: str
    unmapped: bool
    empty: bool


@dataclass
class Block:
    """One block: what it is, its text and where it is drawn. Spans count in the page text."""

    role: BlockRole | None
    level: int | None
    leveled: bool  # whether a level applies (headings and list items)
    text: str
    textual: bool  # False: no runs, the placeholder stands for it
    unmapped: bool
    box: Box | None
    owner: Owner | None = None
    table: Table | None = None
    start: int = 0
    end: int = 0
    cells: list[Cell] = field(default_factory=list)
    rows: dict[int, tuple[int, int]] = field(default_factory=dict)


def _separator(previous: Item, following: Item, threshold: float) -> str:
    before, after = previous.text or "", following.text or ""
    if previous.line is None or following.line is None:
        gap = " "
    else:
        line = previous.line
        dx = following.line.origin[0] - line.end[0]
        dy = following.line.origin[1] - line.end[1]
        along = dx * line.direction[0] + dy * line.direction[1]
        across = -dx * line.direction[1] + dy * line.direction[0]
        if abs(across) > line.height / 2:
            return "\n"
        gap = " " if abs(along) >= threshold * line.height else ""
    if gap and (before[-1:].isspace() or after[:1].isspace()):
        return ""
    return gap


def join(runs: list[Item], threshold: float) -> str:
    parts: list[str] = []
    for index, run in enumerate(runs):
        if index:
            parts.append(_separator(runs[index - 1], run, threshold))
        parts.append(run.text or "")
    return "".join(parts)


def _box(items: list[Item]) -> Box | None:
    boxes = [item.box for item in items]
    if not boxes or any(box is None for box in boxes):
        return None
    found: Box | None = None
    for box in boxes:
        assert box is not None
        found = box if found is None else found.union(box)
    return found


def _plain(items: list[Item], threshold: float, role: BlockRole | None) -> Block:
    runs = [item for item in items if item.text is not None]
    text = join(runs, threshold) if runs else PLACEHOLDER
    return Block(
        role=role,
        level=None,
        leveled=False,
        text=text,
        textual=bool(runs),
        unmapped=any(run.unmapped for run in runs),
        box=_box(items),
    )


def _table_block(
    owner: Owner, table: Table, items: list[tuple[Placement, Item]], threshold: float
) -> Block:
    by_cell: dict[tuple[int, int], list[Item]] = defaultdict(list)
    loose: list[Item] = []
    for placement, item in items:
        if placement.row is None or placement.column is None:
            loose.append(item)
        else:
            by_cell[(placement.row, placement.column)].append(item)
    rows = sorted({row for row, _ in by_cell})
    lines: list[str] = []
    cells: list[Cell] = []
    spans: dict[int, tuple[int, int]] = {}
    position = 0
    for row in rows:
        width = max(
            len(table.rows[row]) if row < len(table.rows) else 0,
            1 + max(column for r, column in by_cell if r == row),
        )
        texts: list[str] = []
        row_start = position
        for column in range(width):
            runs = [item for item in by_cell.get((row, column), []) if item.text is not None]
            text = join(runs, threshold)
            cells.append(
                Cell(
                    row,
                    column,
                    position,
                    position + len(text),
                    text,
                    any(run.unmapped for run in runs),
                    not runs,
                )
            )
            texts.append(text)
            position += len(text) + 1  # the tab after it, or the line feed after the row
        spans[row] = (row_start, position - 1)
        lines.append("\t".join(texts))
    loose_runs = [item for item in loose if item.text is not None]
    if loose_runs:
        lines.append(join(loose_runs, threshold))
    text = "\n".join(lines)
    every = [item for _, item in items]
    runs = [item for item in every if item.text is not None]
    return Block(
        role=BlockRole.TABLE,
        level=None,
        leveled=False,
        text=text if runs else PLACEHOLDER,
        textual=bool(runs),
        unmapped=any(run.unmapped for run in runs),
        box=_box(every),
        owner=owner,
        table=table,
        cells=cells,
        rows=spans,
    )


def _owner_block(owner: Owner, items: list[tuple[Placement, Item]], threshold: float) -> Block:
    ordered = [item for _, item in sorted(items, key=lambda pair: (pair[0].path, pair[1].sequence))]
    block = _plain(ordered, threshold, owner.role)
    block.owner = owner
    block.level = owner.level
    block.leveled = owner.role in (BlockRole.HEADING, BlockRole.LIST_ITEM)
    return block


def blocks(items: list[Item], structure: PageStructure | None, threshold: float) -> list[Block]:
    """The page's blocks in reading order, with their spans in the page text."""
    owned: dict[Key, list[tuple[Placement, Item]]] = defaultdict(list)
    owners: dict[Key, Owner] = {}
    artifacts: dict[int, list[Item]] = defaultdict(list)
    rest: list[tuple[int, int | None, Item]] = []  # first sequence, artifact, the item
    for item in items:
        placement = None
        if structure is not None and item.mcid is not None and item.artifact is None:
            placement = structure.placements.get(item.mcid)
        if placement is not None:
            owned[placement.owner.key].append((placement, item))
            owners[placement.owner.key] = placement.owner
        elif item.artifact is not None:
            if not artifacts[item.artifact]:
                rest.append((item.sequence, item.artifact, item))
            artifacts[item.artifact].append(item)
        else:
            rest.append((item.sequence, None, item))
    found: list[Block] = []
    for key in sorted(owned, key=lambda k: owners[k].path):
        owner = owners[key]
        table = structure.tables.get(key) if structure is not None else None
        if owner.role is BlockRole.TABLE and table is not None:
            ordered = sorted(owned[key], key=_cell_order)
            found.append(_table_block(owner, table, ordered, threshold))
        else:
            found.append(_owner_block(owner, owned[key], threshold))
    for _, artifact, item in sorted(rest, key=lambda entry: entry[0]):
        if artifact is not None:
            members = artifacts[artifact]
            found.append(_plain(members, threshold, _HEADER_FOOTER.get(item.artifact_kind)))
        else:
            found.append(_plain([item], threshold, BlockRole.FIGURE if item.text is None else None))
    position = 0
    for block in found:
        block.start, block.end = position, position + len(block.text)
        block.cells = [
            Cell(c.row, c.column, c.start + position, c.end + position, c.text, c.unmapped, c.empty)
            for c in block.cells
        ]
        block.rows = {row: (s + position, e + position) for row, (s, e) in block.rows.items()}
        position = block.end + 1
    return found


def _cell_order(pair: tuple[Placement, Item]) -> tuple[int, int, tuple[int, ...], int]:
    placement, item = pair
    return (placement.row or 0, placement.column or 0, placement.path, item.sequence)
