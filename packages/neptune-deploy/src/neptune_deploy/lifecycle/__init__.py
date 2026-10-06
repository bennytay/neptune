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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

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
from neptune_deploy.lifecycle.mapper import (
    FINDINGS,
    MAPPER_ID,
    MAPPER_VERSION,
    source_paths,
    source_zones,
)
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

if TYPE_CHECKING:
    from neptune_deploy.eventlogs.mapping import EventLogMapping

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
    "presets",
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


def presets(
    names: Sequence[str],
    source_zones: Mapping[tuple[str, str], str] | None = None,
) -> tuple[list[LifecycleMapping], list["EventLogMapping"]]:
    """The shipped mappings named (lifecycle ``PRESETS`` and event-log presets), each with the
    civil zones the caller declares per ``(preset, source path)`` (ADR 0017 §2). A zone for a
    preset the run does not name is a ``MappingError``: it would otherwise be silently unused."""
    from neptune_deploy import eventlogs

    zones = dict(source_zones or {})
    unknown = sorted({name for name, _ in zones} - set(names))
    if unknown:
        raise MappingError(f"--source-zone names presets this run does not map: {unknown}")
    lifecycle: list[LifecycleMapping] = []
    logs: list[EventLogMapping] = []
    for name in dict.fromkeys(names):
        mine = {
            source: zone for (preset_name, source), zone in zones.items() if preset_name == name
        }
        if name in eventlogs.PRESETS:
            logs.append(eventlogs.preset(name).with_source_zones(mine))
        else:
            lifecycle.append(preset(name).with_source_zones(mine))
    return lifecycle, logs


def map_files(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
    event_logs: Sequence["EventLogMapping"] = (),
) -> dict[str, bytes]:
    """Every file of the mapped package (``neptune.store.package.package_files``)."""
    if not mappings and not templates and not event_logs:
        raise MappingError("name at least one mapping file or document template")
    return package_files(map_records(base, mappings, templates, event_logs))


def map_package(
    base_root: Path,
    mappings: Sequence[LifecycleMapping],
    out: Path,
    templates: Sequence[DocumentTemplate] = (),
    scratch: Path | None = None,
    event_logs: Sequence["EventLogMapping"] = (),
) -> ContentId:
    """Read and verify the package at ``base_root``, map it, write the new package to ``out``.

    ``out`` may not be the base package or inside it: the base is never changed (ADR 0002 §1). The
    records are written as they are mapped (ADR 0012 §3); sorted runs spill under ``scratch``, by
    default the directory ``out`` is made in, and are removed when the write ends.
    """
    base, target = base_root.resolve(), out.resolve()
    if target == base or base in target.parents:
        raise PackageError(f"{out} is inside the base package {base_root}; write it elsewhere")
    if not mappings and not templates and not event_logs:
        raise MappingError("name at least one mapping file or document template")
    check_declared(mappings, templates, event_logs)
    base_package = read_package(base_root)
    _check_sources(base_package, [*mappings, *event_logs])
    # Planned (and every declared zone checked against the tables it reaches) before ``out``.
    records = iter_records(base_package, mappings, templates, event_logs)
    spill = out.parent if scratch is None else scratch
    spill.mkdir(parents=True, exist_ok=True)
    return write_package_stream(out, records, scratch=spill)


def _check_sources(base: IngestPackage, declared: Sequence[Any]) -> None:
    """Refuse, before ``out`` is made, a zone declared for a source the package does not hold."""
    paths = source_paths(base)
    for mapping in declared:
        source_zones(mapping.source_zones, paths)
