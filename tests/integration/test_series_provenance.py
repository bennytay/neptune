"""MVL-67 acceptance, end to end on real bytes: several clocks per sample, and row provenance.

The test plays a small MCAP adapter over ``fixtures/series/imu.mcap`` (spec-valid: two channels, six
JSON messages, a summary with statistics). It emits the clocks, the run, the streams and the series
rows the way an adapter would, then checks that:

- ``/imu``'s three clocks (log time, publish time, ``header.stamp``) all survive, each in its own
  domain, and none of them becomes "the" time of the stream;
- every row's full provenance, rebuilt from its ``Stream`` and the row alone, resolves to exactly
  the message the row was decoded from.
"""

import importlib.util
import json
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_evidence_record_id,
    evidence_record_id,
    transform_record,
)
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Known, KnownAbsent, Unknown
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    adapter_locator,
)
from neptune.model.reference import TimestampDomain, timestamp_domain_from_json
from neptune.model.run import Run, Stream, run_from_json, stream_from_json
from neptune.model.series import (
    SEQ,
    SeriesProvenance,
    locator_column,
    row_order,
    state_column,
    step_template,
    ticks_of,
    time_column,
    value_column,
)
from neptune.model.time import NANOSECOND, ClockRole, DomainMismatchError, Timestamp

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parent.parent / "fixtures/series"
DATA: Final = (FIXTURES / "imu.mcap").read_bytes()
SOURCE: Final = content_id(DATA)
MCAP: Final = transform_record(adapter_id="mcap", adapter_version="0.1.0", config={})
HEADER, SCHEMA, CHANNEL, MESSAGE, STATISTICS, DATA_END = 0x01, 0x03, 0x04, 0x05, 0x0B, 0x0F


# --- A minimal MCAP reader: enough to play the adapter and to check its citations --------------


@dataclass(frozen=True)
class McapRecord:
    opcode: int
    offset: int  # of the opcode byte, in the file
    content: bytes

    @property
    def at(self) -> ByteRange:
        """The whole record: opcode, length and content."""
        return ByteRange(self.offset, 9 + len(self.content))


class Fields:
    """Reads a record's fields in order, keeping track of where each one is in the file."""

    def __init__(self, record: McapRecord) -> None:
        self.record, self.pos = record, 0

    def uint(self, code: str) -> int:
        (value,) = struct.unpack_from("<" + code, self.record.content, self.pos)
        self.pos += struct.calcsize("<" + code)
        assert isinstance(value, int)
        return value

    def blob(self) -> tuple[bytes, ByteRange]:
        length = self.uint("I")
        start, self.pos = self.pos, self.pos + length
        where = ByteRange(self.record.offset + 9 + start, length)
        return self.record.content[start : self.pos], where

    def text(self) -> str:
        return self.blob()[0].decode()

    def text_map(self) -> dict[str, str]:
        end = self.uint("I") + self.pos
        entries: dict[str, str] = {}
        while self.pos < end:
            key = self.text()
            entries[key] = self.text()
        return entries

    def rest(self) -> bytes:
        return self.record.content[self.pos :]


def read_records(data: bytes) -> list[McapRecord]:
    assert data[:8] == data[-8:] == b"\x89MCAP0\r\n"
    records, offset = [], 8
    while offset < len(data) - 8:
        opcode, length = struct.unpack_from("<BQ", data, offset)
        records.append(McapRecord(opcode, offset, data[offset + 9 : offset + 9 + length]))
        offset += 9 + length
    return records


def record_at(evidence: EvidenceRef) -> McapRecord:
    """The one whole MCAP record a citation covers, read back from the source bytes."""
    assert evidence.source == SOURCE
    (step,) = evidence.locator
    assert isinstance(step, ByteRange)
    opcode, length = struct.unpack_from("<BQ", DATA, step.offset)
    assert step.length == 9 + length, "the citation covers exactly one whole record"
    return McapRecord(opcode, step.offset, DATA[step.offset + 9 : step.offset + step.length])


@dataclass(frozen=True)
class Message:
    channel_id: int
    log_time: int
    publish_time: int
    payload: dict[str, Any]


def decode_message(record: McapRecord) -> Message:
    assert record.opcode == MESSAGE
    fields = Fields(record)
    channel_id, _sequence = fields.uint("H"), fields.uint("I")
    log_time, publish_time = fields.uint("Q"), fields.uint("Q")
    return Message(channel_id, log_time, publish_time, json.loads(fields.rest()))


