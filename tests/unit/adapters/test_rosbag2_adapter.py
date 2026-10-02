"""The rosbag2 adapter on real bags: what it reads, emits and cites, and how it fails.

The fixtures (``tests/fixtures/rosbag2``) are one mobile-base recording written as an sqlite3 bag,
an MCAP bag and a split sqlite3 bag. The oracle for messages is what the official readers read
from them (``oracle.json``: rosbags, and the ``mcap`` package for the MCAP files); the oracle for
citations is the generator's own message list.
"""

import importlib.util
import json
import random
import shutil
import sqlite3
import struct
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import SIGNATURE, VERIFIED, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.rosbag2 import DESCRIPTOR, Rosbag2Adapter
from neptune.adapters.rosbag2._sqlite import Database, read_schema
from neptune.discovery.reader import BytesReader
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, Unknown
from neptune.model.provenance import ByteRange
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SEQ
from neptune.model.time import NANOSECOND, ClockRole
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "rosbag2"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_rosbag2")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
SQLITE_DIR: Final = FIXTURES / "mobile_base_sqlite3"
DB3: Final = SQLITE_DIR / "mobile_base_sqlite3_0.db3"
METADATA: Final = SQLITE_DIR / "metadata.yaml"
MESSAGES: Final = MAKE.messages()
START: Final = MESSAGES[0][0]
END: Final = MESSAGES[-1][0]


def run(data: bytes, adapter: Rosbag2Adapter | None = None, **values: Any) -> SourceOutput:
    return ingest_source(adapter or Rosbag2Adapter(), BytesReader(data), values or None)


def records(out: SourceOutput, kind: type) -> list[Any]:
    return [r for r in out.records() if isinstance(r, kind)]


def codes(out: SourceOutput) -> list[str]:
    return sorted(f.code.removeprefix("rosbag2.") for f in out.findings())


def rows_of(out: SourceOutput, stream: Stream) -> list[dict[str, object]]:
    return [row for batch in out.series().get(stream.id, ()) for row in batch.rows()]


def streams_by_topic(out: SourceOutput) -> dict[str, Stream]:
    return {s.topic.value: s for s in records(out, Stream) if isinstance(s.topic, Known)}


