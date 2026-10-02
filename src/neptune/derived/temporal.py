"""The clock-alignment pass over a package's records and series (ADR 0060).

``align_clocks`` takes the records a package holds and a way to read its streams' series, and
returns what relates its clocks, under its transform (``neptune.clocks``):

- **clocks found in values** (``derived/timestamp_domain``): a stream whose type, by its
  producer's published message definition, carries a receiver's time in its fields (PX4's
  ``time_utc_usec``, ArduPilot's GPS week and milliseconds) gets an inferred domain for it;
- **sync anchors**, two readings taken as one instant, from each row that holds both: a stream's
  own clocks against its first (an MCAP message's ``publish_time`` against its ``log_time``) and
  a stream's clock against the clock found in its values;
- **fitted mappings** (``derived/clock_mapping``), one per pair of clocks a stream's anchors
  relate (``neptune.derived.clocks.fit_line``), valid over the source instants they span. Each
  anchor's two readings mark two events (a publish and a receipt, a GPS fix and its publication),
  so the map is exact about its fit and silent about that latency: its ``residual_bound`` is
  ``Unknown`` unless the config states a latency bound for the rule (``ClockConfig.slack``);
- **findings** for every clock the pass could not relate: a rule with no anchor, a clock running
  backward against another, a single instant, an unbounded latency, and, when the package's clocks
  form more than one group no mapping joins, one ``unsynchronised`` finding listing the groups.

Stated mappings (canonical ``ClockMapping`` records) join the graph as they are; the pass never
re-estimates one. It reads only the time and value columns its rules need, decodes no message,
and writes no source tick: every line is a new record beside the evidence.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final

from neptune.derived.clocks import (
    DOMAIN_KIND,
    MAPPING_KIND,
    MAX_RATE_DENOMINATOR,
    ClockGraph,
    FitProblem,
    InferredClockMapping,
    InferredTimestampDomain,
    Line,
    fit_line,
    fitted_mapping,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.ids import record_id
from neptune.identity.provenance import transform_record
from neptune.model.alignment import ClockMapping
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import EvidenceRef, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.run import Stream
from neptune.model.time import INT64_MAX, INT64_MIN, ClockRole, Epoch, Timescale

CLOCKS_ID: Final = "neptune.clocks"
CLOCKS_VERSION: Final = "0.1.0"
_PREFIX: Final = "neptune.clocks."
CO_RECORDED: Final = "stream.co_recorded"

# The rows of a stream's series, only the named columns (those the series has), in any order.
RowReader = Callable[[Stream, Sequence[str]], Iterable[Mapping[str, object]]]

_KNOWN: Final = "known"
_GPS_WEEK_MS: Final = 7 * 86_400 * 1_000


def _tick(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if INT64_MIN <= value <= INT64_MAX else None


def _px4_utc(values: Sequence[object]) -> int | None:
    """``time_utc_usec``: microseconds of UTC since the Unix epoch; 0 means no GPS time yet."""
    tick = _tick(values[0])
    return tick if tick is not None and tick > 0 else None


def _gps_week(values: Sequence[object]) -> int | None:
    """``GWk`` and ``GMS``: GPS weeks since 1980-01-06 and milliseconds into the week; a week of
    0 means the receiver has no time yet."""
    week, ms = _tick(values[0]), _tick(values[1])
    if week is None or ms is None or week <= 0 or not 0 <= ms < _GPS_WEEK_MS:
        return None
    return week * _GPS_WEEK_MS + ms


@dataclass(frozen=True)
class EmbeddedClock:
    """A rule: streams of ``types`` in ``encoding`` carry a clock in their ``columns``.

    ``ticks`` combines one row's cells into a tick on that clock, or ``None`` where the row says
    the receiver had no time (a documented sentinel), which is then no anchor.
    """

    rule: str
    encoding: str
    types: frozenset[str]
    fields: tuple[str, ...]  # verbatim field names, in the order ``ticks`` combines them
    resolution: Fraction
    epoch: Epoch
    timescale: Timescale
    ticks: Callable[[Sequence[object]], int | None]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(f"value/{name}" for name in self.fields)


# The producers' published message definitions are the reading: PX4's sensor_gps.msg
# ("time_utc_usec: Timestamp (microseconds, UTC) ... 0 if unavailable") and ArduPilot's log
# message reference (GPS: "GMS: milliseconds since start of GPS week; GWk: weeks since 5 Jan 1980").
EMBEDDED: Final = (
    EmbeddedClock(
        "px4.gps_utc",
        "ulog",
        frozenset({"vehicle_gps_position", "sensor_gps"}),
        ("time_utc_usec",),
        Fraction(1, 1_000_000),
        Epoch.UNIX,
        Timescale.POSIX,
        _px4_utc,
    ),
    EmbeddedClock(
        "ardupilot.gps_time",
        "dataflash",
        frozenset({"GPS", "GPS2"}),
        ("GWk", "GMS"),
        Fraction(1, 1_000),
        Epoch.GPS,
        Timescale.GPS,
        _gps_week,
    ),
)
RULES: Final = (CO_RECORDED, *(rule.rule for rule in EMBEDDED))


@dataclass(frozen=True)
class ClockConfig:
    """``slack``: per rule, the largest time, in seconds, between an anchor's two readings that
    the user states (a measured transport latency); a rule not named leaves its mappings' residual
    bounds ``Unknown``. Nothing is assumed by default."""

    max_rate_denominator: int = MAX_RATE_DENOMINATOR
    slack: tuple[tuple[str, Fraction], ...] = ()

    def __post_init__(self) -> None:
        value = self.max_rate_denominator
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"max_rate_denominator must be a positive integer, got {value!r}")
        rules = [rule for rule, _ in self.slack]
        if rules != sorted(set(rules)) or any(rule not in RULES for rule in rules):
            raise ValueError(f"slack names known rules, each once, sorted: {rules}")
        for _, seconds in self.slack:
            if not isinstance(seconds, Fraction) or seconds < 0:
                raise ValueError(f"a slack is a non-negative Fraction of seconds, got {seconds!r}")

    def to_json(self) -> JsonObject:
        return {
            "max_rate_denominator": self.max_rate_denominator,
            "slack": {
                rule: {"denominator": s.denominator, "numerator": s.numerator}
                for rule, s in self.slack
            },
        }


@dataclass(frozen=True)
class ClockAlignment:
    transform: TransformRecord
    domains: tuple[InferredTimestampDomain, ...]
    mappings: tuple[InferredClockMapping, ...]
    findings: tuple[IngestFinding, ...]
    graph: ClockGraph = field(compare=False)

    def tables(self) -> dict[str, Iterator[JsonObject]]:
        """The package's two derived tables, each in id order (ADR 0036 §8)."""
        return {
            DOMAIN_KIND: (line.to_json() for line in sorted(self.domains, key=lambda r: r.id)),
            MAPPING_KIND: (line.to_json() for line in sorted(self.mappings, key=lambda r: r.id)),
        }

    def summary(self) -> JsonObject:
        bounded = sum(isinstance(m.residual_bound, Known) for m in self.mappings)
        return {
            "domains": len(self.domains),
            "findings": len(self.findings),
            "mappings": len(self.mappings),
            "mappings_bounded": bounded,
        }


