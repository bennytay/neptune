"""MVL-22's acceptance: a consumer asks for the media around a 10-second event on a named clock
and gets frame-to-time rows and byte-range handles, without decoding the run (ADR 0056).

A job ingests ``media.mcap`` (a manipulator's wrist camera, a quadruped's depth camera, an
autonomous vehicle's lidar, a drone gimbal's compressed images and video) and an hour-long run
generated into a temporary directory; everything is read back through the SDK.
"""

import dataclasses
import importlib.util
import shutil
from pathlib import Path
from types import ModuleType
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from neptune.derived.media import (
    MEDIA_KIND,
    DerivativeState,
    Media,
    MediaConfig,
    MediaState,
    index_media,
    media_stream_from_json,
)
from neptune.derived.semantics import StreamSemantic
from neptune.derived.sessions import read_derived
from neptune.identity import canonical_json
from neptune.model.knowledge import KnowledgeState, Known
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.run import Stream
from neptune.sdk import InvalidRequestError, Neptune
from neptune.sdk.media import (
    FileSource,
    Frame,
    HydrationError,
    Hydrator,
    header_stamp,
    image_thumbnail,
    media_streams,
    media_window,
    point_cloud_ref,
)
from neptune.store.package import IngestPackage

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "media"
SECOND: Final = 10**9


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make: Final = _load("make_media_mcap", FIXTURES / "make_media_mcap.py")
T0: Final[int] = make.T0


def _ingest(tmp_path: Path, source: Path) -> tuple[IngestPackage, Path]:
    folder = tmp_path / "sources"
    folder.mkdir()
    shutil.copy(source, folder / source.name)
    result = Neptune(tmp_path / "workspace").ingest(folder, tmp_path / "package")
    return result.read_package(), folder / source.name


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> tuple[IngestPackage, Path]:
    return _ingest(tmp_path_factory.mktemp("media"), FIXTURES / "media.mcap")


def _topics(package: IngestPackage) -> dict[str, Stream]:
    return {
        r.topic.value: r
        for r in package.records
        if isinstance(r, Stream) and isinstance(r.topic, Known)
    }


def test_the_fixture_is_what_its_generator_writes() -> None:
    assert (FIXTURES / "media.mcap").read_bytes() == make.build()


def test_every_robots_media_stream_is_indexed_and_nothing_else(
    media: tuple[IngestPackage, Path],
) -> None:
    package, _ = media
    topics = _topics(package)
    by_stream = {line.stream: line for line in media_streams(package)}
    kinds = {topic: by_stream[s.id].media for topic, s in topics.items() if s.id in by_stream}
    assert kinds == {
        "/arm/wrist_camera/image_raw": Media.IMAGE,  # manipulator
        "/quadruped/depth/image_rect": Media.IMAGE,  # quadruped
        "/av/lidar/points": Media.POINT_CLOUD,  # autonomous vehicle
        "/drone/gimbal/image/compressed": Media.COMPRESSED_IMAGE,  # drone
        "/drone/gimbal/video": Media.VIDEO,
    }
    semantics = {
        line.id: line for line in read_derived(package.derived) if isinstance(line, StreamSemantic)
    }
    for line in by_stream.values():
        assert line.frames == 10 and line.state is MediaState.KNOWN
        assert line.hydrator == "mcap_message" and line.message_encoding == "cdr"
        if line.media is Media.VIDEO:  # read from the declared type: no semantic is video
            assert line.basis is None
        else:  # the kind is the inferred semantic's, citing what it read
            assert line.basis is not None and semantics[line.basis].stream == line.stream
            assert line.evidence == semantics[line.basis].evidence
        assert (
            media_stream_from_json(canonical_json.loads(canonical_json.dumps(line.to_json())))
            == line
        )
    wrist = by_stream[topics["/arm/wrist_camera/image_raw"].id]
    assert wrist.thumbnail.state is DerivativeState.ON_REQUEST
    video = by_stream[topics["/drone/gimbal/video"].id]
    assert (video.thumbnail.state, video.keyframe.state) == (DerivativeState.NOT_COVERED,) * 2
    assert video.keyframe.reason == "codec_not_covered"
    cloud = by_stream[topics["/av/lidar/points"].id]
    assert cloud.thumbnail.state is DerivativeState.NOT_APPLICABLE
    codes = sorted(f.code for f in package.receipt.findings if f.code.startswith("neptune.media"))
    assert codes == ["neptune.media.derivative_not_covered"] * 2  # thumbnails; video keyframes


