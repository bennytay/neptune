"""The flight-log adapter on PX4 ULog: the fixtures against the official ``pyulog`` reader,
citations, determinism, chunking independence, probing, and hostile logs.

``tests/fixtures/ulog/oracle.json`` is what ``pyulog`` reads from each fixture (regenerate it with
``make_ulog_fixtures.py --oracle``). Every case runs through ``ingest_source``, so every contract
law is checked on the output too: exact citations, one chunk per finding, typed series.
"""

import importlib.util
import json
import random
import struct
import sys
import tracemalloc
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import ProbeHints, configure
from neptune.adapters.flightlog import DESCRIPTOR, FlightLogAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import ByteRange
from neptune.model.run import Run, Stream
from neptune.model.series import SEQ
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "ulog"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_ulog_fixtures")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
NAMES: Final = sorted(ORACLE)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, chunk_bytes: int = 8 << 20, max_rows: int = 100_000) -> SourceOutput:
    return ingest_source(FlightLogAdapter(chunk_bytes, max_rows), BytesReader(data))


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code.removeprefix("flightlog.") for f in output.findings())


def finding(output: SourceOutput, code: str) -> Any:
    (found,) = [f for f in output.findings() if f.code == f"flightlog.{code}"]
    return found


def streams(output: SourceOutput) -> dict[str, Stream]:
    return {r.id: r for r in output.records() if isinstance(r, Stream)}


def key_of(stream: Stream) -> str:
    meta = dict(stream.metadata)
    if "message_type" in meta:
        return meta["message_type"]
    return f"{stream.topic.value}:{meta['multi_id']}"  # type: ignore[union-attr]


def rows_of(output: SourceOutput) -> dict[str, list[dict[str, Any]]]:
    """Every stream's rows in ``seq`` order, by ``topic:multi_id`` (or pseudo-stream name)."""
    by_id = streams(output)
    found: dict[str, list[dict[str, Any]]] = {}
    for stream_id, batches in output.series().items():
        rows = [row for batch in batches for row in batch.rows()]
        found[key_of(by_id[stream_id])] = sorted(rows, key=lambda row: row[SEQ])
    return found


def tables_of(output: SourceOutput) -> dict[str, list[tuple[Any, ...]]]:
    """Each table's rows as tuples of known cell values (``None`` where a cell is not known)."""
    names = {r.id: r.name.value for r in output.records() if isinstance(r, StructuredTable)}  # type: ignore[union-attr]
    found: dict[str, list[tuple[Any, ...]]] = {}
    rows = [r for r in output.records() if isinstance(r, StructuredRecord)]
    for record in sorted(rows, key=lambda r: (names[r.table], r.row)):
        cells = tuple(c.value if isinstance(c, Known) else None for c in record.cells)
        found.setdefault(names[record.table], []).append(cells)
    return found


def as_bytes(output: SourceOutput) -> bytes:
    lines = [canonical_json.dumps(record.to_json()) for record in output.package_records()]
    series = []
    for stream, batches in output.series().items():
        found = sorted((row for batch in batches for row in batch.rows()), key=lambda r: r[SEQ])  # type: ignore[arg-type,return-value]
        series.append(f"{stream} {[sorted(row.items()) for row in found]!r}".encode())
        series.append(repr(sorted({batch.schema() for batch in batches})).encode())
    return b"\n".join(sorted(lines) + series)


# --- The fixtures against pyulog ---------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_topic_reads_the_values_the_official_reader_reads(name: str) -> None:
    output = run(fixture(name))
    rows = rows_of(output)
    expected = ORACLE[name]["topics"]
    assert {k for k in rows if ":" in k} == set(expected)
    for topic, fields in expected.items():
        mine = rows[topic]
        assert [r["time/0"] for r in mine] == fields["timestamp"], topic
        for column, values in fields.items():
            if column == "timestamp" or column.startswith("source["):
                continue
            base = column.split("[")[0] if "." not in column and column.endswith("]") else None
            if base is not None:  # an array element: `q[2]` is element 2 of the repeated `q`
                index = int(column[len(base) + 1 : -1])
                assert [r[f"value/{base}"][index] for r in mine] == values, (topic, column)
            else:
                assert [r[f"value/{column}"] for r in mine] == values, (topic, column)


