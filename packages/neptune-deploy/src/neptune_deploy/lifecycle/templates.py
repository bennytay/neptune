"""Declared document templates: which structure of a document becomes which lifecycle fields
(ADR 0003).

A template is JSON, in the style of a mapping file (``mapping.py``)::

    {
      "schema": "neptune-deploy.document-template/1",
      "id": "risk.amr_iso3691_4", "version": "1", "description": "...",
      "kind": "risk_assessment",
      "formats": ["pdf"],                           # the document formats it reads (default: all)
      "separator": ":",                             # between an inline label and its value
      "zone": "Europe/Berlin",                      # default civil zone for its time fields
      "form": {"label": "Form", "value": "RA-3691-AMR",
               "version_label": "Revision", "version": "2"},   # the form the document declares
      "tables": {"hazards": ["Hazard", "Severity"]},  # tables, by their exact header cells
      "requires": {"labels": ["Assessment No"], "headings": ["Hazard analysis"],
                   "tables": ["hazards"]},          # the structure a document must have
      "ignore": {"labels": ["Reviewer"], "headings": ["Hazard analysis"], "tables": [],
                 "columns": {"hazards": ["Task"]}},  # left unread on purpose, never silently
      "fields": {...}
    }

Fields follow the kind's shapes exactly as in a mapping file, but name what they read by ``label``
(an inline ``Label: value`` paragraph, or a row of a two-column table whose first cell is the
label), by ``section`` (the text under a heading; free text, or the list items of a statements
field), and, inside ``{"rows": <table>, "each": {...}}``, by ``column``. Anything else is a
``MappingError`` before any document is read.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.lifecycle.mapping import (
    DOCUMENT,
    MAX_MAPPING_BYTES,
    MappingError,
    Part,
    Rows,
    Spec,
    _check_clocks,
    _fields,
    _object,
    _reject_constant,
    _reject_duplicates,
    _text,
    _texts,
    _token,
    spec_columns,
)
from neptune_deploy.lifecycle.shapes import KINDS

TEMPLATE_SCHEMA: Final = "neptune-deploy.document-template/1"
DEFAULT_FORMATS: Final = ("markdown", "pdf")


@dataclass(frozen=True)
class Form:
    """The form a document declares itself to be, in two labelled fields (ADR 0003 §3)."""

    label: str
    value: str
    version_label: str
    version: str


@dataclass(frozen=True)
class DocumentTemplate:
    """A template, parsed and checked. ``document`` is its parsed JSON and ``sha256`` the content
    id of its bytes: both enter the transform's config (ADR 0003 §6)."""

    id: str
    version: str
    kind: type[Any]
    formats: tuple[str, ...]
    separator: str
    form: Form | None
    tables: Mapping[str, tuple[str, ...]]
    require_labels: tuple[str, ...]
    require_headings: tuple[str, ...]
    require_tables: tuple[str, ...]
    ignore_labels: tuple[str, ...]
    ignore_headings: tuple[str, ...]
    ignore_tables: tuple[str, ...]
    ignore_columns: Mapping[str, tuple[str, ...]]
    fields: Mapping[str, Spec]
    document: JsonObject
    sha256: ContentId


class TemplateRegistry:
    """The templates a run may match, by ``(id, version)``: two versions of one template coexist,
    and a document declaring a version nobody registered is a finding, never a near match."""

    def __init__(self, templates: Iterable[DocumentTemplate] = ()) -> None:
        self._templates: dict[tuple[str, str], DocumentTemplate] = {}
        for template in templates:
            self.add(template)

    def add(self, template: DocumentTemplate) -> None:
        key = (template.id, template.version)
        if key in self._templates:
            raise MappingError(f"two templates are {template.id!r} version {template.version!r}")
        self._templates[key] = template

    def templates(self) -> tuple[DocumentTemplate, ...]:
        """Every template, in a stable order (by the content id of its bytes)."""
        return tuple(sorted(self._templates.values(), key=lambda t: t.sha256))

    def __len__(self) -> int:
        return len(self._templates)

    def get(self, template_id: str, version: str) -> DocumentTemplate | None:
        return self._templates.get((template_id, version))

    @classmethod
    def from_paths(cls, paths: Iterable[Path]) -> "TemplateRegistry":
        """Templates from files, and from every ``*.json`` directly inside a directory."""
        found: list[Path] = []
        for path in paths:
            found.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
        return cls(load_template(path) for path in found)


def parse_template(data: bytes, name: str = "<template>") -> DocumentTemplate:
    """Parse and check a template's bytes."""
    if len(data) > MAX_MAPPING_BYTES:
        raise MappingError(f"{name}: larger than {MAX_MAPPING_BYTES} bytes")
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MappingError(f"{name}: not a JSON document: {exc}") from exc
    except MappingError as exc:
        raise MappingError(f"{name}: {exc}") from exc
    try:
        canonical_json.dumps(document)
    except ValueError as exc:
        raise MappingError(f"{name}: {exc}") from exc
    try:
        return _template(document, content_id(data))
    except MappingError as exc:
        raise MappingError(f"{name}: {exc}") from exc


def load_template(path: Path) -> DocumentTemplate:
    """Read a template from disk."""
    with path.open("rb") as stream:
        data = stream.read(MAX_MAPPING_BYTES + 1)
    return parse_template(data, str(path))


