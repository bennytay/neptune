"""The lifecycle mapper: lifecycle records as a declared, provenanced mapping over the compiler's
canonical records (ADR 0002, ADR 0003).

A compiler package's ``StructuredTable`` rows (CMMS work orders, tickets, asset and zone registers)
become the compiler's lifecycle kinds through declared mapping files, and its ``DocumentRecord``
structure (labelled fields, tables, sections, lists) through declared document templates, in a new
package whose every value cites the exact source cell or span. The base package is never changed;
the same package, files and mapper version give a byte-identical result.

- ``map_files(base, mappings, templates)``: the new package's files, in memory;
- ``map_package(base_root, mappings, out, templates)``: read, map, write; returns the package id;
- ``preset(name)`` / ``PRESETS``: the mapping files shipped for common exports;
- ``TemplateRegistry`` / ``load_template``: the document templates a run may match.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from neptune.model.ids import ContentId
from neptune.store.package import IngestPackage, package_files, read_package, write_package
from neptune_deploy.lifecycle.documents import (
    DOCUMENT_MAPPER_ID,
    DOCUMENT_MAPPER_VERSION,
)
from neptune_deploy.lifecycle.documents import FINDINGS as DOCUMENT_FINDINGS
from neptune_deploy.lifecycle.mapper import FINDINGS, MAPPER_ID, MAPPER_VERSION
from neptune_deploy.lifecycle.mapping import (
    MAPPING_SCHEMA,
    LifecycleMapping,
    MappingError,
    load_mapping,
    parse_mapping,
)
from neptune_deploy.lifecycle.run import map_records
from neptune_deploy.lifecycle.templates import (
    TEMPLATE_SCHEMA,
    DocumentTemplate,
    TemplateRegistry,
    load_template,
    parse_template,
)

PRESET_DIR: Final = Path(__file__).parent / "presets"
PRESETS: Final = tuple(sorted(path.stem for path in PRESET_DIR.glob("*.json")))

__all__ = [
    "DOCUMENT_FINDINGS",
    "DOCUMENT_MAPPER_ID",
    "DOCUMENT_MAPPER_VERSION",
    "FINDINGS",
    "MAPPER_ID",
    "MAPPER_VERSION",
    "MAPPING_SCHEMA",
    "PRESETS",
    "TEMPLATE_SCHEMA",
    "DocumentTemplate",
    "LifecycleMapping",
    "MappingError",
    "TemplateRegistry",
    "load_mapping",
    "load_template",
    "map_files",
    "map_package",
    "parse_mapping",
    "parse_template",
    "preset",
]


def preset(name: str) -> LifecycleMapping:
    """A shipped mapping file by name (``PRESETS``)."""
    if name not in PRESETS:
        raise MappingError(f"no preset {name!r}: {list(PRESETS)}")
    return load_mapping(PRESET_DIR / f"{name}.json")


def map_files(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
) -> dict[str, bytes]:
    """Every file of the mapped package (``neptune.store.package.package_files``)."""
    if not mappings and not templates:
        raise MappingError("name at least one mapping file or document template")
    return package_files(map_records(base, mappings, templates))


def map_package(
    base_root: Path,
    mappings: Sequence[LifecycleMapping],
    out: Path,
    templates: Sequence[DocumentTemplate] = (),
) -> ContentId:
    """Read and verify the package at ``base_root``, map it, write the new package to ``out``."""
    return write_package(out, map_files(read_package(base_root), mappings, templates))
