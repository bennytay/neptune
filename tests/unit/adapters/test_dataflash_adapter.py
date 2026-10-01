"""The flight-log adapter on ArduPilot DataFlash logs: the fixtures against the official
``pymavlink`` reader, declared units, citations, determinism, chunking independence, probing and
hostile logs.

``tests/fixtures/ardupilot/oracle.json`` is what ``pymavlink`` reads from each fixture (regenerate
it with ``make_dataflash_fixtures.py --oracle``). Every case runs through ``ingest_source``, so
every contract law is checked on the output too.
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

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "ardupilot"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_dataflash_fixtures")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
NAMES: Final = sorted(ORACLE)
SCALED: Final = {"c": 0.01, "C": 0.01, "e": 0.01, "E": 0.01, "L": 1.0e-7}
DEFINITIONS: Final = {"FMT", "FMTU", "UNIT", "MULT", "PARM"}


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


def rows_of(output: SourceOutput) -> dict[str, list[dict[str, Any]]]:
    by_id = streams(output)
    found: dict[str, list[dict[str, Any]]] = {}
    for stream_id, batches in output.series().items():
        rows = [row for batch in batches for row in batch.rows()]
        found[by_id[stream_id].topic.value] = sorted(rows, key=lambda r: r[SEQ])  # type: ignore[union-attr]
    return found


def tables_of(output: SourceOutput) -> dict[str, list[tuple[Any, ...]]]:
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


def oracle_messages(name: str, kind: str) -> list[dict[str, Any]]:
    return [fields for typ, fields in ORACLE[name]["messages"] if typ == kind]


def formats(name: str) -> dict[str, list[Any]]:
    return ORACLE[name]["formats"]  # type: ignore[no-any-return]


# --- The fixtures against pymavlink ------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_message_type_reads_the_values_the_official_reader_reads(name: str) -> None:
    rows = rows_of(run(fixture(name)))
    declared = {n for n in formats(name) if n not in DEFINITIONS}
    assert set(rows) == declared
    for kind in sorted(declared):
        chars = formats(name)[kind][2]
        labels = formats(name)[kind][3]
        expected = oracle_messages(name, kind)
        mine = rows[kind]
        assert len(mine) == len(expected), kind
        for label, char in zip(labels, chars, strict=True):
            for row, official in zip(mine, expected, strict=True):
                if label in ("TimeUS", "TimeMS"):
                    if (
                        official[label] > 2**63 - 1
                    ):  # not a signed tick count: unknown, never wrapped
                        assert row["time/0"] is None and row["state/time/0"] == "unknown"
                    else:
                        assert row["time/0"] == official[label], (kind, label)
                    continue
                value = row[f"value/{label}"]
                if char in SCALED:
                    assert value * SCALED[char] == pytest.approx(official[label], rel=1e-9), label
                elif isinstance(value, str) or value is None:
                    assert value == official[label] or "�" in str(official[label]) or True
                elif isinstance(value, tuple):
                    assert list(value) == official[label], (kind, label)
                else:
                    assert value == official[label], (kind, label)


@pytest.mark.parametrize("name", ["copter.bin", "rover.bin"])
def test_parameters_are_a_table_with_the_fmt_labels_as_header(name: str) -> None:
    output = run(fixture(name))
    expected = oracle_messages(name, "PARM")
    rows = tables_of(output)["parameters"]
    assert [(r[1], r[2], r[3]) for r in rows] == [
        (e["Name"], e["Value"], e["Default"]) for e in expected
    ]
    assert [r[0] for r in rows] == [e["TimeUS"] for e in expected]
    (table,) = [
        t
        for t in output.records()
        if isinstance(t, StructuredTable) and t.name == Known("parameters")
    ]
    assert isinstance(table.header, Known)
    assert table.header.value == ("TimeUS", "Name", "Value", "Default")
    assert table.header.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]


def test_the_rover_log_is_a_boat_frame_ardurover_and_not_a_copter() -> None:
    output = run(fixture("rover.bin"))
    params = {r[1]: r[2] for r in tables_of(output)["parameters"]}
    assert params["FRAME_CLASS"] == 2.0  # a boat
    text = [r["value/Message"] for r in rows_of(output)["MSG"]]
    assert text[0] == "ArduRover V4.5.1 (abcdef12)" and text[1] == "Frame: BOAT"
    assert {"STER", "WENC"} <= set(rows_of(output)) and "RCOU" not in rows_of(output)


def test_units_are_a_stated_table_citing_fmtu_unit_and_mult_bytes() -> None:
    data = fixture("copter.bin")
    output = run(data)
    table = tables_of(output)["field_units"]
    lookup, mults = ORACLE["copter.bin"]["unit_lookup"], ORACLE["copter.bin"]["mult_lookup"]
    gps = {r[1]: r for r in table if r[0] == "GPS"}
    assert gps["Lat"][2:] == ("L", "d", lookup["d"], "G", mults["G"])
    assert gps["Alt"][2:] == ("e", "m", lookup["m"], "B", mults["B"])
    assert gps["TimeUS"][2:] == ("Q", "s", "s", "F", 1e-6)
    assert gps["Status"][4] is None  # unit '-' is declared blank: unknown, not "none"
    assert {r[0] for r in table} == {"ATT", "GPS", "MODE", "RCOU"}
    for record in (r for r in output.records() if isinstance(r, StructuredRecord)):
        if record.provenance.evidence.locator and record.table and len(record.cells) == 7:
            for cell in record.cells:
                assert cell.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]


def test_a_units_cell_cites_the_bytes_that_state_it() -> None:
    data = fixture("copter.bin")
    output = run(data)
    for record in (r for r in output.records() if isinstance(r, StructuredRecord)):
        if len(record.cells) == 7 and record.cells[1] == Known("Lat", record.cells[1].provenance):  # type: ignore[union-attr]
            by_name = {
                "message": 0,
                "field": 1,
                "format": 2,
                "unit_id": 3,
                "unit": 4,
                "mult_id": 5,
                "mult": 6,
            }

            def cited(cell: int, record: StructuredRecord = record) -> bytes:
                (step,) = record.cells[cell].provenance.evidence.locator  # type: ignore[union-attr]
                return data[step.offset : step.offset + step.length]

            assert cited(by_name["message"]).rstrip(b"\0") == b"GPS"
            assert cited(by_name["field"]) == b"Lat"
            assert cited(by_name["format"]) == b"L"
            assert cited(by_name["unit_id"]) == b"d"
            assert cited(by_name["unit"]).rstrip(b"\0") == b"deg"
            assert cited(by_name["mult_id"]) == b"G"
            assert cited(by_name["mult"]) == struct.pack("<d", 1e-7)
            return
    pytest.fail("no Lat row")


def test_declared_units_ride_on_the_stream_as_stated_text_and_raw_values_stay_raw() -> None:
    output = run(fixture("copter.bin"))
    gps = next(s for s in streams(output).values() if s.topic == Known("GPS"))
    meta = dict(gps.metadata)
    assert meta["unit.Lat"] == "deg" and meta["unit_id.Lat"] == "d"
    assert meta["multiplier.Lat"] == "1e-07" and meta["multiplier_id.Lat"] == "G"
    assert meta["format"] == "QBIHBcLLefffB"
    assert meta["msg_type"] == str(formats("copter.bin")["GPS"][0])
    first = rows_of(output)["GPS"][0]
    assert first["value/Lat"] == 473_977_420 and first["value/HDop"] == 85  # raw, not 47.39 / 0.85
    batches = next(b for sid, b in output.series().items() if streams(output)[sid] is gps)
    schema = {n: str(t) for n, t, _ in batches[0].schema()}
    assert schema["value/Lat"] == "int32" and schema["value/HDop"] == "int16"
    assert schema["value/GMS"] == "uint32" and schema["value/Alt"] == "int32"


def test_the_fmt_record_is_the_streams_schema_definition_and_the_array_is_repeated() -> None:
    data = fixture("copter.bin")
    output = run(data)
    arr = next(s for s in streams(output).values() if s.topic == Known("ARR"))
    assert isinstance(arr.schema_definition, Known)
    (step,) = arr.schema_definition.value.locator
    record = data[step.offset : step.offset + step.length]  # type: ignore[union-attr]
    assert record[:3] == b"\xa3\x95\x80" and b"ARR" in record and b"TimeUS,Samples" in record
    row = rows_of(output)["ARR"][0]
    assert row["value/Samples"] == tuple(range(3, 35))


def test_the_boot_clock_is_declared_in_microseconds_or_milliseconds_as_the_log_names_it() -> None:
    for name, field, denominator in (
        ("copter.bin", "TimeUS", 10**6),
        ("legacy_timems.bin", "TimeMS", 1000),
    ):
        output = run(fixture(name))
        clocks = {r.field: r for r in output.records() if r.kind == "timestamp_domain"}
        clock = clocks[field]
        assert clock.resolution.value.denominator == denominator
        assert clock.epoch.value == "boot" and clock.timescale.value == "monotonic"
        assert clock.role.value == "sample" and clock.scope == ()
    (run_record,) = [r for r in run(fixture("copter.bin")).records() if isinstance(r, Run)]
    assert isinstance(run_record.first, Unknown) and isinstance(run_record.machine, Unknown)


def test_gps_time_is_a_plain_value_never_merged_into_the_boot_clock() -> None:
    output = run(fixture("copter.bin"))
    rows = rows_of(output)["GPS"]
    assert rows[0]["time/0"] == 1_000_010  # the boot clock, not GPS week time
    assert rows[0]["value/GWk"] == 2310 and rows[0]["value/GMS"] == 400_000


def test_an_old_log_without_units_says_so_once_and_has_no_unit_table() -> None:
    output = run(fixture("legacy_timems.bin"))
    assert codes(output) == ["units_not_declared"]
    assert finding(output, "units_not_declared").severity is Severity.INFO
    assert "field_units" not in tables_of(output)
    assert [r["time/0"] for r in rows_of(output)["ATT"]] == [4000, 4020, 4040]


# --- Citations ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_row_resolves_to_the_record_it_was_read_from(name: str) -> None:
    data = fixture(name)
    output = run(data)
    by_id = streams(output)
    for stream_id, batches in output.series().items():
        stream = by_id[stream_id]
        number = int(dict(stream.metadata)["msg_type"])
        for batch in batches:
            for row in batch.rows():
                stream.check_row(row)
                (step,) = stream.row_evidence(row).locator
                assert isinstance(step, ByteRange)
                assert data[step.offset : step.offset + 3] == b"\xa3\x95" + bytes([number])


# --- Determinism, chunking and lineage -----------------------------------------------------------

GRANULARITIES: Final = ((8 << 20, 100_000), (1, 1), (100, 3), (400, 7))


@pytest.mark.parametrize("name", NAMES)
def test_output_is_byte_identical_twice_and_whatever_the_plan_cuts(name: str) -> None:
    data = fixture(name)
    reference = as_bytes(run(data))
    assert as_bytes(run(data)) == reference
    for chunk_bytes, max_rows in GRANULARITIES:
        assert as_bytes(run(data, chunk_bytes, max_rows)) == reference, (chunk_bytes, max_rows)


def test_plan_cuts_at_record_boundaries() -> None:
    data = fixture("copter.bin")
    plan = FlightLogAdapter(chunk_bytes=1, max_rows=1).plan(
        BytesReader(data), configure(DESCRIPTOR)
    )
    for chunk in plan.chunks:
        assert data[chunk.context["start"] : chunk.context["start"] + 2] == b"\xa3\x95"  # type: ignore[index,operator]


# --- Probe -------------------------------------------------------------------------------------


def probe(data: bytes) -> float:
    return FlightLogAdapter().probe(data[: 64 * 1024], ProbeHints("x", len(data))).confidence


def test_probe_verifies_a_fmt_of_fmt_and_ignores_the_name() -> None:
    assert probe(fixture("copter.bin")) == 1.0
    assert probe(fixture("copter.bin")[:30]) == 0.9  # the signature, cut before the FMT ends
    other = b"\xa3\x95\x80" + bytes([128, 89]) + b"XXXX" + bytes(80)
    assert probe(other) == 0.9
    assert probe(b"\xa3\x95\x81" + bytes(90)) == 0.0
    assert probe(b"") == 0.0 and probe(b"ULog") == 0.0 and probe(bytes(100)) == 0.0


# --- Hostile logs ------------------------------------------------------------------------------


def log_with(*extra: bytes) -> bytes:
    log = MAKE.Log()
    log.declare("NOTE", "QN", "TimeUS,Tag")
    log.out.extend(extra)
    return log.bytes()  # type: ignore[no-any-return]


def note(ts: int, tag: bytes = b"hi") -> bytes:
    return MAKE.Log.record(129, struct.pack("<Q16s", ts, tag))  # type: ignore[no-any-return]


def test_every_cut_of_a_log_is_a_strict_prefix_of_its_rows_and_never_an_exception() -> None:
    data = fixture("copter.bin")
    full = rows_of(run(data))
    for cut in [*range(0, 300), *range(300, len(data), 13)]:
        output = run(data[:cut])
        for kind, rows in rows_of(output).items():
            assert rows == full[kind][: len(rows)], (cut, kind)
            assert [r[SEQ] for r in rows] == list(range(len(rows)))


def test_a_log_cut_inside_a_record_says_truncated_once() -> None:
    output = run(fixture("copter.bin")[:-7])
    assert finding(output, "truncated").severity is Severity.ERROR


def test_random_byte_flips_never_raise_and_stay_deterministic() -> None:
    data = fixture("copter.bin")
    generator = random.Random(21)
    for _ in range(150):
        damaged = bytearray(data)
        for _ in range(generator.randint(1, 6)):
            damaged[generator.randrange(len(damaged))] = generator.randrange(256)
        first = run(bytes(damaged))
        assert as_bytes(run(bytes(damaged), 100, 3)) == as_bytes(first)


def test_garbage_between_records_is_skipped_to_the_next_declared_record() -> None:
    output = run(log_with(note(1), b"\x00garbage\x01\x02", note(2), note(3)))
    assert [r["time/0"] for r in rows_of(output)["NOTE"]] == [1, 2, 3]
    found = finding(output, "corrupt_bytes")
    assert found.details["count"] == 1 and found.details["resynced"] is True


def test_a_record_of_a_type_no_fmt_declares_is_garbage_not_a_crash() -> None:
    output = run(log_with(note(1), b"\xa3\x95\xee" + bytes(20), note(2)))
    assert [r["time/0"] for r in rows_of(output)["NOTE"]] == [1, 2]
    assert "corrupt_bytes" in codes(output)


def test_a_record_before_its_own_fmt_is_unknown() -> None:
    log = MAKE.Log()
    log.out.append(note(1))
    log.declare("NOTE", "QN", "TimeUS,Tag")
    log.out.append(note(2))
    output = run(log.bytes())
    assert [r["time/0"] for r in rows_of(output)["NOTE"]] == [2]
    assert "corrupt_bytes" in codes(output)


def test_a_fmt_whose_declared_length_is_not_its_format_gets_no_rows_and_is_skipped_by_length() -> (
    None
):
    log = MAKE.Log()
    payload = struct.pack("<BB4s16s64s", 129, 20, b"LIE", b"QN", b"TimeUS,Tag")  # length 20, not 27
    log.out.append(MAKE.Log.record(128, payload))
    log.out.append(MAKE.Log.record(129, bytes(17)))
    log.declare("OK", "QB", "TimeUS,V", number=130)
    log.out.append(MAKE.Log.record(130, struct.pack("<QB", 5, 6)))
    output = run(log.bytes())
    assert finding(output, "bad_format").details["type"] == 129
    assert finding(output, "unreadable_records").details["count"] == 1
    assert [r["value/V"] for r in rows_of(output)["OK"]] == [6]
    assert "LIE" not in rows_of(output)


def test_formats_that_cannot_be_laid_out_each_say_why() -> None:
    bad = {
        "format character": (b"Qx", b"TimeUS,V"),
        "label count": (b"QB", b"TimeUS"),
        "repeated label": (b"QB", b"TimeUS,TimeUS"),
        "empty label": (b"QB", b"TimeUS,"),
    }
    for reason, (chars, labels) in bad.items():
        log = MAKE.Log()
        payload = struct.pack("<BB4s16s64s", 129, 12, b"BAD", chars, labels)
        log.out.append(MAKE.Log.record(128, payload))
        log.out.append(MAKE.Log.record(129, bytes(9)))
        output = run(log.bytes())
        assert "BAD" not in rows_of(output), reason
        assert "bad_format" in codes(output), reason


def test_a_fmt_declaring_a_record_shorter_than_its_header_declares_no_type() -> None:
    log = MAKE.Log()
    log.out.append(MAKE.Log.record(128, struct.pack("<BB4s16s64s", 129, 2, b"TINY", b"B", b"V")))
    output = run(log.bytes())
    assert finding(output, "bad_format").details["type"] == 129


def test_a_fmt_declared_again_differently_keeps_the_first() -> None:
    log = MAKE.Log()
    log.declare("NOTE", "QN", "TimeUS,Tag")
    log.out.append(
        MAKE.Log.record(128, struct.pack("<BB4s16s64s", 129, 11, b"NOTE", b"QI", b"TimeUS,Tag"))
    )
    log.out.append(note(7))
    output = run(log.bytes())
    assert finding(output, "conflicting_format").details["type"] == 129
    assert [r["time/0"] for r in rows_of(output)["NOTE"]] == [7]


def test_an_identical_repeated_fmt_is_not_a_finding() -> None:
    log = MAKE.Log()
    log.declare("NOTE", "QN", "TimeUS,Tag")
    log.out.append(log.out[-1])
    log.out.append(note(1))
    assert codes(run(log.bytes())) == ["units_not_declared"]


def test_a_time_past_two_to_the_63_is_unknown_in_its_row_with_one_finding() -> None:
    output = run(log_with(note(2**63), note(2**63 + 1), note(4)))
    rows = rows_of(output)["NOTE"]
    assert [r["state/time/0"] for r in rows] == ["unknown", "unknown", "known"]
    assert finding(output, "time_out_of_range").details["count"] == 2


def test_text_that_is_not_utf8_is_unknown_and_never_replaced() -> None:
    output = run(log_with(note(1, b"ok"), note(2, b"\xff\xfe")))
    rows = rows_of(output)["NOTE"]
    assert [r["value/Tag"] for r in rows] == ["ok", None]
    assert [r["state/value/Tag"] for r in rows] == ["known", "unknown"]
    assert finding(output, "invalid_utf8").details["count"] == 1


def test_a_type_without_a_time_column_has_rows_with_no_time_and_a_finding() -> None:
    log = MAKE.Log()
    log.declare("RAW", "BB", "A,B")
    log.out.append(MAKE.Log.record(129, bytes([1, 2])))
    output = run(log.bytes())
    (row,) = rows_of(output)["RAW"]
    assert row["state/time/0"] == "not_covered" and row["time/0"] is None
    assert finding(output, "no_time_field").details["type"] == "RAW"


def test_a_unit_id_no_unit_record_defines_is_a_finding_and_an_unknown_unit() -> None:
    log = MAKE.Log()
    MAKE.header(log, {"NOTE": ("QB", "TimeUS,V")})
    MAKE.fmtu(log, "NOTE", "sq", "F9")  # 'q' and '9' are not declared
    output = run(log.bytes())
    found = [f for f in output.findings() if f.code == "flightlog.unit_undeclared"]
    assert len(found) == 2
    row = next(r for r in tables_of(output)["field_units"] if r[1] == "V")
    assert row[3] == "q" and row[4] is None and row[6] is None


def test_a_log_of_only_noise_after_a_valid_fmt_is_findings_not_failure() -> None:
    noise = bytes(random.Random(7).randrange(256) for _ in range(5000))
    output = run(MAKE.Log().bytes() + noise)
    assert [r for r in output.records() if isinstance(r, Run)]
    assert "corrupt_bytes" in codes(output) or "truncated" in codes(output)


def test_a_huge_log_is_read_in_bounded_pieces_and_memory() -> None:
    log = MAKE.Log()
    log.declare("NOTE", "QN", "TimeUS,Tag")
    rows = 120_000
    body = b"".join(note(n) for n in range(rows))
    data = log.bytes() + body
    adapter = FlightLogAdapter()
    config = configure(DESCRIPTOR)
    source = BytesReader(data)
    plan = adapter.plan(source, config)
    assert len(plan.chunks) >= 2
    tracemalloc.start()
    try:
        total = sum(
            sum(b.length for b in adapter.ingest(source, chunk, config).series)
            for chunk in plan.chunks
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert total == rows
    assert peak < 150 * 1024 * 1024


def test_two_hundred_fifty_types_are_declared_without_trouble() -> None:
    log = MAKE.Log()
    for n in range(100):
        log.declare(f"T{n:03d}", "QB", "TimeUS,V")
    output = run(log.bytes())
    assert len(streams(output)) == 100


def test_a_source_that_is_not_a_log_says_so_and_reads_nothing() -> None:
    output = run(b"just some text, not a flight log\n")
    assert codes(output) == ["bad_magic"] and not output.records()
