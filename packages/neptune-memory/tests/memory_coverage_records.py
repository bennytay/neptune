"""Compiler-shaped coverage records for tests, built with the compiler's own types.

Every ``stream``, ``ingest_finding``, ``snapshot_binding``, ``hardware_configuration``,
``hardware_component``, ``image`` and ``video`` here is constructed as the compiler model class and
serialised with its ``to_json``, so a test can never feed the coverage consolidator a shape the
compiler would not write. ``series_interval`` is the Ledger stand-in for the time index's series
rows (Memory ADR 0015 §1). Runs, assemblies, revisions and clocks are ``memory_run_records``'.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from memory_identity_records import OBSERVED, TRANSFORM, Record, cite, provenance, rid, source
from neptune.identity.findings import ingest_finding
from neptune.model.alignment import SnapshotBinding, SnapshotKind
from neptune.model.finding import FindingCategory, Severity
from neptune.model.knowledge import Known, NotCovered, Unknown
from neptune.model.machine import ComponentCategory, HardwareComponent, HardwareConfiguration
from neptune.model.run import Stream
from neptune.model.series import SeriesProvenance, step_template
from neptune.model.world import Capture, Image, Video

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.knowledge import Knowledge
    from neptune.model.time import Timestamp

HZ_100: Final = 10**7  # one sample every 10 ms, in nanosecond ticks
SECOND: Final = 10**9


def stream(
    name: str,
    run_id: RecordId,
    clocks: Sequence[RecordId],
    *,
    count: int | None = None,
    first: Timestamp | None = None,
    last: Timestamp | None = None,
    recording: str = "bag",
) -> tuple[Record, RecordId]:
    """The ``Stream`` topic ``name`` of the file ``recording`` declares, with its source index's
    count and first and last instants (``None``: the index does not state it)."""
    declared = provenance(cite(f"{recording} {name}"))
    record = Stream(
        id=rid("stream", declared.evidence),
        provenance=declared,
        run=run_id,
        topic=Known(name),
        schema_name=Unknown(),
        schema_encoding=Unknown(),
        schema_definition=Unknown(),
        message_encoding=Known("cdr"),
        metadata=(),
        clocks=tuple(clocks),
        message_count=Known(count) if count is not None else NotCovered(),
        first=Known(first) if first is not None else NotCovered(),
        last=Known(last) if last is not None else NotCovered(),
        series=SeriesProvenance(
            source(recording),
            (step_template("byte_range", per_row=("length", "offset")),),
            OBSERVED,
        ),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def series(
    stream_id: RecordId,
    clock: RecordId,
    first: int,
    last: int,
    rows_known: int,
    rows_unknown: int = 0,
) -> Record:
    """The Ledger's series coverage of one stream on one clock (a ``series_interval``)."""
    return {
        "kind": "series_interval",
        "stream": stream_id,
        "clock": clock,
        "first": first,
        "last": last,
        "rows_known": rows_known,
        "rows_unknown": rows_unknown,
    }


def finding(
    code: str,
    recording: str,
    *,
    category: FindingCategory = FindingCategory.CORRUPT,
    severity: Severity = Severity.ERROR,
    records: Sequence[RecordId] = (),
) -> Record:
    """An ``IngestFinding`` the adapter reports about the bytes of ``recording``."""
    return ingest_finding(  # type: ignore[return-value]
        code=code,
        category=category,
        severity=severity,
        subject=cite(recording, 4096, 512),
        transform=TRANSFORM,
        message=f"{code} in {recording}",
        records=records,
    ).to_json()


def configuration(name: str, machine: LogicalId) -> tuple[Record, RecordId]:
    """A ``HardwareConfiguration`` declared by the file ``name`` (a URDF, a manifest)."""
    declared = provenance(cite(name))
    record = HardwareConfiguration(
        id=rid("hardware_configuration", declared.evidence),
        provenance=declared,
        machine=Known(machine),
        name=Unknown(),
        revision=Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def component(
    configuration_file: str,
    configuration_id: RecordId,
    name: str,
    *identifiers: LogicalId,
    category: ComponentCategory = ComponentCategory.SENSOR,
    ambiguous: Knowledge[LogicalId] | None = None,
) -> tuple[Record, RecordId]:
    """A component the configuration file declares, by name, with its declared identifiers."""
    declared = provenance(cite(f"{configuration_file} {name}"))
    ids: list[Knowledge[LogicalId]] = [
        Known(i) for i in sorted(identifiers, key=lambda i: (i.namespace, i.value))
    ]
    if ambiguous is not None:
        ids.append(ambiguous)
    record = HardwareComponent(
        id=rid("hardware_component", declared.evidence),
        provenance=declared,
        configuration=configuration_id,
        category=category,
        name=Known(name),
        model=Unknown(),
        identifiers=tuple(ids),
        frame=NotCovered(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def binding(name: str, run_id: RecordId, snapshot: RecordId) -> Record:
    """A ``SnapshotBinding`` of a run to a hardware configuration, stated by ``name``."""
    declared = provenance(cite(f"binding {name}"))
    return SnapshotBinding(  # type: ignore[return-value]
        id=rid("snapshot_binding", declared.evidence),
        provenance=declared,
        run=run_id,
        snapshot=snapshot,
        snapshot_kind=SnapshotKind.HARDWARE_CONFIGURATION,
        validity=Unknown(),
    ).to_json()


def _capture(device: Sequence[Knowledge[LogicalId]]) -> Capture:
    return Capture(
        time=Unknown(),
        position=Unknown(),
        device_manufacturer=Unknown(),
        device_model=Unknown(),
        device_identifiers=tuple(device),
    )


def image(name: str, *device: Knowledge[LogicalId]) -> tuple[Record, RecordId]:
    """The still image file ``name``, whose EXIF declares ``device``."""
    declared = provenance(cite(name))
    record = Image(
        id=rid("image", declared.evidence),
        provenance=declared,
        width=640,
        height=480,
        encoding="jpeg",
        orientation=Unknown(),
        capture=_capture(device),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def video(name: str, clock: RecordId, *device: Knowledge[LogicalId]) -> tuple[Record, RecordId]:
    """The video file ``name``'s first track, whose container declares ``device``."""
    declared = provenance(cite(name))
    record = Video(
        id=rid("video", declared.evidence),
        provenance=declared,
        track=0,
        width=1280,
        height=720,
        encoding="h264",
        clock=clock,
        frame_count=Unknown(),
        duration=Unknown(),
        capture=_capture(device),
    )
    return record.to_json(), record.id  # type: ignore[return-value]