@dataclass(frozen=True)
class _Task:
    """One pair of clocks a stream's rows relate: ``source`` ticks against ``target`` ticks."""

    rule: str
    stream: Stream
    source: RecordId
    target: RecordId
    columns: tuple[str, ...]
    pair: Callable[[Mapping[str, object]], tuple[int, int] | None]
    target_resolution: Fraction | None
    target_evidence: EvidenceRef


def _time(row: Mapping[str, object], index: int) -> int | None:
    state = row.get(f"state/time/{index}", _KNOWN)
    return _tick(row.get(f"time/{index}")) if state == _KNOWN else None


def _co_recorded(index: int) -> Callable[[Mapping[str, object]], tuple[int, int] | None]:
    def pair(row: Mapping[str, object]) -> tuple[int, int] | None:
        source, target = _time(row, index), _time(row, 0)
        return None if source is None or target is None else (source, target)

    return pair


def _embedded(rule: EmbeddedClock) -> Callable[[Mapping[str, object]], tuple[int, int] | None]:
    def pair(row: Mapping[str, object]) -> tuple[int, int] | None:
        source = _time(row, 0)
        target = rule.ticks([row.get(column) for column in rule.columns])
        return None if source is None or target is None else (source, target)

    return pair


def _text(value: object) -> str | None:
    return value.value if isinstance(value, Known) and isinstance(value.value, str) else None


def _resolution(domain: TimestampDomain) -> Fraction | None:
    resolution = domain.resolution
    return resolution.value if isinstance(resolution, Known) else None


