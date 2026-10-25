"""The compiler's records of ``static/`` become facts (spec §5, §7).

``map_record(record, package)`` is called once per record, in package order. Each corpus file is
recognised by its path under ``static/``; anything else maps to nothing.

Tabular files (the compiler's ``StructuredRecord`` rows, as ``site/corpus/static.py`` writes
them; row 1 is the header), all values verbatim, citing ``row N col "<column as written>"`` with
``N`` counted as a spreadsheet does (header = row 1). Columns are matched case-blind, spaces as
``_``. An equipment (header = row 1). Columns are matched case-blind, spaces as ``_``. An equipment
tag (``CV-2``) names ``<type>:<tag lowercased>``, the type from the asset register row whose name
carries the tag (``conveyor`` -> ``machine``, ``... robot ...`` -> ``robot``). A person is
``person:<name lowercased>``.

- ``cmms/assets.csv``: ``Asset,Name,Type,Serial,Location`` -> ``serial``, ``name`` on
  ``<type>:<Asset>``, the CMMS id (``robot:MR-1142``).
- ``cmms/work_orders.csv``: ``WO,Asset,Equipment,Opened,Closed,Problem,Action,Cause,Done by,LOTO,
  Note`` -> ``work_order`` on the equipment, ``wo_*`` on ``work_order:<WO>``; ``valid_from`` =
  ``Opened``.
- ``changes/changes.csv``: ``Change,Equipment,Parameter,From,To,Unit,When,Requested by,Reason`` ->
  ``change`` on the equipment; ``name`` (the parameter), ``change_from`` / ``change_to`` (with
  ``Unit``), ``change_by`` and ``change_reason`` on ``change:<Change>``; ``valid_from`` = ``When``;
  source ``cmms`` (the change log is a maintenance-system export).
- ``roster/shift_roster.csv``: ``Person,Role,Shift,Start,End,Break start,Break end`` -> ``name`` and
  ``has_role`` (``role:<role, spaces as ->``) from a person's first row; per row ``on_shift``
  ``shift:<Shift>`` [Start, End) and ``on_break`` [Break start, Break end).
- ``roster/qualifications.csv``: ``Person,Qualification,Granted`` -> ``holds_qualification``
  (``valid_from`` = ``Granted`` as written).

PDFs under ``docs/`` (``DocumentBlock``s): ``document:<file stem up to "_">``. A value is stated
only where it is in a block verbatim, citing ``page P block B`` (``B`` counted within the page)
and the block text as the quote. A block's first text is the document's ``name``; a clause
``4.2 <text>`` (``§`` optional) makes ``rule:<document id lowercased>-4.2`` with ``rule_text`` =
the text; a range with a unit (``45-55 N``, hyphen or en dash) is ``states_limit``; a qualification
held in ``roster/qualifications.csv`` named in the block (``conveyor-lockout`` or
``conveyor lockout``) is ``requires_qualification`` of the rule, or of the document outside a rule.

Recorded model output, ``photos/<name>.extraction.json`` (read through the compiler's configuration
records)::

    {"image": "<name>.png", "model": "<model id>", "prompt": "...", "prompt_sha256": "<hex>",
     "regions": [{"id": 1, "box": [x0, y0, x1, y1], "text": "CV-2: PE3 dirty — clean every shift"}]}

Each region is an inferred ``note`` with ``model`` = ``<model id>@<prompt sha256[:12]>``, citing
``region x0,y0,x1,y1`` of the image (no quote). It is about the equipment its text starts with
(``CV-2:`` -> ``machine:cv-2``), else about the image as a document; ``taken``, when present, is
``valid_from``.
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

_RULE = re.compile(r"^§?\s*(\d+(?:\.\d+)+)\s+(.+)$", re.DOTALL)
_DASH = "\N{EN DASH}-"  # en dash first: a trailing "-" stays literal in a class
_LIMIT = re.compile(
    rf"(?<![\w.{_DASH}])\d+(?:\.\d+)?\s?[{_DASH}]\s?\d+(?:\.\d+)?\s?"
    r"(?:N|Nm|mm|cm|m|m/s|kg|bar|°C|%)(?![\w/])"
)
_ASSETS = "cmms/assets.csv"
_ROSTER = "roster/shift_roster.csv"
_QUALIFICATIONS = "roster/qualifications.csv"


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
        return tabular(_Row(f"{STATIC}/{path}", record, index.headers[record.table]), index)
    if isinstance(record, DocumentBlock) and path.startswith("docs/"):
        return _document_block(path, record, index)
    if isinstance(record, ConfigurationSnapshot) and path.endswith(".extraction.json"):
        return _extraction(path, index.config.get(record.id, []), index)
    return []


# --- The package index ------------------------------------------------------------------------


@dataclass
class _Index:
    paths: dict[str, str] = field(default_factory=dict)  # content id -> path under static/
    headers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # table id -> header
    blocks: dict[str, tuple[int, int]] = field(default_factory=dict)  # block id -> page, block
    config: dict[str, list[ConfigurationValue]] = field(default_factory=dict)
    qualifications: tuple[str, ...] = ()
    assets: tuple[tuple[str, str], ...] = ()  # (name, type) per asset register row
    first_row: dict[str, int] = field(default_factory=dict)  # roster person -> first data row


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
    assets: list[tuple[str, str]] = []
    for record in sorted(rows, key=lambda r: r.row):
        path = index.paths.get(str(record.provenance.evidence.source))
        if record.row == 0 or path not in (_ASSETS, _ROSTER, _QUALIFICATIONS):
            continue
        row = _Row(f"{STATIC}/{path}", record, index.headers[record.table])
        if path == _QUALIFICATIONS:
            held.add(row["qualification"])
        elif path == _ASSETS:
            assets.append((row["name"], row["type"]))
        else:
            index.first_row.setdefault(row["person"].lower(), row.number)
    index.qualifications = tuple(sorted(q for q in held if q))
    index.assets = tuple(assets)
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


def _kind_of(asset_type: str) -> str:
    """The site entity type of an asset register ``Type``."""
    return "robot" if "robot" in asset_type.lower() else "machine"


def _equipment(tag: str, index: _Index) -> str:
    """``CV-2`` -> ``machine:cv-2``: typed by the asset whose name carries the tag."""
    for name, asset_type in index.assets:
        if tag in name.split():
            return f"{_kind_of(asset_type)}:{tag.lower()}"
    return f"machine:{tag.lower()}"


def _person(name: str) -> str:
    return f"person:{name.lower()}"


# --- Tables -----------------------------------------------------------------------------------


def _key(column: str) -> str:
    return column.strip().lower().replace(" ", "_")


class _Row:
    """One data row: cells by column key, and the facts it states, each citing its cell."""

    def __init__(self, file: str, record: StructuredRecord, header: tuple[str, ...]) -> None:
        self.file, self.number = file, record.row + 1
        self.columns = {_key(name): name for name in header}
        self.cells = {
            _key(name): _text(cell) for name, cell in zip(header, record.cells, strict=False)
        }

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
        written = self.columns.get(column, column)
        cite = Citation(self.file, f'row {self.number} col "{written}"', self[column])
        value = obj if obj is not None else Value(self[column])
        return Fact(subject, predicate, value, valid_from, valid_to, "stated", source, (cite,))


def _or_none(text: str) -> str | None:
    return text or None


def _assets(row: _Row, index: _Index) -> list[Fact]:
    asset = f"{_kind_of(row['type'])}:{row['asset']}"
    return [
        row.fact("cmms", asset, predicate, predicate)
        for predicate in ("name", "serial")
        if row[predicate]
    ]


def _work_orders(row: _Row, index: _Index) -> list[Fact]:
    wo, opened = f"work_order:{row['wo']}", _or_none(row["opened"])
    asset = _equipment(row["equipment"], index)
    out = [row.fact("cmms", asset, "work_order", "equipment", Ref(wo), opened)]
    for predicate, column in (
        ("wo_opened", "opened"),
        ("wo_closed", "closed"),
        ("wo_problem", "problem"),
        ("wo_action", "action"),
        ("wo_cause", "cause"),
        ("wo_loto", "loto"),
        ("wo_note", "note"),
    ):
        if row[column]:
            out.append(row.fact("cmms", wo, predicate, column, valid_from=opened))
    if row["done_by"]:
        by = Ref(_person(row["done_by"]))
        out.append(row.fact("cmms", wo, "wo_by", "done_by", by, opened))
    return out


def _changes(row: _Row, index: _Index) -> list[Fact]:
    change, at = f"change:{row['change']}", _or_none(row["when"])
    asset = _equipment(row["equipment"], index)
    unit = _or_none(row["unit"])
    out = [row.fact("cmms", asset, "change", "change", Ref(change), at)]
    if row["parameter"]:
        out.append(row.fact("cmms", change, "name", "parameter", valid_from=at))
    for predicate, column in (("change_from", "from"), ("change_to", "to")):
        if row[column]:
            value = Value(row[column], unit)
            out.append(row.fact("cmms", change, predicate, column, value, at))
    if row["requested_by"]:
        by = Ref(_person(row["requested_by"]))
        out.append(row.fact("cmms", change, "change_by", "requested_by", by, at))
    if row["reason"]:
        out.append(row.fact("cmms", change, "change_reason", "reason", valid_from=at))
    return out


def _roster(row: _Row, index: _Index) -> list[Fact]:
    person = _person(row["person"])
    out: list[Fact] = []
    if index.first_row.get(row["person"].lower()) == row.number:
        out.append(row.fact("roster", person, "name", "person"))
        if row["role"]:
            role = Ref("role:" + row["role"].replace(" ", "-"))
            out.append(row.fact("roster", person, "has_role", "role", role))
    if row["shift"]:
        shift = Ref(f"shift:{row['shift']}")
        start, end = _or_none(row["start"]), _or_none(row["end"])
        out.append(row.fact("roster", person, "on_shift", "shift", shift, start, end))
    if row["break_start"]:
        brk = Value("break")
        start, end = row["break_start"], _or_none(row["break_end"])
        out.append(row.fact("roster", person, "on_break", "break_start", brk, start, end))
    return out


def _qualifications(row: _Row, index: _Index) -> list[Fact]:
    qualification = Ref(f"qualification:{row['qualification']}")
    granted = _or_none(row["granted"])
    person = _person(row["person"])
    return [
        row.fact("roster", person, "holds_qualification", "qualification", qualification, granted)
    ]


_TABLES: dict[str, Callable[[_Row, _Index], list[Fact]]] = {
    _ASSETS: _assets,
    "cmms/work_orders.csv": _work_orders,
    "changes/changes.csv": _changes,
    _ROSTER: _roster,
    _QUALIFICATIONS: _qualifications,
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


def _extraction(path: str, values: Iterable[ConfigurationValue], index: _Index) -> list[Fact]:
    scalars = {v.path: v.text.value for v in values if isinstance(v.text, Known)}
    model = f"{scalars[('model',)]}@{scalars[('prompt_sha256',)][:12]}"
    image = PurePosixPath(STATIC, path).parent / scalars[("image",)]
    taken = scalars.get(("taken",))
    kind: Kind = "inferred"
    out: list[Fact] = []
    region = 0
    while ("regions", region, "text") in scalars:
        box = ",".join(scalars[("regions", region, "box", i)] for i in range(4))
        cite = Citation(str(image), f"region {box}", None)
        text = scalars[("regions", region, "text")]
        tag, colon, _ = text.partition(":")
        subject = _equipment(tag.strip(), index) if colon else f"document:{image.stem}"
        out.append(Fact(subject, "note", Value(text), taken, None, kind, "photos", (cite,), model))
        region += 1
    return out
