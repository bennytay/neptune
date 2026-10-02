"""``OpenRmfSource``: an Open-RMF deployment's logs as a read-only fleet-ops source (ADR 0010 §5).

Open-RMF keeps its history as JSON (the API server's task states, fleet states and dispatch states)
and in a SQLite database (the same, stored by the api-server). A deployment's lane and zone map is a
JSON export of its navigation graph. This source reads what the operator points it at, from local
files only (the network is never used, so it asks no workspace), and keeps what it reads:

- ``tasks``: task logs. Each becomes a table and, per task that states an id, a ``Run``.
- ``fleet_states``: fleet-adapter state logs, as a table.
- ``dispatches``: dispatch records, as a table.
- ``map``: levels with their lanes, vertices and zones, as spatial records in the frame the file
  names (``rmf_records.build_map``).

Identity: ``ExternalObjectRef("deploy_open_rmf", "<site>/<part>", "records:<sha256>")``. ``site`` is
declared and is the operator's name for the deployment: two deployments that both call theirs
``warehouse`` share an identity, and the declaration is theirs to keep distinct.

Options are declared and closed: ``site``; ``files``, the part-to-file map (JSON, JSON Lines or a
SQLite table with its JSON columns); ``clock`` for the integer time fields; ``task_fields`` for
where a task states its id, robot and times (the api-server's by default); ``max_rows``,
``max_file_bytes``; for SQLite also ``max_cell_bytes`` and ``max_read_bytes``.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, Severity
from neptune.model.jsonvalue import JsonValue
from neptune.model.reference import TimestampDomain
from neptune_deploy.sources.fleet_ops import options as opt
from neptune_deploy.sources.fleet_ops.base import FleetOpsSource, Part
from neptune_deploy.sources.fleet_ops.rmf_files import (
    FileRefused,
    Rows,
    json_items,
    read_bytes,
    safe_path,
    sqlite_items,
)
from neptune_deploy.sources.fleet_ops.rmf_records import (
    DEFAULT_TASK_FIELDS,
    build_map,
    build_runs,
)
from neptune_deploy.sources.stated_records import (
    CatalogDocument,
    DeclaredClock,
    StatedCatalog,
    parse_clock,
)

CONNECTOR_ID: Final = "deploy_open_rmf"
PARTS: Final = ("tasks", "fleet_states", "dispatches", "map")
_SITE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
_POINTER: Final = re.compile(r"(/([^/~]|~[01])*)*")
_TIME_FIELDS: Final = {
    "tasks": ("unix_millis_finish_time", "unix_millis_start_time"),
    "fleet_states": (),
    "dispatches": (),
    "map": (),
}
_OPTIONS: Final = frozenset(
    {
        "clock",
        "files",
        "max_cell_bytes",
        "max_file_bytes",
        "max_read_bytes",
        "max_rows",
        "site",
        "task_fields",
        "time_fields",
    }
)
_FILE_KEYS: Final = frozenset({"file", "json_columns", "table"})


@dataclass(frozen=True)
class FileSpec:
    """Where one part is: a file, and for SQLite its table and JSON columns."""

    file: str
    table: str | None = None
    json_columns: tuple[str, ...] = ()

    def config(self) -> dict[str, JsonValue]:
        config: dict[str, JsonValue] = {"file": self.file, "json_columns": list(self.json_columns)}
        if self.table is not None:
            config["table"] = self.table
        return config


@dataclass(frozen=True)
class RmfOptions:
    site: str
    files: tuple[tuple[str, FileSpec], ...]
    clock: Any
    task_fields: tuple[tuple[str, str], ...]
    time_fields: tuple[tuple[str, tuple[str, ...]], ...]
    max_rows: int
    max_file_bytes: int
    max_cell_bytes: int
    max_read_bytes: int

    @classmethod
    def parse(cls, options: Mapping[str, JsonValue] | None) -> "RmfOptions":
        given = opt.closed(options, _OPTIONS)
        site = given.get("site")
        if not isinstance(site, str) or not _SITE.fullmatch(site):
            raise opt.FleetOpsConfigError(
                "site is a declared name: letters, digits, _ . - (at most 64)"
            )
        declared = given.get("files")
        if not isinstance(declared, Mapping) or not declared:
            raise opt.FleetOpsConfigError("files maps each part to the file that holds it")
        specs: list[tuple[str, FileSpec]] = []
        for part in PARTS:  # fixed order, whatever order the options came in
            if part not in declared:
                continue
            spec = declared[part]
            if not isinstance(spec, Mapping) or spec.keys() - _FILE_KEYS or "file" not in spec:
                raise opt.FleetOpsConfigError(
                    f"files.{part} is an object of file, and for SQLite table and json_columns"
                )
            file = opt.text(spec, "file", None)
            table = opt.text(spec, "table", None)
            assert file is not None
            specs.append((part, FileSpec(file, table, opt.texts(spec, "json_columns"))))
        unknown = sorted(str(name) for name in declared if name not in PARTS)
        if unknown:
            raise opt.FleetOpsConfigError(f"unknown parts: {unknown}; parts are {list(PARTS)}")
        fields = {**DEFAULT_TASK_FIELDS}
        task_fields = given.get("task_fields", {})
        if not isinstance(task_fields, Mapping) or task_fields.keys() - fields.keys():
            raise opt.FleetOpsConfigError(f"task_fields names some of {sorted(fields)}")
        for name, where in task_fields.items():
            if not isinstance(where, str):
                raise opt.FleetOpsConfigError(f"task_fields.{name} is text")
            fields[name] = where
        for name in ("id", "group", "robot"):
            if not _POINTER.fullmatch(fields[name]):
                raise opt.FleetOpsConfigError(f"task_fields.{name} is a JSON pointer")
        try:
            parse_clock(given.get("clock"))
        except ValueError as exc:
            raise opt.FleetOpsConfigError(str(exc)) from exc
        times = dict(_TIME_FIELDS)
        extra = given.get("time_fields", {})
        if not isinstance(extra, Mapping) or extra.keys() - set(PARTS):
            raise opt.FleetOpsConfigError(
                "time_fields maps a part to a list of integer field names"
            )
        for part in extra:
            times[part] = opt.texts(extra, part)
        times["tasks"] = tuple(sorted({*times["tasks"], fields["start"], fields["finish"]}))
        return cls(
            site=site,
            files=tuple(specs),
            clock=given.get("clock"),
            task_fields=tuple(sorted(fields.items())),
            time_fields=tuple(sorted(times.items())),
            max_rows=opt.integer(given, "max_rows", 1_000_000, 1, 100_000_000),
            max_cell_bytes=opt.integer(
                given, "max_cell_bytes", 16 * 1024 * 1024, 1024, 256 * 1024**2
            ),
            max_read_bytes=opt.integer(
                given, "max_read_bytes", 256 * 1024 * 1024, 1024, 4 * 1024**3
            ),
            max_file_bytes=opt.integer(
                given, "max_file_bytes", 256 * 1024 * 1024, 1024, 4 * 1024**3
            ),
        )


class OpenRmfSource(FleetOpsSource):
    """An Open-RMF deployment's task logs, fleet states, dispatches and maps from local files."""

    connector_id = CONNECTOR_ID
    EXTRA_CODES: Final = {
        "file_refused": (
            FindingCategory.SKIPPED,
            Severity.ERROR,
            "a declared file was not read: a path outside the directory, a symlink, a file that is"
            " missing, not regular, too large or not the declared kind",
        ),
        "cells_not_recorded": (
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            "cells with no JSON form (blobs, infinite floats), or JSON columns that did not parse,"
            " or entries that were not objects; the cells are absent or stay text",
        ),
        "map_keys_not_recorded": (
            FindingCategory.UNSUPPORTED,
            Severity.INFO,
            "a map states keys other than its name, coordinate system and levels; they are not"
            " recorded",
        ),
    }

    def __init__(
        self, root: str, options: RmfOptions, *, ledger: SourceLedger | None = None
    ) -> None:
        super().__init__(ledger)
        self._root = root
        self.options = options
        self.scope = f"{options.site}/"
        self._clock: DeclaredClock = parse_clock(options.clock)
        self._read_bytes: dict[str, int] = {}

    def config(self) -> dict[str, JsonValue]:
        o = self.options
        return {
            "clock": self._clock.config(),
            "files": {part: spec.config() for part, spec in o.files},
            "max_cell_bytes": o.max_cell_bytes,
            "max_file_bytes": o.max_file_bytes,
            "max_read_bytes": o.max_read_bytes,
            "max_rows": o.max_rows,
            "site": o.site,
            "task_fields": dict(o.task_fields),
            "time_fields": {part: list(names) for part, names in o.time_fields},
        }

    def _read(self, part: str, spec: FileSpec) -> Rows:
        path = safe_path(self._root, spec.file)
        if spec.table is not None:
            # One budget per database file, shared by every part read from it.
            left = self.options.max_read_bytes - self._read_bytes.get(spec.file, 0)
            rows = sqlite_items(
                path,
                spec.table,
                spec.json_columns,
                self.options.max_rows,
                self.options.max_file_bytes,
                max_cell_bytes=self.options.max_cell_bytes,
                max_read_bytes=max(left, 0),
            )
            self._read_bytes[spec.file] = self._read_bytes.get(spec.file, 0) + rows.bytes_read
            return rows
        return json_items(
            read_bytes(path, self.options.max_file_bytes),
            self.options.max_rows,
            whole=part == "map",
        )

    def collect(self) -> Sequence[Part]:
        times = dict(self.options.time_fields)
        parts: list[Part] = []
        for name, spec in self.options.files:
            try:
                rows = self._read(name, spec)
            except FileRefused as exc:
                self.report(
                    "file_refused", self.part_subject(name), {"cause": exc.cause, "part": name}
                )
                continue
            except OSError:
                self.report(
                    "file_refused",
                    self.part_subject(name),
                    {"cause": "file_unreadable", "part": name},
                )
                continue
            if rows.counts:
                self.report(
                    "cells_not_recorded",
                    self.part_subject(name),
                    {"part": name, **dict(sorted(rows.counts.items()))},
                )
            items = rows.items
            if name == "map":
                items = self._levels(items)
            part = Part(name, items, times.get(name, ()), self._clock)
            part.stopped = rows.stopped
            if rows.stopped in ("byte_limit", "cell_limit"):
                # Partial coverage, stated: the rows kept, and the bounds that stopped the read.
                part.details = {
                    "bytes_read": rows.bytes_read,
                    "max_cell_bytes": self.options.max_cell_bytes,
                    "max_read_bytes": self.options.max_read_bytes,
                }
            parts.append(part)
        return parts

    def _levels(self, maps: list[JsonValue]) -> list[JsonValue]:
        """One item per level of each map object: ``{"level": name, "data": <level>, ...}``."""
        out: list[JsonValue] = []
        extra: set[str] = set()
        malformed = 0
        for found in maps:
            if not isinstance(found, dict) or not isinstance(found.get("levels"), dict):
                malformed += 1
                continue
            extra.update(
                str(key) for key in found if key not in ("name", "coordinate_system", "levels")
            )
            for level, data in found["levels"].items():
                item: dict[str, JsonValue] = {"level": level, "data": data}
                for key in ("name", "coordinate_system"):
                    if key in found:
                        item["map" if key == "name" else key] = found[key]
                out.append(item)
        if malformed:
            self.report(
                "record_skipped",
                self.part_subject("map"),
                {"count": malformed, "reason": "map_has_no_levels_object", "part": "map"},
            )
        if extra:
            self.report(
                "map_keys_not_recorded",
                self.part_subject("map"),
                {"keys": sorted(extra)[:10], "key_count": len(extra)},
            )
        return out

    def extend(
        self,
        part: Part,
        document: CatalogDocument,
        table: StatedCatalog,
        clocks: Mapping[str, TimestampDomain],
    ) -> Sequence[Any]:
        if part.name == "tasks":
            return build_runs(
                document, self.transform, dict(self.options.task_fields), clocks, self.report
            )
        if part.name == "map":
            return build_map(document, self.transform, self.report)
        return ()


def open_rmf_source(
    path: str | os.PathLike[str],
    *,
    network: object | None = None,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> OpenRmfSource:
    """``deploy_open_rmf``: the Open-RMF logs under the local directory ``path``, read only.

    Local files only: ``network`` is accepted so every factory has one signature (ADR 0006 §1) and
    is never asked, never used. Nothing here takes credentials, so passing any is refused.
    """
    del network, environ
    if credentials:
        raise opt.FleetOpsConfigError("Open-RMF logs are local files; there are no credentials")
    root = os.fspath(path)
    if not Path(root).is_dir():
        raise opt.FleetOpsConfigError("the Open-RMF path is not a directory")
    return OpenRmfSource(root, RmfOptions.parse(options), ledger=ledger)
