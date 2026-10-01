"""The MCAP adapter on damaged and hostile recordings: findings and partial output, never a raise.

Every case runs through ``ingest_source``, so every contract law is checked on the damaged output
too: exact citations, one chunk per finding, typed series, output independent of chunking.
"""

import importlib.util
import struct
import sys
import tracemalloc
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from neptune.adapters.contract import configure
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.finding import Severity
from neptune.model.knowledge import Known, Unknown
from neptune.model.run import Run, Stream

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


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(
    data: bytes, chunk_bytes: int = 64 << 20, max_rows: int = 100_000, **config: Any
) -> SourceOutput:
    return ingest_source(McapAdapter(chunk_bytes, max_rows), BytesReader(data), config)


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings() if f.code != "mcap.payload_not_decoded")


def finding(output: SourceOutput, code: str) -> Any:
    (found,) = [f for f in output.findings() if f.code == f"mcap.{code}"]
    return found


def row_keys(output: SourceOutput) -> set[tuple[object, ...]]:
    by_id = {s.id: s for s in output.records() if isinstance(s, Stream)}
    return {
        (str(by_id[stream].topic), row["seq"], row["time/0"], row["value/sequence"])
        for stream, batches in output.series().items()
        for batch in batches
        for row in batch.rows()
    }


FULL: Final = row_keys(run(fixture("robot.mcap")))


def cited(output: SourceOutput, data: bytes, code: str) -> bytes:
    return bytes(READING.resolve(data, finding(output, code).subject))


# --- The damaged fixtures ------------------------------------------------------------------------


def test_a_recording_cut_inside_its_last_chunk_keeps_every_whole_message_and_more() -> None:
    data = fixture("truncated.mcap")
    output = run(data)
    cut = finding(output, "truncated")
    assert (cut.severity, cut.details["recovered_messages"]) == (Severity.ERROR, 2)
    assert cited(output, data, "truncated")[:1] == b"\x06"  # the Chunk record, as far as it goes
    keys = row_keys(output)
    assert len(keys) == 13 + 2  # the official reader stops at 13; the cut chunk's prefix adds two
    plain = row_keys(run(fixture("robot_plain.mcap")))
    assert keys < plain


def test_a_chunk_failing_its_crc_costs_its_own_messages_only() -> None:
    data = fixture("bad_crc.mcap")
    output = run(data)
    crc = finding(output, "crc_mismatch")
    assert (crc.severity, crc.details["reason"]) == (Severity.ERROR, "crc")
    assert cited(output, data, "crc_mismatch")[:1] == b"\x06"
    assert len(row_keys(output)) == 12 and row_keys(output) < FULL  # chunks 0 and 2 land


def test_a_summary_failing_its_crc_is_not_used_and_the_file_is_scanned() -> None:
    output = run(fixture("bad_summary_crc.mcap"))
    assert codes(output) == ["mcap.attachment_not_extracted", "mcap.summary_unusable"]
    assert finding(output, "summary_unusable").details == {"reason": "crc"}
    assert row_keys(output) == FULL
    (run_record,) = [r for r in output.records() if isinstance(r, Run)]
    assert run_record.first == Unknown()  # the statistics are in the unusable summary


def test_an_overlapping_index_is_not_trusted() -> None:
    output = run(fixture("overlapping_index.mcap"))
    assert finding(output, "index_invalid").details == {"reason": "overlap"}
    assert row_keys(output) == FULL  # the official reader reads 19 messages here, one twice


def test_an_index_that_lies_with_its_statistics_costs_the_message_it_hides() -> None:
    data = fixture("lying_index.mcap")
    output = run(data)
    mismatch = finding(output, "message_count_mismatch")
    assert mismatch.severity is Severity.ERROR
    assert mismatch.details["channels"] == {"1": [3, 4]}
    assert cited(output, data, "message_count_mismatch")[:1] == b"\x06"
    assert finding(output, "index_mismatch").details["reason"] == "message_index"
    assert len(row_keys(output)) == 17


def test_an_unknown_compression_costs_its_chunk_and_nothing_else() -> None:
    output = run(fixture("unknown_compression.mcap"))
    unknown = finding(output, "unknown_compression")
    assert (unknown.details["compression"], unknown.severity) == ("brotli", Severity.ERROR)
    assert len(row_keys(output)) == 12


