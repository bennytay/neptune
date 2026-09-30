"""The four worked examples under tests/fixtures/model/: MVL-1's acceptance, checked on real files.

- Every golden line validates against the generated JSON Schema and reads back byte-identically.
- Every record resolves back to evidence in its example's source ledger: each citation's source is
  a ledger artifact whose bytes are the committed file, each byte range lands on a record the file
  really has, each pointer, row and cell resolves, and every id a record names is in the example.
- Images, geometry, trajectories and time series stay records of their own structure.
"""

import calendar
import csv
import importlib.util
import io
import json
import re
import struct
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import check_evidence_record_id, check_transform_record
from neptune.model.finding import ingest_finding_from_json
from neptune.model.jsonvalue import JsonValue
from neptune.model.machine import (
    calibration_from_json,
    hardware_component_from_json,
    hardware_configuration_from_json,
    machine_from_json,
    software_configuration_from_json,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Row,
    RowCell,
    Span,
    TransformRecord,
    evidence_ref_from_json,
    transform_record_from_json,
)
from neptune.model.reference import (
    frame_from_json,
    frame_graph_from_json,
    frame_transform_from_json,
    timestamp_domain_from_json,
)
from neptune.model.run import run_from_json, stream_from_json
from neptune.model.schema import canonical_schema
from neptune.model.source import (
    SourceArtifact,
    source_artifact_from_json,
    source_revision_from_json,
)
from neptune.model.world import (
    image_from_json,
    site_from_json,
    spatial_artifact_from_json,
    structured_record_from_json,
    structured_table_from_json,
)

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "model"
EXAMPLES: Final = ("drone", "quadruped", "manipulator", "mobile_robot")
READERS: Final[dict[str, Callable[[JsonValue], Any]]] = {
    "calibration": calibration_from_json,
    "frame": frame_from_json,
    "frame_graph": frame_graph_from_json,
    "frame_transform": frame_transform_from_json,
    "hardware_component": hardware_component_from_json,
    "hardware_configuration": hardware_configuration_from_json,
    "image": image_from_json,
    "ingest_finding": ingest_finding_from_json,
    "machine": machine_from_json,
    "run": run_from_json,
    "site": site_from_json,
    "software_configuration": software_configuration_from_json,
    "source_artifact": source_artifact_from_json,
    "source_revision": source_revision_from_json,
    "spatial_artifact": spatial_artifact_from_json,
    "stream": stream_from_json,
    "structured_record": structured_record_from_json,
    "structured_table": structured_table_from_json,
    "timestamp_domain": timestamp_domain_from_json,
    "transform_record": transform_record_from_json,
}


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(FIXTURES))
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILDER: Final = _load("make_examples")
SOURCES: Final = _load("sources")


def tables(example: str) -> dict[str, list[bytes]]:
    records = FIXTURES / example / "records"
    return {path.stem: path.read_bytes().splitlines() for path in sorted(records.glob("*.jsonl"))}


def records(example: str) -> Iterator[tuple[str, bytes, Any]]:
    for kind, lines in tables(example).items():
        for line in lines:
            yield kind, line, READERS[kind](canonical_json.loads(line))


def source_files(example: str) -> dict[str, bytes]:
    root = FIXTURES / example / "sources"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- The files ---------------------------------------------------------------------------------


def test_the_committed_examples_are_exactly_what_the_builder_writes() -> None:
    # On failure, run `uv run python tests/fixtures/model/make_examples.py` and explain the diff.
    built = BUILDER.build()
    committed = {
        path.relative_to(FIXTURES).as_posix(): path.read_bytes()
        for example in EXAMPLES
        for path in sorted((FIXTURES / example).rglob("*"))
        if path.is_file()
    }
    assert sorted(committed) == sorted(built)
    for path, data in built.items():
        assert committed[path] == data, path


def test_the_examples_cover_the_four_platforms_each_with_a_run() -> None:
    assert set(BUILDER.EXAMPLE_BUILDERS) == set(EXAMPLES)
    for example in EXAMPLES:
        assert len(tables(example)["run"]) == 1, example


# --- Schema and readers ------------------------------------------------------------------------

