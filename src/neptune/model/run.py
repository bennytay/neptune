"""Runs and streams: the sessions evidence declares and the timestamped channels recorded in them.

Both are evidence records (ADR 0017) of the ``run`` family, specified by ADR 0018:

- A ``Run`` is a session that one piece of evidence declares: a recording (an MCAP file, a bag, a
  ULog), a rosbag2 bag's ``metadata.yaml``, a manifest entry. A session that a procedure assembles
  from several sources (by folder, by time, by robot id) is inferred and lives in ``derived/``
  (ADR 0017 §5).
- A ``Stream`` is one channel exactly as its source declares it: topic, schema and encodings, the
  clocks its samples carry, its declared count and extent, and the run it was recorded in. Its
  samples are a Parquet series. ``neptune.model.series`` holds the column contract; the ``Stream``
  holds what every row's provenance shares, so any row's provenance can be rebuilt from the two.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from neptune.model._fields import (
    check_text_values,
    check_type,
    json_array,
    json_int,
    json_str,
    text_decoder,
    values_of,
)
from neptune.model.ids import (
    LogicalId,
    RecordId,
    check_verbatim,
    logical_id_from_json,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Knowledge,
    KnowledgeState,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.provenance import (
    EvidenceRef,
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    evidence_ref_from_json,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.series import (
    SEQ,
    Row,
    SeriesProvenance,
    cell_state,
    check_columns,
    seq_of,
    series_provenance_from_json,
    ticks_of,
    time_column,
)
from neptune.model.time import Timestamp, timestamp_from_json


def _count(data: JsonValue) -> int:
    return json_int(data, "message_count")


@dataclass(frozen=True)
class Run:
    """A session one piece of evidence declares (ADR 0018 §1).

    ``provenance`` cites the declaration: a recording's header, a rosbag2 ``metadata.yaml``, a
    manifest's run entry. The other fields are what that evidence says, each with its own
    provenance where another part of the source says it (an MCAP summary's statistics):

    - ``logical_id``: the id the evidence gives the session, as in ``("manifest", "night-42")``.
      Recordings rarely state one, so it is usually ``Unknown``.
    - ``machine``: the declared identifier of the machine that recorded it (a ULog ``sys_uuid``, a
      manifest's robot). Never inferred from a topic prefix, a folder or a hostname.
    - ``first`` / ``last``: the session's first and last instants, both inclusive, each on the clock
      the evidence states it on (a recording's first and last message, a manifest's start and end).
      They are separate because a source may state one and not the other, as a ULog header states
      only the start. Implausible declared values stay as declared; judging them is validation's.
    """

    kind: ClassVar[str] = "run"
    family: ClassVar[Family] = Family.RUN
    id: RecordId
    provenance: Provenance
    logical_id: Knowledge[LogicalId]
    machine: Knowledge[LogicalId]
    first: Knowledge[Timestamp]
    last: Knowledge[Timestamp]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_type("logical_id", self.logical_id, LogicalId)
        check_type("machine", self.machine, LogicalId)
        check_type("first", self.first, Timestamp)
        check_type("last", self.last, Timestamp)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "first": to_json(self.first, Timestamp.to_json),
                "last": to_json(self.last, Timestamp.to_json),
                "logical_id": to_json(self.logical_id, LogicalId.to_json),
                "machine": to_json(self.machine, LogicalId.to_json),
            },
        )


def run_from_json(data: JsonValue) -> Run:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, Run.kind, {"first", "last", "logical_id", "machine"}
    )
    return Run(
        id=record_id,
        provenance=provenance,
        logical_id=from_json(obj["logical_id"], logical_id_from_json, provenance_from_json),
        machine=from_json(obj["machine"], logical_id_from_json, provenance_from_json),
        first=from_json(obj["first"], timestamp_from_json, provenance_from_json),
        last=from_json(obj["last"], timestamp_from_json, provenance_from_json),
    )


@dataclass(frozen=True)
class Stream:
    """One channel exactly as its source declares it, and the series of its samples (ADR 0018 §2).

    ``provenance`` cites the declaration (an MCAP channel record, a bag connection, a ULog
    subscription), and ``run`` is the ``Run`` the same transform says it was recorded in.

    - ``topic``: the channel's name, verbatim (``/imu``); ``NotApplicable`` where a format has none.
    - ``schema_name``, ``schema_encoding``, ``schema_definition``: the declared message type
      (``sensor_msgs/msg/Imu``), the language its definition is written in (``ros2msg``) and where
      the definition's bytes are. ``KnownAbsent`` where the format says a channel has no schema.
    - ``message_encoding``: how each sample's bytes are encoded (``cdr``).
    - ``metadata``: the other properties the declaration states as text, verbatim and sorted by
      key: MCAP channel metadata, ROS connection fields, a ULog ``multi_id``.
    - ``clocks``: the ``TimestampDomain`` of every clock the samples carry, each read into its own
      ``time/<i>`` column. None of them is the stream's "real" time. Clock 0 only orders the rows
      of the series; it is the clock the source itself orders or indexes its samples by.
    - ``message_count``, ``first``, ``last``: what the source declares about the stream (in an
      index or a summary), the times on one of the stream's clocks. What the series actually holds
      is counted from the series.
    - ``series``: what every row's provenance shares; the rest is in the row.
    """

    kind: ClassVar[str] = "stream"
    family: ClassVar[Family] = Family.RUN
    id: RecordId
    provenance: Provenance
    run: RecordId
    topic: Knowledge[str]
    schema_name: Knowledge[str]
    schema_encoding: Knowledge[str]
    schema_definition: Knowledge[EvidenceRef]
    message_encoding: Knowledge[str]
    metadata: tuple[tuple[str, str], ...]
    clocks: tuple[RecordId, ...]
    message_count: Knowledge[int]
    first: Knowledge[Timestamp]
    last: Knowledge[Timestamp]
    series: SeriesProvenance

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.run)
        for name in ("topic", "schema_name", "schema_encoding", "message_encoding"):
            check_text_values(name, getattr(self, name))
        check_type("schema_definition", self.schema_definition, EvidenceRef)
        if not isinstance(self.metadata, tuple):
            raise TypeError(
                f"metadata must be a tuple of pairs, got {type(self.metadata).__name__}"
            )
        keys = [key for key, _ in self.metadata]
        if keys != sorted(set(keys)):
            raise ValueError(f"metadata keys must be unique and sorted: {keys}")
        for key, value in self.metadata:
            check_verbatim("metadata key", key)
            check_verbatim(f"metadata {key!r}", value)
        if not isinstance(self.clocks, tuple) or not self.clocks:
            raise ValueError("a stream's samples carry at least one clock")
        for clock in self.clocks:
            parse_record_id(clock)
        if len(set(self.clocks)) != len(self.clocks):
            raise ValueError(f"clocks repeat: {self.clocks}")
        check_type("message_count", self.message_count, int)
        for count in values_of(self.message_count):
            if isinstance(count, bool) or count < 0:
                raise ValueError(f"message_count must be a non-negative integer, got {count!r}")
        for name in ("first", "last"):
            check_type(name, getattr(self, name), Timestamp)
            for stamp in values_of(getattr(self, name)):
                if stamp.domain_id not in self.clocks:
                    raise ValueError(f"{name} is on a clock the stream does not carry: {stamp}")
        if not isinstance(self.series, SeriesProvenance):
            raise TypeError(f"series must be a SeriesProvenance, got {self.series!r}")

    def series_columns(self) -> tuple[str, ...]:
        """The columns every row of the series has; value and state columns come on top."""
        clocks = (time_column(clock) for clock in range(len(self.clocks)))
        return (SEQ, *clocks, *self.series.columns)

    def row_evidence(self, row: Row) -> EvidenceRef:
        """The exact place in the source the row's sample was read from."""
        return self.series.evidence(row)

    def row_provenance(self, row: Row) -> Provenance:
        """The row's full provenance, rebuilt from this record and the row's locator columns."""
        return Provenance(
            self.series.evidence(row), self.provenance.transform, self.series.assertion_kind
        )

    def row_time(self, row: Row, clock: int) -> Knowledge[Timestamp]:
        """The row's time on clock ``clock``, in that clock's own domain.

        The states carry ``INHERITED`` provenance, which here means the row's.
        """
        if not 0 <= clock < len(self.clocks):
            raise ValueError(f"the stream has {len(self.clocks)} clocks, so no clock {clock}")
        column = time_column(clock)
        match cell_state(row, column):
            case KnowledgeState.KNOWN:
                return Known(Timestamp(ticks_of(row, column), self.clocks[clock]))
            case KnowledgeState.UNKNOWN:
                return Unknown()
            case KnowledgeState.NOT_COVERED:
                return NotCovered()
            case _:
                return NotApplicable()

    def check_row(self, row: Row) -> None:
        """Raise ``ValueError`` unless ``row`` keeps the series column contract (ADR 0018 §4)."""
        check_columns(row, self.series_columns())
        seq_of(row)
        for clock in range(len(self.clocks)):
            self.row_time(row, clock)
        self.series.evidence(row)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "clocks": list(self.clocks),
                "first": to_json(self.first, Timestamp.to_json),
                "last": to_json(self.last, Timestamp.to_json),
                "message_count": to_json(self.message_count),
                "message_encoding": to_json(self.message_encoding),
                "metadata": dict(self.metadata),
                "run": self.run,
                "schema_definition": to_json(self.schema_definition, EvidenceRef.to_json),
                "schema_encoding": to_json(self.schema_encoding),
                "schema_name": to_json(self.schema_name),
                "series": self.series.to_json(),
                "topic": to_json(self.topic),
            },
        )


