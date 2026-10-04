"""Evidence references resolved to bytes, and lazy hydration into the Lance media store (MVL-96).

ADR 0014. Every locator kind the compiler's four worked examples emit (a drone, a manipulator, a
mobile robot, a quadruped) resolves to the cited bytes; frames come out of a manipulator's
referenced MCAP and a quadruped's materialised one; pages, page regions, rows, cells, image
regions, spans and archive members come out of small real files across the same embodiments.
Hydrations are deterministic and pinned by media snapshots, and a moved, changed or hostile
source is a finding, never an exception.
"""

import gzip
import io
import json
import tarfile
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from PIL import Image

from ledger_media_fixtures import (
    EXTRINSICS,
    HEAD_SIZE,
    HEAD_TOPIC,
    MISSION,
    PERIOD,
    START,
    WRIST_SIZE,
    WRIST_TOPIC,
    fixture,
    pixel,
    rewrite_mcap,
)
from ledger_media_packages import content_id, ingest_root, source_package
from neptune.identity import canonical_json
from neptune_ledger.api.types import EvidenceAnchor
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.catalog.sources import LocalSourceStore, SourceStore, location_path
from neptune_ledger.contract_tests.examples import (
    EXAMPLES,
    evidence_anchor,
    examples_dir,
    materialise,
)
from neptune_ledger.lake.decode import Limits, transform_for
from neptune_ledger.lake.evidence import EvidenceResolver
from neptune_ledger.lake.media import Artefact, Hydrated, MediaLake, MediaStore, SourceSlice
from test_ledger_registration import fresh

DOMAIN = "rec:sha256:" + "d" * 64  # the log clock a record_range's ticks are on


def byte_range(offset: int, length: int) -> dict[str, Any]:
    return {"kind": "byte_range", "length": length, "offset": offset}


def record_range(channel: str, start: int, end: int) -> dict[str, Any]:
    return {
        "channel": channel,
        "domain_id": DOMAIN,
        "end": end,
        "kind": "record_range",
        "start": start,
    }


def anchor(data: bytes, *steps: dict[str, Any]) -> EvidenceAnchor:
    return EvidenceAnchor(content_id(data), tuple(steps))


class Counting:
    """A local source store that counts opens and bytes read, to show what a read touches."""

    def __init__(self, root: Path) -> None:
        self._inner = LocalSourceStore(root)
        self.opens = 0
        self.served = 0

    def describe(self) -> str:
        return self._inner.describe()

    def open(self, path: bytes) -> BinaryIO | None:
        self.opens += 1
        opened = self._inner.open(path)
        if opened is None:
            return None
        stream: BinaryIO = opened
        outer = self

        class Tap(io.RawIOBase):
            def readable(self) -> bool:
                return True

            def seekable(self) -> bool:
                return True

            def seek(self, offset: int, whence: int = 0) -> int:
                return stream.seek(offset, whence)

            def readinto(self, buffer: Any) -> int:
                data = stream.read(len(buffer))
                outer.served += len(data)
                buffer[: len(data)] = data
                return len(data)

            def close(self) -> None:
                stream.close()
                super().close()

        return Tap()  # type: ignore[return-value]


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


class Lake:
    """A tenant's catalog, a resolver over its source stores, and a media store under ``tmp``."""

    def __init__(self, pg_uri: str, catalog: PostgresCatalog, tmp: Path) -> None:
        self.catalog = catalog
        self.tmp = tmp
        self.stores: list[SourceStore] = []
        self.resolver = EvidenceResolver(
            pg_uri, "acme", source_roots=lambda package, root: tuple(self.stores)
        )
        self.store = MediaStore(tmp / "ledger-data", "acme")
        self.media = MediaLake(self.resolver, self.store)

    def register(self, root: Path) -> None:
        outcome = self.catalog.register(root)
        assert outcome.outcome == "registered", outcome.findings

    def package(self, name: str, sources: dict[str, bytes], **kwargs: Any) -> Path:
        root = self.tmp / "packages" / name
        source_package(root, sources, **kwargs)
        self.register(root)
        return root

    def read(self, ref: EvidenceAnchor, variant: str, **kwargs: Any) -> Hydrated:
        return self.media.hydrate(ref, variant, **kwargs).read()

    def artefact(self, ref: EvidenceAnchor, variant: str, **kwargs: Any) -> Artefact:
        made = self.read(ref, variant, **kwargs)
        assert isinstance(made.value, Artefact), made.findings
        return made.value

    def codes(self, ref: EvidenceAnchor, variant: str, **kwargs: Any) -> list[str]:
        made = self.read(ref, variant, **kwargs)
        assert made.value is None
        return [f.code for f in made.findings]


@pytest.fixture
def lake(pg_uri: str, catalog: PostgresCatalog, tmp_path: Path) -> Iterator[Lake]:
    made = Lake(pg_uri, catalog, tmp_path)
    try:
        yield made
    finally:
        made.resolver.close()


def picture(artefact: Artefact) -> Image.Image:
    assert artefact.media_type == "image/png"
    image = Image.open(io.BytesIO(artefact.read()))
    image.load()
    return image


def assert_frame(
    image: Image.Image, size: tuple[int, int], frame: int, at: tuple[int, int]
) -> None:
    assert image.size == size
    for y in range(size[1]):
        for x in range(size[0]):
            assert image.getpixel((x, y)) == pixel(x + at[0], y + at[1], frame), (x, y)


# --- Resolution: every locator kind of the four worked examples --------------------------------