def test_unknown_encodings_are_recorded_as_declared_with_a_finding() -> None:
    output = run(fixture("unknown_encoding.mcap"))
    found = sorted(  # type: ignore[type-var]
        f.details["field"] for f in output.findings() if f.code == "mcap.unknown_encoding"
    )
    assert found == ["message_encoding", "schema_encoding"]
    pose = next(s for s in output.records() if isinstance(s, Stream) and s.topic == Known("/pose"))
    assert (
        isinstance(pose.schema_encoding, Known) and pose.schema_encoding.value == "neptune-test-idl"
    )
    assert pose.message_encoding == Known("neptune-test")
    assert len(row_keys(output)) == 19


def test_an_empty_recording_is_a_run_and_a_clock() -> None:
    output = run(fixture("empty.mcap"))
    assert sorted(r.kind for r in output.records()) == ["run", "timestamp_domain"]
    assert codes(output) == []


def test_bytes_that_are_not_mcap_are_one_finding_and_nothing_else() -> None:
    for data in (b"", b"not an mcap file at all", MAKE.MAGIC[:5]):
        output = run(data)
        assert [f.code for f in output.findings()] == ["mcap.bad_magic"]
        assert output.records() == ()


# --- Cut anywhere --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["robot.mcap", "robot_plain.mcap", "unchunked.mcap"])
def test_a_recording_cut_at_any_byte_gives_findings_and_a_subset_of_its_rows(name: str) -> None:
    data = fixture(name)
    full = row_keys(run(data))
    for size in range(8, len(data), 37):
        output = run(data[:size])
        assert row_keys(output) <= full, size
        assert {"mcap.truncated", "mcap.corrupt_record"} & set(codes(output)), size


# --- Hostile bytes -------------------------------------------------------------------------------


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_flipped_bytes_never_raise_and_never_break_the_contract(data: st.DataObject) -> None:
    name = data.draw(
        st.sampled_from(["robot.mcap", "robot_plain.mcap", "unchunked.mcap", "no_summary.mcap"])
    )
    original = bytearray(fixture(name))
    for _ in range(data.draw(st.integers(1, 4))):
        at = data.draw(st.integers(8, len(original) - 1))
        original[at] = data.draw(st.integers(0, 255))
    run(bytes(original), chunk_bytes=data.draw(st.sampled_from([1, 500, 64 << 20])))


def _string(text: bytes | str) -> bytes:
    raw = text.encode() if isinstance(text, str) else text
    return struct.pack("<I", len(raw)) + raw


def _record(opcode: int, content: bytes) -> bytes:
    return MAKE._record(opcode, content)  # type: ignore[no-any-return]


def unchunked(*records: bytes) -> bytes:
    """A file of these records, without chunks or a summary."""
    body = MAKE.MAGIC + _record(0x01, _string("ros2") + _string("test"))
    body += b"".join(records) + _record(0x0F, bytes(4))
    return bytes(body + struct.pack("<BQQQI", 0x02, 20, 0, 0, 0) + MAKE.MAGIC)


def channel(channel_id: int, topic: bytes, metadata: list[tuple[bytes, bytes]] = ()) -> bytes:  # type: ignore[assignment]
    entries = b"".join(_string(k) + _string(v) for k, v in metadata)
    body = struct.pack("<HH", channel_id, 0) + _string(topic) + _string("json")
    return _record(0x04, body + struct.pack("<I", len(entries)) + entries)


def message(channel_id: int, log_time: int, publish_time: int | None = None) -> bytes:
    fields = struct.pack(
        "<HIQQ", channel_id, 0, log_time, log_time if publish_time is None else publish_time
    )
    return _record(0x05, fields + b"{}")


def test_times_past_signed_64_bits_are_unknown_in_their_rows() -> None:
    data = unchunked(channel(1, b"/t"), message(1, 5), message(1, 2**64 - 1), message(1, 7, 2**63))
    output = run(data)
    found = finding(output, "time_out_of_range")
    assert found.details == {"channels": {"1": 2}}
    (batch,) = [b for batches in output.series().values() for b in batches if b.length]
    by_name = {c.name: c.values for c in batch.columns}
    # The rows are there, their times unknown: never wrapped, clamped or dropped.
    assert by_name["state/time/0"] == ("known", "unknown", "known")
    assert by_name["time/0"] == (5, None, 7)
    assert by_name["state/time/1"] == ("known", "unknown", "unknown")