def test_a_window_keeps_every_declared_clock_and_selects_on_the_one_named(
    media: tuple[IngestPackage, Path],
) -> None:
    package, _ = media
    wrist = _topics(package)["/arm/wrist_camera/image_raw"].id
    # Frame n of the wrist camera is logged at T0 + n*100 ms + 1 ms and published 2 ms earlier.
    logged = media_window(package, "log_time", T0 + 201 * 10**6, T0 + 301 * 10**6, streams=[wrist])
    assert [f.seq for f in logged.frames] == [2, 3]
    published = media_window(
        package, "publish_time", T0 + 201 * 10**6, T0 + 301 * 10**6, streams=[wrist]
    )
    assert [f.seq for f in published.frames] == [3]  # 299 ms; frame 2 was published at 199 ms
    frame = logged.frames[0]
    assert [(t.field, t.ticks) for t in frame.times] == [
        ("log_time", T0 + 201 * 10**6),
        ("publish_time", T0 + 199 * 10**6),
    ]
    assert all(t.state is KnowledgeState.KNOWN for t in frame.times)
    assert frame.ticks(frame.times[1].clock) == T0 + 199 * 10**6
    assert len(frame.handle.locator) == 2  # the chunk in the file, the record in the chunk
    everything = media_window(package, "log_time", T0, T0 + 10 * SECOND)
    assert len(everything.frames) == 50
    assert {s.media.media for s in everything.streams} == set(Media)
    assert media_window(package, "header_stamp", T0, T0 + SECOND).frames == ()
    assert {s.reason for s in media_window(package, "nope", 0, 1).streams} == {"no_clock"}


def test_hydration_reads_only_the_frames_bytes(media: tuple[IngestPackage, Path]) -> None:
    package, source = media
    topics = _topics(package)
    lines = {line.stream: line for line in media_streams(package)}
    window = media_window(package, "log_time", T0, T0 + 10 * SECOND)
    with FileSource(source, window.frames[0].handle.source) as file:
        frames = Hydrator(file)
        by_topic: dict[str, list[tuple[Frame, bytes]]] = {}
        for frame in window.frames:
            topic = next(t for t, s in topics.items() if s.id == frame.stream)
            by_topic.setdefault(topic, []).append((frame, frames.payload(frame)))
        assert file.bytes_read <= file.size  # each chunk read once, kept for its frames
    wrist, payload = by_topic["/arm/wrist_camera/image_raw"][3]
    stamp = header_stamp(payload, wrist.message_encoding)
    assert (stamp.sec, stamp.nanosec, stamp.frame_id) == (
        1_700_000_000,
        294_000_000,
        "wrist_camera",
    )
    thumb = image_thumbnail(wrist, payload, lines[wrist.stream])
    assert thumb.state is KnowledgeState.KNOWN and thumb.media_type == "image/x-portable-pixmap"
    assert thumb.data is not None and thumb.data.startswith(b"P6\n8 6\n255\n")
    assert thumb.evidence == wrist.handle and thumb.transform.upstream == (
        lines[wrist.stream].transform,
    )
    assert image_thumbnail(wrist, payload, lines[wrist.stream]) == thumb  # deterministic
    depth, payload = by_topic["/quadruped/depth/image_rect"][0]
    gray = image_thumbnail(depth, payload, lines[depth.stream])
    assert gray.data is not None and gray.data.startswith(b"P5\n8 6\n65535\n")
    cloud, payload = by_topic["/av/lidar/points"][0]
    ref = point_cloud_ref(payload, cloud.message_encoding)
    assert [f.name for f in ref.fields] == ["x", "y", "z", "intensity"]
    assert (ref.width, ref.point_step, ref.data_length) == (16, 16, 256)
    assert payload[ref.data_offset : ref.data_offset + 4] == b"\x00\x00\x00\x00"  # x of point 0
    jpeg, payload = by_topic["/drone/gimbal/image/compressed"][0]
    refused = image_thumbnail(jpeg, payload, lines[jpeg.stream])
    assert (refused.state, refused.reason) == (KnowledgeState.NOT_COVERED, "codec_not_covered")