def stamp_ticks(payload: dict[str, Any]) -> int:
    stamp = payload["header"]["stamp"]
    return int(stamp["sec"]) * 10**9 + int(stamp["nanosec"])


# --- The adapter this test plays ---------------------------------------------------------------


def cite(*steps: Locator) -> Provenance:
    return Provenance(EvidenceRef(SOURCE, steps), MCAP.id, AssertionKind.OBSERVED)


def record_id(kind: str, where: Provenance) -> RecordId:
    return evidence_record_id(kind, where.evidence, MCAP)


def clock(
    field: str, scope: tuple[str, ...], role: ClockRole, where: Provenance
) -> TimestampDomain:
    """What the file says about a clock: its role and resolution, never its epoch or timescale."""
    return TimestampDomain(
        id=record_id("timestamp_domain", where),
        provenance=where,
        field=field,
        scope=scope,
        role=Known(role),
        resolution=Known(NANOSECOND),
        epoch=Unknown(),
        timescale=Unknown(),
        declared_monotonic=Unknown(),
    )


# The MCAP specification defines what log_time, publish_time and schema_id 0 mean. The magic
# establishes that the file is MCAP, so what the specification defines cites it (ADR 0017 §6).
MAGIC: Final = ByteRange(0, 8)
STATS_AT: Final = cite(next(r for r in read_records(DATA) if r.opcode == STATISTICS).at)


@dataclass(frozen=True)
class Ingested:
    domains: tuple[TimestampDomain, ...]
    run: Run
    streams: dict[str, Stream]  # by topic
    rows: dict[str, list[dict[str, object]]]  # by topic, in stored order


def ingest(data: bytes) -> Ingested:
    records = read_records(data)
    end = next(i for i, record in enumerate(records) if record.opcode == DATA_END)
    body, summary = records[:end], records[end + 1 :]
    log_time = clock(
        "log_time",
        (),
        ClockRole.RECEIVE,
        cite(MAGIC, adapter_locator("mcap:time_field", {"name": "log_time"})),
    )
    domains = [log_time]

    (schema,) = (record for record in body if record.opcode == SCHEMA)
    fields = Fields(schema)
    fields.uint("H")
    schema_name, schema_encoding = fields.text(), fields.text()
    _, definition = fields.blob()

    (stats,) = (record for record in summary if record.opcode == STATISTICS)
    fields = Fields(stats)
    for code in "QHIIII":  # the file's counts of messages, schemas, channels, ...
        fields.uint(code)
    start, finish = fields.uint("Q"), fields.uint("Q")
    counts_end = fields.uint("I") + fields.pos
    counts: dict[int, int] = {}
    while fields.pos < counts_end:
        channel_id = fields.uint("H")
        counts[channel_id] = fields.uint("Q")

    (header,) = (record for record in body if record.opcode == HEADER)
    run = Run(
        id=record_id("run", cite(header.at)),
        provenance=cite(header.at),
        logical_id=Unknown(),  # MCAP has no field for a session id
        machine=Unknown(),  # nor for the robot that recorded it
        first=Known(Timestamp(start, log_time.id), STATS_AT),
        last=Known(Timestamp(finish, log_time.id), STATS_AT),
    )

    series = SeriesProvenance(
        SOURCE, (step_template("byte_range", per_row=("length", "offset")),), AssertionKind.OBSERVED
    )
    no_schema = KnownAbsent(cite(MAGIC))  # the specification: schema_id 0 means no schema
    streams: dict[int, Stream] = {}
    for record in [record for record in body if record.opcode == CHANNEL]:
        fields = Fields(record)
        channel_id, schema_id = fields.uint("H"), fields.uint("H")
        topic, message_encoding, metadata = fields.text(), fields.text(), fields.text_map()
        publish = clock(
            "publish_time",
            (topic,),
            ClockRole.PUBLISH,
            cite(record.at, adapter_locator("mcap:time_field", {"name": "publish_time"})),
        )
        clocks = [log_time, publish]
        if schema_id:
            # The schema declares header.stamp as sec + nanosec: a sample time in nanoseconds.
            stamp = cite(definition, JsonPointer("/properties/header/properties/stamp"))
            clocks.append(clock("header.stamp", (topic,), ClockRole.SAMPLE, stamp))
        domains += clocks[1:]
        schema_at = cite(schema.at)
        streams[channel_id] = Stream(
            id=record_id("stream", cite(record.at)),
            provenance=cite(record.at),
            run=run.id,
            topic=Known(topic),
            schema_name=Known(schema_name, schema_at) if schema_id else no_schema,
            schema_encoding=Known(schema_encoding, schema_at) if schema_id else no_schema,
            schema_definition=Known(EvidenceRef(SOURCE, (definition,))) if schema_id else no_schema,
            message_encoding=Known(message_encoding),
            metadata=tuple(sorted(metadata.items())),
            clocks=tuple(domain.id for domain in clocks),
            message_count=Known(counts[channel_id], STATS_AT),
            first=Unknown(),  # without chunk indexes the file declares no extent per channel
            last=Unknown(),
            series=series,
        )

    rows: dict[int, list[dict[str, object]]] = {channel_id: [] for channel_id in streams}
    for record in [record for record in body if record.opcode == MESSAGE]:
        message = decode_message(record)
        row: dict[str, object] = {
            SEQ: len(rows[message.channel_id]),
            time_column(0): message.log_time,
            time_column(1): message.publish_time,
            locator_column(0, "length"): 9 + len(record.content),
            locator_column(0, "offset"): record.offset,
        }
        payload = message.payload
        if "header" in payload:
            row[time_column(2)] = stamp_ticks(payload)
            row[value_column("header.frame_id")] = payload["header"]["frame_id"]
            for axis in "xyz":
                row[value_column(f"linear_acceleration.{axis}")] = payload["linear_acceleration"][
                    axis
                ]
        else:
            row[value_column("voltage")] = payload["voltage"]
            percentage = payload["percentage"]  # null: the message could have said and did not
            row[value_column("percentage")] = percentage
            row[state_column(value_column("percentage"))] = (
                "unknown" if percentage is None else "known"
            )
        rows[message.channel_id].append(row)

    return Ingested(
        domains=tuple(domains),
        run=run,
        streams={topic_of(stream): stream for stream in streams.values()},
        rows={topic_of(streams[c]): sorted(rows[c], key=row_order) for c in streams},
    )