class _Pass:
    def __init__(
        self,
        config: ClockConfig,
        domains: Mapping[RecordId, TimestampDomain],
        upstream: Iterable[RecordId],
    ) -> None:
        self.config, self.domains = config, domains
        self.transform = transform_record(
            adapter_id=CLOCKS_ID,
            adapter_version=CLOCKS_VERSION,
            config=config.to_json(),
            upstream=sorted(set(upstream)),
        )
        self.slack = dict(config.slack)
        self.found: list[IngestFinding] = []
        self.candidates: dict[RecordId, InferredTimestampDomain] = {}  # found by a rule
        self.embedded: dict[RecordId, InferredTimestampDomain] = {}  # ...with a reading
        self.mappings: list[InferredClockMapping] = []
        # mappings left unbounded, by rule and source artifact: one finding each
        self.unbounded: dict[tuple[str, str], list[tuple[Stream, InferredClockMapping, Line]]] = {}

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        message: str,
        details: Mapping[str, JsonValue],
        records: Iterable[RecordId],
    ) -> None:
        self.found.append(
            ingest_finding(
                code=_PREFIX + code,
                category=category,
                severity=severity,
                subject=subject,
                transform=self.transform,
                message=message,
                details=details,
                records=records,
            )
        )

    def tasks(self, stream: Stream) -> Iterator[_Task]:
        clocks = stream.clocks
        if not clocks or any(clock not in self.domains for clock in clocks):
            return
        target = self.domains[clocks[0]]
        for index in range(1, len(clocks)):
            yield _Task(
                CO_RECORDED,
                stream,
                clocks[index],
                clocks[0],
                (f"time/{index}", f"state/time/{index}", "time/0", "state/time/0"),
                _co_recorded(index),
                _resolution(target),
                target.provenance.evidence,
            )
        encoding, name = _text(stream.message_encoding), _text(stream.schema_name)
        for rule in EMBEDDED:
            if encoding == rule.encoding and name in rule.types:
                domain = self._domain(rule, stream, name)
                yield _Task(
                    rule.rule,
                    stream,
                    clocks[0],
                    domain.id,
                    ("time/0", "state/time/0", *rule.columns),
                    _embedded(rule),
                    rule.resolution,
                    stream.provenance.evidence,
                )

    def _domain(self, rule: EmbeddedClock, stream: Stream, name: str) -> InferredTimestampDomain:
        inputs: JsonObject = {"rule": rule.rule, "stream": stream.id}
        domain = InferredTimestampDomain(
            id=record_id(DOMAIN_KIND, {**inputs, "transform": self.transform.id}),
            transform=self.transform.id,
            evidence=(stream.provenance.evidence,),
            field=",".join(rule.fields),
            scope=(_text(stream.topic) or name,),
            role=Known(ClockRole.SAMPLE),
            resolution=Known(rule.resolution),
            epoch=Known(rule.epoch),
            timescale=Known(rule.timescale),
            declared_monotonic=Unknown(),
        )
        self.candidates[domain.id] = domain
        return domain

    def run(self, task: _Task, rows: RowReader) -> None:
        stream = task.stream

        def pairs() -> Iterator[tuple[int, int]]:
            for row in rows(stream, task.columns):
                found = task.pair(row)
                if found is not None:
                    yield found

        line = fit_line(pairs, self.config.max_rate_denominator)
        records = (stream.id, task.source)
        details: dict[str, JsonValue] = {"rule": task.rule, "source": task.source}
        details["target"] = task.target
        if line is FitProblem.NO_ANCHORS:
            self.finding(
                "anchors_absent",
                FindingCategory.MISSING,
                Severity.INFO,
                stream.provenance.evidence,
                "no row holds both readings, so these two clocks stay unrelated",
                details,
                records,
            )
            return
        if line is FitProblem.NOT_INCREASING:
            self.finding(
                "clock_not_increasing",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                stream.provenance.evidence,
                "one clock runs backward against the other across the rows; no mapping is made",
                details,
                records,
            )
            return
        if line is FitProblem.OUT_OF_RANGE:
            self.finding(
                "anchor_out_of_range",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                stream.provenance.evidence,
                "the fitted anchor does not fit a signed 64-bit tick; no mapping is made",
                details,
                records,
            )
            return
        assert isinstance(line, Line)
        slack = self._slack(task)
        mapping = fitted_mapping(
            record_id=record_id(
                MAPPING_KIND,
                {
                    "rule": task.rule,
                    "source": task.source,
                    "stream": stream.id,
                    "target": task.target,
                    "transform": self.transform.id,
                },
            ),
            transform=self.transform.id,
            evidence=(
                self.domains[task.source].provenance.evidence,
                task.target_evidence,
                stream.provenance.evidence,
            ),
            source=task.source,
            target=task.target,
            line=line,
            slack=slack,
        )
        self.mappings.append(mapping)
        if task.target in self.candidates:  # a found clock is kept where it has a reading
            self.embedded[task.target] = self.candidates[task.target]
        if line.rate is None:
            self.finding(
                "single_instant",
                FindingCategory.MISSING,
                Severity.INFO,
                stream.provenance.evidence,
                "every anchor is one instant of the source clock: the offset holds there only and"
                " the rate is unknown",
                {**details, "anchors": line.count, "mapping": mapping.id},
                records,
            )
        if slack is None:
            key = (task.rule, str(stream.provenance.evidence.source))
            self.unbounded.setdefault(key, []).append((stream, mapping, line))

    def _slack(self, task: _Task) -> int | None:
        seconds = self.slack.get(task.rule)
        if seconds is None or task.target_resolution is None:
            return None
        ticks = seconds / task.target_resolution
        return -(-ticks.numerator // ticks.denominator)  # rounded up: it stays a bound

    def report_unbounded(self) -> None:
        for (rule, _), fits in sorted(self.unbounded.items()):
            fits.sort(key=lambda fit: fit[1].id)
            self.finding(
                "latency_unbounded",
                FindingCategory.MISSING,
                Severity.INFO,
                fits[0][0].provenance.evidence,
                f"{len(fits)} clock mapping(s) fitted by {rule}: each anchor's two readings mark"
                " two events, and nothing bounds the time between them, so the mappings'"
                " residual bounds are unknown; the fit residuals are in the details",
                {
                    "fits": [
                        {"anchors": line.count, "fit_residual": line.residual, "mapping": m.id}
                        for _, m, line in fits
                    ],
                    "rule": rule,
                },
                sorted({m.source for _, m, _ in fits}),
            )

    def report_groups(self, graph: ClockGraph) -> None:
        clocks = [*self.domains, *self.embedded]
        groups = graph.groups(clocks)
        if len(groups) < 2:
            return
        first = self.domains.get(groups[0][0])
        subject = (
            first.provenance.evidence
            if first is not None
            else self.domains[min(self.domains)].provenance.evidence
        )
        self.finding(
            "unsynchronised",
            FindingCategory.MISSING,
            Severity.INFO,
            subject,
            f"the package's clocks form {len(groups)} groups that no clock mapping joins; times"
            " in different groups cannot be compared",
            {"groups": [list(group) for group in groups]},
            sorted(self.domains),
        )


def align_clocks(
    records: Iterable[object], rows: RowReader, config: ClockConfig | None = None
) -> ClockAlignment | None:
    """Relate the clocks of a package's ``records`` (module docstring); ``None`` when it holds
    fewer than two clocks, found ones included: nothing to relate, so no tables and no transform.
    Deterministic: the same records, rows and config give the same lines and findings."""
    by_id = {r.id: r for r in records if isinstance(r, TimestampDomain | Stream)}
    domains = {i: r for i, r in sorted(by_id.items()) if isinstance(r, TimestampDomain)}
    streams = [r for _, r in sorted(by_id.items()) if isinstance(r, Stream)]
    stated = [r for r in records if isinstance(r, ClockMapping)]
    if not domains:
        return None
    upstream = [d.provenance.transform for d in domains.values()]
    upstream += [s.provenance.transform for s in streams]
    work = _Pass(config or ClockConfig(), domains, upstream)
    tasks = [task for stream in streams for task in work.tasks(stream)]
    if len(domains) + len(work.candidates) < 2:
        return None
    for task in tasks:
        work.run(task, rows)
    work.report_unbounded()
    graph = ClockGraph([*stated, *work.mappings])
    work.report_groups(graph)
    return ClockAlignment(
        work.transform,
        tuple(sorted(work.embedded.values(), key=lambda d: d.id)),
        tuple(sorted(work.mappings, key=lambda m: m.id)),
        tuple(sorted(work.found, key=lambda f: f.id)),
        graph,
    )


def clock_graph(records: Iterable[object], derived: Iterable[object] = ()) -> ClockGraph:
    """The graph of a package's stated mappings and the inferred ones its derived tables hold
    (``neptune.derived.sessions.read_derived``), for ``ClockGraph.align``."""
    mappings: list[ClockMapping | InferredClockMapping] = [
        r for r in [*records, *derived] if isinstance(r, ClockMapping | InferredClockMapping)
    ]
    return ClockGraph(mappings)