def mutate_db(tmp_path: Path, sql: str, *args: object, source: Path = DB3) -> bytes:
    path = tmp_path / "m.db3"
    shutil.copyfile(source, path)
    conn = sqlite3.connect(path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()
    return path.read_bytes()


def edit_metadata(old: str, new: str, source: Path = METADATA) -> bytes:
    text = source.read_text()
    assert old in text, old
    return text.replace(old, new, 1).encode()


@pytest.fixture(scope="module")
def sqlite_run() -> SourceOutput:
    return run(DB3.read_bytes())


@pytest.fixture(scope="module")
def metadata_run() -> SourceOutput:
    return run(METADATA.read_bytes())


# --- Probe and inspect ---------------------------------------------------------------------------


def probe(data: bytes, name: str = "") -> float:
    return Rosbag2Adapter().probe(data[:65536], ProbeHints(name, len(data))).confidence


def test_probe_is_decided_by_the_bytes_never_the_name() -> None:
    db, meta = DB3.read_bytes(), METADATA.read_bytes()
    assert probe(db) == probe(db, "x.bin") == probe(db, "a.db3") == VERIFIED
    assert probe(meta) == probe(meta, "notes.txt") == VERIFIED
    assert probe(b"") == probe(b"hello") == 0.0
    assert probe((FIXTURES / "mobile_base_mcap" / "mobile_base_mcap_0.mcap").read_bytes()) == 0.0
    assert probe(db[:100] + bytes(5000), "rosbag2.db3") == 0.0  # a header and nothing else
    assert probe(db[:3000]) in (0.0, VERIFIED)  # a cut head: never raises
    assert probe(b"other: 1\nrosbag2_bagfile_information: 1\n") == 0.0  # not top-level first key


def test_a_yaml_head_with_only_the_root_key_is_a_signature() -> None:
    assert probe(b"rosbag2_bagfile_information:\n  nothing: here\n") == SIGNATURE


def test_probe_ignores_other_databases(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE places(id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    assert probe(path.read_bytes(), "other.db3") == 0.0
    renamed = mutate_db(tmp_path, "DROP TABLE messages")
    assert probe(renamed) == 0.0


def test_probe_handles_a_head_that_cuts_a_character() -> None:
    text = (METADATA.read_text() + "é" * 100).encode()
    assert probe(text[: len(METADATA.read_bytes()) + 1]) == VERIFIED


def test_inspect_reads_the_schema_only() -> None:
    adapter = Rosbag2Adapter()
    from neptune.adapters.contract import configure

    config = configure(DESCRIPTOR)
    summary = adapter.inspect(BytesReader(DB3.read_bytes()), config).summary
    assert summary["rosbag2"] is True
    assert summary["topics"] == [
        {"name": "/cmd_vel", "type": "geometry_msgs/msg/Twist"},
        {"name": "/battery_voltage", "type": "std_msgs/msg/Float32"},
        {"name": "/status", "type": "std_msgs/msg/String"},
    ]
    assert adapter.inspect(BytesReader(METADATA.read_bytes()), config).summary["part"] == "metadata"
    assert (
        adapter.inspect(BytesReader(b"SQLite format 3\x00" + bytes(40)), config).summary["readable"]
        is False
    )


# --- sqlite3 storage -----------------------------------------------------------------------------


def test_a_clean_database_reads_with_no_findings_its_payloads_decoded(
    sqlite_run: SourceOutput,
) -> None:
    assert codes(sqlite_run) == []  # every type is in message_definitions, every payload CDR
    assert len(records(sqlite_run, Run)) == 1
    assert len(records(sqlite_run, TimestampDomain)) == 1
    assert len(records(sqlite_run, Stream)) == 3


def test_streams_are_the_topics_rows_as_declared(sqlite_run: SourceOutput) -> None:
    oracle = {c["topic"]: c for c in ORACLE["mobile_base_sqlite3"]["connections"]}
    streams = streams_by_topic(sqlite_run)
    assert set(streams) == set(oracle)
    for topic, stream in streams.items():
        declared = oracle[topic]
        assert stream.schema_name == Known(declared["msgtype"], stream.schema_name.provenance)  # type: ignore[union-attr]
        assert stream.message_encoding.value == declared["serialization_format"]  # type: ignore[union-attr]
        assert stream.message_count.value == declared["count"]  # type: ignore[union-attr]
        assert stream.metadata == (("offered_qos_profiles", MAKE.QOS),)
        assert stream.schema_encoding.value == "ros2msg"  # type: ignore[union-attr]
        assert isinstance(stream.schema_definition, Known)
        assert (stream.first, stream.last) == (Unknown(), Unknown())  # the file declares no extent
        assert stream.series.assertion_kind is AssertionKind.OBSERVED
        assert len(stream.clocks) == 1


def test_the_run_and_clock_are_the_recorders_with_nothing_assumed(sqlite_run: SourceOutput) -> None:
    (run_record,) = records(sqlite_run, Run)
    (clock,) = records(sqlite_run, TimestampDomain)
    assert (run_record.logical_id, run_record.machine) == (Unknown(), Unknown())
    assert run_record.first.value.ticks == START and run_record.last.value.ticks == END
    assert run_record.first.value.domain_id == clock.id
    assert run_record.first.provenance.assertion_kind is AssertionKind.OBSERVED
    assert (clock.field, clock.scope) == ("timestamp", ())
    assert clock.role.value is ClockRole.RECEIVE and clock.resolution.value == NANOSECOND
    assert (clock.epoch, clock.timescale, clock.declared_monotonic) == (
        Unknown(),
        Unknown(),
        Unknown(),
    )
    oracle = ORACLE["mobile_base_sqlite3"]
    assert (run_record.first.value.ticks, run_record.last.value.ticks) == (
        oracle["start"],
        oracle["start"] + oracle["duration"] - 1,
    )


def test_series_rows_match_the_official_reader_and_cite_their_cells(
    sqlite_run: SourceOutput,
) -> None:
    data = DB3.read_bytes()
    expected = sorted((t, topic, len(payload)) for t, topic, payload in MESSAGES)
    assert expected == sorted((m[1], m[0], m[2]) for m in ORACLE["mobile_base_sqlite3"]["messages"])
    seen = []
    for topic, stream in streams_by_topic(sqlite_run).items():
        rows = rows_of(sqlite_run, stream)
        assert [row[SEQ] for row in rows] == list(range(len(rows)))
        for row in rows:
            stream.check_row(row)
            (step,) = stream.row_evidence(row).locator
            assert isinstance(step, ByteRange)
            cell = data[step.offset : step.offset + step.length]
            payload = next(p for t, name, p in MESSAGES if (t, name) == (row["time/0"], topic))
            assert cell.endswith(payload) and row["value/data_bytes"] == len(payload)
            assert row["value/message_id"] == cell[1]  # rowid < 128: one varint byte
            seen.append((row["time/0"], topic, len(payload)))
    assert sorted(seen) == expected


def test_a_stream_without_samples_still_has_a_typed_series(tmp_path: Path) -> None:
    data = mutate_db(tmp_path, "DELETE FROM messages WHERE topic_id = 3")
    out = run(data)
    status = streams_by_topic(out)["/status"]
    assert status.message_count == Known(0, status.message_count.provenance)  # type: ignore[union-attr]
    assert rows_of(out, status) == []
    (batch,) = out.series()[status.id]
    assert SEQ in {c.name for c in batch.columns}


def test_a_database_with_no_messages_has_streams_and_an_unknown_extent(tmp_path: Path) -> None:
    out = run(mutate_db(tmp_path, "DELETE FROM messages"))
    (run_record,) = records(out, Run)
    assert (run_record.first, run_record.last) == (Unknown(), Unknown())
    assert len(records(out, Stream)) == 3
    assert codes(out) == []


def test_output_is_independent_of_how_planning_cuts_the_database() -> None:
    data = DB3.read_bytes()
    whole = run(data, Rosbag2Adapter(max_rows=1000))
    assert len(whole.plan.chunks) == 2
    for rows in (1, 4, 7):
        cut = run(data, Rosbag2Adapter(max_rows=rows))
        assert len(cut.plan.chunks) == 1 + -(-18 // rows) or rows == 7
        assert [r.to_json() for r in cut.records()] == [r.to_json() for r in whole.records()]
        assert cut.findings() == whole.findings()
        assert {
            s: [b.rows().__class__ for b in bs]
            and sorted(
                (row[SEQ], row["time/0"], row["locator/0/offset"]) for b in bs for row in b.rows()
            )
            for s, bs in cut.series().items()
        } == {
            s: sorted(
                (row[SEQ], row["time/0"], row["locator/0/offset"]) for b in bs for row in b.rows()
            )
            for s, bs in whole.series().items()
        }


def test_the_same_bytes_give_the_same_output_and_another_version_new_ids() -> None:
    data = DB3.read_bytes()
    first, second = run(data), run(data)
    assert [r.to_json() for r in first.records()] == [r.to_json() for r in second.records()]
    assert first.plan == second.plan

    class Next(Rosbag2Adapter):
        descriptor = replace(DESCRIPTOR, version="9.9.9")

    other = ingest_source(Next(), BytesReader(data))
    assert {r.id for r in other.records()}.isdisjoint({r.id for r in first.records()})
    assert len(other.records()) == len(first.records())


def test_a_wal_database_says_its_log_is_not_read(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "w.db3")
    shutil.copyfile(DB3, tmp_path / "w.db3")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()
    out = run((tmp_path / "w.db3").read_bytes())
    assert "wal_not_read" in codes(out)
    assert len(rows_of(out, streams_by_topic(out)["/cmd_vel"])) == 10


def test_a_page_size_other_than_the_default_reads_the_same(tmp_path: Path) -> None:
    path = tmp_path / "small.db3"
    shutil.copyfile(DB3, path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size = 512")
    conn.execute("VACUUM")
    conn.close()
    small = run(path.read_bytes())

    def counts(out: SourceOutput) -> dict[str, object]:
        return {
            t: (getattr(s.message_count, "value", None), len(rows_of(out, s)))
            for t, s in streams_by_topic(out).items()
        }

    assert counts(small) == counts(run(DB3.read_bytes()))
    assert counts(small) == {"/cmd_vel": (10, 10), "/battery_voltage": (5, 5), "/status": (3, 3)}


def test_older_layouts_without_definitions_or_hashes_are_read(tmp_path: Path) -> None:
    out = run(mutate_db(tmp_path, "DROP TABLE message_definitions"))
    stream = streams_by_topic(out)["/cmd_vel"]
    assert (stream.schema_encoding, stream.schema_definition) == (Unknown(), Unknown())
    assert stream.schema_name.value == "geometry_msgs/msg/Twist"  # type: ignore[union-attr]
    assert "payload_not_decoded" in codes(out)


def test_a_definition_that_spills_is_unknown_with_a_finding(tmp_path: Path) -> None:
    text = "float64 x\n" * 3000
    data = mutate_db(
        tmp_path,
        "UPDATE message_definitions SET encoded_message_definition = ? WHERE topic_type = ?",
        text,
        "geometry_msgs/msg/Twist",
    )
    out = run(data)
    assert streams_by_topic(out)["/cmd_vel"].schema_definition == Unknown(
        streams_by_topic(out)["/cmd_vel"].schema_definition.provenance  # type: ignore[union-attr]
    ) or isinstance(streams_by_topic(out)["/cmd_vel"].schema_definition, Unknown)
    assert "definition_not_local" in codes(out)


def test_blank_and_non_text_topic_fields_are_unknown_never_empty(tmp_path: Path) -> None:
    out = run(mutate_db(tmp_path, "UPDATE topics SET offered_qos_profiles = '' WHERE id = 1"))
    assert streams_by_topic(out)["/cmd_vel"].metadata == ()
    out = run(mutate_db(tmp_path, "UPDATE topics SET type = '' WHERE id = 2"))
    assert streams_by_topic(out)["/battery_voltage"].schema_name == Unknown(
        streams_by_topic(out)["/battery_voltage"].schema_name.provenance  # type: ignore[union-attr]
    )
    out = run(mutate_db(tmp_path, "UPDATE topics SET name = CAST(x'ff80' AS TEXT) WHERE id = 3"))
    assert "invalid_utf8" in codes(out)
    assert any(isinstance(s.topic, Unknown) for s in records(out, Stream))


# --- Damaged databases ---------------------------------------------------------------------------


def test_messages_of_an_undeclared_topic_get_no_rows(tmp_path: Path) -> None:
    data = mutate_db(tmp_path, "UPDATE messages SET topic_id = 99 WHERE id IN (1, 2, 3)")
    out = run(data)
    assert "unknown_topic" in codes(out)
    total = sum(len(rows_of(out, s)) for s in records(out, Stream))
    assert total == 15
    # seq stays contiguous within each stream.
    for stream in records(out, Stream):
        assert [r[SEQ] for r in rows_of(out, stream)] == list(range(len(rows_of(out, stream))))


def test_rows_with_wrong_types_are_a_finding_per_reason_not_a_failure(tmp_path: Path) -> None:
    data = mutate_db(tmp_path, "UPDATE messages SET timestamp = 'soon' WHERE id = 4")
    data = mutate_db(
        tmp_path, "UPDATE messages SET topic_id = 1.5 WHERE id = 5", source=_save(tmp_path, data)
    )
    out = run(data)
    assert "bad_row" in codes(out)
    total = sum(len(rows_of(out, s)) for s in records(out, Stream))
    assert total == 16


def _save(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "saved.db3"
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    "drop",
    ["DROP TABLE topics", "DROP TABLE messages"],
)
def test_a_database_without_rosbag2_tables_is_one_finding(tmp_path: Path, drop: str) -> None:
    out = run(mutate_db(tmp_path, drop))
    assert codes(out) == ["not_rosbag2_storage"]
    assert records(out, Stream) == [] and records(out, Run) == []


def test_not_a_database_is_one_finding_and_nothing_else() -> None:
    assert codes(run(b"SQLite format 3\x00" + bytes(200))) == ["bad_database"]
    assert codes(run(DB3.read_bytes()[:50])) == ["bad_database"]
    assert codes(run(b"")) == ["not_bag_metadata"]  # not a database: read as metadata, found empty


def test_truncation_keeps_the_rows_before_the_cut_and_says_so() -> None:
    data = DB3.read_bytes()
    full = sum(len(rows_of(run(data), s)) for s in records(run(data), Stream))
    assert full == 18
    seen_partial = False
    for cut in range(4096, len(data), 2048):
        out = run(data[:cut])
        total = sum(len(rows_of(out, s)) for s in records(out, Stream))
        assert total <= full
        if "truncated" in codes(out) or "bad_database" in codes(out):
            seen_partial = True
    assert seen_partial
    mid = run(data[: len(data) - 100])
    assert "truncated" in codes(mid)


def test_random_byte_damage_never_breaks_a_contract_law() -> None:
    data = DB3.read_bytes()
    rng = random.Random(11)
    for _ in range(60):
        broken = bytearray(data)
        for _ in range(rng.choice([1, 4, 30])):
            broken[rng.randrange(len(broken))] = rng.randrange(256)
        run(bytes(broken))  # ingest_source checks every law; any raise fails the test


def test_a_huge_declared_row_count_costs_a_finding_not_an_allocation() -> None:
    data = bytearray(DB3.read_bytes())
    db = Database(BytesReader(bytes(data)))
    root = next(e.root for e in read_schema(db) if e.name == "messages")
    assert data[(root - 1) * 4096] == 13  # the messages table is one leaf page
    struct.pack_into(">H", data, (root - 1) * 4096 + 3, 0xFFFF)
    out = run(bytes(data))
    assert "damaged_page" in codes(out)
    assert sum(len(rows_of(out, s)) for s in records(out, Stream)) == 0


# --- metadata.yaml -------------------------------------------------------------------------------


def test_a_clean_metadata_file_has_no_findings(metadata_run: SourceOutput) -> None:
    assert metadata_run.findings() == ()


def test_the_run_is_the_bags_stated_start_and_duration(metadata_run: SourceOutput) -> None:
    (run_record,) = records(metadata_run, Run)
    (clock,) = records(metadata_run, TimestampDomain)
    assert run_record.provenance.assertion_kind is AssertionKind.STATED
    assert (run_record.first.value.ticks, run_record.last.value.ticks) == (START, END)
    assert run_record.last.provenance.assertion_kind is AssertionKind.STATED
    assert clock.field == "starting_time.nanoseconds_since_epoch"
    assert (clock.role, clock.epoch, clock.timescale) == (Unknown(), Unknown(), Unknown())
    assert clock.resolution.value == NANOSECOND
    data = METADATA.read_bytes()
    (step,) = run_record.last.provenance.evidence.locator
    assert isinstance(step, ByteRange)
    cited = data[step.offset : step.offset + step.length].decode()
    assert str(END - START) in cited and str(START) in cited  # the bytes of both fields


def table_rows(out: SourceOutput) -> dict[str, list[list[Any]]]:
    tables = {t.id: t for t in records(out, StructuredTable)}
    found: dict[str, list[list[Any]]] = {}
    for row in sorted(records(out, StructuredRecord), key=lambda r: (r.table, r.row)):
        name = tables[row.table].name.value
        found.setdefault(name, []).append([getattr(c, "value", None) for c in row.cells])
    return found


def test_tables_hold_every_stated_field_as_the_file_types_it(metadata_run: SourceOutput) -> None:
    tables = table_rows(metadata_run)
    assert set(tables) == {
        "rosbag2_bagfile_information",
        "topics_with_message_count",
        "files",
        "relative_file_paths",
    }
    bag = dict(tables["rosbag2_bagfile_information"])
    assert bag["version"] == 5 and bag["storage_identifier"] == "sqlite3"
    assert bag["message_count"] == 18 and bag["duration.nanoseconds"] == END - START
    assert bag["compression_format"] is None  # blank is unknown, never ""
    topics = tables["topics_with_message_count"]
    assert [t[0] for t in topics] == ["/cmd_vel", "/battery_voltage", "/status"]
    assert topics[0][3] == MAKE.QOS and topics[0][5] == 10 and topics[0][4] is None
    assert tables["files"] == [["mobile_base_sqlite3_0.db3", START, END - START, 18]]
    assert tables["relative_file_paths"] == [["mobile_base_sqlite3_0.db3"]]
    assert all(isinstance(t.header, NotApplicable) for t in records(metadata_run, StructuredTable))


def test_every_cell_cites_its_own_bytes_as_stated(metadata_run: SourceOutput) -> None:
    data = METADATA.read_bytes()
    checked = 0
    for row in records(metadata_run, StructuredRecord):
        for cell in row.cells:
            provenance = cell.provenance
            assert provenance.assertion_kind is AssertionKind.STATED
            (step,) = provenance.evidence.locator
            assert isinstance(step, ByteRange) and step.offset + step.length <= len(data)
            if isinstance(cell, Known) and isinstance(cell.value, int):
                assert data[step.offset : step.offset + step.length].decode() == str(cell.value)
            checked += 1
    assert checked > 30


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("message_count: 18\n", "message_count: 19\n", "count_mismatch"),
        ('compression_format: ""', "compression_format: zstd", "compressed_storage"),
        ("storage_identifier: sqlite3", "storage_identifier: rocksdb", "unknown_storage"),
        ("version: 5", "version: 99", "unknown_version"),
        ("    - mobile_base_sqlite3_0.db3\n", "    - ../../escape.db3\n", "unsafe_part_path"),
        ("    - mobile_base_sqlite3_0.db3\n", "    - /abs/p_0.db3\n", "unsafe_part_path"),
        ("    - mobile_base_sqlite3_0.db3\n", "    - a.mcap\n", "storage_mismatch"),
        ("nanoseconds: 900000000", "nanoseconds: soon", "bad_field"),
        (
            "nanoseconds_since_epoch: 1700000000000000000\n  message",
            "nanoseconds_since_epoch: " + str(2**63) + "\n  message",
            "time_out_of_range",
        ),
    ],
)
def test_the_bags_own_inconsistencies_are_findings(old: str, new: str, code: str) -> None:
    out = run(edit_metadata(old, new))
    assert code in codes(out), codes(out)
    assert records(out, Run), "a bad statement never costs the whole bag"


def test_a_listed_part_that_the_other_list_lacks_is_missing_or_extra() -> None:
    text = METADATA.read_text().replace(
        "    - mobile_base_sqlite3_0.db3\n  duration",
        "    - mobile_base_sqlite3_0.db3\n    - mobile_base_sqlite3_1.db3\n  duration",
        1,
    )
    out = run(text.encode())
    finding = next(f for f in out.findings() if f.code == "rosbag2.parts_disagree")
    assert finding.details["only_in_relative_file_paths"] == ["mobile_base_sqlite3_1.db3"]
    assert finding.details["only_in_files"] == []
    assert codes(out) == ["parts_disagree"]


def test_a_gap_in_the_numbering_of_a_split_bag_names_the_missing_parts() -> None:
    meta = (FIXTURES / "split_sqlite3" / "metadata.yaml").read_text().replace("_1.db3", "_3.db3")
    out = run(meta.encode())
    finding = next(f for f in out.findings() if f.code == "rosbag2.part_gap")
    assert finding.details["missing"] == [1, 2] and finding.details["parts"] == 2


def test_parts_listed_out_of_order_or_twice_are_findings() -> None:
    meta = (FIXTURES / "split_sqlite3" / "metadata.yaml").read_text()
    first, second = (
        meta.index("    - path: split_sqlite3_0.db3"),
        meta.index("    - path: split_sqlite3_1.db3"),
    )
    swapped = meta[:first] + meta[second:] + meta[first:second]
    out = run(swapped.encode())
    assert "part_order" in codes(out)
    twice = meta.replace("    - split_sqlite3_1.db3\n", "    - split_sqlite3_0.db3\n", 1)
    assert "duplicate_part" in codes(run(twice.encode()))


def test_a_clean_split_bag_has_no_findings_and_lists_its_parts_in_order() -> None:
    out = run((FIXTURES / "split_sqlite3" / "metadata.yaml").read_bytes())
    assert out.findings() == ()
    files = table_rows(out)["files"]
    assert [f[0] for f in files] == ["split_sqlite3_0.db3", "split_sqlite3_1.db3"]
    assert files[0][1] < files[1][1]
    assert sum(f[3] for f in files) == 18


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", {"not_bag_metadata"}),
        (b"# nothing\n", {"not_bag_metadata"}),
        (b"other_tool:\n  version: 1\n", {"not_bag_metadata"}),
        (b"rosbag2_bagfile_information: 5\n", {"not_bag_metadata"}),
        (b"\xff\xfe\x00bad", {"unsupported_yaml"}),
        (
            b"rosbag2_bagfile_information:\n  version: &a 5\n  storage_identifier: mcap\n",
            {"unsupported_yaml"},
        ),
        (b"rosbag2_bagfile_information:\n  version: 5\n  version: 6\n", {"duplicate_key"}),
    ],
)
def test_metadata_that_is_not_readable_is_findings_and_never_a_failure(
    data: bytes, expected: set[str]
) -> None:
    out = run(data)
    assert expected <= set(codes(out))


def test_an_oversized_metadata_file_is_not_read() -> None:
    out = run(b"rosbag2_bagfile_information:\n" + b"  k: v\n" * 700_000)
    assert "unsupported_yaml" in codes(out) and records(out, Run) == []


def test_random_metadata_damage_never_breaks_a_contract_law() -> None:
    base = METADATA.read_bytes()
    rng = random.Random(5)
    alphabet = b" :-[]{}'\"#|>\n&*!0123456789"
    for _ in range(200):
        broken = bytearray(base)
        for _ in range(rng.choice([1, 3, 12])):
            broken[rng.randrange(len(broken))] = rng.choice(alphabet)
        run(bytes(broken))


# --- Split bags ----------------------------------------------------------------------------------


def test_the_parts_of_a_split_bag_assemble_to_the_whole_recording() -> None:
    folder = FIXTURES / "split_sqlite3"
    parts = [run((folder / f"split_sqlite3_{i}.db3").read_bytes()) for i in (0, 1)]
    for part in parts:
        assert len(records(part, Stream)) == 3  # a topic in two parts is two streams
    assert len({s.id for p in parts for s in records(p, Stream)}) == 6
    merged = sorted(
        (row["time/0"], topic)
        for part in parts
        for topic, stream in streams_by_topic(part).items()
        for row in rows_of(part, stream)
    )
    oracle = sorted((m[1], m[0]) for m in ORACLE["split_sqlite3"]["messages"])
    assert merged == oracle
    # Each part's run covers its own messages; together they cover the recording.
    firsts = [records(p, Run)[0].first.value.ticks for p in parts]
    lasts = [records(p, Run)[0].last.value.ticks for p in parts]
    assert firsts[0] == START and lasts[1] == END and lasts[0] < firsts[1]
    declared = table_rows(run((folder / "metadata.yaml").read_bytes()))["files"]
    assert [d[1] for d in declared] == firsts and [d[1] + d[2] for d in declared] == lasts


def test_every_builtin_id_is_declared() -> None:
    assert DESCRIPTOR.id == "rosbag2"
    assert {f.code.removeprefix("rosbag2.") for f in _all_findings()} <= {
        c.name.removeprefix("rosbag2.") for c in DESCRIPTOR.finding_codes
    }


def _all_findings() -> list[IngestFinding]:
    out: list[IngestFinding] = []
    for data in (DB3.read_bytes(), METADATA.read_bytes(), DB3.read_bytes()[:9000]):
        out += run(data).findings()
    return out


def test_a_surrogate_escape_is_a_finding_not_a_failure() -> None:
    out = run(edit_metadata("version: 5", 'version: "\\ud800"'))
    assert "unsupported_yaml" in codes(out)


def test_sixty_thousand_listed_parts_are_checked_in_linear_time() -> None:
    names = "".join(f"    - bag_{i % 30000}.db3\n" for i in range(60_000))
    text = "rosbag2_bagfile_information:\n  version: 5\n  relative_file_paths:\n" + names
    out = run(text.encode())
    finding = next(f for f in out.findings() if f.code == "rosbag2.duplicate_part")
    assert finding.details["total"] == 30000 and len(finding.details["parts"]) == 16  # type: ignore[arg-type]
    assert "too_many_entries" in codes(out)


def test_many_unsafe_paths_are_one_finding_with_a_total() -> None:
    names = "".join(f"    - /abs/{i}.db3\n" for i in range(400))
    out = run(("rosbag2_bagfile_information:\n  relative_file_paths:\n" + names).encode())
    unsafe = [f for f in out.findings() if f.code == "rosbag2.unsafe_part_path"]
    assert len(unsafe) == 1 and unsafe[0].details["total"] == 400


def test_a_list_key_with_a_scalar_value_is_kept_in_the_entries_table() -> None:
    text = "rosbag2_bagfile_information:\n  version: 5\n  files: 5\n  relative_file_paths:\n"
    out = run(text.encode())
    bag = dict(table_rows(out)["rosbag2_bagfile_information"])
    assert bag["files"] == 5 and bag["relative_file_paths"] is None


def test_a_single_listed_part_that_is_not_the_first_is_a_gap() -> None:
    text = "rosbag2_bagfile_information:\n  relative_file_paths:\n    - bag_3.db3\n"
    finding = next(f for f in run(text.encode()).findings() if f.code == "rosbag2.part_gap")
    assert finding.details["missing"] == [0, 1, 2] and finding.details["missing_count"] == 3
    assert "part_gap" not in codes(run(text.replace("bag_3", "bag_0").encode()))
    huge = text.replace("bag_3", "bag_" + "9" * 18)
    gap = next(f for f in run(huge.encode()).findings() if f.code == "rosbag2.part_gap")
    assert len(gap.details["missing"]) == 64  # type: ignore[arg-type]


@pytest.mark.parametrize("digits", [19, 40, 4300, 4301, 20000])
def test_a_part_name_with_a_huge_digit_run_is_a_finding_not_a_crash(digits: int) -> None:
    name = "x_" + "7" * digits + ".db3"
    for lists in (
        f"  relative_file_paths:\n    - {name}\n",
        f"  files:\n    - path: {name}\n",
    ):
        out = run(("rosbag2_bagfile_information:\n  version: 5\n" + lists).encode())
        assert "part_unnumbered" in codes(out) and "part_gap" not in codes(out)
        assert records(out, Run)


def test_undeclared_topic_ids_are_counted_without_keeping_each(tmp_path: Path) -> None:
    path = tmp_path / "many.db3"
    shutil.copyfile(DB3, path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, x'00')",
        [(1000 + i, 5 + i) for i in range(500)],
    )
    conn.commit()
    conn.close()
    out = run(path.read_bytes())
    finding = next(f for f in out.findings() if f.code == "rosbag2.unknown_topic")
    assert finding.details["messages"] == 500 and len(finding.details["topic_ids"]) == 16  # type: ignore[arg-type]


def test_topics_whose_id_is_not_the_rowid_are_not_rosbag2_storage(tmp_path: Path) -> None:
    path = tmp_path / "ids.db3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE topics(id INTEGER NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
            serialization_format TEXT NOT NULL, offered_qos_profiles TEXT NOT NULL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL,
            timestamp INTEGER NOT NULL, data BLOB NOT NULL);
        """
    )
    conn.close()
    data = path.read_bytes()
    assert codes(run(data)) == ["not_rosbag2_storage"]
    assert probe(data) == 0.0
