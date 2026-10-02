"""The optional Neptune manifest: what a folder's evidence does not say, declared (ADR 0047).

A manifest is the escape hatch when discovery is not enough: a ``neptune.yaml`` at the root of a
folder that declares runs and the machines, sites, tasks and software they involved, and which
adapter (with which options) reads which paths. It never restates source content, and nothing it
declares overrides what the evidence shows: every declaration is ``stated``, with the manifest
file (itself a source of the package) as its provenance, and one that contradicts the evidence
is a finding.

- ``reader``: bytes to a tree, as hostile input (size, depth, node count; no anchors or aliases).
- ``schema``: the version-1 schema (``Manifest`` and its declarations) and its JSON Schema.
- ``load``: finding the manifest in a folder, reading it safely, and its lineage.
- ``generate``: ``neptune init-manifest``, a commented manifest from a dry run (imports the SDK,
  so it is not imported here).

Imports ``model``, ``identity`` and ``discovery``; the runtime and the SDK import it.
"""

from neptune.manifest.load import (
    MANIFEST_ID,
    MANIFEST_NAMES,
    MANIFEST_VERSION,
    LoadedManifest,
    discover,
    locate,
    parse_bytes,
    read,
)
from neptune.manifest.reader import MAX_BYTES, MAX_DEPTH, MAX_NODES, ManifestError
from neptune.manifest.schema import (
    SCHEMA_VERSION,
    Entity,
    Manifest,
    RunDecl,
    SourceRule,
    json_schema,
    parse_manifest,
)

__all__ = [
    "MANIFEST_ID",
    "MANIFEST_NAMES",
    "MANIFEST_VERSION",
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_NODES",
    "SCHEMA_VERSION",
    "Entity",
    "LoadedManifest",
    "Manifest",
    "ManifestError",
    "RunDecl",
    "SourceRule",
    "discover",
    "json_schema",
    "locate",
    "parse_bytes",
    "parse_manifest",
    "read",
]
