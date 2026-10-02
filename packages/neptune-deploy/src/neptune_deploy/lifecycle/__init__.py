"""The lifecycle mapper: lifecycle records as a declared, provenanced mapping over the compiler's
canonical tables (ADR 0002).

A compiler package's ``StructuredTable`` rows (CMMS work orders, tickets, asset and zone registers)
become the compiler's lifecycle kinds through declared mapping files, in a new package whose every
value cites the exact source cell. The base package is never changed; the same package, mapping
files and mapper version give a byte-identical result.

- ``map_files(base, mappings)``: the new package's files, in memory;
- ``map_package(base_root, mappings, out)``: read, map, write; returns the new package id;
- ``preset(name)`` / ``PRESETS``: the mapping files shipped for common exports.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from neptune.model.ids import ContentId
from neptune.store.package import IngestPackage, package_files, read_package, write_package
from neptune_deploy.lifecycle.mapper import FINDINGS, MAPPER_ID, MAPPER_VERSION, map_records
from neptune_deploy.lifecycle.mapping import (
    MAPPING_SCHEMA,
    LifecycleMapping,
    MappingError,
    load_mapping,
    parse_mapping,
)

PRESET_DIR: Final = Path(__file__).parent / "presets"
PRESETS: Final = tuple(sorted(path.stem for path in PRESET_DIR.glob("*.json")))

__all__ = [
    "FINDINGS",
    "MAPPER_ID",
    "MAPPER_VERSION",
    "MAPPING_SCHEMA",
    "PRESETS",
    "LifecycleMapping",
    "MappingError",
    "load_mapping",
    "map_files",
    "map_package",
    "parse_mapping",
    "preset",
]


def preset(name: str) -> LifecycleMapping:
    """A shipped mapping file by name (``PRESETS``)."""
    if name not in PRESETS:
        raise MappingError(f"no preset {name!r}: {list(PRESETS)}")
    return load_mapping(PRESET_DIR / f"{name}.json")


def map_files(base: IngestPackage, mappings: Sequence[LifecycleMapping]) -> dict[str, bytes]:
    """Every file of the mapped package (``neptune.store.package.package_files``)."""
    if not mappings:
        raise MappingError("name at least one mapping file")
    return package_files(map_records(base, mappings))


def map_package(base_root: Path, mappings: Sequence[LifecycleMapping], out: Path) -> ContentId:
    """Read and verify the package at ``base_root``, map it, write the new package to ``out``."""
    return write_package(out, map_files(read_package(base_root), mappings))
