"""The compiler's records of ``static/`` become facts, as the mapping file says (spec §5, §12).

``map_record(record, package, mapping)`` is called once per record, in package order. A record's
file (its path under ``static/``) picks the mapping entry; this module knows no site's paths.

- **Tables** (``StructuredRecord`` rows): a CSV's row 1 is its header, cited
  ``row N col "<column as written>"``; a JSONL line's keys are its columns, cited
  ``line N col "<key>"``. Each declared fact cites its ``column`` cell, verbatim.
- **Documents** (``DocumentBlock``): cited ``page P block B`` (``B`` counted within the page) with
  the block text as the quote. The first block is the document's ``name``; a block matching the
  ``rule`` pattern is a clause; ``lift`` states values found in a block verbatim.
- **Extractions** (recorded model output, read through the compiler's configuration records)::

      {"image": "<name>.png", "model": "<model id>", "prompt_sha256": "<hex>", "taken": "...",
       "regions": [{"id": 1, "box": [x0, y0, x1, y1], "text": "CV-2: PE3 dirty"}]}

  Each region is an inferred fact with ``model`` = ``<model id>@<prompt sha256[:12]>``, citing
  ``region x0,y0,x1,y1`` of the image (no quote), about the ``<tag>:`` its text starts with, else
  the image as a document.
- **Unmapped** tables (CSV, TSV, JSONL, XLSX) and documents: ``document:<path>``
  ``unmapped = "not mapped"``, and one stated ``text`` fact per row or block, so they stay
  searchable.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from neptune.model.configuration import ConfigurationSnapshot, ConfigurationValue
from neptune.model.knowledge import Known, NotApplicable
from neptune.model.provenance import JsonPointer, Page
from neptune.model.source import LocalPath, SourceRevision
from neptune.model.world import DocumentBlock, StructuredRecord, StructuredTable
from neptune_memory.site.facts import Citation, Fact, Kind, Ref, Value
from neptune_memory.site.mappers.config import Mapping, render, tag_ref
from neptune_memory.site.mappers.layout import STATIC

if TYPE_CHECKING:
    from collections.abc import Iterable

UNMAPPED_SOURCE = "files"
TABLE_SUFFIXES = (".csv", ".tsv", ".jsonl", ".xlsx")
"""Files whose rows are logged when unmapped; an image's metadata table is not an export."""


def map_record(record: Any, package: Any, mapping: Mapping) -> list[Fact]:
    """The facts one compiler record states about the site; ``[]`` for records that state none."""
    index = _index(package, mapping)
    provenance = getattr(record, "provenance", None)
    if provenance is None:
        return []
    path = index.paths.get(str(provenance.evidence.source))
    if path is None:
        return []
    if isinstance(record, StructuredRecord):
        row = index.row(path, record)
        entry = mapping.match("tables", path)
        if entry is None:
            return _unmapped_row(path, record, row) if path.endswith(TABLE_SUFFIXES) else []
        return [] if row is None else _table(entry, row, index, mapping)
    if isinstance(record, DocumentBlock | ConfigurationSnapshot) and record.id in index.json_rows:
        table = mapping.match("tables", path)  # a JSON table, read as configuration or as text
        out: list[Fact] = []
        for row in index.json_rows[record.id] if table is not None else []:
            out.extend(_table(table or {}, row, index, mapping))
        return out
    if isinstance(record, DocumentBlock):
        if mapping.match("tables", path) is not None:
            return []
        entry = mapping.match("documents", path)
        if entry is None:
            return _unmapped_block(path, record, index)
        return _document_block(entry, path, record, index, mapping)
    if isinstance(record, ConfigurationSnapshot):
        entry = mapping.match("extractions", path)
        if entry is not None:
            return _extraction(entry, path, index.config.get(record.id, []), index, mapping)
    return []


# --- The package index ------------------------------------------------------------------------