def _template(document: Any, sha256: ContentId) -> DocumentTemplate:
    obj = _object(
        document,
        "template",
        {"schema", "id", "version", "kind", "fields"},
        {"description", "zone", "formats", "separator", "form", "tables", "requires", "ignore"},
    )
    if obj["schema"] != TEMPLATE_SCHEMA:
        raise MappingError(f"schema must be {TEMPLATE_SCHEMA!r}, got {obj['schema']!r}")
    if "description" in obj:
        _text(obj["description"], "description")
    zone = _text(obj["zone"], "zone") if "zone" in obj else None
    kind = _token(obj["kind"], "kind")
    if kind not in KINDS:
        raise MappingError(f"kind: {kind!r} is not a lifecycle kind: {sorted(KINDS)}")
    formats = _texts(obj.get("formats", list(DEFAULT_FORMATS)), "formats")
    if not formats:
        raise MappingError("formats: name at least one format")
    separator = _text(obj.get("separator", ":"), "separator")
    if not separator.strip():
        raise MappingError("separator: not only whitespace")
    form = _form(obj["form"]) if "form" in obj else None
    tables = _tables(obj.get("tables", {}))
    requires = _object(obj.get("requires", {}), "requires", set(), {"labels", "headings", "tables"})
    ignore = _object(
        obj.get("ignore", {}), "ignore", set(), {"labels", "headings", "tables", "columns"}
    )
    require_labels = _texts(requires.get("labels", []), "requires.labels")
    require_headings = _texts(requires.get("headings", []), "requires.headings")
    require_tables = _texts(requires.get("tables", []), "requires.tables")
    ignore_tables = _texts(ignore.get("tables", []), "ignore.tables")
    if form is None and not (require_labels or require_headings or require_tables):
        raise MappingError("requires: name a form, or at least one label, heading or table")
    fields = _fields(KINDS[kind], obj["fields"], "fields", zone, DOCUMENT)
    _check_clocks(fields, "fields")
    rows = _row_tables(fields)
    used = set(require_tables) | set(ignore_tables) | {t for t, _ in rows}
    for name in sorted(used - tables.keys()):
        raise MappingError(f"table {name!r} is not declared in tables")
    for name in sorted(tables.keys() - used):
        raise MappingError(f"table {name!r} is declared and never used")
    for table, columns in rows:
        missing = sorted(set(columns) - set(tables[table]))
        if missing:
            raise MappingError(
                f"rows of table {table!r} read columns it has no header for: {missing}"
            )
    ignore_columns = _ignored_columns(ignore.get("columns", {}), tables, {t for t, _ in rows})
    return DocumentTemplate(
        id=_token(obj["id"], "id"),
        version=_text(obj["version"], "version"),
        kind=KINDS[kind],
        formats=formats,
        separator=separator,
        form=form,
        tables=tables,
        require_labels=require_labels,
        require_headings=require_headings,
        require_tables=require_tables,
        ignore_labels=_texts(ignore.get("labels", []), "ignore.labels"),
        ignore_headings=_texts(ignore.get("headings", []), "ignore.headings"),
        ignore_tables=ignore_tables,
        ignore_columns=ignore_columns,
        fields=fields,
        document=document,
        sha256=sha256,
    )


def _form(value: Any) -> Form:
    obj = _object(value, "form", {"label", "value", "version_label", "version"}, set())
    return Form(**{key: _text(obj[key], f"form.{key}") for key in sorted(obj)})


def _tables(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise MappingError("tables: expected an object of header lists")
    out: dict[str, tuple[str, ...]] = {}
    for name, header in value.items():
        _token(name, f"tables.{name}")
        cells = _texts(header, f"tables.{name}")
        if not cells:
            raise MappingError(f"tables.{name}: name at least one header cell")
        out[name] = cells
    return out


def _ignored_columns(
    value: Any, tables: Mapping[str, tuple[str, ...]], read: set[str]
) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise MappingError("ignore.columns: expected an object of column lists by table")
    out: dict[str, tuple[str, ...]] = {}
    for name, columns in value.items():
        if name not in read:
            raise MappingError(f"ignore.columns: table {name!r} has no rows field reading it")
        cells = _texts(columns, f"ignore.columns.{name}")
        missing = sorted(set(cells) - set(tables[name]))
        if missing:
            raise MappingError(f"ignore.columns.{name}: no header cells {missing}")
        out[name] = cells
    return out


def rows_read(template: DocumentTemplate) -> dict[str, set[str]]:
    """The tables whose rows fields read, with the columns each reads."""
    out: dict[str, set[str]] = {}
    for table, columns in _row_tables(template.fields):
        out.setdefault(table, set()).update(columns)
    return out


def _row_tables(fields: Mapping[str, Spec]) -> list[tuple[str, list[str]]]:
    """The tables whose rows fields read, with the columns each reads."""
    out: list[tuple[str, list[str]]] = []
    for spec in fields.values():
        if isinstance(spec, Rows):
            out.append((spec.table, sorted(spec_columns(spec.part))))
        elif isinstance(spec, Part):
            out.extend(_row_tables(spec.fields))
        elif isinstance(spec, tuple):
            for item in spec:
                if isinstance(item, Part):
                    out.extend(_row_tables(item.fields))
    return out


def config_of(template: DocumentTemplate, base_package: ContentId) -> JsonObject:
    """The transform config of one template applied to one package (ADR 0003 §6)."""
    document: JsonValue = template.document
    return {"base_package": base_package, "template": document, "template_sha256": template.sha256}
