"""Declared vendor mapping files: diagnostic status codes and names to event kinds (ADR 0010 §7).

A mapping file is JSON::

    {
      "schema": "neptune-deploy.diagnostics-mapping/1",
      "id": "ros2.diagnostics", "version": "1", "vendor": "ros2", "description": "...",
      "array": "/status",                 # where a row's statuses are (a JSON pointer)
      "fields": {"level": "level", "name": "name", "message": "message",
                 "hardware_id": "hardware_id", "values": "values"},   # keys inside one status
      "stamp": {"sec": "/header/stamp/sec", "nanosec": "/header/stamp/nanosec"},   # optional
      "clock": {"epoch": "unix", "resolution": "1/1000000000"},   # optional, each part optional
      "topics": ["/diagnostics", "/diagnostics_agg"],    # streams a bag's package may carry
      "levels": {"0": "diagnostic.ok", ...},   # status code (as declared text) -> event kind
      "names": {"/amr/battery": "battery_status"}    # status name -> event kind; wins over levels
    }

The target strings are this file's declaration. Neptune registers no event vocabulary here: Memory
G3 (MVL-137) has not registered one, so nothing is checked against it (ADR 0010 §7). A status whose
code is not in ``levels`` is a finding, and stays as declared. Anything else wrong with the file is
a ``MappingError`` before any record is read.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.lifecycle.mapping import MappingError
from neptune_deploy.sources.fleet_ops.documents import (
    DeclaredClock,
    DocumentInvalid,
    parse_clock,
    parse_json,
)

MAPPING_SCHEMA: Final = "neptune-deploy.diagnostics-mapping/1"
MAX_BYTES: Final = 1024 * 1024
MAX_ENTRIES: Final = 4096
MAX_KIND: Final = 128
_TOKEN: Final = re.compile(r"[a-z][a-z0-9_.\-]*")
_POINTER: Final = re.compile(r"(/([^/~]|~[01])+)+")
_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CODE: Final = re.compile(r"0|[1-9][0-9]{0,2}")
_FIELDS: Final = ("hardware_id", "level", "message", "name", "values")
_TOP: Final = frozenset(
    {
        "schema",
        "id",
        "version",
        "vendor",
        "description",
        "array",
        "fields",
        "stamp",
        "clock",
        "topics",
        "levels",
        "names",
    }
)


@dataclass(frozen=True)
class DiagnosticsMapping:
    id: str
    version: str
    vendor: str
    array: str
    fields: Mapping[str, str]
    sec: str | None
    nanosec: str | None
    clock: DeclaredClock
    topics: frozenset[str]
    levels: Mapping[str, str]
    names: Mapping[str, str]
    sha256: ContentId

    def config(self) -> dict[str, JsonValue]:
        """What decided every kind: the file's whole declaration, in the transform."""
        found: dict[str, JsonValue] = {
            "array": self.array,
            "clock": self.clock.config(),
            "fields": dict(sorted(self.fields.items())),
            "id": self.id,
            "levels": dict(sorted(self.levels.items())),
            "mapping": self.sha256,
            "names": dict(sorted(self.names.items())),
            "topics": sorted(self.topics),
            "vendor": self.vendor,
            "version": self.version,
        }
        if self.sec is not None and self.nanosec is not None:
            found["stamp"] = {"nanosec": self.nanosec, "sec": self.sec}
        return found


def _kind(where: str, value: JsonValue) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_KIND
        or not value.isprintable()
        or value != value.strip()
    ):
        raise MappingError(f"{where}: an event kind is printable text of at most {MAX_KIND}")
    return value


def parse_mapping(data: bytes) -> DiagnosticsMapping:
    """A mapping file's bytes, checked. ``MappingError`` names where the file is wrong."""
    if len(data) > MAX_BYTES:
        raise MappingError(f"a mapping file is at most {MAX_BYTES} bytes")
    try:
        raw = parse_json(data)  # strict: no repeated key, no NaN, bounded nesting
    except DocumentInvalid as exc:
        raise MappingError(f"not strict JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MappingError("a mapping file is a JSON object")
    unknown = sorted(set(raw) - _TOP)
    if unknown:
        raise MappingError(f"unknown keys: {unknown}")
    if raw.get("schema") != MAPPING_SCHEMA:
        raise MappingError(f"schema must be {MAPPING_SCHEMA!r}")
    for key in ("id", "vendor"):
        if not isinstance(raw.get(key), str) or not _TOKEN.fullmatch(raw[key]):
            raise MappingError(f"{key} is a lowercase token")
    version = raw.get("version")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 32
        or not version.isprintable()
    ):
        raise MappingError("version is short printable text")
    array = raw.get("array")
    if not isinstance(array, str) or not _POINTER.fullmatch(array):
        raise MappingError("array is a JSON pointer such as '/status'")
    fields = raw.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - set(_FIELDS):
        raise MappingError(f"fields names some of {list(_FIELDS)}")
    named = {name: name for name in _FIELDS}
    for name, key in fields.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise MappingError(f"fields.{name} is a key name")
        named[name] = key
    sec = nanosec = None
    stamp = raw.get("stamp")
    if stamp is not None:
        if not isinstance(stamp, dict) or set(stamp) != {"sec", "nanosec"}:
            raise MappingError("stamp is {sec, nanosec}, each a JSON pointer")
        for part in ("sec", "nanosec"):
            if not isinstance(stamp[part], str) or not _POINTER.fullmatch(stamp[part]):
                raise MappingError(f"stamp.{part} is a JSON pointer")
        sec, nanosec = stamp["sec"], stamp["nanosec"]
    try:
        clock = parse_clock(raw.get("clock"))
    except ValueError as exc:
        raise MappingError(f"clock: {exc}") from exc
    topics = raw.get("topics", [])
    if (
        not isinstance(topics, list)
        or len(topics) > MAX_ENTRIES
        or not all(isinstance(t, str) and t for t in topics)
    ):
        raise MappingError("topics is a list of stream topics")
    levels = raw.get("levels")
    names = raw.get("names", {})
    if not isinstance(levels, dict) or not levels or len(levels) > MAX_ENTRIES:
        raise MappingError("levels maps status codes to event kinds")
    if not isinstance(names, dict) or len(names) > MAX_ENTRIES:
        raise MappingError("names maps status names to event kinds")
    for code in levels:
        if not _CODE.fullmatch(code):
            raise MappingError(f"levels: {code!r} is not a status code (1 to 3 digits)")
    return DiagnosticsMapping(
        id=raw["id"],
        version=version,
        vendor=raw["vendor"],
        array=array,
        fields=named,
        sec=sec,
        nanosec=nanosec,
        clock=clock,
        topics=frozenset(topics),
        levels={code: _kind(f"levels.{code}", kind) for code, kind in levels.items()},
        names={name: _kind(f"names.{name!r}", kind) for name, kind in names.items()},
        sha256=content_id(data),
    )


def load_mapping(path: Path) -> DiagnosticsMapping:
    """The mapping file at ``path``; at most ``MAX_BYTES`` of it are ever read."""
    with path.open("rb") as handle:
        return parse_mapping(handle.read(MAX_BYTES + 1))