@dataclass
class _Index:
    paths: dict[str, str] = field(default_factory=dict)  # content id -> path under static/
    headers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # table id -> header
    keyed: set[str] = field(default_factory=set)  # tables with no header row (JSONL)
    blocks: dict[str, tuple[int, int]] = field(default_factory=dict)  # block id -> page, block
    config: dict[str, list[ConfigurationValue]] = field(default_factory=dict)
    rows: dict[str, list[_Row]] = field(default_factory=dict)  # path -> data rows, in order
    json_rows: dict[str, list[_Row]] = field(default_factory=dict)  # record id -> JSON rows
    register: tuple[tuple[str, str], ...] = ()  # (name, type) per `tags` register row
    _first: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)

    def row(self, path: str, record: StructuredRecord) -> _Row | None:
        """``record`` as a data row of ``path``; ``None`` for a header row."""
        if record.table in self.keyed:
            header = tuple(_pointer(cell, i) for i, cell in enumerate(record.cells))
            return _Row.tabular(f"{STATIC}/{path}", record, header, "line")
        if record.row == 0:
            return None
        header = self.headers.get(record.table, ())
        return _Row.tabular(f"{STATIC}/{path}", record, header, "row")

    def column(self, path: str, column: str) -> list[str]:
        return [row[_key(column)] for row in self.rows.get(path, [])]

    def first(self, path: str, column: str, value: str) -> int | None:
        """The first row of ``path`` whose ``column`` holds ``value`` (case-blind)."""
        if (path, column) not in self._first:
            first: dict[str, int] = {}
            for row in self.rows.get(path, []):
                first.setdefault(row[column].lower(), row.number)
            self._first[(path, column)] = first
        return self._first[(path, column)].get(value.lower())


_CACHE: dict[tuple[str, int], _Index] = {}


def _index(package: Any, mapping: Mapping) -> _Index:
    """What a record needs from the rest of its package, built once per package and mapping."""
    key = (str(package.id), id(mapping))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    index = _Index()
    records: list[StructuredRecord] = []
    snapshots: list[ConfigurationSnapshot] = []
    pages: dict[tuple[str, int], list[DocumentBlock]] = defaultdict(list)
    for record in package.records:
        if isinstance(record, SourceRevision) and isinstance(record.location, LocalPath):
            index.paths[record.content_id] = record.location.path
        elif isinstance(record, StructuredTable) and isinstance(record.header, NotApplicable):
            index.keyed.add(record.id)
        elif isinstance(record, StructuredRecord):
            records.append(record)
            if record.row == 0:
                index.headers[record.table] = tuple(_text(cell) for cell in record.cells)
        elif isinstance(record, DocumentBlock):
            pages[(record.document, _page(record))].append(record)
        elif isinstance(record, ConfigurationSnapshot):
            snapshots.append(record)
        elif isinstance(record, ConfigurationValue):
            index.config.setdefault(record.snapshot, []).append(record)
    for (_, page), blocks in pages.items():
        for number, block in enumerate(sorted(blocks, key=lambda b: b.order), start=1):
            index.blocks[block.id] = (page + 1, number)
    for record in sorted(records, key=lambda r: r.row):
        path = index.paths.get(str(record.provenance.evidence.source))
        row = None if path is None else index.row(path, record)
        if path is not None and row is not None:
            index.rows.setdefault(path, []).append(row)
    tags = mapping.tags

    def json_table(record: Any, leaves: Iterable[tuple[tuple[Any, ...], str]]) -> None:
        path = index.paths.get(str(record.provenance.evidence.source))
        entry = None if path is None else mapping.match("tables", path)
        if path is not None and (entry is not None or tags.get("table") == path):
            rows_path = (entry or {}).get("rows") or (tags.get("rows") if entry is None else None)
            rows = _json_rows(f"{STATIC}/{path}", leaves, rows_path)
            index.json_rows[record.id] = rows
            index.rows.setdefault(path, []).extend(rows)

    # A JSON table reaches the package as configuration values, or as one text document.
    for snapshot in snapshots:
        values = index.config.get(snapshot.id, [])
        json_table(snapshot, [(v.path, v.text.value) for v in values if isinstance(v.text, Known)])
    documents: dict[str, list[DocumentBlock]] = defaultdict(list)
    for (document, _), blocks in sorted(pages.items()):
        documents[document].extend(sorted(blocks, key=lambda b: b.order))
    for blocks in documents.values():
        path = index.paths.get(str(blocks[0].provenance.evidence.source), "")
        if path.endswith(".json"):
            text = "\n".join(b.text.value for b in blocks if isinstance(b.text, Known))
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            first = min(blocks, key=lambda b: b.order)
            json_table(first, _leaves(parsed, ()))
    if "table" in tags:
        names = index.column(str(tags["table"]), str(tags["name"]))
        types = index.column(str(tags["table"]), str(tags["type"]))
        index.register = tuple(zip(names, types, strict=True))
    _CACHE.clear()
    _CACHE[key] = index
    return index


