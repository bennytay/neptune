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
import tarfile
import threading
from collections.abc import Iterator
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
            assert isinstance(sliced.value, SourceSlice)
            assert sliced.value.read() == data
    assert cited > 60
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
    whole = canonical_json.loads(value.read())
    assert isinstance(whole, dict) and whole["robot_base_frame"] == "base_link"
    tool = {"kind": "json_pointer", "pointer": "/transformation/qw"}
    assert (
        canonical_json.loads(
            lake.artefact(anchor(handeye, byte_range(0, 348), tool), "value").read()
        )
        == 0.7071067811865476
    )

    sites = fixture_path("mobile_robot", "sites.csv")
    row = lake.artefact(anchor(sites, {"kind": "row", "row": 2}), "row")
    assert canonical_json.loads(row.read()) == {
        "cells": ["S-008", "Berth 4", "", "-33.8612", "151.2111", ""],
        "format": "csv",
        "row": 2,
    }
    cell = {"column": 2, "column_name": "aka", "kind": "row_cell", "row": 1}
    assert canonical_json.loads(lake.artefact(anchor(sites, cell), "row").read()) == {
        "cell": "NP;Plant 3",
        "column": 2,
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
    assert set(raw.transform["libraries"]) == {"mcap", "mcap-ros2-support", "pillow"}
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
    ref = anchor(clip, frame)

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
    # A video frame is never decoded by this version: only its bytes are served.
    assert lake.codes(ref, "frame") == ["invalid_request"]


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
    assert lake.codes(ref, "bytes") == ["file_missing"]
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
