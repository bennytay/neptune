"""Parsing the Ledger records the coverage consolidator reads (ADR 0015 §1).

Parsing is kept apart from the coverage policy: each parser turns one Ledger record into a typed
value or raises ``Malformed``, and decides nothing about coverage. ``consolidate.coverage`` applies
the policy.

Compiler kinds are read with the compiler's own strict readers, so Memory reads exactly the
package-schema shape:

- ``run`` (root ADR 0018 §1) and ``stream`` (ADR 0018 §2): a session and each channel recorded in
  it, with the count and first and last instants the source's index or summary declares;
- ``ingest_finding`` (root ADR 0017 §9): what the compiler found wrong with the evidence, with the
  records it qualifies and the severity it judged;
- ``timestamp_domain``: a clock's resolution (seconds per tick), and whether it declares itself
  civil;
- ``snapshot_binding`` (root ADR 0050 §8), ``hardware_configuration`` and ``hardware_component``
  (root ADR 0019 §4): the sensors each configuration bound to a run declares;
- ``image`` and ``video``: media whose capture declares the device that took it;
- ``run_assembly`` and ``source_revision`` (through ``run_records``): which files a run holds.

One kind is a Ledger stand-in until the catalog API publishes the time index's series rows
(Ledger ADR 0015 §2): ``series_interval {stream, clock, first, last, rows_known, rows_unknown}``,
exactly the ``time_interval`` row the Ledger writes per series file and clock (``subject =
"series"``): the least and greatest known tick on that clock, and how many rows have a known tick
there and how many do not. Like the Ledger, there is no row for a clock no row has a known tick on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.model.alignment import SnapshotBinding, snapshot_binding_from_json
from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.knowledge import Ambiguous, Known
from neptune.model.machine import (
    HardwareComponent,
    HardwareConfiguration,
    hardware_component_from_json,
    hardware_configuration_from_json,
)
from neptune.model.reference import timestamp_domain_from_json
from neptune.model.run import Stream, stream_from_json
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune.model.world import Image, Video, image_from_json, video_from_json
from neptune_memory.consolidate.identity_records import clock, declared
from neptune_memory.consolidate.run_records import Inferred, Malformed

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from fractions import Fraction

    from neptune.model.ids import LogicalId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune_memory.schema.interval import CivilClock

_T = TypeVar("_T")

# Ledger record kinds the coverage consolidator reads (``run``, ``run_assembly`` and
# ``source_revision`` are ``run_records``').
STREAM: Final = "stream"
INGEST_FINDING: Final = "ingest_finding"
TIMESTAMP_DOMAIN: Final = "timestamp_domain"
SNAPSHOT_BINDING: Final = "snapshot_binding"
HARDWARE_CONFIGURATION: Final = "hardware_configuration"
HARDWARE_COMPONENT: Final = "hardware_component"
IMAGE: Final = "image"
VIDEO: Final = "video"
SERIES_INTERVAL: Final = "series_interval"

__all__ = [
    "HARDWARE_COMPONENT",
    "HARDWARE_CONFIGURATION",
    "IMAGE",
    "INGEST_FINDING",
    "SERIES_INTERVAL",
    "SNAPSHOT_BINDING",
    "STREAM",
    "TIMESTAMP_DOMAIN",
    "VIDEO",
    "Domain",
    "Inferred",
    "Malformed",
    "SeriesInterval",
    "binding",
    "component",
    "configuration",
    "domain",
    "finding",
    "media",
    "series",
    "stream",
]


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    """A compiler reader over one record; whatever it refuses is malformed here."""
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("assertion_kind") == "inferred":
        raise Inferred(f"an inferred {record.get('kind')!r} record is a derived/ record")
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def _declared_ids(identifiers: Iterable[Knowledge[LogicalId]]) -> None:
    """Every id an identifier list states is a declared value (ADR 0006 §9)."""
    for identifier in identifiers:
        if isinstance(identifier, Known):
            declared(identifier.value)
        elif isinstance(identifier, Ambiguous):
            for candidate in identifier.candidates:
                declared(candidate.value)


def stream(record: Mapping[str, object]) -> Stream:
    return _strict(stream_from_json, record)


def finding(record: Mapping[str, object]) -> IngestFinding:
    return _strict(ingest_finding_from_json, record)


def binding(record: Mapping[str, object]) -> SnapshotBinding:
    return _strict(snapshot_binding_from_json, record)


def configuration(record: Mapping[str, object]) -> HardwareConfiguration:
    return _strict(hardware_configuration_from_json, record)


def component(record: Mapping[str, object]) -> HardwareComponent:
    """The compiler's component; a declared identifier that is blank or padded is malformed."""
    parsed = _strict(hardware_component_from_json, record)
    _declared_ids(parsed.identifiers)
    return parsed


def media(record: Mapping[str, object]) -> Image | Video:
    """An ``image`` or ``video``; a declared device identifier that is blank or padded is
    malformed."""
    parsed: Image | Video
    if record.get("kind") == VIDEO:
        parsed = _strict(video_from_json, record)
    else:
        parsed = _strict(image_from_json, record)
    _declared_ids(parsed.capture.device_identifiers)
    return parsed


@dataclass(frozen=True)
class Domain:
    """A ``TimestampDomain``: its stated resolution (seconds per tick, ``None`` when not stated)
    and the ``CivilClock`` it names when it declares itself civil (ADR 0002 §3)."""

    record: RecordId
    resolution: Fraction | None
    civil: CivilClock | None


def domain(record: Mapping[str, object]) -> Domain:
    parsed = _strict(timestamp_domain_from_json, record)
    resolution = parsed.resolution.value if isinstance(parsed.resolution, Known) else None
    return Domain(parsed.id, resolution, clock(record).civil)


@dataclass(frozen=True)
class SeriesInterval:
    """One series file's coverage on one of its stream's clocks, as the Ledger indexes it:
    ``[first, last]`` are the least and greatest known ticks (both inclusive), ``rows_known`` the
    rows with a known tick on the clock and ``rows_unknown`` the rows without one."""

    stream: RecordId
    clock: RecordId
    first: int
    last: int
    rows_known: int
    rows_unknown: int


_SERIES_KEYS: Final = frozenset(
    {"clock", "first", "kind", "last", "rows_known", "rows_unknown", "stream"}
)


def _int(record: Mapping[str, object], name: str, low: int) -> int:
    value = record[name]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= INT64_MAX:
        raise Malformed(f"{name!r} must be an integer in [{low}, 2^63), got {value!r}")
    return value


def series(record: Mapping[str, object]) -> SeriesInterval:
    keys = set(record)
    if keys != _SERIES_KEYS:
        missing, extra = sorted(_SERIES_KEYS - keys), sorted(keys - _SERIES_KEYS)
        raise Malformed(f"series_interval keys: missing {missing}, unexpected {extra}")
    try:
        stream_id = parse_record_id(record["stream"])  # type: ignore[arg-type]
        clock_id = parse_record_id(record["clock"])  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc
    first, last = _int(record, "first", INT64_MIN), _int(record, "last", INT64_MIN)
    known, unknown = _int(record, "rows_known", 1), _int(record, "rows_unknown", 0)
    if last < first:
        raise Malformed(f"series_interval's last tick {last} is before its first {first}")
    if known == 1 and first != last:
        raise Malformed("a series_interval with one known row has first == last")
    return SeriesInterval(stream_id, clock_id, first, last, known, unknown)
