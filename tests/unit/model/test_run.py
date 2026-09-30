"""Run and Stream records: what each field may hold, and reading a stream's rows (ADR 0018).

The column contract is tested rule by rule in test_series.py, and the acceptance end to end on
real MCAP bytes in tests/integration/test_series_provenance.py.
"""

from dataclasses import replace
from typing import Any

import pytest

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_evidence_record_id,
    evidence_record_id,
    transform_record,
)
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    TransformRecord,
    adapter_locator,
)
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.run import Run, Stream, run_from_json, stream_from_json
from neptune.model.series import SeriesProvenance, step_template
from neptune.model.time import DomainMismatchError, Timestamp

LOG_BYTES = b"\x89MCAP0\r\n" + bytes(1024)
MANIFEST_BYTES = b'{"run":{"id":"night-42","robot":"spot-07","start":"2026-09-30T20:00:00+10:00"}}'
LOG, MANIFEST = content_id(LOG_BYTES), content_id(MANIFEST_BYTES)
MCAP = transform_record(adapter_id="mcap", adapter_version="1.0.0", config={})
MANIFEST_ADAPTER = transform_record(adapter_id="manifest", adapter_version="1.0.0", config={})
SOURCE_OF = {MCAP.id: LOG, MANIFEST_ADAPTER.id: MANIFEST}
TRANSFORMS = {MCAP.id: MCAP, MANIFEST_ADAPTER.id: MANIFEST_ADAPTER}
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED


def cite(transform: TransformRecord, *steps: Locator, kind: AssertionKind = OBSERVED) -> Provenance:
    return Provenance(EvidenceRef(SOURCE_OF[transform.id], steps), transform.id, kind)


def record_id_of(kind: str, provenance: Provenance) -> RecordId:
    return evidence_record_id(kind, provenance.evidence, TRANSFORMS[provenance.transform])


def clock_id(transform: TransformRecord, *steps: Locator) -> RecordId:
    """A clock's id; streams only refer to their clocks, so the records themselves are elsewhere."""
    return record_id_of("timestamp_domain", cite(transform, *steps))


MAGIC = ByteRange(0, 8)
LOG_TIME = clock_id(MCAP, MAGIC, adapter_locator("mcap:time_field", {"name": "log_time"}))
PUBLISH_TIME = clock_id(MCAP, MAGIC, adapter_locator("mcap:time_field", {"name": "publish_time"}))
STAMP = clock_id(MCAP, ByteRange(90, 200), JsonPointer("/header/stamp"))
MANIFEST_START = clock_id(
    MANIFEST_ADAPTER, ByteRange(0, len(MANIFEST_BYTES)), JsonPointer("/run/start")
)
HEADER_AT, SCHEMA_AT, CHANNEL_AT = (
    cite(MCAP, ByteRange(8, 40)),
    cite(MCAP, ByteRange(48, 250)),
    cite(MCAP, ByteRange(300, 90)),
)
STATS_AT, INDEX_AT = cite(MCAP, ByteRange(900, 80)), cite(MCAP, ByteRange(980, 40))
# A message in a compressed chunk: the chunk's stored bytes, then the message's decompressed ones.
SERIES = SeriesProvenance(
    LOG,
    (
        step_template("byte_range", per_row=("length", "offset")),
        step_template("byte_range", per_row=("length", "offset")),
    ),
    OBSERVED,
)


def recording(**changes: Any) -> Run:
    """What an MCAP file declares about its session: its extent, and nothing about who or which."""
    run = Run(
        id=record_id_of("run", HEADER_AT),
        provenance=HEADER_AT,
        logical_id=Unknown(),
        machine=Unknown(),
        first=Known(Timestamp(1_000, LOG_TIME), STATS_AT),
        last=Known(Timestamp(9_000, LOG_TIME), STATS_AT),
    )
    return replace(run, **changes)