def test_every_worked_example_citation_resolves_to_its_bytes(lake: Lake, tmp_path: Path) -> None:
    kinds: set[str] = set()
    cited = 0
    hydrated = {"json_pointer": 0, "row": 0}
    for name in EXAMPLES:
        package = materialise(name, tmp_path / "examples" / name)
        lake.register(package.root)
        lake.stores = [LocalSourceStore(examples_dir() / name / "sources")]
        files = {
            content_id(p.read_bytes()): p.read_bytes()
            for p in (examples_dir() / name / "sources").rglob("*")
            if p.is_file()
        }
        for _, _, record in package.every_record():
            ref = evidence_anchor(record)
            if ref is None:
                continue
            cited += 1
            kinds.update(str(step["kind"]) for step in ref.locator)
            evidence = lake.media.resolve(ref)
            assert evidence.status == "resolved", (name, record["kind"], evidence.findings)
            assert evidence.route is not None and evidence.route.storage == "referenced"
            assert evidence.route.package_id == package.package_id
            span = evidence.span
            assert span is not None
            data = files[ref.source][span.offset : span.offset + span.length]
            assert evidence.open().read_all() == data
            first = ref.locator[0]["kind"]
            assert [s.kind for s in evidence.inner] == [
                s["kind"] for s in ref.locator[1 if first == "byte_range" else 0 :]
            ]
            sliced = lake.read(ref, "bytes")
            if all(step["kind"] == "byte_range" for step in ref.locator):
                assert isinstance(sliced.value, SourceSlice), sliced.findings
                assert sliced.value.read() == data or len(ref.locator) > 1
            else:  # bytes never drops a step it cannot follow
                assert sliced.value is None
                assert [f.code for f in sliced.findings] == ["invalid_request"]
            last = ref.locator[-1]["kind"]
            if last in ("json_pointer", "row"):  # the compiler's own pointers and rows decode
                made = lake.artefact(ref, "value" if last == "json_pointer" else "row")
                hydrated[last] += 1
                if record["kind"] == "structured_record":
                    assert_row_states(made, record)
    assert cited > 60
    assert hydrated["json_pointer"] > 10 and hydrated["row"] >= 3
    adapter_steps = {k for k in kinds if ":" in k}
    assert kinds - adapter_steps == {"byte_range", "json_pointer", "row"}
    assert adapter_steps == {
        "exif:tag",
        "mcap:time_field",
        "ros1msg:field",
        "ros2msg:field",
        "rosbag1:time_field",
        "ulog:field",
    }


def assert_row_states(made: Artefact, record: Mapping[str, Any]) -> None:
    """Every cell the compiler states as known is the text the hydrated row holds there."""
    row = canonical_json.loads(made.read())
    assert isinstance(row, dict) and row["format"] == "csv" and row["delimiter"] == ","
    cells = row["cells"]
    assert isinstance(cells, list) and len(cells) == len(record["cells"])
    for stated, cell in zip(record["cells"], cells, strict=True):
        if stated["knowledge"] == "known":
            assert cell == stated["value"]
        else:
            assert cell == ""  # a blank cell is Unknown in the record and "" in the source


def test_worked_example_pointers_and_rows_hydrate_to_what_the_records_state(
    lake: Lake, tmp_path: Path
) -> None:
    for name in ("manipulator", "quadruped", "mobile_robot"):
        package = materialise(name, tmp_path / name)
        lake.register(package.root)
    lake.stores = [LocalSourceStore(examples_dir() / n / "sources") for n in EXAMPLES]
    handeye = fixture_path("manipulator", "handeye.yaml")
    ref = anchor(handeye, byte_range(0, len(handeye)), {"kind": "json_pointer", "pointer": ""})
    value = lake.artefact(ref, "value")
    assert value.media_type == "application/json"
    assert value.read() == handeye.strip()  # the document's own text, verbatim
    assert json.loads(value.read())["robot_base_frame"] == "base_link"
    tool = {"kind": "json_pointer", "pointer": "/transformation/qw"}
    assert (
        lake.artefact(anchor(handeye, byte_range(0, 348), tool), "value").read()
        == b"0.7071067811865476"
    )

    sites = fixture_path("mobile_robot", "sites.csv")
    row = lake.artefact(anchor(sites, {"kind": "row", "row": 2}), "row")
    assert canonical_json.loads(row.read()) == {
        "cells": ["S-008", "Berth 4", "", "-33.8612", "151.2111", ""],
        "delimiter": ",",
        "delimiter_rule": "sniffed",
        "format": "csv",
        "row": 2,
    }
    assert row.metadata == {"delimiter": ",", "delimiter_rule": "sniffed", "format": "csv"}
    cell = {"column": 2, "column_name": "aka", "kind": "row_cell", "row": 1}
    assert canonical_json.loads(lake.artefact(anchor(sites, cell), "row").read()) == {
        "cell": "NP;Plant 3",
        "column": 2,
        "delimiter": ",",
        "delimiter_rule": "sniffed",
        "format": "csv",
        "row": 1,
    }
    wrong = {**cell, "column_name": "alias"}
    assert lake.codes(anchor(sites, wrong), "row") == ["invalid_request"]
    assert lake.codes(anchor(sites, {"kind": "row", "row": 9}), "row") == ["invalid_request"]


def fixture_path(example: str, path: str) -> bytes:
    return (examples_dir() / example / "sources" / path).read_bytes()


# --- Frames ------------------------------------------------------------------------------------


def test_a_frame_from_a_referenced_mcap_and_from_a_materialised_one(lake: Lake) -> None:
    wrist, head = fixture("wrist_camera.mcap"), fixture("head_camera.mcap")
    arm = lake.package("arm", {"session/wrist_camera.mcap": wrist}, chunk_size=1024)
    legged = lake.package(
        "legged", {"walk/head_camera.mcap": head}, materialise=frozenset({"walk/head_camera.mcap"})
    )
    lake.stores = [
        LocalSourceStore(ingest_root(lake.tmp / "ingest", {"session/wrist_camera.mcap": wrist}))
    ]

    at = START + PERIOD
    referenced = anchor(wrist, record_range(WRIST_TOPIC, at, at + 1))
    frame = lake.artefact(referenced, "frame")
    assert lake.media.resolve(referenced).route.storage == "referenced"  # type: ignore[union-attr]
    assert_frame(picture(frame), WRIST_SIZE, 1, (0, 0))
    assert frame.metadata == {
        "channel": WRIST_TOPIC,
        "encoding": "png",
        "height": 12,
        "log_time": at,
        "mode": "RGB",
        "schema": "sensor_msgs/msg/CompressedImage",
        "width": 16,
    }

    materialised = anchor(head, record_range(HEAD_TOPIC, START, START + 1))
    evidence = lake.media.resolve(materialised)
    assert evidence.route is not None and evidence.route.storage == "materialised"
    assert evidence.route.where.startswith(str(legged))
    raw = lake.artefact(materialised, "frame")
    assert_frame(picture(raw), HEAD_SIZE, 0, (0, 0))
    assert raw.metadata["encoding"] == "rgb8"
    del arm

    # Provenance: the evidence ref and the decoder, its version and its libraries' versions.
    assert raw.evidence_ref == materialised
    assert raw.transform == transform_for("frame").to_json()
    assert raw.transform["decoder"] == "neptune_ledger.media.frame"
    assert set(raw.transform["libraries"]) == {
        "lz4",
        "mcap",
        "mcap-ros2-support",
        "pillow",
        "zstandard",
    }
    assert raw.transform_id == transform_for("frame").id
    assert raw.sha256 == "sha256:" + __import__("hashlib").sha256(raw.read()).hexdigest()

    # A frame cropped by a later image_region step.
    crop = {"kind": "image_region", "x0": 4, "x1": 10, "y0": 2, "y1": 7}
    region = lake.artefact(
        anchor(wrist, record_range(WRIST_TOPIC, at, at + 1), crop), "image_region"
    )
    assert_frame(picture(region), (6, 5), 1, (4, 2))