def test_messages_of_an_undeclared_channel_are_one_finding_for_the_file() -> None:
    data = unchunked(channel(1, b"/t"), message(1, 1), message(9, 2), message(9, 3))
    output = run(data, chunk_bytes=1)
    found = finding(output, "unknown_channel")
    assert found.details == {"id": 9, "messages": 2}
    assert len(row_keys(output)) == 1


def test_a_channel_declared_twice_differently_keeps_its_first_declaration() -> None:
    data = unchunked(channel(1, b"/first"), message(1, 1), channel(1, b"/second"), message(1, 2))
    output = run(data)
    found = finding(output, "conflicting_declaration")
    assert found.details == {"id": 1, "record": "channel", "records": 1}
    (stream,) = [s for s in output.records() if isinstance(s, Stream)]
    assert stream.topic == Known("/first") and len(row_keys(output)) == 2


def test_bad_text_and_repeated_metadata_keys_are_findings_not_guesses() -> None:
    data = unchunked(
        channel(1, b"/bad\xff", [(b"k", b"1"), (b"k", b"2"), (b"ok", b"v")]), message(1, 1)
    )
    output = run(data)
    (stream,) = [s for s in output.records() if isinstance(s, Stream)]
    assert stream.topic == Unknown()  # never decoded with replacement characters
    assert stream.metadata == (("ok", "v"),)
    assert finding(output, "invalid_utf8").details["fields"] == ["topic"]
    assert finding(output, "duplicate_key").details["keys"] == ["k"]
    domains = {d.scope for d in output.records() if getattr(d, "field", None) == "publish_time"}
    assert domains == {("channel", "1")}


def test_short_messages_and_private_records_are_reported_and_skipped() -> None:
    short = _record(0x05, struct.pack("<HI", 1, 0))
    data = unchunked(
        channel(1, b"/t"), message(1, 1), short, _record(0x80, b"vendor"), message(1, 2)
    )
    output = run(data, chunk_bytes=1)
    assert finding(output, "corrupt_record").details == {"count": 1, "reason": "malformed"}
    assert finding(output, "unknown_record").details == {"opcodes": {"0x80": 1}}
    seqs = sorted(int(str(key[1])) for key in row_keys(output))
    assert seqs == [0, 2]  # the short message keeps its place in the stream's order


def test_a_top_level_message_in_a_chunked_file_is_reported_where_it_is() -> None:
    written, at = MAKE.write(MAKE.Options(summary=False))
    offset, _length = at["chunk:1"]
    stray = message(1, MAKE.T0)
    data = written[:offset] + stray + written[offset:]
    output = run(data)
    found = finding(output, "message_outside_layout")
    assert READING.resolve(data, found.subject) == stray
    assert row_keys(output) == FULL


def test_a_chunk_over_max_chunk_bytes_is_not_read() -> None:
    output = run(fixture("robot.mcap"), max_chunk_bytes=100)
    found = [f for f in output.findings() if f.code == "mcap.record_too_large"]
    assert len(found) == 3 and all(f.details["max_chunk_bytes"] == 100 for f in found)
    assert row_keys(output) == set()


def test_a_damaged_header_leaves_the_run_citing_the_magic() -> None:
    data = bytearray(fixture("no_summary.mcap"))
    data[8] = 0x7F  # the Header's opcode
    output = run(bytes(data))
    assert finding(output, "corrupt_record").details == {"reason": "header"}
    (run_record,) = [r for r in output.records() if isinstance(r, Run)]
    assert run_record.provenance.evidence.locator[0].length == 8  # type: ignore[union-attr]