@pytest.mark.parametrize("name", NAMES)
def test_info_parameters_logs_and_dropouts_match_the_official_reader(name: str) -> None:
    output = run(fixture(name))
    oracle = ORACLE[name]
    tables = tables_of(output)
    info = {row[0]: row[2] for row in tables["info"]}
    assert info == oracle["info"]
    assert {row[0]: row[2] for row in tables["parameters"]} == oracle["initial_parameters"]
    changed = [(row[0], row[2]) for row in tables.get("parameter_changes", [])]
    assert changed == [(key, value) for _, key, value in oracle["changed_parameters"]]
    logged = rows_of(output)["logged_message"]
    plain = [r for r in logged if r["state/value/tag"] == "not_applicable"]
    assert [(r["time/0"], r["value/level"], r["value/message"]) for r in plain] == [
        tuple(entry) for entry in oracle["logged"]
    ]
    drops = rows_of(output).get("dropout", [])
    assert [r["value/duration"] for r in drops] == [d for _, d in oracle["dropouts"]]
    assert all(r["state/time/0"] == "not_covered" for r in drops)


def test_the_run_the_clock_and_the_machine_are_what_the_header_and_info_state() -> None:
    output = run(fixture("copter.ulg"))
    (run_record,) = [r for r in output.records() if isinstance(r, Run)]
    (clock,) = [r for r in output.records() if r.kind == "timestamp_domain"]
    assert run_record.first.value.ticks == ORACLE["copter.ulg"]["start_timestamp"]  # type: ignore[union-attr]
    assert run_record.first.value.domain_id == clock.id  # type: ignore[union-attr]
    assert run_record.first.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
    assert isinstance(run_record.last, Unknown)
    machine = run_record.machine
    assert isinstance(machine, Known)
    assert (machine.value.namespace, machine.value.value) == ("px4.sys_uuid", "0123456789abcdef")
    assert clock.field == "timestamp" and clock.scope == ()
    assert clock.resolution.value.denominator == 10**6
    assert clock.epoch.value == "boot" and clock.timescale.value == "monotonic"
    assert clock.role.value == "sample"


def test_the_rover_log_is_a_ground_vehicle_with_no_aerial_topic() -> None:
    output = run(fixture("rover.ulg"))
    topics = {s.topic.value for s in streams(output).values() if isinstance(s.topic, Known)}
    assert topics == {
        "rover_throttle_setpoint",
        "rover_steering_setpoint",
        "wheel_encoders",
        "rover_rate_status",
    }
    assert not any("attitude" in t or "gps" in t for t in topics)
    (run_record,) = [r for r in output.records() if isinstance(r, Run)]
    assert run_record.machine.value.value == "rover-0123456789"  # type: ignore[union-attr]


def test_definitions_are_kept_with_the_stream_and_the_nested_formats() -> None:
    output = run(fixture("copter.ulg"))
    data = fixture("copter.ulg")
    by_key = {key_of(s): s for s in streams(output).values()}
    esc = by_key["esc_status:0"]
    meta = dict(esc.metadata)
    assert meta["format.esc_report"].startswith("esc_report:uint64_t timestamp;")
    assert isinstance(esc.schema_definition, Known)
    (step,) = esc.schema_definition.value.locator
    assert isinstance(step, ByteRange)
    assert data[step.offset : step.offset + step.length].decode() == (
        "esc_status:uint64_t timestamp;uint8_t esc_count;esc_report[2] esc;uint8_t[7] _padding0;"
    )
    gps = [s for s in streams(output).values() if key_of(s).startswith("vehicle_gps_position")]
    assert sorted(dict(s.metadata)["multi_id"] for s in gps) == ["0", "1"]
    assert esc.schema_encoding == Known("ulog_format", esc.schema_encoding.provenance)  # type: ignore[union-attr]


