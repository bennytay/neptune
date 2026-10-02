"""The MCAP adapter on real recordings: what it reads, what it emits, and what it cites.

The fixtures (``tests/fixtures/mcap``) are one recording written several ways. The oracle for the
messages is the official ``mcap`` reader's reading of the same files (``oracle.json``); the oracle
for citations is ``mcap_reading.resolve``, which decompresses chunks itself.
"""

import importlib.util
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import (
    SIGNATURE,
    VERIFIED,
    ConfigError,
    ProbeHints,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.mcap import DESCRIPTOR, McapAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.knowledge import AssertionKind, Known, KnownAbsent, NotApplicable, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SEQ, row_order
from neptune.model.time import NANOSECOND, ClockRole, Timestamp
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "mcap"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_mcap")
READING: Final = _load("mcap_reading")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
VALID: Final = (
    "robot.mcap",
    "robot_lz4.mcap",
    "robot_plain.mcap",
    "unchunked.mcap",
    "no_summary.mcap",
    "no_message_index.mcap",
    "unknown_encoding.mcap",
    "empty.mcap",
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(
    data: bytes, chunk_bytes: int = 64 << 20, max_rows: int = 100_000, **config: Any
) -> SourceOutput:
    return ingest_source(McapAdapter(chunk_bytes, max_rows), BytesReader(data), config)


def of(output: SourceOutput, kind: type) -> list[Any]:
    return [record for record in output.records() if isinstance(record, kind)]


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings() if f.code != "mcap.payload_not_decoded")


def streams(output: SourceOutput) -> dict[str, Stream]:
    found = {}
    for stream in of(output, Stream):
        topic = stream.topic.known_or_raise() if isinstance(stream.topic, Known) else ""
        found[topic] = stream
    return found


def rows(output: SourceOutput) -> Iterator[tuple[Stream, dict[str, object]]]:
    by_id = {stream.id: stream for stream in of(output, Stream)}
    for stream_id, batches in output.series().items():
        for batch in batches:
            for row in batch.rows():
                yield by_id[stream_id], row


def in_file_order(output: SourceOutput) -> list[tuple[Stream, dict[str, object]]]:
    def place(item: tuple[Stream, dict[str, object]]) -> tuple[int, ...]:
        steps = item[0].row_evidence(item[1]).locator
        return tuple(step.offset for step in steps if isinstance(step, ByteRange))

    return sorted(rows(output), key=place)


def as_bytes(output: SourceOutput) -> bytes:
    """Records and findings as canonical lines, then every stream's rows in ``seq`` order."""
    lines = [canonical_json.dumps(record.to_json()) for record in output.package_records()]
    series = []
    for stream, batches in output.series().items():
        found = sorted((row for batch in batches for row in batch.rows()), key=lambda r: r[SEQ])  # type: ignore[arg-type, return-value]
        series.append(f"{stream} {[sorted(row.items()) for row in found]!r}".encode())
        series.append(repr(sorted({batch.schema() for batch in batches})).encode())
    return b"\n".join(sorted(lines) + series)


# --- Probe ---------------------------------------------------------------------------------------


def probe(data: bytes, name: str = "x") -> tuple[float, list[str], str | None]:
    result = McapAdapter().probe(data[: 64 * 1024], ProbeHints(name, len(data)))
    return result.confidence, [r.code for r in result.reasons], result.version


def test_a_recording_is_verified_by_its_magic_and_header_whatever_its_name() -> None:
    expected = (VERIFIED, ["mcap.magic", "mcap.header"], "0")
    assert probe(fixture("robot.mcap"), "robot.mcap") == expected
    assert probe(fixture("robot.mcap"), "notes.txt") == expected
    assert probe(fixture("robot.mcap"), "") == expected


def test_magic_without_a_well_formed_header_is_a_signature_match() -> None:
    assert probe(MAKE.MAGIC + b"\x01" + bytes(8))[:2] == (SIGNATURE, ["mcap.magic"])
    assert probe(MAKE.MAGIC)[:2] == (SIGNATURE, ["mcap.magic"])


def test_anything_else_is_not_mcap() -> None:
    for data in (b"", b"\x89MCAP", b"\x89MCAP1\r\n" + bytes(20), b"plain text\n", b"PK\x03\x04"):
        assert probe(data)[:2] == (0.0, ["mcap.no_magic"])


# --- Inspect and plan: cheap, from the summary ---------------------------------------------------


class Recording:
    """A reader that records every range read."""

    def __init__(self, data: bytes) -> None:
        self.inner = BytesReader(data)
        self.reads: list[tuple[int, int]] = []

    @property
    def content_id(self) -> Any:
        return self.inner.content_id

    @property
    def size(self) -> int:
        return self.inner.size

    def read(self, offset: int, length: int) -> bytes:
        self.reads.append((offset, length))
        return self.inner.read(offset, length)


def data_section_reads(data: bytes, reads: list[tuple[int, int]]) -> int:
    """Bytes read between the Header record and the summary."""
    footer = len(data) - 37
    summary_start = int.from_bytes(data[footer + 9 : footer + 17], "little")
    header_end = 8 + 9 + int.from_bytes(data[9:17], "little")
    return sum(
        max(0, min(offset + length, summary_start) - max(offset, header_end))
        for offset, length in reads
    )


def test_inspect_summarises_from_the_head_and_the_summary_alone() -> None:
    data = fixture("robot.mcap")
    source = Recording(data)
    result = McapAdapter().inspect(source, configure(DESCRIPTOR))
    assert data_section_reads(data, source.reads) == 0
    summary: Any = result.summary
    assert result.findings == ()
    assert (summary["planning"], summary["summary"], summary["format_version"]) == (
        "indexed",
        "usable",
        "0",
    )
    assert summary["header"] == {"library": "neptune-fixture/1", "profile": "ros2"}
    assert [c["topic"] for c in summary["channels"]] == [
        "/imu",
        "/battery",
        "/diagnostics",
        "/imu_rear",
    ]
    assert summary["statistics"]["message_count"] == 18
    assert summary["chunks"]["count"] == 3
    assert summary["attachments"][0]["name"] == "calibration.yaml"
    assert summary["metadata"][0]["name"] == "recording"
    canonical_json.dumps(summary)


def test_inspect_says_a_file_without_a_footer_is_scanned() -> None:
    result = McapAdapter().inspect(BytesReader(fixture("truncated.mcap")), configure(DESCRIPTOR))
    assert (result.summary["summary"], result.summary["planning"]) == ("no footer", "scan")
    assert [f.code for f in result.findings] == ["mcap.truncated"]
    not_mcap = McapAdapter().inspect(BytesReader(b"hello"), configure(DESCRIPTOR))
    assert [f.code for f in not_mcap.findings] == ["mcap.bad_magic"]


def test_an_indexed_plan_reads_the_data_section_only_to_confirm_two_chunks() -> None:
    data = fixture("robot.mcap")
    source = Recording(data)
    plan = McapAdapter(chunk_bytes=1, max_rows=1).plan(source, configure(DESCRIPTOR))
    assert data_section_reads(data, source.reads) == 2 * 9  # the first and the last chunk's heads
    assert len(plan.chunks) > 3 and plan.findings == ()
    assert all("index" in chunk.context for chunk in plan.chunks[1:])


def test_a_file_without_an_index_is_planned_by_scanning_it() -> None:
    plan = McapAdapter().plan(BytesReader(fixture("no_summary.mcap")), configure(DESCRIPTOR))
    assert all("index" not in chunk.context for chunk in plan.chunks)
    assert plan.findings == ()


# --- What a recording declares -------------------------------------------------------------------


ROBOT: Final = run(fixture("robot.mcap"))


def cites(data: bytes, evidence: EvidenceRef) -> bytes:
    return bytes(READING.resolve(data, evidence))


def grounds(state: Known[Any]) -> EvidenceRef:
    """The evidence a value's own provenance cites."""
    assert isinstance(state.provenance, Provenance)
    return state.provenance.evidence


def test_the_run_cites_the_header_and_its_extent_the_statistics_fields() -> None:
    data = fixture("robot.mcap")
    (run_record,) = of(ROBOT, Run)
    assert cites(data, run_record.provenance.evidence)[:1] == b"\x01"  # the Header record
    assert (run_record.logical_id, run_record.machine) == (Unknown(), Unknown())
    log_time = streams(ROBOT)["/imu"].clocks[0]
    assert isinstance(run_record.first, Known) and isinstance(run_record.last, Known)
    assert run_record.first.value == Timestamp(MAKE.T0 + 10 * MAKE.MS, log_time)
    assert run_record.last.value == Timestamp(MAKE.T0 + 90 * MAKE.MS, log_time)
    for state, value in (
        (run_record.first, MAKE.T0 + 10 * MAKE.MS),
        (run_record.last, MAKE.T0 + 90 * MAKE.MS),
    ):
        assert cites(data, grounds(state)) == value.to_bytes(8, "little")


def test_each_clock_is_its_own_domain_as_the_specification_defines_it() -> None:
    domains = {domain.id: domain for domain in of(ROBOT, TimestampDomain)}
    imu, battery = streams(ROBOT)["/imu"], streams(ROBOT)["/battery"]
    log_time, publish = domains[imu.clocks[0]], domains[imu.clocks[1]]
    assert (log_time.field, log_time.scope, log_time.role) == (
        "log_time",
        (),
        Known(ClockRole.RECEIVE),
    )
    assert (publish.field, publish.scope) == ("publish_time", ("/imu",))
    assert isinstance(publish.role, Known) and publish.role.value is ClockRole.PUBLISH
    for domain in (log_time, publish):
        assert isinstance(domain.resolution, Known) and domain.resolution.value == NANOSECOND
        assert (domain.epoch, domain.timescale) == (Unknown(), Unknown())  # never assumed
    assert battery.clocks[0] == imu.clocks[0]  # one recorder clock per file
    assert battery.clocks[1] != imu.clocks[1]  # each publisher's clock is its own
    # one recorder clock, a publisher clock per channel, and a header stamp per Imu channel
    assert len(domains) == 1 + 4 + 2


def test_a_stream_holds_its_channel_and_schema_as_declared() -> None:
    data = fixture("robot.mcap")
    found = streams(ROBOT)
    imu = found["/imu"]
    assert cites(data, imu.provenance.evidence)[:1] == b"\x04"  # its Channel record
    assert isinstance(imu.schema_name, Known) and imu.schema_name.value == "sensor_msgs/msg/Imu"
    assert cites(data, grounds(imu.schema_name))[:1] == b"\x03"  # its Schema record
    assert isinstance(imu.schema_encoding, Known) and imu.schema_encoding.value == "ros2msg"
    assert isinstance(imu.schema_definition, Known)
    assert cites(data, imu.schema_definition.value) == MAKE.IMU_DEFINITION.encode()
    assert imu.message_encoding == Known("cdr")
    assert imu.metadata == (("offered_qos_profiles", MAKE.QOS),)
    assert isinstance(imu.message_count, Known) and imu.message_count.value == 11
    entry = bytes.fromhex("0100") + (11).to_bytes(8, "little")  # channel 1, count 11
    assert cites(data, grounds(imu.message_count)) == entry
    diagnostics = found["/diagnostics"]  # schema 0: the specification says it has none
    assert isinstance(diagnostics.schema_name, KnownAbsent)
    assert diagnostics.schema_name == diagnostics.schema_definition
    rear = found["/imu_rear"]  # declared, never written to
    assert rear.message_count == Unknown()
    assert (rear.first, rear.last) == (Unknown(), Unknown())
    assert ROBOT.series()[rear.id][0].length == 0


def test_metadata_is_a_table_of_its_entries_and_attachments_are_cited() -> None:
    data = fixture("robot.mcap")
    (table,) = of(ROBOT, StructuredTable)
    assert (table.name, table.header) == (Known("recording"), NotApplicable())
    entries = sorted(of(ROBOT, StructuredRecord), key=lambda r: r.row)
    assert [[c.value if isinstance(c, Known) else None for c in r.cells] for r in entries] == [
        ["operator", "ci"],
        ["robot_id", "arm-7"],
        ["site", None],  # a blank value is unknown, never ""
    ]
    assert entries[2].cells[1] == Unknown()
    assert entries[0].provenance.evidence.locator[-1] == Row(0)
    assert cites(data, table.provenance.evidence)[:1] == b"\x0c"
    (attachment,) = [f for f in ROBOT.findings() if f.code == "mcap.attachment_not_extracted"]
    details: Any = attachment.details
    offset, length = details["data"]
    assert data[offset : offset + length] == MAKE.CALIBRATION
    assert attachment.details["crc_checked"] is True


def test_every_undecoded_stream_has_a_payload_finding_and_nothing_else_is_reported() -> None:
    assert codes(ROBOT) == ["mcap.attachment_not_extracted"]
    payloads = [f for f in ROBOT.findings() if f.code == "mcap.payload_not_decoded"]
    # /battery and /diagnostics are JSON; both Imu channels decode by their ros2msg definition
    assert len(payloads) == 2 and all(len(f.records) == 1 for f in payloads)
    assert sorted(f.details["reason"] for f in payloads) == ["message_encoding"] * 2


# --- Every message a row, every row its bytes ----------------------------------------------------


@pytest.mark.parametrize("name", VALID)
def test_rows_are_the_official_readers_messages_in_file_order(name: str) -> None:
    output = run(fixture(name))
    expected = ORACLE[name]["streamed"]
    assert expected["error"] is None
    found = []
    for stream, row in in_file_order(output):
        topic = stream.topic.value if isinstance(stream.topic, Known) else ""
        length = stream.row_evidence(row).locator[-1]
        assert isinstance(length, ByteRange)
        found.append(
            [topic, row["value/sequence"], row["time/0"], row["time/1"], length.length - 31]
        )
    assert found == expected["messages"]


@pytest.mark.parametrize("name", VALID)
def test_every_row_cites_exactly_its_message(name: str) -> None:
    data = fixture(name)
    output = run(data)
    for stream, row in rows(output):
        stream.check_row(row)
        _opcode, record = READING.record_at(data, stream.row_evidence(row))
        message = READING.message(record)
        assert (row["time/0"], row["time/1"], row["value/sequence"]) == (
            message.log_time,
            message.publish_time,
            message.sequence,
        )
        assert (row["state/time/0"], row["state/time/1"]) == ("known", "known")
        channel = READING.record_at(data, stream.provenance.evidence)[1]
        assert int.from_bytes(channel[9:11], "little") == message.channel_id


@pytest.mark.parametrize("name", VALID)
def test_seq_is_each_streams_source_order_and_rows_sort_by_log_time(name: str) -> None:
    output = run(fixture(name))
    by_stream: dict[Any, list[dict[str, object]]] = {}
    for stream, row in in_file_order(output):
        by_stream.setdefault(stream.id, []).append(row)
    for found in by_stream.values():
        assert [row[SEQ] for row in found] == list(range(len(found)))
    imu = [row for stream, row in rows(output) if stream.topic == Known("/imu")]
    stored = sorted(imu, key=row_order)
    if imu:
        assert [row[SEQ] for row in stored][:5] == [0, 1, 4, 2, 3]  # logged late; a tie by seq


def test_every_layout_reads_the_same_messages_with_the_same_seq() -> None:
    def keyed(output: SourceOutput) -> list[tuple[object, ...]]:
        return sorted(
            (str(stream.topic), row[SEQ], row["time/0"], row["time/1"], row["value/sequence"])
            for stream, row in rows(output)
        )

    reference = keyed(ROBOT)
    for name in ("robot_lz4.mcap", "robot_plain.mcap", "unchunked.mcap", "no_summary.mcap"):
        assert keyed(run(fixture(name))) == reference, name


# --- Determinism, chunking and lineage -----------------------------------------------------------


GRANULARITIES: Final = ((64 << 20, 100_000), (1, 1), (300, 2), (2_000, 5))


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.mcap")))
def test_output_is_byte_identical_twice_and_whatever_the_plan_cuts(name: str) -> None:
    data = fixture(name)
    reference = as_bytes(run(data))
    assert as_bytes(run(data)) == reference
    for chunk_bytes, max_rows in GRANULARITIES:
        assert as_bytes(run(data, chunk_bytes, max_rows)) == reference, (chunk_bytes, max_rows)


def test_a_chunk_with_more_messages_than_a_planned_chunk_holds_is_read_in_stretches() -> None:
    plan = McapAdapter(max_rows=2).plan(BytesReader(fixture("robot.mcap")), configure(DESCRIPTOR))
    stretches = [c.context for c in plan.chunks if "first" in c.context]
    assert stretches and stretches[0]["first"] == 0
    assert [("last" in c) for c in stretches].count(False) == 3  # one open-ended per chunk


def test_another_version_or_config_is_another_lineage() -> None:
    data = fixture("robot.mcap")

    class Bumped(McapAdapter):
        descriptor = replace(DESCRIPTOR, version="0.1.1")

    bumped = ingest_source(Bumped(), BytesReader(data))
    configured = run(data, max_chunk_bytes=1 << 20)
    original = {record.id for record in ROBOT.records()}
    for other in (bumped, configured):
        assert other.config.transform.id != ROBOT.config.transform.id
        assert original.isdisjoint(record.id for record in other.records())


def test_the_decompressors_are_output_affecting_libraries_of_the_lineage() -> None:
    data = fixture("robot.mcap")
    assert {name for name, _ in DESCRIPTOR.libraries} == {"lz4", "zstandard"}
    assert all(version for _, version in DESCRIPTOR.libraries)

    class Upgraded(McapAdapter):
        descriptor = replace(
            DESCRIPTOR,
            libraries=tuple((name, version + ".1") for name, version in DESCRIPTOR.libraries),
        )

    upgraded = ingest_source(Upgraded(), BytesReader(data))
    assert upgraded.config.transform.id != ROBOT.config.transform.id
    assert dict(upgraded.config.transform.libraries) == {
        name: version + ".1" for name, version in DESCRIPTOR.libraries
    }
    (run_record,) = [r for r in upgraded.records() if isinstance(r, Run)]
    (original,) = [r for r in ROBOT.records() if isinstance(r, Run)]
    assert run_record.provenance.transform == upgraded.config.transform.id
    assert run_record.provenance.transform != original.provenance.transform


# --- Selecting topics and times ------------------------------------------------------------------


def test_a_topic_pattern_selects_whole_topics_and_says_what_it_left_out() -> None:
    output = run(fixture("robot.mcap"), topic_pattern="/imu")
    topics = {str(stream.topic) for stream, _ in rows(output)}
    assert topics == {str(Known("/imu"))}
    skipped = [f for f in output.findings() if f.code == "mcap.not_selected"]
    assert sorted(f.details["id"] for f in skipped) == [2, 3, 4]  # type: ignore[type-var]
    assert len(of(output, Stream)) == 4  # every channel is still declared


def test_a_log_time_window_keeps_each_rows_seq() -> None:
    start, end = MAKE.T0 + 30 * MAKE.MS, MAKE.T0 + 60 * MAKE.MS
    output = run(fixture("robot.mcap"), log_time_start=start, log_time_end=end)
    windowed = {(str(s.topic), r[SEQ], r["time/0"]) for s, r in rows(output)}
    full = {(str(s.topic), r[SEQ], r["time/0"]) for s, r in rows(ROBOT)}
    assert windowed == {item for item in full if start <= item[2] <= end}


def test_a_window_outside_a_chunk_never_decompresses_it() -> None:
    data = fixture("bad_crc.mcap")  # chunk 1 fails its CRC, which only decompressing finds
    assert "mcap.crc_mismatch" in codes(run(data))
    after = run(data, log_time_start=MAKE.T0 + 10**12)
    assert "mcap.crc_mismatch" not in codes(after)
    assert list(rows(after)) == []


def test_a_pattern_that_is_not_a_regular_expression_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        run(fixture("robot.mcap"), topic_pattern="(")


def test_a_chunk_limit_above_what_the_adapter_declares_memory_for_is_a_config_error() -> None:
    for limit in (0, (256 << 20) + 1):
        with pytest.raises(ConfigError):
            run(fixture("robot.mcap"), max_chunk_bytes=limit)
    assert run(fixture("robot.mcap"), max_chunk_bytes=256 << 20).config.transform.id


def test_what_the_statistics_state_is_stated_not_observed() -> None:
    (run_record,) = of(ROBOT, Run)
    for knowledge in (run_record.first, run_record.last):
        assert knowledge.provenance.assertion_kind is AssertionKind.STATED
    counted = [s for s in of(ROBOT, Stream) if isinstance(s.message_count, Known)]
    assert len(counted) == 3  # /imu_rear has no entry in the statistics
    for stream in counted:
        assert stream.message_count.provenance.assertion_kind is AssertionKind.STATED


def test_inspect_lists_at_most_a_bounded_number_of_entries_per_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("neptune.adapters.mcap.summary.MAX_LISTED", 2)
    result = McapAdapter().inspect(BytesReader(fixture("robot.mcap")), configure(DESCRIPTOR))
    summary: Any = result.summary
    assert len(summary["channels"]) == 2 and summary["channels_omitted"] == 2
    assert len(summary["schemas"]) <= 2 and "metadata_omitted" not in summary
    assert len(summary["attachments"]) == 1 and "attachments_omitted" not in summary
