"""Writes the connector API fixtures in this directory (ADR 0009 §10).

Run: python make_connector_fixtures.py

These are recorded-shape fixtures, not captures of a live service: each Roboto document is written
to the shape of the vendor's published models (``roboto`` 0.58.0, ``DatasetRecord``, ``FileRecord``,
``EventRecord``, ``CommentRecord`` and the ``{"data": {"items": [...], "next_token": ...}}`` page),
and ``roboto/`` was validated against those models with ``uv run --no-project --with roboto`` (see
the ADR). The Rerun export carries the documented segment-table column names. One AMR fleet shift,
one arm cell and one legged inspection: no morphology is assumed anywhere.
"""

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
ORG, DATASET = "og_acme_robotics", "ds_shift_0914"
NOW = "2026-09-14T08:00:00.000000+00:00"


def file_record(
    file_id: str, path: str, size: int, version: int, **extra: object
) -> dict[str, Any]:
    return {
        "association_id": DATASET,
        "created": NOW,
        "created_by": "usr_ops_bot",
        "description": None,
        "device_id": None,
        "file_id": file_id,
        "fs_type": "file",
        "ingestion_status": "ingested",
        "metadata": {"robot": path.split("/")[0]},
        "modified": NOW,
        "modified_by": "usr_ops_bot",
        "name": path.rpartition("/")[2],
        "org_id": ORG,
        "origination": "upload-agent",
        "parent_id": None,
        "relative_path": path,
        "size": size,
        "status": "available",
        "storage_type": "uploaded",
        "tags": [],
        "upload_id": "tx_0914",
        "uri": f"s3://roboto-acme-uploads/{ORG}/{DATASET}/{path}",
        "version": version,
        **extra,
    }


def event(
    event_id: str, name: str, start: int, end: int, file_id: str, **extra: object
) -> dict[str, Any]:
    return {
        "associations": [
            {"association_id": file_id, "association_type": "file", "association_version": None},
            {"association_id": DATASET, "association_type": "dataset", "association_version": None},
        ],
        "created": NOW,
        "created_by": "usr_reviewer",
        "custom_fields": {},
        "description": None,
        "display_options": None,
        "end_time": end,
        "event_id": event_id,
        "metadata": {},
        "modified": NOW,
        "modified_by": "usr_reviewer",
        "name": name,
        "org_id": ORG,
        "start_time": start,
        "tags": [],
        **extra,
    }


def page(items: list[dict[str, Any]], next_token: str | None) -> dict[str, Any]:
    return {"data": {"items": items, "next_token": next_token}}


T0 = 1_757_836_800_000_000_000  # 2026-09-14T08:00:00Z in nanoseconds, as Roboto writes event times

