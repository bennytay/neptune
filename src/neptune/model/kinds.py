"""Every canonical record kind and its strict reader: the tables an ingest package holds.

One entry per kind, in the order of ADR 0017's families. The JSON Schema (``neptune.model.schema``)
and the ingest package (``neptune.store``) both read this list, so a new record kind is added here
once. A test checks that every record class in ``neptune.model`` is listed.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.jsonvalue import JsonValue
from neptune.model.machine import (
    Calibration,
    DescriptionExpansion,
    DescriptionExtension,
    HardwareComponent,
    HardwareConfiguration,
    HardwareSpecification,
    Machine,
    SoftwareConfiguration,
    calibration_from_json,
    description_expansion_from_json,
    description_extension_from_json,
    hardware_component_from_json,
    hardware_configuration_from_json,
    hardware_specification_from_json,
    machine_from_json,
    software_configuration_from_json,
)
from neptune.model.provenance import TransformRecord, transform_record_from_json
from neptune.model.reference import (
    Frame,
    FrameGraph,
    FrameTransform,
    TimestampDomain,
    frame_from_json,
    frame_graph_from_json,
    frame_transform_from_json,
    timestamp_domain_from_json,
)
from neptune.model.run import Run, Stream, run_from_json, stream_from_json
from neptune.model.source import (
    SourceAbsence,
    SourceArtifact,
    SourceRevision,
    source_absence_from_json,
    source_artifact_from_json,
    source_revision_from_json,
)
from neptune.model.world import (
    Asset,
    DocumentBlock,
    DocumentRecord,
    Image,
    Site,
    SpatialArtifact,
    StructuredRecord,
    StructuredTable,
    Video,
    asset_from_json,
    document_block_from_json,
    document_record_from_json,
    image_from_json,
    site_from_json,
    spatial_artifact_from_json,
    structured_record_from_json,
    structured_table_from_json,
    video_from_json,
)

Reader = Callable[[JsonValue], Any]

# Record class and reader, by kind. The kind names the table: records/<kind>.jsonl (ADR 0002).
RECORD_KINDS: Final[Mapping[str, tuple[type, Reader]]] = {
    cls.kind: (cls, read)
    for cls, read in (
        (SourceArtifact, source_artifact_from_json),
        (SourceRevision, source_revision_from_json),
        (SourceAbsence, source_absence_from_json),
        (TransformRecord, transform_record_from_json),
        (IngestFinding, ingest_finding_from_json),
        (TimestampDomain, timestamp_domain_from_json),
        (FrameGraph, frame_graph_from_json),
        (Frame, frame_from_json),
        (FrameTransform, frame_transform_from_json),
        (Run, run_from_json),
        (Stream, stream_from_json),
        (Machine, machine_from_json),
        (HardwareConfiguration, hardware_configuration_from_json),
        (HardwareComponent, hardware_component_from_json),
        (SoftwareConfiguration, software_configuration_from_json),
        (Calibration, calibration_from_json),
        (HardwareSpecification, hardware_specification_from_json),
        (DescriptionExtension, description_extension_from_json),
        (DescriptionExpansion, description_expansion_from_json),
        (Site, site_from_json),
        (Asset, asset_from_json),
        (SpatialArtifact, spatial_artifact_from_json),
        (Image, image_from_json),
        (Video, video_from_json),
        (DocumentRecord, document_record_from_json),
        (DocumentBlock, document_block_from_json),
        (StructuredTable, structured_table_from_json),
        (StructuredRecord, structured_record_from_json),
    )
}


def record_key(record: Any) -> str:
    """The key a table is sorted by: a record's id, or an artifact's content id (ADR 0002 §1)."""
    key = record.content_id if isinstance(record, SourceArtifact) else record.id
    if not isinstance(key, str):
        raise TypeError(f"not a record: {record!r}")
    return key