def test_unused_formats_make_no_stream_and_the_float_columns_stay_float32() -> None:
    output = run(fixture("copter.ulg"))
    assert "airspeed:0" not in rows_of(output)
    batches = next(
        b
        for sid, b in output.series().items()
        if key_of(streams(output)[sid]) == "vehicle_attitude:0"
    )
    schema = {name: (str(kind), repeated) for name, kind, repeated in batches[0].schema()}
    assert schema["value/q"] == ("float32", True)
    assert schema["value/rollspeed"] == ("float32", False)
    assert schema["time/0"] == ("int64", False)


def test_flag_bits_and_the_appended_offset_are_preserved_as_a_table() -> None:
    plain = tables_of(run(fixture("copter.ulg")))["flag_bits"]
    appended = tables_of(run(fixture("copter_appended.ulg")))["flag_bits"]
    assert len(plain[0]) == 19 and plain[0][:19] == (0,) * 19
    assert appended[0][8] == 1  # incompat_flags[0], bit 0: data appended
    assert appended[0][16] == len(fixture("copter.ulg"))  # appended_offsets[0]


def test_appended_data_is_read_with_its_own_subscriptions() -> None:
    output = run(fixture("copter_appended.ulg"))
    assert codes(output) == ["dropout"]
    rows = rows_of(output)
    assert [r["value/indicated_airspeed_m_s"] for r in rows["airspeed:0"]] == [12.5]
    assert len(rows["vehicle_attitude:0"]) == 8
    last = tables_of(output)["parameter_changes"][-1]
    assert last[0] == "SYS_AUTOSTART" and last[2] == 4002


# --- Citations ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_row_and_cell_resolves_to_the_bytes_it_was_read_from(name: str) -> None:
    data = fixture(name)
    output = run(data)
    by_id = streams(output)
    kinds = {"logged_message": {ord("L"), ord("C")}, "dropout": {ord("O")}}
    for stream_id, batches in output.series().items():
        stream = by_id[stream_id]
        for batch in batches:
            for row in batch.rows():
                stream.check_row(row)
                (step,) = stream.row_evidence(row).locator
                assert isinstance(step, ByteRange)
                size, kind = struct.unpack_from("<HB", data, step.offset)
                assert step.length == 3 + size
                key = key_of(stream)
                if key in kinds:
                    assert kind in kinds[key]
                else:
                    assert kind == ord("D")
                    (msg_id,) = struct.unpack_from("<H", data, step.offset + 3)
                    assert str(msg_id) == dict(stream.metadata)["msg_id"]
    for record in output.records():
        if isinstance(record, StructuredRecord):
            (step,) = record.provenance.evidence.locator
            assert isinstance(step, ByteRange) and data[step.offset + 2] in b"BIMPQ"
            for cell in record.cells:
                if isinstance(cell, Known):
                    assert cell.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
                    (inner,) = cell.provenance.evidence.locator  # type: ignore[union-attr]
                    assert step.offset <= inner.offset
                    assert inner.offset + inner.length <= step.offset + step.length


def test_a_table_cell_cites_exactly_its_value_bytes() -> None:
    data = fixture("copter.ulg")
    output = run(data)
    rows = [r for r in output.records() if isinstance(r, StructuredRecord)]
    for record in rows:
        name_cell = record.cells[0]
        if isinstance(name_cell, Known) and name_cell.value == "SYS_AUTOSTART":
            (inner,) = name_cell.provenance.evidence.locator  # type: ignore[union-attr]
            assert data[inner.offset : inner.offset + inner.length] == b"SYS_AUTOSTART"
            value = record.cells[2]
            (where,) = value.provenance.evidence.locator  # type: ignore[union-attr]
            assert data[where.offset : where.offset + where.length] == struct.pack("<i", 4001)
            return
    pytest.fail("no SYS_AUTOSTART row")


# --- Determinism, chunking and lineage -----------------------------------------------------------

GRANULARITIES: Final = ((8 << 20, 100_000), (1, 1), (60, 3), (300, 5))