documents = {
    "roboto/dataset.json": {
        "data": {
            "administrator": "Roboto",
            "created": NOW,
            "created_by": "usr_ops_bot",
            "custom_fields": {},
            "dataset_id": DATASET,
            "description": "Warehouse AMR shift, one arm cell and one legged patrol, 14 Sep",
            "device_id": None,
            "metadata": {"site": "dc-4", "standard": "ISO 3691-4"},
            "modified": NOW,
            "modified_by": "usr_ops_bot",
            "name": "Shift 2026-09-14",
            "org_id": ORG,
            "roboto_record_version": 3,
            "storage_ctx": {},
            "storage_location": "S3",
            "tags": ["amr", "arm-cell", "legged"],
        }
    },
    "roboto/files_page_1.json": page(
        [
            file_record("fl_amr07_am", "amr07/run_0914_am.mcap", 300, 2),
            file_record("fl_amr07_cal", "amr07/calibration.yaml", 120, 1),
            {
                **file_record("fl_dir_amr07", "amr07", 0, 1),
                "fs_type": "directory",
                "storage_type": "directory",
            },
            {**file_record("fl_old", "amr07/old_run.mcap", 99, 1), "status": "deleted"},
        ],
        "tok_files_2",
    ),
    "roboto/files_page_2.json": page(
        [
            file_record("fl_arm3_pick", "arm_cell3/pick_place_0914.mcap", 220, 1),
            file_record("fl_legged_patrol", "legged01/patrol_0914.bag", 180, 4),
            {**file_record("fl_upload", "legged01/uploading.bag", 0, 1), "status": "reserved"},
        ],
        None,
    ),
    "roboto/events_page_1.json": page(
        [
            event(
                "ev_amr07_stop",
                "protective_stop",
                T0 + 5_000_000_000,
                T0 + 9_500_000_000,
                "fl_amr07_am",
                tags=["safety", "amr"],
                description="Lidar field breach, person in aisle 12",
                metadata={"zone": "aisle-12", "rule": "ISO3691-4 5.2"},
            ),
            event(
                "ev_arm3_grasp",
                "grasp_failure",
                T0 + 61_000_000_000,
                T0 + 61_000_000_000,
                "fl_arm3_pick",
                metadata={"object": "tote-4411"},
            ),
        ],
        "tok_events_2",
    ),
    "roboto/events_page_2.json": page(
        [
            event(
                "ev_legged_slip",
                "foot_slip",
                T0 + 120_000_000_000,
                T0 + 121_250_000_000,
                "fl_legged_patrol",
                tags=["gait"],
                metadata={"leg": "front_left"},
            )
        ],
        None,
    ),
    "roboto/comments.json": page(
        [
            {
                "comment_id": "cm_001",
                "comment_text": "Operator confirmed the stop at 08:00:09 local.",
                "created": NOW,
                "created_by": "usr_reviewer",
                "entity_id": DATASET,
                "entity_type": "dataset",
                "mentions": [],
                "modified": NOW,
                "modified_by": "usr_reviewer",
                "org_id": ORG,
            }
        ],
        None,
    ),
    "rerun/catalog_export.json": {
        "catalog": "hub-acme",
        "dataset": {"id": "ds-warehouse-0914", "name": "Warehouse episodes 14 Sep"},
        "format": "neptune.rerun_catalog_export",
        "indexes": [
            {"kind": "timestamp", "name": "log_time"},
            {"kind": "sequence", "name": "frame"},
        ],
        "schema": [
            {
                "archetype": "rerun.archetypes.Scalars",
                "component": "Scalars:scalars",
                "entity_path": "/arm_cell3/joint_states/shoulder",
                "is_static": False,
            },
            {
                "archetype": "rerun.archetypes.Transform3D",
                "component": "Transform3D:translation",
                "entity_path": "/amr07/odometry",
                "is_static": False,
            },
            {
                "archetype": "rerun.archetypes.Points3D",
                "component": "Points3D:positions",
                "entity_path": "/legged01/lidar",
                "is_static": False,
            },
        ],
        "segments": [
            {
                "property:RecordingInfo:name": "amr07 aisle 12 stop",
                "rerun_last_updated_at": "2026-09-14T09:00:00Z",
                "rerun_layer_names": ["base"],
                "rerun_segment_id": "amr07_aisle12",
                "rerun_size_bytes": 260,
                "rerun_storage_urls": ["s3://fleet-rrd/episodes/amr07_aisle12.rrd"],
            },
            {
                "property:RecordingInfo:name": "arm cell 3 pick and place",
                "rerun_last_updated_at": "2026-09-14T09:05:00Z",
                "rerun_layer_names": ["base", "annotations"],
                "rerun_segment_id": "arm3_pick_0914",
                "rerun_size_bytes": 340,
                "rerun_storage_urls": [
                    "s3://fleet-rrd/episodes/arm3_pick_0914.rrd",
                    "s3://fleet-rrd/episodes/arm3_pick_0914.annotations.rrd",
                ],
            },
            {
                "property:RecordingInfo:name": "legged01 foot slip",
                "rerun_last_updated_at": "2026-09-14T09:10:00Z",
                "rerun_layer_names": ["base"],
                "rerun_segment_id": "legged01_slip",
                "rerun_size_bytes": 180,
                "rerun_storage_urls": ["s3://fleet-rrd/episodes/legged01_slip.rrd"],
            },
        ],
        "version": 1,
    },
}

for name, document in documents.items():
    (HERE / name).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
