"""A tagged PDF's logical structure, as one page sees it: which element owns each marked-content id.

A tagged PDF declares its structure in a tree of structure elements (``StructTreeRoot``). Page
content is tied to it by marked-content ids (MCIDs): the page's ``/StructParents`` key selects,
in the root's ``ParentTree`` number tree, an array whose entry ``i`` is the element that owns
MCID ``i``. Element types pass through ``/RoleMap`` to the standard types of ISO 32000.

For each MCID on a page this module gives its **owner**, the element whose block holds it:

- inside a table row (a ``TR`` ancestor), the row's ``Table``: a table is one block, its cells
  addressed by row and column;
- otherwise the nearest ancestor-or-self that is a block-level type with a role: ``P``, ``H``,
  ``H1``-``H6``, ``Title``, ``LI``, ``Caption``, ``Figure``, ``Formula``, ``BlockQuote``,
  ``Note``, ``FENote``, ``TOCI`` (``Lbl`` and ``LBody`` belong to their ``LI``);
- otherwise the top-level element above it, whose role is not declared.

and its **path**, the child indices from the root to the MCID, which orders content in the
structure's reading order. Every walk is bounded in depth and in elements visited; a tree that
loops or exceeds a bound raises ``StructureError`` and the page is read as untagged.
"""

from dataclasses import dataclass, field
from typing import Final

from pypdf.generic import DictionaryObject, IndirectObject

from neptune.model.world import BlockRole

from ._objects import (
    array,
    dictionary,
    entry,
    integer,
    name,
    reference,
    resolve,
)

MAX_DEPTH: Final = 128
MAX_VISITS: Final = 200_000
MAX_ROLE_MAP_HOPS: Final = 16

STANDARD_TYPES: Final = frozenset(
    [
        "Document",
        "DocumentFragment",
        "Part",
        "Art",
        "Sect",
        "Div",
        "Aside",
        "BlockQuote",
        "Caption",
        "TOC",
        "TOCI",
        "Index",
        "NonStruct",
        "Private",
        "H",
        "H1",
        "H2",
        "H3",
        "H4",
        "H5",
        "H6",
        "Title",
        "P",
        "L",
        "LI",
        "Lbl",
        "LBody",
        "Table",
        "TR",
        "TH",
        "TD",
        "THead",
        "TBody",
        "TFoot",
        "Span",
        "Quote",
        "Note",
        "FENote",
        "Reference",
        "BibEntry",
        "Code",
        "Link",
        "Annot",
        "Ruby",
        "RB",
        "RT",
        "RP",
        "Warichu",
        "WT",
        "WP",
        "Figure",
        "Formula",
        "Form",
        "Em",
        "Strong",
        "Sub",
        "Artifact",
    ]
)
OWNER_ROLES: Final[dict[str, BlockRole | None]] = {
    "P": BlockRole.PARAGRAPH,
    "H": BlockRole.HEADING,
    **{f"H{n}": BlockRole.HEADING for n in range(1, 7)},
    "Title": BlockRole.HEADING,
    "LI": BlockRole.LIST_ITEM,
    "Caption": BlockRole.CAPTION,
    "Figure": BlockRole.FIGURE,
    "Formula": BlockRole.FORMULA,
    "BlockQuote": BlockRole.QUOTE,
    "Note": BlockRole.FOOTNOTE,
    "FENote": BlockRole.FOOTNOTE,
    "TOCI": None,
}
TABLE_GROUPS: Final = frozenset({"THead", "TBody", "TFoot"})


class StructureError(Exception):
    """The structure tree cannot be read for this page: missing, looping or past a bound."""


Key = tuple[int, int] | int


@dataclass(frozen=True)
class Owner:
    """The element whose block holds some content, with what it declares the block to be."""

    key: Key
    path: tuple[int, ...]
    kind: str  # the standard type, or the declared type when it maps to none
    role: BlockRole | None
    level: int | None  # a heading's number, a list item's depth


@dataclass(frozen=True)
class Placement:
    """Where one MCID sits: its owner, its own path, and its table cell if in one."""

    owner: Owner
    path: tuple[int, ...]
    row: int | None = None
    column: int | None = None


@dataclass
class Table:
    """A table's rows as the structure declares them, and where each cell's content starts."""

    owner: Owner
    rows: list[list[str]] = field(default_factory=list)  # each cell's type: TH or TD
    row_pages: list[int | None] = field(default_factory=list)  # each row's first content page
    cell_pages: list[list[int | None]] = field(default_factory=list)
    caption: Key | None = None
    first_page: int | None = None


