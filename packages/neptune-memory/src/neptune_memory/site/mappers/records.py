"""The compiler's records of ``static/`` become facts (spec §5, §7).

``map_record(record, package)`` is called once per record, in package order. Each corpus file is
recognised by its path under ``static/``; anything else maps to nothing.

Tabular files (the compiler's ``StructuredRecord`` rows; row 1 is the header), all values verbatim,
citing ``row N col "<column>"`` with ``N`` counted as a spreadsheet does (header = row 1):

- ``cmms/assets.csv``: ``asset,type,name,serial`` -> ``serial``, ``name`` on ``<type>:<asset>``.
- ``cmms/work_orders.csv``: ``wo,asset_type,asset,opened,closed,action,cause,by,loto`` ->
  ``work_order`` on the asset, ``wo_*`` on ``work_order:<wo>``; ``valid_from`` = ``opened``.
- ``changes/changes.csv``: ``change,asset_type,asset,at,description`` -> ``change`` on the asset,
  ``name`` (the description) on ``change:<change>``; ``valid_from`` = ``at``.
- ``roster/shift_roster.csv``: ``person,name,role,shift,start,end,break_start,break_end`` ->
  ``name``, ``has_role``, ``on_shift`` [start, end) and ``on_break`` [break_start, break_end).
- ``roster/qualifications.csv``: ``person,qualification,granted`` -> ``holds_qualification``
  (``valid_from`` = ``granted``, blank = not stated).

PDFs under ``docs/`` (``DocumentBlock``s): ``document:<file stem up to "_">``. A value is stated
only where it is in a block verbatim, citing ``page P block B`` (``B`` counted within the page)
and the block text as the quote. A block's first text is the document's ``name``;
``§4.2 <clause>`` makes ``rule:<document id lowercased>-4.2`` with ``rule_text`` = the clause; a
range with a unit (``45-55 N``, hyphen or en dash) is ``states_limit``; a qualification held in
``roster/qualifications.csv`` named in the block (``conveyor-lockout`` or ``conveyor lockout``) is
``requires_qualification`` of the rule, or of the document outside a rule.

Recorded model output, ``photos/<name>.extraction.json`` (read through the compiler's configuration
records)::

    {"image": "<name>.png", "model": "<model id>", "prompt_sha256": "<hex>",
     "taken": "2026-10-20T06:15:00", "regions": [{"box": [x0, y0, x1, y1],
     "subject": "machine:cv-2", "text": "CV-2: PE3 dirty — clean every shift"}]}

Each region is an inferred ``note`` with ``model`` = ``<model id>@<prompt sha256[:12]>``, citing
``region x0,y0,x1,y1`` of the image (no quote).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from neptune.model.configuration import ConfigurationSnapshot, ConfigurationValue
from neptune.model.knowledge import Known
from neptune.model.provenance import Page
from neptune.model.source import LocalPath, SourceRevision
from neptune.model.world import DocumentBlock, StructuredRecord
from neptune_memory.site.facts import Citation, Fact, Kind, Ref, Value
from neptune_memory.site.mappers.layout import STATIC

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

_RULE = re.compile(r"^§\s*(\d+(?:\.\d+)*)\s+(.+)$", re.DOTALL)
_DASH = "\N{EN DASH}-"  # en dash first: a trailing "-" stays literal in a class
_LIMIT = re.compile(
    rf"(?<![\w.{_DASH}])\d+(?:\.\d+)?\s?[{_DASH}]\s?\d+(?:\.\d+)?\s?"
    r"(?:N|Nm|mm|cm|m|m/s|kg|bar|°C|%)(?![\w/])"
)


def map_record(record: Any, package: Any) -> list[Fact]:
    """The facts one compiler record states about the site; ``[]`` for records that state none."""
    index = _index(package)
    provenance = getattr(record, "provenance", None)
    if provenance is None:
        return []
    path = index.paths.get(str(provenance.evidence.source))
    if path is None:
        return []
    if isinstance(record, StructuredRecord):
        tabular = _TABLES.get(path)
        if tabular is None or record.row == 0:
            return []
        return tabular(_Row(f"{STATIC}/{path}", record, index.headers[record.table]))
    if isinstance(record, DocumentBlock) and path.startswith("docs/"):
        return _document_block(path, record, index)
    if isinstance(record, ConfigurationSnapshot) and path.endswith(".extraction.json"):
        return _extraction(path, index.config.get(record.id, []))
    return []


# --- The package index ------------------------------------------------------------------------


@dataclass
class _Index:
    paths: dict[str, str] = field(default_factory=dict)  # content id -> path under static/
    headers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # table id -> header
    blocks: dict[str, tuple[int, int]] = field(default_factory=dict)  # block id -> page, block
    config: dict[str, list[ConfigurationValue]] = field(default_factory=dict)
    qualifications: tuple[str, ...] = ()


_CACHE: dict[str, _Index] = {}


def _index(package: Any) -> _Index:
    """What a record needs from the rest of its package, built once per package."""
    cached = _CACHE.get(package.id)
    if cached is not None:
        return cached
    index = _Index()
    rows: list[StructuredRecord] = []
    pages: dict[tuple[str, int], list[DocumentBlock]] = defaultdict(list)
    for record in package.records:
        if isinstance(record, SourceRevision) and isinstance(record.location, LocalPath):
            index.paths[record.content_id] = record.location.path
        elif isinstance(record, StructuredRecord):
            rows.append(record)
            if record.row == 0:
                index.headers[record.table] = tuple(_text(cell) for cell in record.cells)
        elif isinstance(record, DocumentBlock):
            pages[(record.document, _page(record))].append(record)
        elif isinstance(record, ConfigurationValue):
            index.config.setdefault(record.snapshot, []).append(record)
    for (_, page), blocks in pages.items():
        for number, block in enumerate(sorted(blocks, key=lambda b: b.order), start=1):
            index.blocks[block.id] = (page + 1, number)
    held: set[str] = set()
    for row in rows:
        if index.paths.get(str(row.provenance.evidence.source)) == "roster/qualifications.csv":
            header = index.headers[row.table]
            if row.row > 0 and "qualification" in header:
                held.add(_text(row.cells[header.index("qualification")]))
    index.qualifications = tuple(sorted(q for q in held if q))
    _CACHE.clear()
    _CACHE[package.id] = index
    return index


def _text(cell: Any) -> str:
    return str(cell.value) if isinstance(cell, Known) else ""


def _page(block: DocumentBlock) -> int:
    for step in block.provenance.evidence.locator:
        if isinstance(step, Page):
            return step.index
    return 0


# --- Tables -----------------------------------------------------------------------------------


class _Row:
    """One data row: cells by column name, and the facts it states, each citing its cell."""

    def __init__(self, file: str, record: StructuredRecord, header: tuple[str, ...]) -> None:
        self.file, self.number = file, record.row + 1
        self.cells = {name: _text(cell) for name, cell in zip(header, record.cells, strict=False)}

    def __getitem__(self, column: str) -> str:
        return self.cells.get(column, "")

    def fact(
        self,
        source: str,
        subject: str,
        predicate: str,
        column: str,
        obj: Ref | Value | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> Fact:
        cite = Citation(self.file, f'row {self.number} col "{column}"', self[column])
        value = obj if obj is not None else Value(self[column])
        return Fact(subject, predicate, value, valid_from, valid_to, "stated", source, (cite,))


def _or_none(text: str) -> str | None:
    return text or None


def _assets(row: _Row) -> list[Fact]:
    asset = f"{row['type']}:{row['asset']}"
    return [
        row.fact("cmms", asset, predicate, column)
        for predicate, column in (("name", "name"), ("serial", "serial"))
        if row[column]
    ]


def _work_orders(row: _Row) -> list[Fact]:
    wo, opened = f"work_order:{row['wo']}", _or_none(row["opened"])
    asset = f"{row['asset_type']}:{row['asset']}"
    out = [row.fact("cmms", asset, "work_order", "asset", Ref(wo), opened)]
    for predicate, column in (
        ("wo_opened", "opened"),
        ("wo_closed", "closed"),
        ("wo_action", "action"),
        ("wo_cause", "cause"),
        ("wo_loto", "loto"),
    ):
        if row[column]:
            out.append(row.fact("cmms", wo, predicate, column, valid_from=opened))
    if row["by"]:
        out.append(row.fact("cmms", wo, "wo_by", "by", Ref(f"person:{row['by']}"), opened))
    return out


def _changes(row: _Row) -> list[Fact]:
    change, at = f"change:{row['change']}", _or_none(row["at"])
    asset = f"{row['asset_type']}:{row['asset']}"
    out = [row.fact("changes", asset, "change", "asset", Ref(change), at)]
    if row["description"]:
        out.append(row.fact("changes", change, "name", "description", valid_from=at))
    return out


def _roster(row: _Row) -> list[Fact]:
    person = f"person:{row['person']}"
    out: list[Fact] = []
    if row["name"]:
        out.append(row.fact("roster", person, "name", "name"))
    if row["role"]:
        out.append(row.fact("roster", person, "has_role", "role", Ref(f"role:{row['role']}")))
    if row["shift"]:
        shift = Ref(f"shift:{row['shift']}")
        start, end = _or_none(row["start"]), _or_none(row["end"])
        out.append(row.fact("roster", person, "on_shift", "shift", shift, start, end))
    if row["break_start"]:
        brk = Value("break")
        start, end = row["break_start"], _or_none(row["break_end"])
        out.append(row.fact("roster", person, "on_break", "break_start", brk, start, end))
    return out


def _qualifications(row: _Row) -> list[Fact]:
    qualification = Ref(f"qualification:{row['qualification']}")
    granted = _or_none(row["granted"])
    person = f"person:{row['person']}"
    return [
        row.fact("roster", person, "holds_qualification", "qualification", qualification, granted)
    ]


_TABLES: dict[str, Callable[[_Row], list[Fact]]] = {
    "cmms/assets.csv": _assets,
    "cmms/work_orders.csv": _work_orders,
    "changes/changes.csv": _changes,
    "roster/shift_roster.csv": _roster,
    "roster/qualifications.csv": _qualifications,
}


# --- Documents --------------------------------------------------------------------------------


def _document_block(path: str, block: DocumentBlock, index: _Index) -> list[Fact]:
    if not isinstance(block.text, Known) or not block.text.value.strip():
        return []
    text = block.text.value
    document = "document:" + PurePosixPath(path).stem.split("_", 1)[0]
    page, number = index.blocks[block.id]
    cite = (Citation(f"{STATIC}/{path}", f"page {page} block {number}", text),)

    def stated(subject: str, predicate: str, obj: Ref | Value) -> Fact:
        return Fact(subject, predicate, obj, None, None, "stated", "docs", cite)

    out: list[Fact] = []
    if block.order == 0:
        out.append(stated(document, "name", Value(text.strip())))
    holder = document
    rule = _RULE.match(text.strip())
    if rule:
        holder = f"rule:{document.split(':', 1)[1].lower()}-{rule.group(1)}"
        out.append(stated(holder, "rule_text", Value(rule.group(2).strip())))
    out.extend(stated(document, "states_limit", Value(m.group(0))) for m in _LIMIT.finditer(text))
    lowered = text.lower()
    for qualification in index.qualifications:
        if qualification.lower() in lowered or qualification.replace("-", " ").lower() in lowered:
            qref = Ref(f"qualification:{qualification}")
            out.append(stated(holder, "requires_qualification", qref))
    return out


# --- Recorded model output --------------------------------------------------------------------


def _extraction(path: str, values: Iterable[ConfigurationValue]) -> list[Fact]:
    scalars = {v.path: v.text.value for v in values if isinstance(v.text, Known)}
    model = f"{scalars[('model',)]}@{scalars[('prompt_sha256',)][:12]}"
    image = str(PurePosixPath(STATIC, path).parent / scalars[("image",)])
    taken = scalars.get(("taken",))
    kind: Kind = "inferred"
    out: list[Fact] = []
    region = 0
    while ("regions", region, "text") in scalars:
        box = ",".join(scalars[("regions", region, "box", i)] for i in range(4))
        cite = Citation(image, f"region {box}", None)
        subject = scalars[("regions", region, "subject")]
        note = Value(scalars[("regions", region, "text")])
        out.append(Fact(subject, "note", note, taken, None, kind, "photos", (cite,), model))
        region += 1
    return out