def manifest_run(**changes: Any) -> Run:
    """A manifest entry states a session about the world: its id, its robot and when it began."""
    entry = cite(
        MANIFEST_ADAPTER, ByteRange(0, len(MANIFEST_BYTES)), JsonPointer("/run"), kind=STATED
    )
    run = Run(
        id=record_id_of("run", entry),
        provenance=entry,
        logical_id=Known(LogicalId("manifest", "night-42")),
        machine=Known(LogicalId("manifest", "spot-07")),
        first=Known(Timestamp(1_790_762_400, MANIFEST_START)),  # Unix seconds, as the offset says
        last=Unknown(),  # it states a start and no end
    )
    return replace(run, **changes)


def imu(**changes: Any) -> Stream:
    stream = Stream(
        id=record_id_of("stream", CHANNEL_AT),
        provenance=CHANNEL_AT,
        run=recording().id,
        topic=Known("/imu"),
        schema_name=Known("sensor_msgs/msg/Imu", SCHEMA_AT),
        schema_encoding=Known("ros2msg", SCHEMA_AT),
        schema_definition=Known(EvidenceRef(LOG, (ByteRange(90, 200),))),
        message_encoding=Known("cdr"),
        metadata=(("offered_qos_profiles", "- history: 1\n  depth: 10\n"),),
        clocks=(LOG_TIME, PUBLISH_TIME, STAMP),
        message_count=Known(1200, STATS_AT),
        first=Known(Timestamp(1_000, LOG_TIME), INDEX_AT),
        last=Known(Timestamp(9_000, LOG_TIME), INDEX_AT),
        series=SERIES,
    )
    return replace(stream, **changes)


def schemaless(**changes: Any) -> Stream:
    """A channel the format says has no schema, and whose extent the file does not declare."""
    no_schema = KnownAbsent(cite(MCAP, MAGIC))  # the specification: schema_id 0 means none
    at = cite(MCAP, ByteRange(400, 60))
    return imu(
        id=record_id_of("stream", at),
        provenance=at,
        topic=Known("/battery"),
        schema_name=no_schema,
        schema_encoding=no_schema,
        schema_definition=no_schema,
        message_encoding=Ambiguous((Candidate("json"), Candidate("cbor", INDEX_AT))),
        metadata=(),
        clocks=(LOG_TIME,),
        message_count=Unknown(),
        first=Unknown(),
        last=Unknown(),
        **changes,
    )


def imu_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "seq": 0,
        "time/0": 1_000,
        "time/1": 990,
        "time/2": 950,
        "locator/0/length": 4096,
        "locator/0/offset": 512,
        "locator/1/length": 60,
        "locator/1/offset": 88,
        "value/linear_acceleration.x": 0.1,
    }
    return {**row, **changes}


RECORDS: list[tuple[Any, Any]] = [
    (recording(), run_from_json),
    (manifest_run(), run_from_json),
    (imu(), stream_from_json),
    (schemaless(), stream_from_json),
]
IDS = ["recording", "manifest run", "stream", "schemaless stream"]


# --- What makes them records -------------------------------------------------------------------


@pytest.mark.parametrize(("record", "read"), RECORDS, ids=IDS)
def test_runs_and_streams_round_trip_byte_identically(record: Any, read: Any) -> None:
    line = canonical_json.dumps(record.to_json())
    assert read(canonical_json.loads(line)) == record
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    assert (data["kind"], data["schema_version"]) == (record.kind, SCHEMA_VERSION)
    assert record.family is Family.RUN
    check_evidence_record_id(record, TRANSFORMS[record.provenance.transform])


@pytest.mark.parametrize(("record", "read"), RECORDS, ids=IDS)
def test_their_json_is_read_strictly(record: Any, read: Any) -> None:
    data = record.to_json()
    with pytest.raises(SchemaVersionError, match="newer"):
        read({**data, "schema_version": SCHEMA_VERSION + 1, "added_later": 1})
    for broken in (
        {**data, "confidence": 0.9},
        {key: value for key, value in data.items() if key != "first"},
        {**data, "kind": "run" if record.kind == "stream" else "stream"},
        {**data, "first": {"knowledge": "known", "value": 1_000}},  # ticks without their clock
    ):
        with pytest.raises(ValueError):
            read(broken)