def _text(cell: Any) -> str:
    return str(cell.value) if isinstance(cell, Known) else ""


def _pointer(cell: Any, position: int) -> str:
    provenance = getattr(cell, "provenance", None)
    evidence = getattr(provenance, "evidence", None)
    if evidence is not None and evidence.locator and isinstance(evidence.locator[-1], JsonPointer):
        return evidence.locator[-1].pointer.lstrip("/")
    return f"col{position + 1}"


def _page(block: DocumentBlock) -> int:
    for step in block.provenance.evidence.locator:
        if isinstance(step, Page):
            return step.index
    return 0


def _or_none(text: str) -> str | None:
    return text or None


# --- Tables -----------------------------------------------------------------------------------


def _key(column: str) -> str:
    return str(column).strip().lower().replace(" ", "_")


class _Row:
    """One data row: cells by column key, and the facts it states, each citing its cell.

    ``cells`` holds (column as written, text, locator) per cell."""

    def __init__(
        self, file: str, number: int, label: str, cells: list[tuple[str, str, str]]
    ) -> None:
        self.file, self.number, self.label = file, number, label
        self.written = [(name, text) for name, text, _ in cells]
        self.cells = {_key(name): text for name, text, _ in cells}
        self.locators = {_key(name): locator for name, _, locator in cells}

    @staticmethod
    def tabular(file: str, record: StructuredRecord, header: tuple[str, ...], label: str) -> _Row:
        """A CSV/XLSX row (``row N col "X"``) or a JSONL line (``line N col "key"``)."""
        number = record.row + 1
        cells = [
            (name, _text(cell), f'{label} {number} col "{name}"')
            for name, cell in zip(header, record.cells, strict=False)
        ]
        return _Row(file, number, label, cells)

    def __getitem__(self, column: str) -> str:
        return self.cells.get(column, "")

    def render(
        self, template: Any, index: _Index, mapping: Mapping, extra: dict[str, str] | None = None
    ) -> str:
        def lookup(name: str) -> str:
            return extra[name] if extra and name in extra else self[_key(name)]

        return render(template, lookup, mapping, index.register)

    def when(self, name: Any, index: _Index, mapping: Mapping) -> str | None:
        """A ``time`` / ``until``: a column, or a template (``None`` if a cell it names is
        blank)."""
        if not name:
            return None
        if "{" not in str(name):
            return _or_none(self[_key(name)])
        blank: list[str] = []

        def lookup(column: str) -> str:
            text = self[_key(column)]
            if not text:
                blank.append(column)
            return text

        text = render(name, lookup, mapping, index.register)
        return None if blank else text

    def fact(
        self,
        source: str,
        kind: Kind,
        subject: str,
        predicate: str,
        column: str,
        obj: Ref | Value,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> Fact:
        locator = self.locators.get(column, f'{self.label} {self.number} col "{column}"')
        cite = Citation(self.file, locator, self[column])
        return Fact(subject, predicate, obj, valid_from, valid_to, kind, source, (cite,))


def _leaves(value: Any, path: tuple[Any, ...]) -> list[tuple[tuple[Any, ...], str]]:
    """Every scalar in parsed JSON with its path; text as is, other scalars as JSON."""
    if isinstance(value, dict):
        return [leaf for key, item in value.items() for leaf in _leaves(item, (*path, key))]
    if isinstance(value, list):
        return [leaf for i, item in enumerate(value) for leaf in _leaves(item, (*path, i))]
    return [(path, value if isinstance(value, str) else json.dumps(value))]


def _json_rows(
    file: str, leaves: Iterable[tuple[tuple[Any, ...], str]], rows_path: Any
) -> list[_Row]:
    """A JSON file's rows: the objects of the array at dotted ``rows_path`` (or the top-level
    array, or the whole object as one row); columns are dotted paths, cited ``json <pointer>``."""
    prefix = tuple(str(rows_path).split(".")) if rows_path else ()
    rows: dict[int, list[tuple[str, str, str]]] = {}
    for path, text in leaves:
        if tuple(str(p) for p in path[: len(prefix)]) != prefix:
            continue
        rest = path[len(prefix) :]
        index, column = (rest[0], rest[1:]) if rest and isinstance(rest[0], int) else (0, rest)
        if column:
            pointer = "/" + "/".join(str(p) for p in path)
            name = ".".join(str(p) for p in column)
            rows.setdefault(index, []).append((name, str(text), f"json {pointer}"))
    return [_Row(file, i + 1, "json", cells) for i, cells in sorted(rows.items())]


def _table(entry: dict[str, Any], row: _Row, index: _Index, mapping: Mapping) -> list[Fact]:
    source, kind = str(entry["source"]), entry.get("kind", "stated")
    path = row.file.removeprefix(f"{STATIC}/")
    out: list[Fact] = []
    for spec in entry.get("facts") or []:
        column = _key(spec["column"])
        if not row[column]:
            continue
        once = spec.get("once")
        if once and index.first(path, _key(once), row[_key(once)]) != row.number:
            continue
        times = [row.when(spec.get(w, entry.get(w)), index, mapping) for w in ("time", "until")]
        items = [row[column]]
        if "split" in spec:
            items = [item.strip() for item in row[column].split(spec["split"]) if item.strip()]
        for item in items:
            extra = {"item": item}
            default = entry.get("subject", "")
            subject = row.render(spec.get("subject", default), index, mapping, extra)
            obj: Ref | Value
            if "ref" in spec:
                obj = Ref(row.render(spec["ref"], index, mapping, extra))
            else:
                value = item
                if "value" in spec:
                    value = row.render(spec["value"], index, mapping, extra)
                unit = row.render(spec["unit"], index, mapping, extra) if "unit" in spec else ""
                obj = Value(value, unit or None)
            out.append(row.fact(source, kind, subject, spec["predicate"], column, obj, *times))
    return out


def _unmapped_row(path: str, record: StructuredRecord, row: _Row | None) -> list[Fact]:
    document = f"document:{path}"
    file, number = f"{STATIC}/{path}", record.row + 1
    out: list[Fact] = []
    if row is None:  # a CSV header: the file's marker
        quote = ",".join(_text(cell) for cell in record.cells)
        out.append(_unmapped(document, Citation(file, f"row {number}", quote)))
        return out
    if record.row == 0:
        out.append(_unmapped(document, Citation(file, f"line {number}", None)))
    if row.label == "line":
        text = ", ".join(f"{name}: {value}" for name, value in row.written)
    else:
        text = ",".join(value for _, value in row.written)
    cite = Citation(file, f"{row.label} {number}", text)
    out.append(Fact(document, "text", Value(text), None, None, "stated", UNMAPPED_SOURCE, (cite,)))
    return out


def _unmapped(document: str, cite: Citation) -> Fact:
    obj = Value("not mapped")
    return Fact(document, "unmapped", obj, None, None, "stated", UNMAPPED_SOURCE, (cite,))


# --- Documents --------------------------------------------------------------------------------


def _block_text(block: DocumentBlock) -> str | None:
    if not isinstance(block.text, Known) or not block.text.value.strip():
        return None
    return block.text.value


def _unmapped_block(path: str, block: DocumentBlock, index: _Index) -> list[Fact]:
    text = _block_text(block)
    if text is None:
        return []
    page, number = index.blocks[block.id]
    document = f"document:{path}"
    cite = Citation(f"{STATIC}/{path}", f"page {page} block {number}", text)
    out = [_unmapped(document, cite)] if block.order == 0 else []
    out.append(Fact(document, "text", Value(text), None, None, "stated", UNMAPPED_SOURCE, (cite,)))
    return out


def _document_block(
    entry: dict[str, Any], path: str, block: DocumentBlock, index: _Index, mapping: Mapping
) -> list[Fact]:
    text = _block_text(block)
    if text is None:
        return []
    names = {"stem": PurePosixPath(path).stem}
    document = render(entry["id"], names.__getitem__, mapping, index.register)
    names["id"] = document.split(":", 1)[1]
    page, number = index.blocks[block.id]
    cite = (Citation(f"{STATIC}/{path}", f"page {page} block {number}", text),)
    source = str(entry["source"])

    def stated(subject: str, predicate: str, obj: Ref | Value) -> Fact:
        return Fact(subject, predicate, obj, None, None, "stated", source, cite)

    out: list[Fact] = []
    if block.order == 0:
        out.append(stated(document, "name", Value(text.strip())))
    holder = document
    rule = entry.get("rule")
    clause = re.match(rule["pattern"], text.strip(), re.DOTALL) if rule else None
    if rule and clause:
        groups = {**names, **{str(i): g or "" for i, g in enumerate(clause.groups(), start=1)}}
        holder = render(rule["id"], groups.__getitem__, mapping, index.register)
        rule_text = clause.group(int(rule.get("text", 2))).strip()
        out.append(stated(holder, "rule_text", Value(rule_text)))
    lowered = text.lower()
    for lift in entry.get("lift") or []:
        about = lift.get("about", lift.get(True, lift.get("on", "document")))  # YAML 1.1: on = True
        on = holder if about == "rule" else document
        predicate, ref = lift["predicate"], lift.get("ref")
        found: list[str] = []
        if "pattern" in lift:
            for match in re.finditer(lift["pattern"], text):
                found.append(match.group(1) if match.groups() else match.group(0))
        if "terms" in lift:
            terms = lift["terms"]
            held = index.column(str(terms["table"]), str(terms["column"]))
            if "split" in terms:
                held = [t.strip() for cell in held for t in cell.split(terms["split"])]
            for term in sorted({t for t in held if t}):
                if term.lower() in lowered or term.replace("-", " ").lower() in lowered:
                    found.append(term)
        for value in found:
            out.append(stated(on, predicate, Ref(f"{ref}:{value}") if ref else Value(value)))
    return out


# --- Recorded model output --------------------------------------------------------------------


def _extraction(
    entry: dict[str, Any],
    path: str,
    values: Iterable[ConfigurationValue],
    index: _Index,
    mapping: Mapping,
) -> list[Fact]:
    scalars = {v.path: v.text.value for v in values if isinstance(v.text, Known)}
    model = f"{scalars[('model',)]}@{scalars[('prompt_sha256',)][:12]}"
    image = PurePosixPath(STATIC, path).parent / scalars[("image",)]
    taken = scalars.get(("taken",))
    kind: Kind = "inferred"
    source, predicate = str(entry["source"]), str(entry.get("predicate", "note"))
    out: list[Fact] = []
    region = 0
    while ("regions", region, "text") in scalars:
        box = ",".join(scalars[("regions", region, "box", i)] for i in range(4))
        cite = Citation(str(image), f"region {box}", None)
        text = scalars[("regions", region, "text")]
        tag, colon, _ = text.partition(":")
        subject = (
            tag_ref(tag.strip(), mapping, index.register) if colon else f"document:{image.stem}"
        )
        out.append(Fact(subject, predicate, Value(text), taken, None, kind, source, (cite,), model))
        region += 1
    return out