@dataclass
class _Node:
    obj: DictionaryObject
    key: Key
    kind: str
    parent: "_Node | None"
    index: int  # position among the parent's kids


@dataclass
class PageStructure:
    placements: dict[int, Placement] = field(default_factory=dict)
    tables: dict[Key, Table] = field(default_factory=dict)


class Structure:
    """The structure tree of one document as one page needs it, read lazily and bounded.

    Build one per page: its visit budget and caches are then the page's alone, so whether a
    page's tags are read never depends on which other pages share its chunk.
    """

    def __init__(self, catalog: DictionaryObject, page_numbers: dict[tuple[int, int], int]) -> None:
        self._root = dictionary(entry(catalog, "/StructTreeRoot"))
        self._parent_tree = dictionary(entry(self._root, "/ParentTree")) if self._root else None
        self._role_map = dictionary(entry(self._root, "/RoleMap")) if self._root else None
        self._pages = page_numbers
        self._nodes: dict[Key, _Node] = {}
        self._tables: dict[Key, Table] = {}
        self._positions: dict[object, dict[object, int]] = {}
        self._table_rows: dict[Key, list[DictionaryObject]] = {}
        self._visits = 0

    @property
    def tagged(self) -> bool:
        return self._parent_tree is not None

    # --- Bounds and identity ----------------------------------------------------------------

    def _visit(self) -> None:
        self._visits += 1
        if self._visits > MAX_VISITS:
            raise StructureError("the structure tree is larger than the adapter reads")

    @staticmethod
    def _key(obj: object, raw: object) -> Key:
        found = reference(raw) or reference(obj)
        return found if found is not None else id(obj)

    def standard_type(self, declared: str) -> str:
        """``declared`` through the role map, until a standard type or ``MAX_ROLE_MAP_HOPS``."""
        current, seen = declared, {declared}
        for _ in range(MAX_ROLE_MAP_HOPS):
            if current in STANDARD_TYPES:
                return current
            mapped = name(entry(self._role_map, "/" + current)) if self._role_map else None
            if mapped is None or mapped in seen:
                return current
            seen.add(mapped)
            current = mapped
        return current

    # --- The tree, upward -------------------------------------------------------------------

    @staticmethod
    def kids(element: DictionaryObject) -> list[object]:
        """An element's ``/K`` as a list of its raw (unresolved) entries."""
        raw = dict.get(element, "/K")
        resolved = resolve(raw)
        found = array(resolved)
        if found is not None:
            return list(found)
        return [] if resolved is None else [raw]

    def _is_root(self, obj: DictionaryObject) -> bool:
        return obj is self._root or name(entry(obj, "/Type")) == "StructTreeRoot"

    def _node(self, raw: object) -> _Node:
        """The node for an element, built with its ancestors on first use."""
        obj = dictionary(raw)
        if obj is None:
            raise StructureError("a structure parent is not a dictionary")
        key = self._key(obj, raw)
        if key in self._nodes:
            return self._nodes[key]
        chain: list[tuple[DictionaryObject, Key, object]] = [(obj, key, raw)]
        seen = {key}
        while True:
            self._visit()
            current = chain[-1][0]
            parent_raw = dict.get(current, "/P")
            parent = dictionary(parent_raw)
            if parent is None:
                raise StructureError("a structure element has no parent")
            if self._is_root(parent):
                break
            parent_key = self._key(parent, parent_raw)
            if parent_key in self._nodes:
                break
            if parent_key in seen or len(chain) > MAX_DEPTH:
                raise StructureError("the structure tree loops or nests too deep")
            seen.add(parent_key)
            chain.append((parent, parent_key, parent_raw))
        top_parent = dictionary(dict.get(chain[-1][0], "/P"))
        assert top_parent is not None
        above: _Node | None = None
        holder: DictionaryObject = top_parent
        if not self._is_root(top_parent):
            above = self._nodes[self._key(top_parent, dict.get(chain[-1][0], "/P"))]
            holder = above.obj
        for obj_i, key_i, raw_i in reversed(chain):
            index = self._index_in(holder, obj_i, raw_i)
            declared = name(entry(obj_i, "/S")) or ""
            node = _Node(obj_i, key_i, self.standard_type(declared), above, index)
            self._nodes[key_i] = node
            above, holder = node, obj_i
        return self._nodes[key]

    @staticmethod
    def _identity(raw: object, resolved: object) -> object:
        """What names a kid: the reference it is listed by, or the object itself if direct."""
        if isinstance(raw, IndirectObject):
            return (int(raw.idnum), int(raw.generation))
        return id(resolved)

    def _position_of(
        self, holder_key: object, kids: list[object], raw: object, child: object
    ) -> int:
        """``child``'s position among ``kids``, indexed once per holder (the end if unlisted)."""
        positions = self._positions.get(holder_key)
        if positions is None:
            positions = {}
            for index, kid in enumerate(kids):
                self._visit()
                positions.setdefault(self._identity(kid, kid), index)
            self._positions[holder_key] = positions
        found = positions.get(self._identity(raw, child))
        return found if found is not None else len(kids)

    def _index_in(self, holder: DictionaryObject, child: DictionaryObject, raw: object) -> int:
        """The child's position among ``holder``'s kids (the end if it is not listed)."""
        holder_key = "root" if self._is_root(holder) else ("kids", self._key(holder, holder))
        return self._position_of(holder_key, self.kids(holder), raw, child)

    @staticmethod
    def _path(node: _Node) -> tuple[int, ...]:
        path: list[int] = []
        current: _Node | None = node
        while current is not None:
            path.append(current.index)
            current = current.parent
        return tuple(reversed(path))

    @staticmethod
    def _ancestors(node: _Node) -> list[_Node]:
        found: list[_Node] = []
        current: _Node | None = node
        while current is not None:
            found.append(current)
            current = current.parent
        return found

    # --- Owners -----------------------------------------------------------------------------

    def _owner(self, node: _Node) -> tuple[Owner, int | None, int | None]:
        """The owner of content under ``node``, and its row and column if in a table."""
        chain = self._ancestors(node)
        for depth, current in enumerate(chain):
            if current.kind == "TR":
                table = next((n for n in chain[depth + 1 :] if n.kind == "Table"), None)
                if table is not None:
                    cell = chain[depth - 1] if depth > 0 else None
                    shape = self._table(table)
                    row = self._row_index(table, current)
                    column = cell.index if cell is not None else None
                    if column is not None and cell is not None:
                        column = self._cell_index(current, cell)
                    return shape.owner, row, column
        for current in chain:
            if current.kind in OWNER_ROLES:
                return self._make_owner(current, chain), None, None
        return self._make_owner(chain[-1], chain), None, None

    def _make_owner(self, node: _Node, chain: list[_Node]) -> Owner:
        role = OWNER_ROLES.get(node.kind)
        level: int | None = None
        if node.kind in {f"H{n}" for n in range(1, 7)}:
            level = int(node.kind[1])
        elif node.kind == "LI":
            above = chain[chain.index(node) + 1 :]
            level = sum(1 for n in above if n.kind == "L") or None
        return Owner(node.key, self._path(node), node.kind, role, level)

    # --- Tables -----------------------------------------------------------------------------

    def _rows(self, table: _Node) -> list[DictionaryObject]:
        cached = self._table_rows.get(table.key)
        if cached is not None:
            return cached
        rows: list[DictionaryObject] = []
        for raw in self.kids(table.obj):
            self._visit()
            kid = dictionary(raw)
            if kid is None:
                continue
            kind = self.standard_type(name(entry(kid, "/S")) or "")
            if kind == "TR":
                rows.append(kid)
            elif kind in TABLE_GROUPS:
                for inner in self.kids(kid):
                    self._visit()
                    row = dictionary(inner)
                    if row is not None and self.standard_type(name(entry(row, "/S")) or "") == "TR":
                        rows.append(row)
        self._table_rows[table.key] = rows
        return rows

    def _row_index(self, table: _Node, row: _Node) -> int:
        rows: list[object] = list(self._rows(table))
        index = self._position_of(("rows", table.key), rows, row.obj, row.obj)
        if index == len(rows):
            raise StructureError("a table row is not among its table's rows")
        return index

    def _cells(self, row: DictionaryObject) -> list[DictionaryObject]:
        return [kid for kid in (dictionary(raw) for raw in self.kids(row)) if kid is not None]

    def _cell_index(self, row: _Node, cell: _Node) -> int:
        cells: list[object] = list(self._cells(row.obj))
        index = self._position_of(("cells", row.key), cells, cell.obj, cell.obj)
        if index == len(cells):
            raise StructureError("a table cell is not among its row's cells")
        return index

    def _table(self, table: _Node) -> Table:
        found = self._tables.get(table.key)
        if found is not None:
            return found
        shape = Table(Owner(table.key, self._path(table), "Table", BlockRole.TABLE, None))
        for raw in self.kids(table.obj):
            kid = dictionary(raw)
            if kid is not None and self.standard_type(name(entry(kid, "/S")) or "") == "Caption":
                shape.caption = self._key(kid, raw)
                break
        table_page = dictionary(entry(table.obj, "/Pg"))
        for row in self._rows(table):
            kinds, pages = [], []
            row_page = dictionary(entry(row, "/Pg")) or table_page
            for cell in self._cells(row):
                kinds.append(self.standard_type(name(entry(cell, "/S")) or ""))
                pages.append(self._first_page(cell, row_page))
            shape.rows.append(kinds)
            shape.cell_pages.append(pages)
            shape.row_pages.append(next((p for p in pages if p is not None), None))
        shape.first_page = next((p for p in shape.row_pages if p is not None), None)
        self._tables[table.key] = shape
        return shape

    def _first_page(
        self, element: DictionaryObject, inherited: DictionaryObject | None
    ) -> int | None:
        """The page of the first marked content under ``element``, depth first."""
        stack: list[tuple[object, DictionaryObject | None, int]] = [(element, inherited, 0)]
        while stack:
            self._visit()
            raw, page, depth = stack.pop()
            if depth > MAX_DEPTH:
                raise StructureError("a table cell nests too deep")
            if integer(raw) is not None:
                return self._page_number(page)
            obj = dictionary(raw)
            if obj is None:
                continue
            page = dictionary(entry(obj, "/Pg")) or page
            if integer(entry(obj, "/MCID")) is not None:
                return self._page_number(page)
            if name(entry(obj, "/Type")) == "OBJR":
                continue
            stack.extend((kid, page, depth + 1) for kid in reversed(self.kids(obj)))
        return None

    def _page_number(self, page: DictionaryObject | None) -> int | None:
        found = reference(page) if page is not None else None
        return self._pages.get(found) if found is not None else None

    # --- One page ---------------------------------------------------------------------------

    def page(self, page: DictionaryObject) -> PageStructure | None:
        """The placements of every MCID on ``page``, or ``None`` if the page is not tagged."""
        key = integer(entry(page, "/StructParents"))
        if key is None or self._parent_tree is None:
            return None
        parents = array(self._number_tree(self._parent_tree, key))
        if parents is None:
            raise StructureError("the parent tree has no entry for this page")
        result = PageStructure()
        for mcid, raw in enumerate(parents):
            self._visit()
            if dictionary(raw) is None:
                continue
            node = self._node(raw)
            owner, row, column = self._owner(node)
            own = self._mcid_index(node.obj, mcid)
            result.placements[mcid] = Placement(owner, (*self._path(node), own), row, column)
            if owner.role is BlockRole.TABLE and owner.key in self._tables:
                result.tables[owner.key] = self._tables[owner.key]
        return result

    def _mcid_index(self, element: DictionaryObject, mcid: int) -> int:
        kids = self.kids(element)
        for index, raw in enumerate(kids):
            if integer(raw) == mcid or integer(entry(raw, "/MCID")) == mcid:
                return index
        return len(kids)

    def _number_tree(self, node: DictionaryObject, key: int) -> object:
        """``key``'s value in a number tree, walking ``/Kids`` within their ``/Limits``."""
        stack: list[tuple[DictionaryObject, int]] = [(node, 0)]
        while stack:
            self._visit()
            current, depth = stack.pop()
            if depth > MAX_DEPTH:
                raise StructureError("the parent tree nests too deep")
            nums = array(entry(current, "/Nums"))
            if nums is not None:
                for index in range(0, len(nums) - 1, 2):
                    self._visit()
                    if integer(nums[index]) == key:
                        value = nums[index + 1]
                        return value.get_object() if isinstance(value, IndirectObject) else value
            for kid_raw in reversed(array(entry(current, "/Kids")) or []):
                kid = dictionary(kid_raw)
                if kid is None:
                    continue
                limits = [integer(v) for v in array(entry(kid, "/Limits")) or []]
                if len(limits) == 2 and None not in limits:
                    low, high = limits
                    assert low is not None and high is not None
                    if not low <= key <= high:
                        continue
                stack.append((kid, depth + 1))
        return None