def test_stream_json_holds_metadata_and_clocks_exactly() -> None:
    data = imu().to_json()
    assert data["metadata"] == {"offered_qos_profiles": "- history: 1\n  depth: 10\n"}
    assert data["clocks"] == [LOG_TIME, PUBLISH_TIME, STAMP]
    for broken in (
        {**data, "metadata": [["offered_qos_profiles", "x"]]},
        {**data, "metadata": {"multi_id": 1}},
        {**data, "clocks": LOG_TIME},
        {**data, "clocks": [LOG]},  # a content id is not a clock
        {**data, "run": 7},
    ):
        with pytest.raises(ValueError):
            stream_from_json(broken)


def test_logical_ids_round_trip() -> None:
    serial = LogicalId("serial", "SPOT-1234")
    assert (
        logical_id_from_json(canonical_json.loads(canonical_json.dumps(serial.to_json()))) == serial
    )


@pytest.mark.parametrize(
    "broken",
    [
        {"namespace": "serial"},
        {"namespace": "serial", "value": "SPOT-1234", "confidence": 1},
        {"namespace": "serial", "value": 1234},
        {"namespace": "Serial No.", "value": "SPOT-1234"},
        ["serial", "SPOT-1234"],
    ],
)
def test_logical_ids_are_read_strictly(broken: Any) -> None:
    with pytest.raises(ValueError):
        logical_id_from_json(broken)


# --- Run ---------------------------------------------------------------------------------------


def test_a_run_says_only_what_its_evidence_declares() -> None:
    assert (recording().logical_id, recording().machine) == (Unknown(), Unknown())
    run = manifest_run()
    assert run.provenance.assertion_kind is STATED  # a manifest asserts a session about the world
    assert run.machine == Known(LogicalId("manifest", "spot-07"))
    assert (run.first.state, run.last.state) == ("known", "unknown")


def test_implausible_declared_extents_stay_as_declared() -> None:
    # A summary stating last < first is a finding for validation, never a rewrite at parse time.
    backwards = recording(last=Known(Timestamp(10, LOG_TIME), STATS_AT))
    assert backwards.last.known_or_raise().ticks < backwards.first.known_or_raise().ticks
    manifest_run(last=Known(Timestamp(2, LOG_TIME)))  # a run's times need not share a clock


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"machine": Known("spot-07")}, ValueError),  # an id is (namespace, value), not a name
        ({"logical_id": Known(Timestamp(1, LOG_TIME))}, ValueError),
        ({"first": Known(1_000)}, ValueError),  # ticks mean nothing without their clock
        ({"provenance": InferredProvenance((HEADER_AT.evidence,), MCAP.id)}, TypeError),
        ({"id": LOG}, ValueError),
    ],
)
def test_run_fields_are_typed(change: dict[str, Any], error: type) -> None:
    with pytest.raises(error):
        recording(**change)


