"""Every canonical record kind and its strict reader: the tables an ingest package holds.

One entry per kind, in the order of ADR 0017's families. The JSON Schema (``neptune.model.schema``)
and the ingest package (``neptune.store``) both read this list, so a new record kind is added here
once. A test checks that every record class in ``neptune.model`` is listed.

A kind added after the M1 gate says so with a class attribute ``since``, the schema version that
added it; every other kind is from version 1. A package written at version ``v`` holds a table for
each kind of ``kinds_at(v)``, and no other (ADR 0037 §1).
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from neptune.model.alignment import (
    ClockMapping,
    FrameBinding,
    IdentityLink,
    RunAssembly,
    SnapshotBinding,
    clock_mapping_from_json,
    frame_binding_from_json,
    identity_link_from_json,
    run_assembly_from_json,
    snapshot_binding_from_json,
)
from neptune.model.configuration import (
    ConfigurationSnapshot,
    ConfigurationValue,
    configuration_snapshot_from_json,
    configuration_value_from_json,
)
from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.jsonvalue import JsonValue
from neptune.model.lifecycle import (
    AuthorisationEnvelope,
    ChangeRecord,
    CommissioningBaseline,
    IncidentRecord,
    Intervention,
    MaintenanceEvent,
    RequalificationRecord,
    RiskAssessment,
    authorisation_envelope_from_json,
    change_record_from_json,
    commissioning_baseline_from_json,
    incident_record_from_json,
    intervention_from_json,
    maintenance_event_from_json,
    requalification_record_from_json,
    risk_assessment_from_json,
)
from neptune.model.machine import (
    Calibration,
    HardwareComponent,
    HardwareConfiguration,
    Machine,
    SoftwareConfiguration,
    calibration_from_json,
    hardware_component_from_json,
    hardware_configuration_from_json,
    machine_from_json,
    software_configuration_from_json,
)
from neptune.model.provenance import TransformRecord, transform_record_from_json
from neptune.model.record import OLDEST_READABLE_VERSION, check_schema_version
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
        (ConfigurationSnapshot, configuration_snapshot_from_json),
        (ConfigurationValue, configuration_value_from_json),
        (Site, site_from_json),
        (Asset, asset_from_json),
        (SpatialArtifact, spatial_artifact_from_json),
        (Image, image_from_json),
        (Video, video_from_json),
        (DocumentRecord, document_record_from_json),
        (DocumentBlock, document_block_from_json),
        (StructuredTable, structured_table_from_json),
        (StructuredRecord, structured_record_from_json),
        (IdentityLink, identity_link_from_json),
        (ClockMapping, clock_mapping_from_json),
        (FrameBinding, frame_binding_from_json),
        (RunAssembly, run_assembly_from_json),
        (SnapshotBinding, snapshot_binding_from_json),
        (CommissioningBaseline, commissioning_baseline_from_json),
        (AuthorisationEnvelope, authorisation_envelope_from_json),
        (Intervention, intervention_from_json),
        (MaintenanceEvent, maintenance_event_from_json),
        (RequalificationRecord, requalification_record_from_json),
        (IncidentRecord, incident_record_from_json),
        (ChangeRecord, change_record_from_json),
        (RiskAssessment, risk_assessment_from_json),
    )
}

# The schema version that added each kind: the version every record of it is written at.
KIND_SINCE: Final[Mapping[str, int]] = {
    kind: getattr(cls, "since", OLDEST_READABLE_VERSION) for kind, (cls, _) in RECORD_KINDS.items()
}


def kinds_at(version: int) -> tuple[str, ...]:
    """The record kinds of schema version ``version``: a package of that version's tables."""
    check_schema_version(version)
    return tuple(kind for kind, since in KIND_SINCE.items() if since <= version)


def package_version(kinds: Iterable[str]) -> int:
    """The lowest schema version that holds records of ``kinds``: what a package is written at.

    A package holding only kinds of version 1 is a version 1 package, byte for byte what a
    version 1 writer wrote, so an addition to the model never changes a package that does not
    use it (ADR 0037 §1).
    """
    return max((KIND_SINCE[kind] for kind in kinds), default=OLDEST_READABLE_VERSION)


def record_key(record: Any) -> str:
    """The key a table is sorted by: a record's id, or an artifact's content id (ADR 0002 §1)."""
    key = record.content_id if isinstance(record, SourceArtifact) else record.id
    if not isinstance(key, str):
        raise TypeError(f"not a record: {record!r}")
    return key