def stream_from_json(data: JsonValue) -> Stream:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        Stream.kind,
        {
            "clocks",
            "first",
            "last",
            "message_count",
            "message_encoding",
            "metadata",
            "run",
            "schema_definition",
            "schema_encoding",
            "schema_name",
            "series",
            "topic",
        },
    )
    metadata = obj["metadata"]
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a JSON object of text to text")
    return Stream(
        id=record_id,
        provenance=provenance,
        run=parse_record_id(json_str(obj["run"], "run")),
        topic=from_json(obj["topic"], text_decoder("topic"), provenance_from_json),
        schema_name=from_json(
            obj["schema_name"], text_decoder("schema_name"), provenance_from_json
        ),
        schema_encoding=from_json(
            obj["schema_encoding"], text_decoder("schema_encoding"), provenance_from_json
        ),
        schema_definition=from_json(
            obj["schema_definition"], evidence_ref_from_json, provenance_from_json
        ),
        message_encoding=from_json(
            obj["message_encoding"], text_decoder("message_encoding"), provenance_from_json
        ),
        metadata=tuple(sorted((key, json_str(value, key)) for key, value in metadata.items())),
        clocks=tuple(
            parse_record_id(json_str(clock, "clock"))
            for clock in json_array(obj["clocks"], "clocks")
        ),
        message_count=from_json(obj["message_count"], _count, provenance_from_json),
        first=from_json(obj["first"], timestamp_from_json, provenance_from_json),
        last=from_json(obj["last"], timestamp_from_json, provenance_from_json),
        series=series_provenance_from_json(obj["series"]),
    )