@pytest.mark.parametrize("name", [*NAMES, "copter_truncated.ulg", "empty_after_header.ulg"])
def test_output_is_byte_identical_twice_and_whatever_the_plan_cuts(name: str) -> None:
    data = fixture(name)
    reference = as_bytes(run(data))
    assert as_bytes(run(data)) == reference
    for chunk_bytes, max_rows in GRANULARITIES:
        assert as_bytes(run(data, chunk_bytes, max_rows)) == reference, (chunk_bytes, max_rows)


def test_plan_cuts_the_log_into_pieces_that_start_at_messages() -> None:
    data = fixture("copter.ulg")
    plan = FlightLogAdapter(chunk_bytes=1, max_rows=1).plan(
        BytesReader(data), configure(DESCRIPTOR)
    )
    starts = [c.context["start"] for c in plan.chunks]
    assert starts == sorted(set(starts)) and starts[0] == 16  # type: ignore[type-var]
    for start in starts[1:]:
        size, kind = struct.unpack_from("<HB", data, start)  # type: ignore[arg-type]
        assert kind in b"FIMPQABDLCSO" and start + 3 + size <= len(data)  # type: ignore[operator]


def test_another_adapter_version_is_another_lineage() -> None:
    from dataclasses import replace

    from neptune.adapters.contract import configure as configure_

    base = configure_(DESCRIPTOR).transform.id
    newer = configure_(replace(DESCRIPTOR, version="0.1.1")).transform.id
    assert base != newer


# --- Probe -------------------------------------------------------------------------------------


def probe(data: bytes) -> tuple[float, list[str]]:
    result = FlightLogAdapter().probe(data[: 64 * 1024], ProbeHints("x", len(data)))
    return result.confidence, [r.code for r in result.reasons]


def test_probe_verifies_a_header_and_signs_a_bare_magic_and_ignores_the_name() -> None:
    assert probe(fixture("copter.ulg"))[0] == 1.0
    assert probe(fixture("empty_after_header.ulg"))[0] == 0.9  # header, nothing after
    assert probe(MAKE.MAGIC)[0] == 0.9
    assert probe(b"")[0] == 0.0
    assert probe(b"ULOG" + bytes(40))[0] == 0.0
    assert probe(b"\x89MCAP0\r\n" + bytes(30))[0] == 0.0
    assert probe(fixture("rover.ulg")[:7])[0] == 0.9


# --- Hostile logs ------------------------------------------------------------------------------


def build(*parts: bytes, flags: bytes | None = None, start: int = MAKE.BOOT) -> bytes:
    return (  # type: ignore[no-any-return]
        MAKE.header(1, start) + (flags if flags is not None else MAKE.flag_bits()) + b"".join(parts)
    )


ATT_SUB: Final = (MAKE.fmt(MAKE.UNUSED), MAKE.subscribe(0, 0, "airspeed"))


def airspeed(ts: int) -> bytes:
    return MAKE.data(0, struct.pack("<Qf", ts, 1.5))  # type: ignore[no-any-return]


def test_every_cut_of_a_log_is_a_strict_prefix_of_its_rows_and_never_an_exception() -> None:
    data = fixture("copter.ulg")
    full = rows_of(run(data))
    cuts = [*range(0, 200), *range(200, len(data), 11)]
    for cut in cuts:
        output = run(data[:cut])
        for key, rows in rows_of(output).items():
            assert rows == full[key][: len(rows)], (cut, key)
            assert [r[SEQ] for r in rows] == list(range(len(rows)))
        if 16 < cut < len(data):
            assert {"truncated"} >= {c for c in codes(output) if c in ("truncated",)}


def test_a_log_cut_inside_a_message_says_truncated_once_and_keeps_the_whole_messages() -> None:
    data = fixture("copter.ulg")
    output = run(data[:-5])
    found = finding(output, "truncated")
    assert found.severity is Severity.ERROR
    assert len(rows_of(output)["logged_message"]) == 2  # the last logged message is lost
    assert len(rows_of(run(data))["logged_message"]) == 3