# --- Stream ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"clocks": ()}, ValueError),  # a timestamped stream carries at least one clock
        ({"clocks": (LOG_TIME, LOG_TIME)}, ValueError),
        ({"clocks": ("log_time",)}, ValueError),
        ({"first": Known(Timestamp(1, MANIFEST_START))}, ValueError),  # not one of its clocks
        (
            {
                "last": Ambiguous(
                    (Candidate(Timestamp(1, LOG_TIME)), Candidate(Timestamp(2, STAMP)))
                )
            },
            None,
        ),
        (
            {
                "last": Ambiguous(
                    (Candidate(Timestamp(1, LOG_TIME)), Candidate(Timestamp(2, MANIFEST_START)))
                )
            },
            ValueError,
        ),
        ({"message_count": Known(-1)}, ValueError),
        ({"message_count": Known(True)}, ValueError),
        ({"message_count": Known("1200")}, ValueError),
        ({"topic": Known("")}, ValueError),  # a blank is Unknown, never a value
        ({"topic": NotApplicable()}, None),  # a CSV series has no topic
        ({"schema_name": Known(7)}, ValueError),
        ({"schema_definition": Known(LOG)}, ValueError),  # a content id alone is not a citation
        ({"metadata": (("b", "1"), ("a", "2"))}, ValueError),
        ({"metadata": (("a", "1"), ("a", "2"))}, ValueError),
        ({"metadata": (("a", "\ud800"),)}, ValueError),
        ({"metadata": (("", ""),)}, None),  # verbatim, even when blank
        ({"metadata": {"a": "1"}}, TypeError),
        ({"run": "run-1"}, ValueError),
        ({"series": SERIES.to_json()}, TypeError),
        ({"provenance": InferredProvenance((CHANNEL_AT.evidence,), MCAP.id)}, TypeError),
    ],
)
def test_stream_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        assert stream_from_json(imu(**change).to_json()) == imu(**change)
        return
    with pytest.raises(error):
        imu(**change)


def test_a_stream_keeps_every_clock_its_samples_carry() -> None:
    stream, row = imu(), imu_row()
    times = [stream.row_time(row, clock) for clock in range(3)]
    assert times == [
        Known(Timestamp(1_000, LOG_TIME)),
        Known(Timestamp(990, PUBLISH_TIME)),
        Known(Timestamp(950, STAMP)),
    ]
    log_time, stamp = times[0].known_or_raise(), times[2].known_or_raise()
    with pytest.raises(DomainMismatchError):
        _ = stamp < log_time


@pytest.mark.parametrize(
    ("state", "expected"),
    [("unknown", Unknown()), ("not_covered", NotCovered()), ("not_applicable", NotApplicable())],
)
def test_a_time_that_is_not_known_reads_as_its_state(state: str, expected: Any) -> None:
    row = imu_row(**{"time/2": None, "state/time/2": state})
    assert imu().row_time(row, 2) == expected
    imu().check_row(row)


@pytest.mark.parametrize("ticks", [True, 950.0, "950", 2**63, None])
def test_known_ticks_are_signed_64_bit_integers(ticks: object) -> None:
    with pytest.raises(ValueError):
        imu().row_time(imu_row(**{"time/2": ticks}), 2)


def test_there_is_no_clock_beyond_the_streams_own() -> None:
    with pytest.raises(ValueError, match="no clock 3"):
        imu().row_time(imu_row(), 3)


def test_a_rows_provenance_is_what_the_stream_hoists_plus_its_locator() -> None:
    expected = Provenance(
        EvidenceRef(LOG, (ByteRange(512, 4096), ByteRange(88, 60))), MCAP.id, OBSERVED
    )
    assert imu().row_provenance(imu_row()) == expected
    assert imu().row_evidence(imu_row()) == expected.evidence
    # Rows may assert what the declaration does not: a register's rows are stated.
    stated = imu(series=replace(SERIES, assertion_kind=STATED))
    assert stated.row_provenance(imu_row()).assertion_kind is STATED
    assert stated.provenance.assertion_kind is OBSERVED


def test_a_stream_names_the_columns_every_row_has() -> None:
    assert imu().series_columns() == (
        "seq",
        "time/0",
        "time/1",
        "time/2",
        "locator/0/length",
        "locator/0/offset",
        "locator/1/length",
        "locator/1/offset",
    )
    assert schemaless().series_columns()[:2] == ("seq", "time/0")
    imu().check_row(imu_row())


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in imu_row().items() if key != "time/2"},
        imu_row(**{"time/3": 1}),  # a clock the stream does not carry
        imu_row(**{"state/time/2": "unknown"}),  # an unknown time that has a value
        imu_row(seq=-1),
        imu_row(**{"locator/1/offset": None}),
        imu_row(**{"value/linear_acceleration.x": None}),
    ],
)
def test_rows_that_break_the_contract_are_refused(row: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        imu().check_row(row)
