"""A Rerun Hub dataset's catalog, as an export file (ADR 0009 §5).

Rerun's catalog is served over gRPC (the ``RerunCloudService`` of the ``redap`` protocol, HTTP/2
with protobuf), and Rerun documents no HTTP or REST access to it. The connector has no HTTP/2 or
protobuf client and adds no dependency (ADR 0006 §10), so it does not speak that protocol. It reads
what Rerun's own SDK hands out: ``DatasetEntry.segment_table()`` (one row per segment, with the
columns ``rerun_segment_id``, ``rerun_layer_names``, ``rerun_storage_urls``, ``rerun_size_bytes``,
``rerun_last_updated_at`` and property columns), ``DatasetEntry.schema()`` (entity paths,
archetypes, components) and the dataset's indexes (timelines). An operator writes them to one JSON
file with this envelope:

    {"format": "neptune.rerun_catalog_export", "version": 1,
     "catalog": "<a name for this Hub, lower-case letters, digits and -; part of every id>",
     "dataset": {"id": "...", "name": "..."},
     "segments": [{"rerun_segment_id": "...", "rerun_layer_names": ["base"],
                   "rerun_storage_urls": ["s3://bucket/key.rrd"], ...}],
     "schema":   [{"entity_path": "/arm/joint_states", "archetype": "...", "component": "..."}],
     "indexes":  [{"name": "log_time", "kind": "timestamp"}]}

Only the envelope is Neptune's. ``segments`` rows keep whatever columns the SDK gave, and only the
three documented columns above are read by the connector; every other key of every row is carried
through as stated catalog metadata and interpreted by nothing. ``schema`` and ``indexes`` rows are
carried through the same way; ``indexes`` rows need a ``name`` for the clock it declares.
"""

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.config import ObjectStoreConfigError
from neptune_deploy.sources.stated_records import DocumentInvalid, parse_json

FORMAT: Final = "neptune.rerun_catalog_export"
VERSION: Final = 1
MAX_EXPORT_BYTES: Final = 64 * 1024 * 1024
CATALOG_NAME: Final = re.compile(r"[a-z0-9][a-z0-9\-]{0,62}")
DATASET_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}")


@dataclass(frozen=True)
class RerunExport:
    catalog: str
    dataset: Mapping[str, JsonValue]
    segments: tuple[JsonValue, ...]
    schema: tuple[JsonValue, ...]
    indexes: tuple[JsonValue, ...]

    @property
    def dataset_id(self) -> str:
        value = self.dataset["id"]
        assert isinstance(value, str)
        return value


def read_export(path: str, *, limit: int = MAX_EXPORT_BYTES) -> bytes:
    """The bytes of the export file at ``path``: a regular file the operator named, not a symlink,
    at most ``limit`` bytes."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)  # a FIFO opens at once and is refused, never waited on
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ObjectStoreConfigError("the catalog export cannot be opened") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ObjectStoreConfigError("the catalog export is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise ObjectStoreConfigError("the catalog export cannot be read") from exc
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise ObjectStoreConfigError(f"the catalog export is larger than {limit} bytes")
    return data


def parse_export(data: bytes) -> RerunExport:
    """The export in ``data``, or ``ObjectStoreConfigError`` naming what is wrong."""
    try:
        document = parse_json(data)
    except DocumentInvalid as exc:
        raise ObjectStoreConfigError(f"the catalog export is not strict JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ObjectStoreConfigError("the catalog export is not a JSON object")
    if document.get("format") != FORMAT or document.get("version") != VERSION:
        raise ObjectStoreConfigError(f"the catalog export is not {FORMAT} version {VERSION}")
    allowed = {"format", "version", "catalog", "dataset", "segments", "schema", "indexes"}
    if unknown := sorted(set(document) - allowed):
        raise ObjectStoreConfigError(f"the catalog export has unknown members: {unknown}")
    catalog = document.get("catalog")
    if not isinstance(catalog, str) or not CATALOG_NAME.fullmatch(catalog):
        raise ObjectStoreConfigError("catalog names this Hub: lower-case letters, digits and -")
    dataset = document.get("dataset")
    if (
        not isinstance(dataset, Mapping)
        or not isinstance(dataset.get("id"), str)
        or not DATASET_ID.fullmatch(str(dataset["id"]))
    ):
        raise ObjectStoreConfigError("dataset is an object with an id of letters, digits . _ and -")
    lists: dict[str, tuple[JsonValue, ...]] = {}
    for name in ("segments", "schema", "indexes"):
        value = document.get(name, [])
        if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
            raise ObjectStoreConfigError(f"{name} is a list of objects")
        lists[name] = tuple(value)
    return RerunExport(catalog, dataset, lists["segments"], lists["schema"], lists["indexes"])
