"""Log exports as typed event tables, for Memory's event index (ADR 0017).

A syslog, PLC or safety-controller export arrives as a CSV whose times are text. Memory's event
index reads integer times on a named clock, so a declared event-log mapping writes, beside the base
package's records, a typed table: the listed columns verbatim, the time as ``sec`` / ``nanosec`` on
the log's own clock, the clock's ``TimestampDomain`` id, and the row's identifier. It runs inside
``python -m neptune_deploy map`` (``-p syslog_csv``), next to the lifecycle mappings.

- ``preset(name)`` / ``PRESETS``: the mapping files shipped for common log exports.
"""

from pathlib import Path
from typing import Final

from neptune_deploy.eventlogs.mapper import FINDINGS, MAPPER_ID, MAPPER_VERSION, plan_event_logs
from neptune_deploy.eventlogs.mapping import (
    MAPPING_SCHEMA,
    EventLogMapping,
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
    "EventLogMapping",
    "load_mapping",
    "parse_mapping",
    "plan_event_logs",
    "preset",
]


def preset(name: str) -> EventLogMapping:
    """A shipped mapping file by name (``PRESETS``)."""
    if name not in PRESETS:
        raise MappingError(f"no event-log preset {name!r}: {list(PRESETS)}")
    return load_mapping(PRESET_DIR / f"{name}.json")