def test_an_hour_long_run_answers_a_ten_second_event_from_a_sliver_of_its_bytes(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    size = make.hour(run / "hour.mcap")  # ~60 MB: generated, never committed
    package, source = _ingest(tmp_path, run / "hour.mcap")
    assert {line.frames for line in media_streams(package)} == {18_000, 3_600}
    event = T0 + 1800 * SECOND
    window = media_window(package, "log_time", event, event + 10 * SECOND)
    assert [len(s.frames) for s in window.streams] == [51, 10]  # 5 Hz camera, 1 Hz lidar
    with FileSource(source, window.frames[0].handle.source) as file:
        frames = Hydrator(file)
        payloads = [frames.payload(frame) for frame in window.frames]
        read = file.bytes_read
    assert all(payloads) and read < size // 200  # ≪ the file: under 0.5 % of it
    assert all(event <= (f.ticks("log_time") or 0) <= event + 10 * SECOND for f in window.frames)


def test_past_the_frame_budget_a_stream_is_not_covered_with_one_finding(
    media: tuple[IngestPackage, Path],
) -> None:
    package, _ = media
    streams = [r for r in package.records if isinstance(r, Stream)]
    semantics = [r for r in read_derived(package.derived) if isinstance(r, StreamSemantic)]
    adapters = {s.provenance.transform: "mcap" for s in streams}
    # A stream of millions of tiny messages costs its count, not its rows: nothing is read.
    frames = {s.id: 10 for s in streams}
    wrist = _topics(package)["/arm/wrist_camera/image_raw"].id
    frames[wrist] = 5_000_000
    index = index_media(streams, semantics, frames, adapters, MediaConfig(max_frames=1_000_000))
    assert index is not None
    states = {line.stream: line.state for line in index.lines}
    assert states[wrist] is MediaState.NOT_COVERED
    assert sum(state is MediaState.KNOWN for state in states.values()) == 4
    line = next(line for line in index.lines if line.stream == wrist)
    assert dict(line.counts) == {
        "frames": 5_000_000,
        "indexed": line.counts["indexed"],  # streams before it, in id order
        "limit": 1_000_000,
    }
    assert line.counts["indexed"] in (0, 10, 20, 30, 40)
    budget = [f for f in index.findings if f.code == "neptune.media.frame_budget"]
    assert len(budget) == 1 and budget[0].records == (wrist,)
    window = media_window(
        dataclasses.replace(
            package,
            derived={
                **package.derived,
                MEDIA_KIND: tuple(index.tables()[MEDIA_KIND]),
            },
        ),
        "log_time",
        T0,
        T0 + SECOND,
    )
    refused = next(s for s in window.streams if s.stream.id == wrist)
    assert (refused.state, refused.reason, refused.frames) == (
        KnowledgeState.NOT_COVERED,
        "frame_budget",
        (),
    )
    # Same inputs in any order: the same lines and findings.
    again = index_media(
        reversed(streams), reversed(semantics), frames, adapters, MediaConfig(max_frames=1_000_000)
    )
    assert again == index


def test_a_window_over_millions_of_tiny_frames_is_bounded(
    media: tuple[IngestPackage, Path], tmp_path: Path
) -> None:
    package, _ = media
    wrist = _topics(package)["/arm/wrist_camera/image_raw"]
    rows = 3_000_000
    seq = pa.array(range(rows), pa.int64())
    log = pa.array(range(T0, T0 + rows), pa.int64())  # one tiny frame a nanosecond
    columns = {
        "locator/0/length": pa.array([64] * rows, pa.int64()),
        "locator/0/offset": pa.array([8] * rows, pa.int64()),
        "locator/1/length": pa.array([40] * rows, pa.int64()),
        "locator/1/offset": seq,
        "seq": seq,
        "time/0": log,
        "time/1": log,
    }
    series = tmp_path / "tiny.parquet"
    pq.write_table(pa.table(columns), series, row_group_size=1 << 16)
    tiny = dataclasses.replace(package, series={**package.series, wrist.id: series})
    everything = media_window(tiny, "log_time", T0, T0 + rows, streams=[wrist.id])
    (part,) = everything.streams
    assert (part.state, part.reason, part.matched, part.frames) == (
        KnowledgeState.NOT_COVERED,
        "max_frames",
        rows,
        (),
    )
    narrow = media_window(tiny, "log_time", T0 + 2_000_000, T0 + 2_000_009, streams=[wrist.id])
    assert [f.seq for f in narrow.frames] == list(range(2_000_000, 2_000_010))
    assert narrow.frames[0].handle == EvidenceRef(
        wrist.series.source, (ByteRange(8, 64), ByteRange(2_000_000, 40))
    )


def test_hostile_handles_and_payloads_are_refused_not_trusted(
    media: tuple[IngestPackage, Path], tmp_path: Path
) -> None:
    package, source = media
    window = media_window(package, "log_time", T0, T0 + SECOND)
    frame = next(f for f in window.frames if f.media is Media.IMAGE)
    lines = {line.stream: line for line in media_streams(package)}
    with FileSource(source, frame.handle.source) as file:
        frames = Hydrator(file)
        moved = dataclasses.replace(
            frame, handle=EvidenceRef(frame.handle.source, (ByteRange(0, 64),))
        )
        with pytest.raises(HydrationError):
            frames.payload(moved)  # the magic, not a Message record
        huge = dataclasses.replace(
            frame, handle=EvidenceRef(frame.handle.source, (ByteRange(0, 1 << 40),))
        )
        with pytest.raises(HydrationError):
            frames.payload(huge)
        payload = frames.payload(frame)
    other = dataclasses.replace(frame, hydrator=None)
    with pytest.raises(HydrationError):
        Hydrator(FileSource(source, frame.handle.source)).payload(other)
    with pytest.raises(ValueError):
        header_stamp(payload[:9], "cdr")
    with pytest.raises(ValueError):
        header_stamp(payload, "protobuf")
    lying = bytearray(payload)
    lying[32:36] = (10_000).to_bytes(4, "little")  # height far past the pixels there are
    refused = image_thumbnail(frame, bytes(lying), lines[frame.stream])
    assert (refused.state, refused.reason) == (KnowledgeState.NOT_COVERED, "payload_malformed")
    with pytest.raises(ValueError):
        point_cloud_ref(payload[:40], "cdr")
    with pytest.raises(InvalidRequestError):
        media_window(package, "log_time", 2, 1)
    with pytest.raises(InvalidRequestError):
        media_window(package, "log_time", 0, 1, media=["hologram"])
