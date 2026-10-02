"""ROS 2 diagnostics as stated event records, through a declared vendor mapping (ADR 0010 §7).

A compiler package's diagnostics (a JSON export read by the tabular adapter) become a table of
events whose ``event_kind`` is the target string the mapping file declares for the status code or
name. A code outside the mapping is a finding and stays as declared. Diagnostics inside a bag are
the compiler's to decode: this package never opens a bag.

- ``map_diagnostics_files(base, mappings)``: the new package's files, in memory;
- ``preset("ros2_diagnostics")``: the mapping file shipped for ``diagnostic_msgs``' own codes.
"""

from pathlib import Path
from typing import Final

from neptune_deploy.diagnostics.mapper import (
    FINDINGS,
    MAPPER_ID,
    MAPPER_VERSION,
    map_diagnostics,
    map_diagnostics_files,
)
from neptune_deploy.diagnostics.mapping import (
    MAPPING_SCHEMA,
    DiagnosticsMapping,
    load_mapping,
    parse_mapping,
)
from neptune_deploy.lifecycle.mapping import MappingError

PRESET_DIR: Final = Path(__file__).parent / "presets"
PRESETS: Final = tuple(sorted(path.stem for path in PRESET_DIR.glob("*.json")))

__all__ = [
    "FINDINGS",
    "MAPPER_ID",
    "MAPPER_VERSION",
    "MAPPING_SCHEMA",
    "PRESETS",
    "DiagnosticsMapping",
    "MappingError",
    "load_mapping",
    "map_diagnostics",
    "map_diagnostics_files",
    "parse_mapping",
    "preset",
]


def preset(name: str) -> DiagnosticsMapping:
    """A shipped mapping file by name (``PRESETS``)."""
    if name not in PRESETS:
        raise MappingError(f"no preset {name!r}: {list(PRESETS)}")
    return load_mapping(PRESET_DIR / f"{name}.json")