def test_random_byte_flips_never_raise_and_stay_deterministic() -> None:
    data = fixture("copter.ulg")
    generator = random.Random(20)
    for _ in range(150):
        damaged = bytearray(data)
        for _ in range(generator.randint(1, 6)):
            damaged[generator.randrange(len(damaged))] = generator.randrange(256)
        first = run(bytes(damaged))
        assert as_bytes(run(bytes(damaged), 60, 3)) == as_bytes(first)


def test_damage_in_the_data_section_is_skipped_to_the_next_sync_message() -> None:
    data = bytearray(fixture("copter.ulg"))
    start = bytes(data).index(MAKE.sync())  # the first sync message
    data[start + 11 + 2] = 0x01  # the type byte of the message after it: not a letter
    output = run(bytes(data))
    found = finding(output, "corrupt_bytes")
    assert found.severity is Severity.ERROR and found.details["count"] >= 1


def test_unknown_message_ids_are_one_finding_with_a_count_and_no_rows() -> None:
    log = build(*ATT_SUB, MAKE.data(9, bytes(12)), MAKE.data(9, bytes(12)), airspeed(MAKE.BOOT))
    output = run(log)
    found = finding(output, "unknown_message_id")
    assert found.details["count"] == 2 and found.details["msg_id"] == 9
    assert len(rows_of(output)["airspeed:0"]) == 1


def test_data_before_its_subscription_is_unknown_even_though_the_id_is_declared_later() -> None:
    log = build(MAKE.fmt(MAKE.UNUSED), airspeed(MAKE.BOOT), MAKE.subscribe(0, 0, "airspeed"))
    output = run(log)
    assert finding(output, "unknown_message_id").details["count"] == 1
    assert rows_of(output)["airspeed:0"] == []


def test_a_message_shorter_or_longer_than_its_format_is_counted_not_trusted() -> None:
    log = build(
        *ATT_SUB,
        MAKE.data(0, struct.pack("<Q", 1)),  # 8 of 12 bytes
        MAKE.data(0, struct.pack("<Qf", MAKE.BOOT + 1, 2.5) + bytes(9)),  # longer than the format
        airspeed(MAKE.BOOT + 2),
    )
    output = run(log)
    assert [r["value/indicated_airspeed_m_s"] for r in rows_of(output)["airspeed:0"]] == [2.5, 1.5]
    mismatches = [f for f in output.findings() if f.code == "flightlog.size_mismatch"]
    assert sorted(f.severity.value for f in mismatches) == ["error", "warning"]


def test_unknown_message_types_are_skipped_by_size_and_counted() -> None:
    log = build(*ATT_SUB, MAKE.msg("Z", b"future"), MAKE.msg("Z", b"more"), airspeed(MAKE.BOOT))
    output = run(log)
    found = finding(output, "unknown_message_type")
    assert found.details["count"] == 2 and found.severity is Severity.INFO
    assert len(rows_of(output)["airspeed:0"]) == 1


def test_a_bad_sync_message_is_a_warning_and_the_log_goes_on() -> None:
    log = build(*ATT_SUB, MAKE.msg("S", b"notsync!"), airspeed(MAKE.BOOT))
    output = run(log)
    assert finding(output, "bad_sync").severity is Severity.WARNING
    assert len(rows_of(output)["airspeed:0"]) == 1


def test_formats_that_lie_get_no_stream_and_say_why() -> None:
    cases = {
        "cycle": "loop:uint64_t timestamp;loop inner;",
        "missing_type": "gap:uint64_t timestamp;nothere x;",
        "huge_array": "big:uint64_t timestamp;float[65535] x;",
        "no_colon": "nocolonhere",
        "repeat": "dup:uint64_t timestamp;float a;float a;",
    }
    for name, text in cases.items():
        topic = text.split(":")[0]
        log = build(MAKE.fmt(text), MAKE.subscribe(0, 0, topic), MAKE.data(0, bytes(32)))
        output = run(log)
        assert not streams(output) or all(
            key_of(s).startswith("logged") for s in streams(output).values()
        ), name
        assert any(c in codes(output) for c in ("bad_format", "unknown_format")), name