def test_a_chunk_the_index_leaves_out_has_no_rows_however_the_file_is_cut() -> None:
    data, at = MAKE.write(MAKE.Options(unindexed=(1,), statistics=False))
    reference = None
    for chunk_bytes, max_rows in ((64 << 20, 100_000), (1, 1), (2_000, 5)):
        output = run(data, chunk_bytes, max_rows)
        found = finding(output, "index_mismatch")
        assert found.details == {"reason": "unindexed_chunk"}
        assert READING.resolve(data, found.subject) == data[slice(*_span(at["chunk:1"]))]
        keys = row_keys(output)
        reference = reference or keys
        assert keys == reference  # numbered alike whatever the plan cuts
    assert reference is not None and len(reference) == 7 + 5  # chunks 0 and 2
    unnumbered = {(topic, time, sequence) for topic, _, time, sequence in reference}
    assert unnumbered < {(topic, time, sequence) for topic, _, time, sequence in FULL}
    for topic in {key[0] for key in reference}:  # numbered as the index counts: no gap, no repeat
        seqs = sorted(int(str(key[1])) for key in reference if key[0] == topic)
        assert seqs == list(range(len(seqs)))


def _span(place: tuple[int, int]) -> tuple[int, int]:
    return place[0], place[0] + place[1]


def test_a_summary_without_declarations_is_planned_by_scanning_and_loses_nothing() -> None:
    data, _ = MAKE.write(MAKE.Options(summary_declarations=False))
    plan = McapAdapter().plan(BytesReader(data), configure(McapAdapter.descriptor))
    assert all("index" not in chunk.context for chunk in plan.chunks)
    output = run(data)
    assert codes(output) == ["mcap.attachment_not_extracted"]
    assert row_keys(output) == FULL


# --- Chunks walked lazily, and the index checked against the chunks ------------------------------


def chunk_file(records: bytes, compression: str = "zstd", times: tuple[int, int] = (0, 0)) -> bytes:
    """A chunked file of one chunk holding these records, without a summary (so it is scanned)."""
    stored = MAKE._compress(compression, records)
    fields = struct.pack("<QQQI", *times, len(records), zlib.crc32(records))
    content = fields + _string(compression) + struct.pack("<Q", len(stored)) + stored
    body = MAKE.MAGIC + _record(0x01, _string("ros2") + _string("test"))
    body += _record(0x06, content) + _record(0x0F, bytes(4))
    return bytes(body + struct.pack("<BQQQI", 0x02, 20, 0, 0, 0) + MAKE.MAGIC)


def test_a_chunk_of_many_small_messages_is_walked_in_memory_proportional_to_its_bytes() -> None:
    count = 120_000
    records = channel(1, b"/dense") + b"".join(message(1, i) for i in range(count))
    data = chunk_file(records, times=(0, count - 1))
    # Nothing is selected, so no row is held: what is measured is the walk and the index kept
    # for each message (16 bytes), not the output. An object per record would cost ten times that.
    tracemalloc.start()
    try:
        output = run(data, topic_pattern="^nothing$")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert row_keys(output) == set() and "mcap.not_selected" in {f.code for f in output.findings()}
    assert peak < 4 * len(records), (peak, len(records))


@pytest.mark.parametrize(
    ("name", "compression"),
    [("robot.mcap", "zstd"), ("robot_lz4.mcap", "lz4"), ("robot_plain.mcap", "")],
)
def test_a_file_cut_inside_its_last_chunk_keeps_a_strict_prefix_of_every_topic(
    name: str, compression: str
) -> None:
    data = fixture(name)
    full = row_keys(run(data))
    _, at = MAKE.write(MAKE.Options(compression=compression))
    start, length = at["chunk:2"]
    kept: set[tuple[object, ...]] = set()
    smallest = row_keys(run(data[: start + 1]))
    for size in range(start, start + length, 3):
        output = run(data[:size])
        keys = row_keys(output)
        assert keys <= full and kept <= keys, size  # more bytes never lose a row
        kept = keys
        for topic in {key[0] for key in keys}:  # no gap: what is kept is the first k messages
            seqs = sorted(int(str(key[1])) for key in keys if key[0] == topic)
            assert seqs == list(range(len(seqs))), (size, topic)
        assert {"mcap.truncated", "mcap.corrupt_record"} & set(codes(output)), size
    assert 0 < len(smallest) < len(full)  # the first two chunks land; the cut one is lost in part