def topic_of(stream: Stream) -> str:
    return stream.topic.known_or_raise()


INGESTED: Final = ingest(DATA)
IMU, BATTERY = INGESTED.streams["/imu"], INGESTED.streams["/battery"]
DOMAINS: Final = {domain.id: domain for domain in INGESTED.domains}


def every_row() -> Iterator[tuple[Stream, dict[str, object]]]:
    for topic, stream in INGESTED.streams.items():
        for row in INGESTED.rows[topic]:
            yield stream, row


ROWS: Final = list(every_row())


# --- The fixture -------------------------------------------------------------------------------


def test_the_fixture_is_exactly_what_its_generator_writes() -> None:
    spec = importlib.util.spec_from_file_location("make_imu_mcap", FIXTURES / "make_imu_mcap.py")
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.build() == DATA


# --- Acceptance 1: several clocks, none chosen -------------------------------------------------


def test_every_clock_a_message_carries_survives_in_its_own_domain() -> None:
    clocks = [DOMAINS[clock] for clock in IMU.clocks]
    assert [domain.field for domain in clocks] == ["log_time", "publish_time", "header.stamp"]
    assert [domain.role for domain in clocks] == [
        Known(ClockRole.RECEIVE),
        Known(ClockRole.PUBLISH),
        Known(ClockRole.SAMPLE),
    ]
    for row in INGESTED.rows["/imu"]:
        message = decode_message(record_at(IMU.row_evidence(row)))
        assert [IMU.row_time(row, clock) for clock in range(3)] == [
            Known(Timestamp(message.log_time, IMU.clocks[0])),
            Known(Timestamp(message.publish_time, IMU.clocks[1])),
            Known(Timestamp(stamp_ticks(message.payload), IMU.clocks[2])),
        ]