VALIDATOR: Final = Draft202012Validator(canonical_schema())


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_line_validates_against_the_schema(example: str) -> None:
    for kind, line, _ in records(example):
        data = canonical_json.loads(line)
        errors = [error.message for error in VALIDATOR.iter_errors(data)]
        assert errors == [], (kind, line[:200])
        assert isinstance(data, dict) and data["kind"] == kind


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_line_reads_strictly_and_writes_back_byte_identically(example: str) -> None:
    for kind, lines in tables(example).items():
        ids = []
        for line in lines:
            record = READERS[kind](canonical_json.loads(line))
            assert canonical_json.dumps(record.to_json()) == line
            ids.append(record.content_id if kind == "source_artifact" else record.id)
        assert ids == sorted(ids) and len(set(ids)) == len(ids), kind  # sorted by id (ADR 0002)


# --- Every record resolves back to evidence in the ledger --------------------------------------


def _walk(value: JsonValue) -> Iterator[JsonValue]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _evidence(data: JsonValue) -> Iterator[EvidenceRef]:
    """Every evidence reference a record's JSON holds, at any depth."""
    for value in _walk(data):
        if isinstance(value, dict) and value.keys() == {"locator", "source"}:
            yield evidence_ref_from_json(value)


def _ids(data: JsonValue) -> Iterator[str]:
    for value in _walk(data):
        if isinstance(value, str) and value.startswith("rec:sha256:"):
            yield value


def _layout_bounds(layout: dict[str, tuple[int, int]]) -> tuple[set[int], set[int]]:
    starts = {start for start, _ in layout.values()}
    ends = {start + length for start, length in layout.values()}
    return starts, ends


def _resolve(ref: EvidenceRef, data: bytes, layout: dict[str, tuple[int, int]]) -> None:
    """Resolve a citation in the bytes it names, as far as the core steps allow."""
    steps = list(ref.locator)
    first = steps[0]
    if isinstance(first, ByteRange):
        assert first.offset + first.length <= len(data)
        whole = (first.offset, first.length) == (0, len(data))
        if not whole:  # a part: it starts and ends on records the file really has
            starts, ends = _layout_bounds(layout)
            assert first.offset in starts and first.offset + first.length in ends, first
        if whole and len(steps) > 1 and isinstance(steps[1], JsonPointer):
            document: Any = json.loads(data)
            for token in steps[1].pointer.split("/")[1:]:
                key = token.replace("~1", "/").replace("~0", "~")
                document = document[int(key)] if isinstance(document, list) else document[key]
        return
    rows = list(csv.reader(io.StringIO(data.decode())))
    if isinstance(first, Row):
        assert first.row < len(rows)
    elif isinstance(first, RowCell):
        text = rows[first.row][first.column]
        assert first.column_name == rows[0][first.column]
        if len(steps) > 1 and isinstance(steps[1], Span):
            assert 0 <= steps[1].start <= steps[1].end <= len(text)
    else:
        raise AssertionError(f"unexpected first step {first!r}")


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_record_resolves_back_to_evidence_in_its_ledger(example: str) -> None:
    files = source_files(example)
    layouts = {source.path: source.layout for source in SOURCES.EXAMPLES[example]()}
    artifacts: dict[str, SourceArtifact] = {}
    transforms: dict[str, TransformRecord] = {}
    everything: dict[str, Any] = {}
    for kind, _, record in records(example):
        if kind == "source_artifact":
            artifacts[record.content_id] = record
        elif kind == "transform_record":
            transforms[record.id] = check_transform_record(record)
        else:
            everything[record.id] = record
    # The ledger is the committed files: one artifact and one revision per file, bytes and all.
    by_content = {content_id(data): path for path, data in files.items()}
    assert set(artifacts) == set(by_content)
    revisions = [r for r in everything.values() if r.kind == "source_revision"]
    assert {r.location.path: r.content_id for r in revisions} == {
        path: content_id(data) for path, data in files.items()
    }
    known_ids = set(everything) | set(transforms)
    for record in everything.values():
        if record.kind == "source_revision":
            continue
        data = canonical_json.loads(canonical_json.dumps(record.to_json()))
        if record.kind == "ingest_finding":
            check_ingest_finding(record)
            assert record.transform in transforms
        else:
            check_evidence_record_id(record, transforms[record.provenance.transform])
        for ref in _evidence(data):
            assert isinstance(ref.source, str) and ref.source in artifacts, ref
            path = by_content[ref.source]
            _resolve(ref, files[path], layouts[path])
        for named in _ids(data):
            assert named in known_ids, (record.kind, named)


# --- Nothing collapses into text ---------------------------------------------------------------