def test_nested_types_that_flatten_past_the_column_limit_are_refused() -> None:
    inner = "leaf:" + "".join(f"uint8_t f{n};" for n in range(60))
    outer = "wide:uint64_t timestamp;leaf[40] items;"
    log = build(MAKE.fmt(inner), MAKE.fmt(outer), MAKE.subscribe(0, 0, "wide"))
    output = run(log)
    assert finding(output, "bad_format").details["format"] == "wide"
    assert not [s for s in streams(output).values() if s.topic == Known("wide")]


def test_a_subscription_to_an_undefined_format_is_an_error_not_a_crash() -> None:
    output = run(build(MAKE.subscribe(0, 0, "ghost"), MAKE.data(0, bytes(16))))
    assert finding(output, "unknown_format").severity is Severity.ERROR
    assert finding(output, "unknown_message_id").details["count"] == 1


def test_a_message_id_subscribed_twice_keeps_the_first() -> None:
    log = build(
        *ATT_SUB,
        MAKE.fmt("other:uint64_t timestamp;"),
        MAKE.subscribe(0, 0, "other"),
        airspeed(MAKE.BOOT),
    )
    output = run(log)
    assert finding(output, "duplicate_subscription").details["msg_id"] == 0
    assert list(rows_of(output)) == ["airspeed:0"]


def test_a_conflicting_format_redefinition_keeps_the_first() -> None:
    log = build(MAKE.fmt(MAKE.UNUSED), MAKE.fmt("airspeed:uint64_t timestamp;"))
    output = run(log)
    assert finding(output, "conflicting_format").details["format"] == "airspeed"


def test_flag_bits_that_are_not_first_and_formats_in_the_data_section_are_misplaced() -> None:
    log = build(*ATT_SUB, MAKE.flag_bits(), MAKE.fmt("late:uint64_t timestamp;"), airspeed(1))
    output = run(log)
    found = [f for f in output.findings() if f.code == "flightlog.misplaced_message"]
    assert sorted(f.details["type"] for f in found) == ["B", "F"]  # type: ignore[type-var]


def test_incompatible_flag_bits_this_adapter_does_not_know_refuse_the_messages() -> None:
    log = build(*ATT_SUB, airspeed(1), flags=MAKE.flag_bits(0x02))
    output = run(log)
    assert codes(output) == ["unknown_flags"]
    assert [r for r in output.records() if isinstance(r, Run)] and not streams(output)


def test_a_newer_file_version_is_read_with_a_warning() -> None:
    log = MAKE.header(2) + MAKE.flag_bits() + b"".join(ATT_SUB) + airspeed(MAKE.BOOT)
    output = run(log)
    assert finding(output, "unknown_version").details["version"] == 2
    assert len(rows_of(output)["airspeed:0"]) == 1


def test_appended_offsets_that_are_not_inside_the_file_are_ignored_with_a_finding() -> None:
    log = build(*ATT_SUB, airspeed(1), flags=MAKE.flag_bits(1, (10**9, 0, 0)))
    output = run(log)
    assert finding(output, "appended_misaligned").details["count"] == 1
    assert len(rows_of(output)["airspeed:0"]) == 1


def test_an_appended_offset_inside_a_message_skips_the_bytes_and_says_so() -> None:
    body = build(*ATT_SUB, airspeed(1))
    offset = len(body) - 5
    log = build(*ATT_SUB, airspeed(1), airspeed(2), flags=MAKE.flag_bits(1, (offset, 0, 0)))
    output = run(log)
    assert any(f.code == "flightlog.appended_misaligned" for f in output.findings())


def test_a_timestamp_past_two_to_the_63_is_unknown_in_its_row_with_a_finding() -> None:
    log = build(*ATT_SUB, MAKE.data(0, struct.pack("<Qf", 2**63 + 3, 1.0)), airspeed(5))
    output = run(log)
    rows = rows_of(output)["airspeed:0"]
    assert [r["state/time/0"] for r in rows] == ["unknown", "known"]
    assert rows[0]["time/0"] is None
    assert finding(output, "time_out_of_range").details["count"] == 1


