"""Declared event-log mapping files: a log export's columns into a typed event table (ADR 0017).

A mapping file is JSON::

    {
      "schema": "neptune-deploy.event-log-mapping/1",
      "id": "syslog.csv", "version": "1", "description": "...",
      "table": "syslog events",                  # the typed table's name
      "requires": ["Timestamp", "Message"],      # columns a table must have to be this log
      "identifier": {"column": "Seq", "namespace": "syslog"},   # optional: the row's own id
      "time": {"column": "Timestamp", "format": ["%Y-%m-%d %H:%M:%S"], "zone": "unstated"},
      "columns": ["MsgID", "Host", "Severity", "Message"],    # copied verbatim, cited
      "ignore": ["Facility"]                     # left unread on purpose, never silently
    }

Formats and zones are the lifecycle mapper's (``lifecycle.times``, ADR 0012 §2, ADR 0016). A
caller may declare a zone per source (``with_source_zones``, ADR 0017 §2). Anything wrong with the
file is a ``MappingError`` before any record is read.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.lifecycle.mapping import (
    MAX_MAPPING_BYTES,
    MappingError,
    _reject_constant,
    _reject_duplicates,
    check_zone,
)
from neptune_deploy.lifecycle.times import check_format

MAPPING_SCHEMA: Final = "neptune-deploy.event-log-mapping/1"
_TOP: Final = frozenset(
    {"schema", "id", "version", "description", "table", "requires", "identifier", "time"}
    | {"columns", "ignore"}
)
MAX_COLUMNS: Final = 256


@dataclass(frozen=True)
class EventLogMapping:
    """A mapping file, parsed and checked; ``document`` and ``sha256`` enter the transform."""

    id: str
    version: str
    table: str
    requires: tuple[str, ...]
    identifier: tuple[str, str] | None  # (column, namespace)
    time_column: str
    formats: tuple[str, ...]
    zone: str
    columns: tuple[str, ...]
    ignore: tuple[str, ...]
    document: JsonObject
    sha256: ContentId
    source_zones: tuple[tuple[str, str], ...] = ()

    @property
    def read(self) -> frozenset[str]:
        """Every column the mapping reads or leaves unread on purpose."""
        named = {self.time_column, *self.columns, *self.ignore}
        if self.identifier is not None:
            named.add(self.identifier[0])
        return frozenset(named)

    def header(self) -> tuple[str, ...]:
        """The typed table's header (ADR 0017 §1)."""
        time = self.time_column
        out = (*self.columns, time, f"{time}.sec", f"{time}.nanosec", f"@clock:{time}")
        if self.identifier is not None:
            out = (*out, f"@id:{self.identifier[1]}")
        return out

    def with_source_zones(self, zones: Mapping[str, str]) -> "EventLogMapping":
        """As ``LifecycleMapping.with_source_zones``: a caller's zone per source path."""
        merged = dict(self.source_zones)
        for path, zone in zones.items():
            if not isinstance(path, str) or not path or not path.isprintable():
                raise MappingError(f"civil_time_zone: {path!r} is not a source path")
            merged[path] = check_zone(zone, f"civil_time_zone for {path!r}")
        return replace(self, source_zones=tuple(sorted(merged.items())))

    def config(self, base_package: ContentId) -> JsonObject:
        document: JsonValue = self.document
        out: dict[str, JsonValue] = {
            "base_package": base_package,
            "mapping": document,
            "mapping_sha256": self.sha256,
        }
        if self.source_zones:
            out["civil_time_zones"] = dict(self.source_zones)
        return out


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or not value.isprintable() or len(value) > 256:
        raise MappingError(f"{where}: printable text of at most 256 characters")
    return value


def _texts(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_COLUMNS:
        raise MappingError(f"{where}: a list of at most {MAX_COLUMNS} column names")
    out = tuple(_text(item, f"{where}[{i}]") for i, item in enumerate(value))
    if len(set(out)) != len(out):
        raise MappingError(f"{where}: a column is named twice")
    return out


def parse_mapping(data: bytes, name: str = "<mapping>") -> EventLogMapping:
    """A mapping file's bytes, checked. ``MappingError`` names where the file is wrong."""
    if len(data) > MAX_MAPPING_BYTES:
        raise MappingError(f"{name}: larger than {MAX_MAPPING_BYTES} bytes")
    try:
        raw = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
        canonical_json.dumps(raw)  # floats out of range, nulls
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MappingError(f"{name}: not a JSON document: {exc}") from exc
    if not isinstance(raw, dict):
        raise MappingError(f"{name}: a mapping file is a JSON object")
    unknown = sorted(set(raw) - _TOP)
    if unknown:
        raise MappingError(f"{name}: unknown keys {unknown}")
    if raw.get("schema") != MAPPING_SCHEMA:
        raise MappingError(f"{name}: schema must be {MAPPING_SCHEMA!r}")
    for key in ("id", "version", "table", "time"):
        if key not in raw:
            raise MappingError(f"{name}: {key} is required")
    time = raw["time"]
    if (
        not isinstance(time, dict)
        or set(time) - {"column", "format", "zone"}
        or "column" not in time
    ):
        raise MappingError(f"{name}: time is {{column, format, zone}}")
    formats = time.get("format")
    formats = [formats] if isinstance(formats, str) else formats
    if not isinstance(formats, list) or not formats:
        raise MappingError(f"{name}: time.format is a format or a list of them")
    for pattern in formats:
        try:
            check_format(_text(pattern, f"{name}: time.format"))
        except ValueError as exc:
            raise MappingError(f"{name}: time.format: {exc}") from exc
    if "zone" not in time:
        raise MappingError(f"{name}: time.zone is required (an IANA name or 'unstated')")
    identifier = raw.get("identifier")
    ident: tuple[str, str] | None = None
    if identifier is not None:
        if not isinstance(identifier, dict) or set(identifier) != {"column", "namespace"}:
            raise MappingError(f"{name}: identifier is {{column, namespace}}")
        ident = (
            _text(identifier["column"], f"{name}: identifier.column"),
            _text(identifier["namespace"], f"{name}: identifier.namespace"),
        )
    mapping = EventLogMapping(
        id=_text(raw["id"], f"{name}: id"),
        version=_text(raw["version"], f"{name}: version"),
        table=_text(raw["table"], f"{name}: table"),
        requires=_texts(raw.get("requires", []), f"{name}: requires"),
        identifier=ident,
        time_column=_text(time["column"], f"{name}: time.column"),
        formats=tuple(formats),
        zone=check_zone(time["zone"], f"{name}: time.zone"),
        columns=_texts(raw.get("columns", []), f"{name}: columns"),
        ignore=_texts(raw.get("ignore", []), f"{name}: ignore"),
        document=raw,
        sha256=content_id(data),
    )
    header = mapping.header()
    if len(set(header)) != len(header):
        raise MappingError(f"{name}: the typed table would name a column twice: {list(header)}")
    if mapping.time_column in mapping.columns:
        raise MappingError(f"{name}: the time column is copied already; do not list it")
    return mapping


def load_mapping(path: Path) -> EventLogMapping:
    """The mapping file at ``path``; at most ``MAX_MAPPING_BYTES`` of it are ever read."""
    with path.open("rb") as handle:
        return parse_mapping(handle.read(MAX_MAPPING_BYTES + 1), str(path))