REPRESENTED_AS: Final = {
    ("drone", "flight.ulg"): {
        "calibration",
        "hardware_component",
        "hardware_configuration",
        "machine",
        "run",
        "software_configuration",
        "stream",
        "timestamp_domain",
    },
    ("quadruped", "bag/metadata.yaml"): {"run", "software_configuration", "timestamp_domain"},
    ("quadruped", "bag/walk_0.mcap"): {"stream", "timestamp_domain"},
    ("quadruped", "robot.urdf"): {
        "frame",
        "frame_graph",
        "frame_transform",
        "hardware_component",
        "hardware_configuration",
    },
    ("quadruped", "meshes/body.stl"): {"spatial_artifact"},
    ("manipulator", "session.mcap"): {"run", "stream", "timestamp_domain"},
    ("manipulator", "handeye.yaml"): {"calibration", "frame", "frame_graph", "frame_transform"},
    ("mobile_robot", "drive.bag"): {"run", "stream", "timestamp_domain"},
    ("mobile_robot", "sites.csv"): {"site", "structured_record", "structured_table"},
    ("mobile_robot", "photos/dock.png"): {"image", "timestamp_domain"},
}


@pytest.mark.parametrize("example", EXAMPLES)
def test_each_source_is_records_of_its_own_structure_never_text(example: str) -> None:
    files = source_files(example)
    by_content = {content_id(data): path for path, data in files.items()}
    kinds: dict[str, set[str]] = {path: set() for path in files}
    for kind, _, record in records(example):
        provenance = getattr(record, "provenance", None)
        if kind in {"source_revision", "ingest_finding"} or provenance is None:
            continue
        kinds[by_content[provenance.evidence.source]].add(kind)
    for path, found in kinds.items():
        assert found == REPRESENTED_AS[(example, path)], path
        assert "document_block" not in found  # no source here is text; none is turned into text


def test_trajectories_and_video_frames_are_streams_of_samples() -> None:
    poses = [
        s
        for _, _, s in records("quadruped")
        if s.kind == "stream" and s.topic.value == "/body_pose"
    ]
    camera = [
        s
        for _, _, s in records("manipulator")
        if s.kind == "stream" and s.topic.value == "/wrist_camera/image/compressed"
    ]
    for stream in (*poses, *camera):
        assert len(stream.clocks) == 3  # log time, publish time and header.stamp, none chosen
        assert stream.series.locator[0].per_row == ("length", "offset")


# --- Declared values are what the bytes say ----------------------------------------------------


def _record(example: str, kind: str) -> Any:
    (record,) = [r for k, _, r in records(example) if k == kind]
    return record


def _cited(example: str, provenance: Any) -> bytes:
    """The bytes a provenance's first step names, in the committed file."""
    files = {content_id(data): data for data in source_files(example).values()}
    step = provenance.evidence.locator[0]
    assert isinstance(step, ByteRange)
    data = files[provenance.evidence.source]
    return data[step.offset : step.offset + step.length]


def test_declared_times_are_what_the_bytes_say() -> None:
    # The drone's run starts at the ULog header's timestamp, in microseconds.
    run = _record("drone", "run")
    header = _cited("drone", run.provenance)
    assert run.first.value.ticks == struct.unpack_from("<Q", header, 8)[0]
    # The manipulator's run spans the MCAP statistics' first and last log time.
    run = _record("manipulator", "run")
    statistics = _cited("manipulator", run.first.provenance)
    start, end = struct.unpack_from("<QQ", statistics, 9 + 8 + 2 + 4 * 4)
    assert (run.first.value.ticks, run.last.value.ticks) == (start, end)
    # The quadruped's run starts at rosbag2's starting_time and lasts its duration.
    run = _record("quadruped", "run")
    info = json.loads(source_files("quadruped")["bag/metadata.yaml"])["rosbag2_bagfile_information"]
    start = info["starting_time"]["nanoseconds_since_epoch"]
    assert (run.first.value.ticks, run.last.value.ticks) == (
        start,
        start + info["duration"]["nanoseconds"],
    )
    # The photo's capture time counts EXIF's zone-less civil fields as POSIX does (ADR 0023 §2).
    image = _record("mobile_robot", "image")
    exif = _cited("mobile_robot", image.capture.time.provenance)
    match = re.search(rb"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})\x00", exif)
    assert match is not None
    fields = [int(group) for group in match.groups()]
    assert image.capture.time.value.ticks == calendar.timegm((*fields, 0, 0, 0))