def test_a_header_timestamp_past_the_signed_range_leaves_the_run_start_unknown() -> None:
    output = run(build(*ATT_SUB, start=2**64 - 1))
    (run_record,) = [r for r in output.records() if isinstance(r, Run)]
    assert isinstance(run_record.first, Unknown)


def test_logged_text_that_is_not_utf8_is_unknown_and_never_replaced() -> None:
    log = build(*ATT_SUB, MAKE.msg("L", struct.pack("<BQ", ord("6"), 5) + b"bad \xff text"))
    output = run(log)
    (row,) = rows_of(output)["logged_message"]
    assert row["value/message"] is None and row["state/value/message"] == "unknown"
    assert finding(output, "invalid_utf8").details["where"] == "logged"


def test_the_rover_text_field_that_is_not_utf8_is_unknown() -> None:
    output = run(fixture("rover.ulg"))
    rows = rows_of(output)["rover_rate_status:0"]
    assert [r["value/source"] for r in rows] == ["gyro_z", None]
    assert [r["state/value/source"] for r in rows] == ["known", "unknown"]


def test_info_values_that_lie_about_their_type_are_unknown_cells() -> None:
    info = MAKE.msg("I", MAKE.key("uint32_t lying", b"\x01\x02"))  # 2 of 4 bytes
    output = run(build(info, MAKE.info("char[3] ok", b"abc")))
    assert finding(output, "unreadable_value").details["reason"] == "length"
    rows = tables_of(output)["info"]
    assert rows[0][:2] == ("lying", "uint32_t") and rows[0][2] is None
    assert rows[1][2] == "abc"


def test_messages_too_short_for_their_type_are_malformed_not_a_crash() -> None:
    log = build(
        MAKE.msg("I", b""),
        MAKE.msg("P", b"\x09abc"),
        MAKE.msg("A", b"\x00"),
        MAKE.msg("O", b"\x01"),
    )
    output = run(log)
    found = [f for f in output.findings() if f.code == "flightlog.malformed_message"]
    assert sorted(f.details["type"] for f in found) == ["A", "I", "O", "P"]  # type: ignore[type-var]


def test_dropouts_are_rows_and_one_warning_with_the_total_milliseconds() -> None:
    log = build(*ATT_SUB, MAKE.dropout(7), MAKE.dropout(30))
    output = run(log)
    found = finding(output, "dropout")
    assert (found.details["count"], found.details["amount"]) == (2, 37)
    assert found.severity is Severity.WARNING
    assert [r["value/duration"] for r in rows_of(output)["dropout"]] == [7, 30]


def test_thousands_of_subscriptions_and_formats_are_bounded_with_one_finding_each() -> None:
    formats = [MAKE.fmt(f"t{n}:uint64_t timestamp;") for n in range(5000)]
    subs = [MAKE.subscribe(0, n, f"t{n}") for n in range(5000)]
    output = run(build(*formats, *subs))
    found = {f.details["key"]: f for f in output.findings() if f.code == "flightlog.limit_exceeded"}
    assert set(found) == {"formats", "streams"}
    assert len(streams(output)) == 4096


def test_a_huge_log_is_read_in_bounded_pieces_and_memory() -> None:
    rows = 150_000
    body = b"".join(airspeed(MAKE.BOOT + n) for n in range(rows))
    data = build(*ATT_SUB, body)
    adapter = FlightLogAdapter()
    config = configure(DESCRIPTOR)
    source = BytesReader(data)
    plan = adapter.plan(source, config)
    assert len(plan.chunks) >= 2  # 100,000 rows at most per chunk
    tracemalloc.start()
    try:
        total = 0
        for chunk in plan.chunks:
            output = adapter.ingest(source, chunk, config)
            total += sum(b.length for b in output.series)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert total == rows
    assert peak < 120 * 1024 * 1024