def test_nothing_relates_the_clocks_until_an_alignment_does() -> None:
    row = INGESTED.rows["/imu"][0]
    log_time, stamp = IMU.row_time(row, 0), IMU.row_time(row, 2)
    assert isinstance(log_time, Known) and isinstance(stamp, Known)
    with pytest.raises(DomainMismatchError):
        _ = stamp.value < log_time.value
    # The recorder's clock is one clock for the whole file; each publisher's clock is its own.
    assert BATTERY.clocks[0] == IMU.clocks[0]
    assert BATTERY.clocks[1] != IMU.clocks[1]
    assert len(BATTERY.clocks) == 2  # /battery messages carry no header.stamp


def test_clock_zero_orders_the_stored_rows_and_nothing_else() -> None:
    stored = INGESTED.rows["/imu"]
    # Message 3 was recorded late; messages 1 and 2 share a log time, so source order breaks it.
    assert [row[SEQ] for row in stored] == [0, 3, 1, 2]
    log_times = [ticks_of(row, time_column(0)) for row in stored]
    assert log_times == sorted(log_times)


# --- Acceptance 2: any row's provenance from its Stream plus the row ---------------------------


@pytest.mark.parametrize(
    ("stream", "row"), ROWS, ids=[f"{topic_of(stream)}#{row[SEQ]}" for stream, row in ROWS]
)
def test_every_rows_provenance_resolves_to_its_message(
    stream: Stream, row: dict[str, object]
) -> None:
    stream.check_row(row)
    provenance = stream.row_provenance(row)
    assert provenance.transform == MCAP.id
    assert provenance.assertion_kind is AssertionKind.OBSERVED
    message = decode_message(record_at(provenance.evidence))
    assert message.channel_id == Fields(record_at(stream.provenance.evidence)).uint("H")
    assert (row[time_column(0)], row[time_column(1)]) == (message.log_time, message.publish_time)
    for column, value in row.items():
        if column.startswith("value/"):
            node: Any = message.payload
            for key in column.removeprefix("value/").split("."):
                node = node[key]
            assert value == node


def test_the_stream_record_alone_carries_what_rows_share() -> None:
    # The series file's metadata holds the Stream's canonical JSON (ADR 0018 §8): that line plus
    # a row is everything its provenance needs.
    for stream in (IMU, BATTERY):
        check_evidence_record_id(stream, MCAP)
        line = canonical_json.dumps(stream.to_json())
        from_file = stream_from_json(canonical_json.loads(line))
        for row in INGESTED.rows[topic_of(stream)]:
            assert from_file.row_provenance(row) == stream.row_provenance(row)


# --- What the file declares, and what it does not ----------------------------------------------


def test_declared_facts_cite_where_they_are_stated_and_gaps_stay_explicit() -> None:
    run = INGESTED.run
    check_evidence_record_id(run, MCAP)
    assert (run.logical_id, run.machine) == (Unknown(), Unknown())
    assert run.first == Known(Timestamp(1_700_000_000_010_000_000, IMU.clocks[0]), STATS_AT)
    assert run.last == Known(Timestamp(1_700_000_000_030_000_000, IMU.clocks[0]), STATS_AT)
    assert (IMU.message_count, BATTERY.message_count) == (Known(4, STATS_AT), Known(2, STATS_AT))
    assert (IMU.first, IMU.last) == (Unknown(), Unknown())
    assert BATTERY.schema_name == BATTERY.schema_definition == KnownAbsent(cite(MAGIC))
    assert [key for key, _ in IMU.metadata] == ["offered_qos_profiles"]
    reported = [row[state_column(value_column("percentage"))] for row in INGESTED.rows["/battery"]]
    assert reported == ["known", "unknown"]


def lines(out: Ingested) -> list[tuple[bytes, Callable[[JsonValue], Any]]]:
    """Every record's canonical JSON line and its reader; the rows go to Parquet instead."""
    return [
        *((canonical_json.dumps(d.to_json()), timestamp_domain_from_json) for d in out.domains),
        (canonical_json.dumps(out.run.to_json()), run_from_json),
        *((canonical_json.dumps(s.to_json()), stream_from_json) for s in out.streams.values()),
    ]


def test_ingesting_twice_gives_identical_bytes_and_every_record_round_trips() -> None:
    again = ingest(DATA)
    assert [line for line, _ in lines(again)] == [line for line, _ in lines(INGESTED)]
    assert again.rows == INGESTED.rows
    for line, read in lines(INGESTED):
        assert canonical_json.dumps(read(canonical_json.loads(line)).to_json()) == line