def test_a_chunk_cut_short_reports_what_its_stored_prefix_decodes_to() -> None:
    data = fixture("robot_plain.mcap")  # a compressed chunk may decode to nothing before its block
    _, at = MAKE.write(MAKE.Options(compression=""))
    start, length = at["chunk:2"]
    found = finding(run(data[: start + length // 2]), "chunk_truncated")
    assert found.severity is Severity.WARNING
    details = found.details
    assert 0 < details["framed_bytes"] <= details["decoded_bytes"] < details["uncompressed_bytes"]


def _with_index_field(data: bytes, at: dict[str, tuple[int, int]], index: int, field: int) -> bytes:
    """Chunk index ``index`` with its 8-byte field ``field`` (0 start time, 1 end time) changed."""
    offset = at[f"summary:8:{index}"][0] + 9 + 8 * field
    (value,) = struct.unpack_from("<Q", data, offset)
    patched = MAKE._patch(data, offset, struct.pack("<Q", value + 1))
    return MAKE._refresh_summary_crc(patched)  # type: ignore[no-any-return]


def test_a_chunk_index_entry_that_disagrees_with_its_chunk_is_a_finding() -> None:
    data, at = MAKE.write(MAKE.Options())
    output = run(_with_index_field(data, at, 1, 1))
    found = finding(output, "index_mismatch")
    assert found.details["reason"] == "chunk_fields" and found.severity is Severity.WARNING
    assert list(found.details["fields"]) == ["end_time"]
    assert READING.resolve(data, found.subject) == data[slice(*_span(at["chunk:1"]))]
    assert row_keys(output) == FULL  # the chunk's own fields are read, so nothing is lost


def test_chunk_index_entries_that_agree_with_their_chunks_say_nothing() -> None:
    output = run(fixture("robot.mcap"))
    assert not [f for f in output.findings() if f.code == "mcap.index_mismatch"]


def test_chunks_skipped_on_the_index_alone_are_one_finding_with_their_reasons() -> None:
    data = fixture("robot.mcap")
    late = run(data, log_time_start=MAKE.T0 + 10**12)
    skipped = finding(late, "skipped_by_index")
    assert skipped.details == {"chunks": 3, "reasons": {"time": 3}}
    assert skipped.severity is Severity.INFO
    _, at = MAKE.write(MAKE.Options())
    assert READING.resolve(data, skipped.subject) == data[slice(*_span(at["chunk:0"]))]
    assert row_keys(late) == set()
    # By topic: only the middle chunk holds /battery, so the other two are skipped, not read.
    messages = (
        MAKE._imu(0, 10, 1, 0.1),
        MAKE._imu(1, 20, 1, 0.1),
        MAKE._battery(0, 30, 24.1, 0.8),
        MAKE._imu(2, 40, 1, 0.1),
        MAKE._imu(3, 50, 1, 0.1),
        MAKE._imu(4, 60, 1, 0.1),
    )
    sparse, at = MAKE.write(MAKE.Options(messages=messages, chunks=((0, 2), (2, 4), (4, 6))))
    battery = run(sparse, topic_pattern="^/battery$")
    topic = finding(battery, "skipped_by_index")
    assert topic.details == {"chunks": 2, "reasons": {"topics": 2}}
    assert READING.resolve(sparse, topic.subject) == sparse[slice(*_span(at["chunk:0"]))]
    assert {key[0] for key in row_keys(battery)} == {str(Known("/battery"))}
    assert not [f for f in run(data).findings() if f.code == "mcap.skipped_by_index"]


def test_a_chunk_of_more_records_than_its_size_can_hold_is_read_up_to_the_bound() -> None:
    limit = 2_000
    empty = _record(0x80, b"")  # 9 bytes: a private record, smaller than any message
    data = chunk_file(channel(1, b"/t") + message(1, 1) + empty * 150, "")
    output = run(data, max_chunk_bytes=limit)
    found = finding(output, "too_many_records")
    assert found.severity is Severity.ERROR
    assert found.details == {"max_chunk_bytes": limit, "max_records": limit // 31}
    assert len(row_keys(output)) == 1  # the message came before the bound
    assert "mcap.too_many_records" not in codes(run(data))