def test_a_log_of_only_noise_after_a_valid_header_is_findings_not_failure() -> None:
    noise = bytes(random.Random(5).randrange(256) for _ in range(5000))
    output = run(MAKE.header() + noise)
    assert [r for r in output.records() if isinstance(r, Run)]
    assert "corrupt_bytes" in codes(output) or "truncated" in codes(output)


def test_a_source_that_is_not_a_log_says_so_and_reads_nothing() -> None:
    output = run(b"just some text, not a flight log\n")
    assert codes(output) == ["bad_magic"] and not output.records()


def test_thousands_of_distinct_unknown_ids_fold_into_a_bounded_number_of_findings() -> None:
    log = build(*ATT_SUB, *[MAKE.data(100 + n, bytes(12)) for n in range(2000)])
    found = [f for f in run(log).findings() if f.code == "flightlog.unknown_message_id"]
    assert len(found) == 65
    other = next(f for f in found if f.details["key"] == "other")
    assert other.details["count"] == 2000 - 64


def test_inspect_reports_the_header_flags_and_definitions_without_reading_data() -> None:
    result = FlightLogAdapter().inspect(BytesReader(fixture("copter.ulg")), configure(DESCRIPTOR))
    summary = result.summary
    assert summary["version"] == 1 and summary["start_timestamp"] == MAKE.BOOT
    assert summary["formats"] == sorted(
        [
            "airspeed",
            "esc_report",
            "esc_status",
            "vehicle_attitude",
            "vehicle_gps_position",
            "vehicle_status",
        ]
    )
    assert summary["parameters"] == 3 and summary["info_messages"] == 5
    assert result.findings == ()
    bad = FlightLogAdapter().inspect(BytesReader(b"nope"), configure(DESCRIPTOR))
    assert [f.code for f in bad.findings] == ["flightlog.bad_magic"]


def test_a_format_without_a_timestamp_has_rows_whose_time_is_not_covered() -> None:
    log = build(
        MAKE.fmt("plain:uint32_t x;"),
        MAKE.subscribe(0, 0, "plain"),
        MAKE.data(0, struct.pack("<I", 7)),
    )
    output = run(log)
    (row,) = rows_of(output)["plain:0"]
    assert row["state/time/0"] == "not_covered" and row["value/x"] == 7
    assert finding(output, "no_time_field").details["format"] == "plain"


def test_an_unknown_type_followed_by_no_message_is_damage_not_a_skip() -> None:
    bad = struct.pack("<HB", 20, ord("Z")) + b"\x01" * 23 + MAKE.sync()
    output = run(build(*ATT_SUB, airspeed(1), bad, airspeed(2)))
    assert finding(output, "corrupt_bytes").severity is Severity.ERROR
    assert "unknown_message_type" not in codes(output)
    assert len(rows_of(output)["airspeed:0"]) == 2


def test_a_uint64_past_the_signed_range_in_info_is_unknown_with_a_finding() -> None:
    output = run(build(MAKE.info("uint64_t big", struct.pack("<Q", 2**64 - 1))))
    assert finding(output, "unreadable_value").details["reason"] == "range"
    assert tables_of(output)["info"][0][2] is None


def test_formats_that_repeat_padding_cannot_burn_the_cpu_budget() -> None:
    import time

    pad = MAKE.fmt("Pad:uint8_t _padding0;")
    formats = [MAKE.fmt(f"F{n}:uint64_t timestamp;Pad[65535] p;") for n in range(4096)]
    subs = [MAKE.subscribe(0, n, f"F{n}") for n in range(4096)]
    data = build(pad, *formats, *subs)
    started = time.perf_counter()
    plan = FlightLogAdapter().plan(BytesReader(data), configure(DESCRIPTOR))
    assert time.perf_counter() - started < 1.0
    work = [f for f in plan.findings if f.code == "flightlog.limit_exceeded"]
    assert any(f.details["key"] == "layout_work" for f in work)
    assert any(f.code == "flightlog.bad_format" for f in plan.findings)