def test_a_frame_is_one_message_or_a_finding(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    two = START + 2 * PERIOD  # frames 2 and 3 share this tick
    assert lake.codes(anchor(head, record_range(HEAD_TOPIC, two, two + 1)), "frame") == [
        "invalid_request"
    ]
    assert lake.codes(anchor(head, record_range(HEAD_TOPIC, 0, 1)), "frame") == ["invalid_request"]
    assert lake.codes(anchor(head, record_range("/nowhere", START, two)), "frame") == [
        "invalid_request"
    ]
    # A ROS 1 bag has no decoder in this version; a record_range over it is no_decoder.
    bag = fixture_path("mobile_robot", "drive.bag")
    lake.package("mobile", {"drive.bag": bag}, materialise=frozenset({"drive.bag"}))
    assert lake.codes(anchor(bag, record_range("/wheel_odom", 0, 2**62)), "frame") == ["no_decoder"]


# --- Determinism and snapshots -----------------------------------------------------------------


def test_two_hydrations_are_byte_identical(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    ref = anchor(head, record_range(HEAD_TOPIC, START + PERIOD, START + PERIOD + 1))
    first = lake.artefact(ref, "frame")
    other = MediaLake(lake.resolver, MediaStore(lake.tmp / "elsewhere", "acme"))
    second = other.hydrate(ref, "frame").read().value
    assert isinstance(second, Artefact)
    assert first == second  # every column, including the snapshot (1 in both stores)
    assert first.read() == second.read()
    assert first.artefact_id == lake.media.hydrate(ref, "frame").artefact_id

    # Hydrating again reads the stored row: no new snapshot, the same bytes.
    snapshot = lake.store.snapshot()
    again = lake.artefact(ref, "frame")
    assert lake.store.snapshot() == snapshot and again == first
    assert again.read() == first.read()


def test_a_snapshot_pins_what_a_hydration_returns(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    frame0 = anchor(head, record_range(HEAD_TOPIC, START, START + 1))
    frame1 = anchor(head, record_range(HEAD_TOPIC, START + PERIOD, START + PERIOD + 1))
    first = lake.artefact(frame0, "frame")
    assert first.snapshot == 1
    lake.artefact(frame1, "frame")
    assert lake.store.snapshot() == 2
    pinned = lake.artefact(frame0, "frame", snapshot=1)
    assert pinned.read() == first.read() and pinned.snapshot == 1
    assert lake.codes(frame1, "frame", snapshot=1) == ["unknown_artefact"]
    assert lake.codes(frame1, "frame", snapshot=3) == ["as_of_out_of_range"]
    assert lake.artefact(frame1, "frame", snapshot=2).snapshot == 2


def test_concurrent_hydrations_all_land(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    refs = [
        anchor(head, record_range(HEAD_TOPIC, START + i * PERIOD, START + i * PERIOD + 1))
        for i in (0, 1)
    ]
    work = [refs[i % 2] for i in range(6)]

    def hydrate(ref: EvidenceAnchor) -> bytes:
        resolver = EvidenceResolver(lake.resolver._conninfo, "acme")  # one connection per thread
        try:
            made = MediaLake(resolver, lake.store).hydrate(ref, "frame").read()
            assert isinstance(made.value, Artefact), made.findings
            return made.value.read()
        finally:
            resolver.close()

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(hydrate, work))
    assert results[0::2] == [results[0]] * 3 and results[1::2] == [results[1]] * 3
    for ref in refs:
        assert lake.artefact(ref, "frame").read() in results


# --- Laziness and byte-range slicing -----------------------------------------------------------


def test_hydration_is_lazy_and_video_bytes_slice_by_range(lake: Lake) -> None:
    clip = fixture("gimbal.mp4")
    lake.package("drone", {"gimbal.mp4": clip}, chunk_size=1024)
    store = Counting(ingest_root(lake.tmp / "ingest", {"gimbal.mp4": clip}))
    lake.stores = [store]
    frame = {"domain_id": DOMAIN, "index": 3, "kind": "video_frame", "pts": 3, "track": 0}
    ref = anchor(clip, byte_range(0, len(clip)))

    handle = lake.media.hydrate(ref, "bytes")
    assert store.opens == 0  # nothing resolved or read yet
    made = handle.read()
    assert isinstance(made.value, SourceSlice) and made.value.size == len(clip)
    opened, served = store.opens, store.served
    assert served == 0  # resolving checks the size only
    assert made.value.read_range(2000, 16) == clip[2000:2016]
    assert store.served - served == 1024  # only the one verified chunk holding the slice
    assert store.opens == opened + 1
    assert made.value.read_range(4130, 10) == clip[4130:4140]
    assert made.value.read_range(4130, 11).code == "invalid_request"  # type: ignore[union-attr]
    # A video frame is never decoded by this version, and bytes do not drop its step.
    assert lake.codes(anchor(clip, frame), "frame") == ["invalid_request"]
    assert lake.codes(anchor(clip, frame), "bytes") == ["invalid_request"]


# --- Pages, regions, rows, spans and archive members across embodiments ------------------------


def test_pages_and_page_regions_render_from_a_drone_report(lake: Lake) -> None:
    report = fixture("inspection_report.pdf")
    lake.package("drone", {"report.pdf": report}, materialise=frozenset({"report.pdf"}))
    page = lake.artefact(anchor(report, {"index": 0, "kind": "page"}), "page")
    image = picture(page)
    assert image.size == (400, 200) and page.metadata["rotation"] == 0
    assert image.getpixel((100, 120)) == (255, 0, 0)  # inside the red box (user y 10..60)
    assert image.getpixel((5, 5)) == (255, 255, 255)
    box = {"kind": "page_region", "page": 0, "x0": 20.0, "x1": 120.0, "y0": 10.0, "y1": 60.0}
    region = picture(lake.artefact(anchor(report, box), "page"))
    assert region.size == (200, 100)
    assert {region.getpixel((x, y)) for x in (1, 100, 198) for y in (1, 50, 98)} == {(255, 0, 0)}

    assert (
        lake.artefact(anchor(report, {"index": 1, "kind": "page"}), "page").metadata["rotation"]
        == 90
    )
    rotated = {**box, "page": 1, "x1": 40.0, "y1": 40.0}
    assert lake.codes(anchor(report, rotated), "page") == ["no_decoder"]
    assert lake.codes(anchor(report, {"index": 2, "kind": "page"}), "page") == ["invalid_request"]
    outside = {**box, "x1": 260.0}
    assert lake.codes(anchor(report, outside), "page") == ["invalid_request"]


def test_an_image_region_of_a_mobile_robots_photo(lake: Lake, tmp_path: Path) -> None:
    package = materialise("mobile_robot", tmp_path / "mobile_robot")
    lake.register(package.root)
    lake.stores = [LocalSourceStore(examples_dir() / "mobile_robot" / "sources")]
    photo = fixture_path("mobile_robot", "photos/dock.png")
    whole = Image.open(io.BytesIO(photo)).convert("RGB")
    box = {"kind": "image_region", "x0": 2, "x1": 7, "y0": 1, "y1": 5}
    region = lake.artefact(anchor(photo, byte_range(0, len(photo)), box), "image_region")
    assert region.metadata["source_width"] == 8 and region.metadata["region"] == [2, 1, 7, 5]
    assert picture(region).tobytes() == whole.crop((2, 1, 7, 5)).tobytes()
    beyond = {**box, "x1": 9}
    assert lake.codes(anchor(photo, beyond), "image_region") == ["invalid_request"]
    empty = {**box, "x1": 2}
    assert lake.codes(anchor(photo, empty), "image_region") == ["invalid_request"]


def test_parquet_rows_and_cells_of_a_site_register(lake: Lake) -> None:
    sites = fixture("sites.parquet")
    lake.package("mobile", {"sites.parquet": sites}, materialise=frozenset({"sites.parquet"}))
    row = canonical_json.loads(
        lake.artefact(anchor(sites, {"kind": "row", "row": 2}), "row").read()
    )
    assert row == {
        "cells": [
            {"column": 0, "name": "site_id", "type": "string", "value": "S-009"},
            {"column": 1, "name": "name", "type": "string", "value": "Charging bay"},
            {"column": 2, "name": "latitude", "type": "double", "null": True},
            {"column": 3, "name": "dock", "type": "int64", "value": 3},
        ],
        "format": "parquet",
        "row": 2,
    }
    cell = {"column": 2, "column_name": "latitude", "kind": "row_cell", "row": 1}
    made = canonical_json.loads(lake.artefact(anchor(sites, cell), "row").read())
    assert isinstance(made, dict)
    assert made["cells"] == [{"column": 2, "name": "latitude", "type": "double", "value": -33.8612}]
    assert lake.codes(anchor(sites, {"kind": "row", "row": 3}), "row") == ["invalid_request"]
    assert lake.codes(anchor(sites, {**cell, "column": 4}), "row") == ["invalid_request"]


def test_a_span_counts_code_points_of_a_drone_mission_note(lake: Lake) -> None:
    note = fixture("mission.txt")
    lake.package("drone", {"mission.txt": note}, materialise=frozenset({"mission.txt"}))
    start = MISSION.index("façade")
    span = {"end": start + 6, "kind": "span", "start": start}
    made = lake.artefact(anchor(note, byte_range(0, len(note)), span), "value")
    assert made.read().decode("utf-8") == "façade" and made.media_type.startswith("text/plain")
    assert lake.codes(anchor(note, {"end": 999, "kind": "span", "start": 0}), "value") == [
        "invalid_request"
    ]


def test_archive_members_of_a_quadruped_calibration_bundle(lake: Lake) -> None:
    bundle = fixture("leg_calibration.tar")
    lake.package("legged", {"calibration.tar": bundle}, chunk_size=512)
    lake.stores = [LocalSourceStore(ingest_root(lake.tmp / "ingest", {"calibration.tar": bundle}))]
    with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
        members = {m.name: m for m in archive}
    yaml_member, gz_member = members["intrinsics.yaml"], members["extrinsics.json.gz"]
    fx = anchor(
        bundle,
        byte_range(yaml_member.offset_data, yaml_member.size),
        {"kind": "json_pointer", "pointer": "/fx"},
    )
    assert lake.artefact(fx, "value").read() == b"412.5"
    child = anchor(
        bundle,
        byte_range(gz_member.offset_data, gz_member.size),
        {"kind": "json_pointer", "pointer": "/child"},
    )  # the member is a gzip stream, decoded before the pointer
    assert lake.artefact(child, "value").read() == b'"head_camera_optical"'
    assert EXTRINSICS["child"] == "head_camera_optical"
    member = lake.read(anchor(bundle, byte_range(gz_member.offset_data, gz_member.size)), "bytes")
    assert isinstance(member.value, SourceSlice)
    assert gzip.decompress(member.value.read()).startswith(b"{")  # type: ignore[arg-type]

    # A member range may not leave the archive, and a nested range may not leave its member.
    escape = anchor(bundle, byte_range(len(bundle) - 4, 8), {"kind": "json_pointer", "pointer": ""})
    assert lake.codes(escape, "value") == ["invalid_request"]
    nested = anchor(
        bundle,
        byte_range(yaml_member.offset_data, yaml_member.size),
        byte_range(70, 10),
        {"kind": "json_pointer", "pointer": ""},
    )
    assert lake.codes(nested, "value") == ["invalid_request"]


# --- Moved, changed and hostile sources --------------------------------------------------------


def test_a_moved_source_resolves_to_a_finding_not_a_crash(lake: Lake) -> None:
    wrist = fixture("wrist_camera.mcap")
    lake.package("arm", {"cam/wrist.mcap": wrist})
    ingest = ingest_root(lake.tmp / "ingest", {"cam/wrist.mcap": wrist})
    lake.stores = [LocalSourceStore(ingest)]
    ref = anchor(wrist, record_range(WRIST_TOPIC, START, START + 1))
    (ingest / "cam" / "wrist.mcap").rename(ingest / "cam" / "moved.mcap")
    evidence = lake.media.resolve(ref)
    assert evidence.status == "unavailable" and evidence.route is None
    assert [f.code for f in evidence.findings] == ["file_missing"]
    assert str(ingest) in evidence.findings[0].detail
    assert lake.codes(ref, "frame") == ["file_missing"]
    assert lake.codes(anchor(wrist, byte_range(0, len(wrist))), "bytes") == ["file_missing"]
    with pytest.raises(ValueError, match="unavailable"):
        evidence.open()

    # Its package moved too: a materialised source in a moved package is missing likewise.
    head = fixture("head_camera.mcap")
    root = lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    root.rename(root.with_name("legged-moved"))
    assert lake.codes(anchor(head, record_range(HEAD_TOPIC, START, START + 1)), "frame") == [
        "file_missing"
    ]


def test_a_changed_source_is_never_served(lake: Lake) -> None:
    wrist = fixture("wrist_camera.mcap")
    lake.package("arm", {"wrist.mcap": wrist}, chunk_size=1024)
    ingest = ingest_root(lake.tmp / "ingest", {"wrist.mcap": wrist})
    lake.stores = [LocalSourceStore(ingest)]
    ref = anchor(wrist, record_range(WRIST_TOPIC, START, START + 1))
    sliced = lake.read(anchor(wrist, byte_range(0, len(wrist))), "bytes").value
    assert isinstance(sliced, SourceSlice)
    changed = bytearray(wrist)
    changed[1500] ^= 0xFF  # same size, one byte in chunk 1
    (ingest / "wrist.mcap").write_bytes(bytes(changed))
    assert sliced.read_range(0, 1024) == wrist[:1024]  # chunk 0 is still the stated bytes
    assert sliced.read_range(1400, 200).code == "file_digest_mismatch"  # type: ignore[union-attr]
    assert lake.codes(ref, "frame") == ["file_digest_mismatch"]
    (ingest / "wrist.mcap").write_bytes(wrist + b"!")
    assert lake.codes(ref, "frame") == ["file_digest_mismatch"]
    (ingest / "wrist.mcap").unlink()
    # The last chunk read is kept, and it was verified; any other chunk is now missing.
    assert sliced.read_range(0, 10) == wrist[:10]
    assert sliced.read_range(2048, 10).code == "file_missing"  # type: ignore[union-attr]


def test_links_and_escaping_paths_are_never_followed(lake: Lake) -> None:
    note = fixture("mission.txt")
    lake.package("drone", {"notes/mission.txt": note})
    outside = ingest_root(lake.tmp / "outside", {"mission.txt": note})
    ingest = lake.tmp / "ingest"
    (ingest / "notes").mkdir(parents=True)
    (ingest / "notes" / "mission.txt").symlink_to(outside / "mission.txt")
    lake.stores = [LocalSourceStore(ingest)]
    ref = anchor(note, byte_range(0, len(note)))
    assert lake.codes(ref, "bytes") == ["file_missing"]
    (ingest / "notes" / "mission.txt").unlink()
    (ingest / "notes").rmdir()
    (ingest / "notes").symlink_to(outside)
    assert lake.codes(ref, "bytes") == ["file_missing"]
    # A stated path that would leave a store is never opened.
    for hostile in ("../outside/mission.txt", "/etc/passwd", "a//b", "./a", "a/\x00"):
        assert (
            location_path(canonical_json.dumps({"kind": "local", "path": hostile}).decode()) is None
        )
    assert location_path('{"kind":"external","system":"s3","value":"x"}') is None


def test_hostile_and_malformed_requests_are_findings(lake: Lake) -> None:
    head, report = fixture("head_camera.mcap"), fixture("inspection_report.pdf")
    lake.package(
        "legged",
        {"head.mcap": head, "report.pdf": report},
        materialise=frozenset({"head.mcap", "report.pdf"}),
    )
    frame = record_range(HEAD_TOPIC, START, START + 1)
    cases: list[tuple[EvidenceAnchor, str, list[str]]] = [
        (EvidenceAnchor("sha256:" + "0" * 64, (frame,)), "frame", ["unresolvable_evidence"]),
        (EvidenceAnchor("not-a-content-id", (frame,)), "frame", ["invalid_request"]),
        (EvidenceAnchor(content_id(head), ()), "bytes", ["invalid_request"]),
        (anchor(head, {"kind": "teleport"}), "bytes", ["invalid_request"]),
        (
            anchor(head, {"kind": "byte_range", "length": 1, "offset": -1}),
            "bytes",
            ["invalid_request"],
        ),
        (anchor(head, byte_range(len(head), 1)), "bytes", ["invalid_request"]),
        (anchor(head, frame), "hologram", ["invalid_request"]),
        (anchor(head, frame), "page", ["invalid_request"]),
        (anchor(head, byte_range(0, 10)), "frame", ["invalid_request"]),
        (anchor(head, {"kind": "page", "index": 0}), "page", ["undecodable"]),
        (anchor(report, record_range(HEAD_TOPIC, 0, 1)), "frame", ["undecodable"]),
        (anchor(head, {"kind": "row", "row": 0}), "row", ["undecodable"]),
        (anchor(head, {"kind": "mcap:message", "index": 0}), "frame", ["invalid_request"]),
        (anchor(head, {"kind": "object", "object_id": "x"}, frame), "frame", ["no_decoder"]),
    ]
    for ref, variant, codes in cases:
        assert lake.codes(ref, variant) == codes, (ref, variant)
    assert lake.codes(anchor(head, frame), "frame", as_of=99) == ["as_of_out_of_range"]


def test_truncated_and_bomb_sources_are_findings(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    truncated = head[: len(head) // 2]
    bomb = gzip.compress(b"{" + b" " * 300_000 + b"}", mtime=0)
    lake.package(
        "hostile",
        {"cut.mcap": truncated, "bomb.json.gz": bomb, "alias.yaml": b"a: &x [1]\nb: *x\n"},
        materialise=frozenset({"cut.mcap", "bomb.json.gz", "alias.yaml"}),
    )
    assert lake.codes(anchor(truncated, record_range(HEAD_TOPIC, START, START + 1)), "frame") == [
        "undecodable"
    ]
    pointer = {"kind": "json_pointer", "pointer": ""}
    small = MediaLake(lake.resolver, lake.store, limits=Limits(max_decoded_bytes=100_000))
    made = small.hydrate(anchor(bomb, byte_range(0, len(bomb)), pointer), "value").read()
    assert [f.code for f in made.findings] == ["unsafe_entry"]
    alias = b"a: &x [1]\nb: *x\n"
    assert lake.codes(anchor(alias, byte_range(0, len(alias)), pointer), "value") == ["undecodable"]
    tiny = MediaLake(lake.resolver, lake.store, limits=Limits(max_pixels=10))
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    made = tiny.hydrate(anchor(head, record_range(HEAD_TOPIC, START, START + 1)), "frame").read()
    assert [f.code for f in made.findings] == ["unsafe_entry"]


def test_the_media_table_lives_under_the_tenants_prefix(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    assert lake.store.snapshot() is None
    lake.artefact(anchor(head, record_range(HEAD_TOPIC, START, START + 1)), "frame")
    table = lake.tmp / "ledger-data" / "tenant_acme" / "tables" / "media"
    assert lake.store.uri == str(table) and (table / "_versions").is_dir()
    assert lake.store.get("sha256:" + "0" * 64) is None
    assert lake.store.get("x' OR '1'='1") is None
    assert threading.active_count() >= 1


# --- MCAP variants: compressed, unindexed, nested, and a chunk bomb ----------------------------


def test_compressed_unindexed_and_nested_recordings_give_the_same_frame(lake: Lake) -> None:
    from mcap.writer import CompressionType

    head = fixture("head_camera.mcap")
    variants = {
        "zstd.mcap": rewrite_mcap(head, compression=CompressionType.ZSTD),
        "lz4.mcap": rewrite_mcap(head, compression=CompressionType.LZ4),
        "unchunked.mcap": rewrite_mcap(head, use_chunking=False),  # no index: read through once
    }
    members = io.BytesIO()
    with tarfile.open(fileobj=members, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo("walk/head_camera.mcap")
        info.size = len(head)
        archive.addfile(info, io.BytesIO(head))
    bundle = members.getvalue()
    lake.package("legged", {**variants, "walk.tar": bundle}, materialise=frozenset(variants))
    lake.stores = [LocalSourceStore(ingest_root(lake.tmp / "ingest", {"walk.tar": bundle}))]
    at = START + PERIOD
    frame1 = record_range(HEAD_TOPIC, at, at + 1)
    expected = lake.artefact(anchor(variants["zstd.mcap"], frame1), "frame")
    assert_frame(picture(expected), HEAD_SIZE, 1, (0, 0))
    for data in (*variants.values(),):
        made = lake.artefact(anchor(data, record_range(HEAD_TOPIC, at, at + 1)), "frame")
        assert made.read() == expected.read() and made.sha256 == expected.sha256
        two = START + 2 * PERIOD
        assert lake.codes(anchor(data, record_range(HEAD_TOPIC, two, two + 1)), "frame") == [
            "invalid_request"
        ]
    assert variants["zstd.mcap"] != head
    with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
        member = archive.getmember("walk/head_camera.mcap")
    nested = anchor(
        bundle, byte_range(member.offset_data, member.size), record_range(HEAD_TOPIC, at, at + 1)
    )
    assert lake.artefact(nested, "frame").read() == expected.read()


def test_an_mcap_chunk_inflating_past_the_limit_is_unsafe(lake: Lake) -> None:
    from mcap.writer import CompressionType, Writer

    from ledger_media_fixtures import IMAGE_DEF

    out = io.BytesIO()
    writer = Writer(out, compression=CompressionType.ZSTD)
    writer.start(profile="ros2")
    schema = writer.register_schema("sensor_msgs/msg/Image", "ros2msg", IMAGE_DEF.encode())
    channel = writer.register_channel(HEAD_TOPIC, "cdr", schema)
    writer.add_message(channel, START, b"\0" * (4 << 20), START)  # 4 MiB of zeros, a few KiB packed
    writer.finish()  # type: ignore[no-untyped-call]
    bomb = out.getvalue()
    assert len(bomb) < 64 << 10
    lake.package("hostile", {"bomb.mcap": bomb}, materialise=frozenset({"bomb.mcap"}))
    small = MediaLake(lake.resolver, lake.store, limits=Limits(max_decoded_bytes=1 << 20))
    made = small.hydrate(anchor(bomb, record_range(HEAD_TOPIC, START, START + 1)), "frame").read()
    assert made.value is None and [f.code for f in made.findings] == ["unsafe_entry"]
    assert "MCAP chunk" in made.findings[0].detail


# --- Tables as the compiler reads them, and pointers into JSON and YAML -------------------------


def test_csv_rows_follow_the_compilers_grammar(lake: Lake) -> None:
    # An arm's joint-limit sheet: a byte-order mark, tabs, CRLF, a blank line (not a record), a
    # quoted cell holding a line break and doubled quotes, and a cell that is not UTF-8.
    sheet = (
        b"\xef\xbb\xbfjoint\tlimit\tnote\r\n\r\n"
        b'shoulder\t2.5\t"soft ""stop""\nat 2.4"\r\n'
        b"elbow\t1.9\t\r\n"
        b"wrist\t\xff\tok\n"
    )
    single = b"joint;limit\n"  # one record: no delimiter can be sniffed, so the default applies
    lake.package(
        "arm",
        {"limits.tsv": sheet, "one.csv": single},
        materialise=frozenset({"limits.tsv", "one.csv"}),
    )

    def row(data: bytes, **step: Any) -> Any:
        kind = "row_cell" if "column" in step else "row"
        return canonical_json.loads(
            lake.artefact(anchor(data, {"kind": kind, **step}), "row").read()
        )

    shoulder = row(sheet, row=1)
    assert shoulder["cells"] == ["shoulder", "2.5", 'soft "stop"\nat 2.4']
    assert (shoulder["delimiter"], shoulder["delimiter_rule"]) == ("\t", "sniffed")
    assert row(sheet, row=2)["cells"] == ["elbow", "1.9", ""]
    assert row(sheet, row=3, column=1, column_name="limit")["cell"] == {"hex": "ff"}
    assert row(sheet, row=0)["cells"] == ["joint", "limit", "note"]
    assert lake.codes(anchor(sheet, {"kind": "row", "row": 4}), "row") == ["invalid_request"]
    one = row(single, row=0)
    assert one["cells"] == ["joint;limit"] and one["delimiter_rule"] == "default"


def test_pointers_into_json_and_yaml(lake: Lake) -> None:
    # A legged robot's controller config: YAML whose scalars a reader could type two ways.
    config = (
        b"controller:\n"
        b"  gait: trot\n"
        b"  on: yes\n"
        b"  gains: [1.5, 0x10]\n"
        b"  'a/b': 3\n"
        b"twice: 1\n"
        b"twice: 2\n"
    )
    document = b'{"wheel": {"radius": 0.0825, "count": 4}, "dup": 1, "dup": 2, "ok": [null]}'
    lake.package(
        "fleet",
        {"controller.yaml": config, "base.json": document},
        materialise=frozenset({"controller.yaml", "base.json"}),
    )

    def value(data: bytes, pointer: str) -> Artefact:
        return lake.artefact(anchor(data, {"kind": "json_pointer", "pointer": pointer}), "value")

    on = value(config, "/controller/on")
    assert on.read() == b"yes" and on.media_type == "application/yaml"
    assert on.metadata == {"format": "yaml", "pointer": "/controller/on"}
    assert value(config, "/controller/gains/1").read() == b"0x10"
    assert value(config, "/controller/a~1b").read() == b"3"
    assert value(config, "/controller").read() == (
        b"gait: trot\n  on: yes\n  gains: [1.5, 0x10]\n  'a/b': 3"
    )
    for missing in ("/twice", "/controller/gains/2", "/controller/gains/01", "/nowhere"):
        ref = anchor(config, {"kind": "json_pointer", "pointer": missing})
        assert lake.codes(ref, "value") == ["invalid_request"], missing

    radius = value(document, "/wheel/radius")
    assert radius.read() == b"0.0825" and radius.media_type == "application/json"
    assert value(document, "/ok/0").read() == b"null"  # the text written, never a fact
    assert lake.codes(anchor(document, {"kind": "json_pointer", "pointer": "/dup"}), "value") == [
        "invalid_request"
    ]


def test_a_stored_artefact_still_needs_the_catalog_and_bytes_take_no_snapshot(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    ref = anchor(head, record_range(HEAD_TOPIC, START, START + 1))
    stored = lake.artefact(ref, "frame")
    assert lake.codes(ref, "frame", as_of=99) == ["as_of_out_of_range"]
    assert lake.codes(ref, "frame", as_of=99, snapshot=stored.snapshot) == ["as_of_out_of_range"]
    whole = anchor(head, byte_range(0, len(head)))
    assert lake.codes(whole, "bytes", snapshot=1) == ["invalid_request"]


def test_byte_order_marks_name_the_encoding_and_are_not_text(lake: Lake) -> None:
    import codecs

    note = codecs.BOM_UTF8 + MISSION.encode("utf-8")
    params = codecs.BOM_UTF16_LE + '{"gait": "trot", "hz": 400}'.encode("utf-16-le")
    lake.package(
        "legged",
        {"note.txt": note, "params.json": params},
        materialise=frozenset({"note.txt", "params.json"}),
    )
    start = MISSION.index("façade")
    span = anchor(note, {"end": start + 6, "kind": "span", "start": start})
    assert lake.artefact(span, "value").read() == "façade".encode()
    gait = lake.artefact(anchor(params, {"kind": "json_pointer", "pointer": "/gait"}), "value")
    assert gait.read() == b'"trot"' and gait.media_type == "application/json"


def test_a_changed_copy_falls_back_to_an_intact_one(lake: Lake) -> None:
    wrist = fixture("wrist_camera.mcap")
    lake.package("arm-referenced", {"wrist.mcap": wrist}, chunk_size=1024)
    lake.package(
        "arm-materialised",
        {"wrist.mcap": wrist},
        chunk_size=1024,
        materialise=frozenset({"wrist.mcap"}),
    )
    edited = bytearray(wrist)
    edited[100] ^= 0xFF  # same size, chunk 0 changed
    lake.stores = [
        LocalSourceStore(ingest_root(lake.tmp / "ingest", {"wrist.mcap": bytes(edited)}))
    ]
    whole = anchor(wrist, byte_range(0, len(wrist)))
    evidence = lake.media.resolve(whole)
    assert evidence.status == "resolved" and evidence.route is not None
    assert evidence.route.storage == "referenced"  # the first copy of the stated size
    assert evidence.open().read_all() == wrist  # chunk 0 comes from the intact blob
    frame = lake.artefact(anchor(wrist, record_range(WRIST_TOPIC, START, START + 1)), "frame")
    assert_frame(picture(frame), WRIST_SIZE, 0, (0, 0))


def test_small_limits_bound_parsers_not_read_ahead(lake: Lake) -> None:
    head = fixture("head_camera.mcap")
    lake.package("legged", {"head.mcap": head}, materialise=frozenset({"head.mcap"}))
    tight = MediaLake(lake.resolver, lake.store, limits=Limits(max_decoded_bytes=4096))
    made = tight.hydrate(anchor(head, record_range(HEAD_TOPIC, START, START + 1)), "frame").read()
    assert isinstance(made.value, Artefact), made.findings
    # A frame step with a further step other than image_region inside it has no decoder.
    pointer = {"kind": "json_pointer", "pointer": "/data"}
    ref = anchor(head, record_range(HEAD_TOPIC, START, START + 1), pointer)
    assert lake.codes(ref, "value") == ["no_decoder"]


# --- Frames from the compiler's own citations ---------------------------------------------------


def compiled_citations() -> list[tuple[str, int, EvidenceAnchor]]:
    """Every message the compiled package's series cite: (topic, seq, its evidence anchor)."""
    import pyarrow.parquet as pq

    from ledger_media_compiled import PACKAGE

    made = []
    for line in (PACKAGE / "records" / "stream.jsonl").read_text().splitlines():
        stream = json.loads(line)
        source = stream["provenance"]["evidence"]["source"]
        series = PACKAGE / "series" / (stream["id"].split(":")[-1] + ".parquet")
        for row in pq.read_table(series).to_pylist():
            steps = [byte_range(row["locator/0/offset"], row["locator/0/length"])]
            if row.get("locator/1/length") is not None:
                steps.append(byte_range(row["locator/1/offset"], row["locator/1/length"]))
            made.append(
                (stream["topic"]["value"], row["seq"], EvidenceAnchor(source, tuple(steps)))
            )
    return made


def test_frames_hydrate_from_the_compilers_own_message_citations(lake: Lake) -> None:
    from mcap.reader import make_reader

    from ledger_media_compiled import HEAD, PACKAGE, SOURCES, WRIST

    lake.register(PACKAGE)
    lake.stores = [LocalSourceStore(SOURCES)]
    cited = compiled_citations()
    assert {topic for topic, _, _ in cited} == {HEAD_TOPIC, WRIST_TOPIC} and len(cited) == 7
    payloads = {
        topic: [
            m.data
            for _, c, m in make_reader(io.BytesIO((SOURCES / path).read_bytes())).iter_messages()
            if c.topic == topic
        ]
        for topic, path in ((HEAD_TOPIC, HEAD), (WRIST_TOPIC, WRIST))
    }
    for topic, seq, ref in cited:
        assert len(ref.locator) == 2  # the Chunk record, then the Message record inside it
        frame = lake.artefact(ref, "frame")
        size = HEAD_SIZE if topic == HEAD_TOPIC else WRIST_SIZE
        assert_frame(picture(frame), size, seq, (0, 0))
        assert frame.metadata["channel"] == topic and frame.evidence_ref == ref
        # bytes follows both steps: the Message record, inflated from its zstd or lz4 chunk.
        sliced = lake.read(ref, "bytes").value
        assert isinstance(sliced, SourceSlice) and sliced.reader is None
        assert sliced.inflated == (
            ("mcap-chunk:zstd",) if topic == HEAD_TOPIC else ("mcap-chunk:lz4",)
        )
        record = sliced.read()
        assert (
            isinstance(record, bytes)
            and record[0] == 0x05
            and len(record) == ref.locator[1]["length"]
        )
        assert record[31:] == payloads[topic][seq]
    topic, seq, ref = cited[-1]
    crop = {"kind": "image_region", "x0": 1, "x1": 5, "y0": 2, "y1": 4}
    region = lake.artefact(EvidenceAnchor(ref.source, (*ref.locator, crop)), "image_region")
    size = HEAD_SIZE if topic == HEAD_TOPIC else WRIST_SIZE
    assert_frame(picture(region), (4, 2), seq, (1, 2))
    # A range inside the chunk that is not exactly one Message record is not a frame.
    off = EvidenceAnchor(ref.source, (ref.locator[0], byte_range(ref.locator[1]["offset"] + 1, 30)))
    assert lake.codes(off, "frame") == ["invalid_request"]
    beyond = EvidenceAnchor(ref.source, (ref.locator[0], byte_range(0, 10**6)))
    assert lake.codes(beyond, "bytes") == ["invalid_request"]
    del size


def test_archive_members_inside_bzip2_and_xz_streams(lake: Lake) -> None:
    import bz2
    import lzma

    bundle = fixture("leg_calibration.tar")
    with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
        member = archive.getmember("intrinsics.yaml")
    packed = {"cal.tar.bz2": bz2.compress(bundle, 9), "cal.tar.xz": lzma.compress(bundle)}
    lake.package("legged", packed, materialise=frozenset(packed))
    for data in packed.values():
        ref = anchor(
            data,
            byte_range(0, len(data)),
            byte_range(member.offset_data, member.size),
            {"kind": "json_pointer", "pointer": "/fx"},
        )
        assert lake.artefact(ref, "value").read() == b"412.5"
        inner = anchor(data, byte_range(0, len(data)), byte_range(member.offset_data, member.size))
        sliced = lake.read(inner, "bytes").value
        assert (
            isinstance(sliced, SourceSlice)
            and sliced.read() == bundle[member.offset_data : member.offset_data + member.size]
        )
    tiny = MediaLake(lake.resolver, lake.store, limits=Limits(max_decoded_bytes=4096))
    data = packed["cal.tar.bz2"]
    ref = anchor(data, byte_range(0, len(data)), byte_range(0, 10))
    assert [f.code for f in tiny.hydrate(ref, "bytes").read().findings] == ["unsafe_entry"]


# --- Bounded documents and Parquet --------------------------------------------------------------


def test_documents_past_the_limit_are_refused_and_parsing_costs_no_tree(lake: Lake) -> None:
    import tracemalloc

    limit = 1 << 20
    flat_json = b"[" + b",".join([b"{}"] * ((limit - 2) // 3)) + b"]"
    flat_yaml = b"[" + b",".join([b"a"] * ((limit - 2) // 2)) + b"]"
    nested = b"x:\n" + b"".join(b"  k%d: [1, 2]\n" % i for i in range(60_000))
    over = flat_json[:-1] + b",{}" * 400 + b"]"
    docs = {
        "flat.json": flat_json,
        "flat.yaml": flat_yaml,
        "nested.yaml": nested[:limit],
        "over.json": over,
    }
    lake.package("hostile", docs, materialise=frozenset(docs))
    media = MediaLake(lake.resolver, lake.store, limits=Limits(max_document_bytes=limit))
    pointer = {"kind": "json_pointer", "pointer": "/5"}
    assert (
        all(len(d) <= limit for name, d in docs.items() if name != "over.json")
        and len(over) > limit
    )
    made = media.hydrate(anchor(over, pointer), "value").read()
    assert [f.code for f in made.findings] == ["unsafe_entry"]
    for name, expected in (("flat.json", b"{}"), ("flat.yaml", b"a")):
        tracemalloc.start()
        made = media.hydrate(anchor(docs[name], pointer), "value").read()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert isinstance(made.value, Artefact) and made.value.read() == expected, made.findings
        # The document's bytes and text, a source chunk and the store's own work: no tree.
        assert peak < 16 * limit, (name, peak)


def test_parquet_guards_hold_before_any_page_is_decoded(lake: Lake) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    def written(table: Any, **options: Any) -> bytes:
        out = io.BytesIO()
        pq.write_table(table, out, **options)
        return out.getvalue()

    # A wheel-odometry log: one constant column of 2M rows packs to a few KiB (run-length) and
    # decodes to 16 MB; a cited row decodes one batch of it, never the row group.
    odometry = written(
        pa.table({"ticks": pa.array([7] * 2_000_000, pa.int64())}), row_group_size=2_000_000
    )
    small = written(pa.table({"ticks": pa.array([1, 2, 3], pa.int64())}))
    big = written(pa.table({"ticks": pa.array(range(50_000), pa.int64())}), compression="NONE")
    # Forged: the small file's data under the big file's footer, which points past its data.
    footer = int.from_bytes(big[-8:-4], "little")
    forged = small[: len(small) - 8 - int.from_bytes(small[-8:-4], "little")] + big[-8 - footer :]
    lying = big[:-8] + (len(big)).to_bytes(4, "little") + b"PAR1"
    encrypted = small[:-4] + b"PARE"
    files = {
        "odo.parquet": odometry,
        "forged.parquet": forged,
        "lying.parquet": lying,
        "enc.parquet": encrypted,
        "big.parquet": big,
    }
    lake.package("mobile", files, materialise=frozenset(files))
    cell = {"column": 0, "column_name": "ticks", "kind": "row_cell", "row": 1_999_999}
    value = canonical_json.loads(lake.artefact(anchor(odometry, cell), "row").read())
    assert isinstance(value, dict) and value["cells"] == [
        {"column": 0, "name": "ticks", "type": "int64", "value": 7}
    ]
    row = {"kind": "row", "row": 2}
    assert lake.codes(anchor(forged, row), "row") == ["undecodable"]
    assert lake.codes(anchor(lying, row), "row") == ["undecodable"]
    assert lake.codes(anchor(encrypted, row), "row") == ["no_decoder"]
    tight = MediaLake(lake.resolver, lake.store, limits=Limits(max_decoded_bytes=100_000))
    made = tight.hydrate(anchor(big, row), "row").read()
    assert [f.code for f in made.findings] == ["unsafe_entry"]
