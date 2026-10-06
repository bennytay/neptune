"""The lifecycle mapper: lifecycle records as a declared, provenanced mapping over the compiler's
canonical records (ADR 0002, ADR 0003).

A compiler package's ``StructuredTable`` rows (CMMS work orders, tickets, asset and zone registers)
become the compiler's lifecycle kinds through declared mapping files, and its ``DocumentRecord``
structure (labelled fields, tables, sections, lists) through declared document templates, in a new
package whose every value cites the exact source cell or span. The base package is never changed;
the same package, files and mapper version give a byte-identical result.

- ``map_files(base, mappings, templates)``: the new package's files, in memory;
- ``map_package(base_root, mappings, out, templates)``: read, map, write through the compiler's
  streaming writer (ADR 0012 §3); returns the package id;
- ``preset(name)`` / ``PRESETS``: the mapping files shipped for common exports;
- ``template_preset(name)`` / ``TEMPLATE_PRESETS``: the document templates shipped for common forms;
- ``TemplateRegistry`` / ``load_template``: the document templates a run may match.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from neptune.model.ids import ContentId
from neptune.store.package import (
    IngestPackage,
    PackageError,
    package_files,
    read_package,
)
from neptune.store.writer import write_package_stream
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
from neptune_deploy.lifecycle.run import check_declared, iter_records, map_records
from neptune_deploy.lifecycle.templates import (
    TEMPLATE_SCHEMA,
    DocumentTemplate,
    TemplateRegistry,
    load_template,
    parse_template,
)

PRESET_DIR: Final = Path(__file__).parent / "presets"
PRESETS: Final = tuple(sorted(path.stem for path in PRESET_DIR.glob("*.json")))
TEMPLATE_PRESET_DIR: Final = PRESET_DIR / "templates"
TEMPLATE_PRESETS: Final = tuple(sorted(p.stem for p in TEMPLATE_PRESET_DIR.glob("*.json")))

__all__ = [
    "DOCUMENT_FINDINGS",
    "DOCUMENT_MAPPER_ID",
    "DOCUMENT_MAPPER_VERSION",
    "FINDINGS",
    "MAPPER_ID",
    "MAPPER_VERSION",
    "MAPPING_SCHEMA",
    "PRESETS",
    "TEMPLATE_PRESETS",
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
    "template_preset",
]


def preset(name: str) -> LifecycleMapping:
    """A shipped mapping file by name (``PRESETS``)."""
    if name not in PRESETS:
        raise MappingError(f"no preset {name!r}: {list(PRESETS)}")
    return load_mapping(PRESET_DIR / f"{name}.json")


def template_preset(name: str) -> DocumentTemplate:
    """A shipped document template by name (``TEMPLATE_PRESETS``)."""
    if name not in TEMPLATE_PRESETS:
        raise MappingError(f"no template preset {name!r}: {list(TEMPLATE_PRESETS)}")
    return load_template(TEMPLATE_PRESET_DIR / f"{name}.json")


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
    scratch: Path | None = None,
) -> ContentId:
    """Read and verify the package at ``base_root``, map it, write the new package to ``out``.

    ``out`` may not be the base package or inside it: the base is never changed (ADR 0002 §1). The
    records are written as they are mapped (ADR 0012 §3); sorted runs spill under ``scratch``, by
    default the directory ``out`` is made in, and are removed when the write ends.
    """
    base, target = base_root.resolve(), out.resolve()
    if target == base or base in target.parents:
        raise PackageError(f"{out} is inside the base package {base_root}; write it elsewhere")
    if not mappings and not templates:
        raise MappingError("name at least one mapping file or document template")
    check_declared(mappings, templates)
    spill = out.parent if scratch is None else scratch
    spill.mkdir(parents=True, exist_ok=True)
    records = iter_records(read_package(base_root), mappings, templates)
    return write_package_stream(out, records, scratch=spill)
