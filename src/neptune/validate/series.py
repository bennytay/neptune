"""Checks over a stream's stored series: declared count against rows, time order per clock.

A series file holds its rows sorted by clock 0, then by ``seq`` (the sample's place in source
order). So clock 0 runs forward in source order exactly when ``seq`` rises through the file: a
fall in ``seq`` between two neighbouring rows is a sample stamped earlier than one the source
wrote before it. Every other clock is judged in file order only when file order is source order;
otherwise it is left unjudged and the finding says so. Only ``seq``, the time columns and the
locator columns (to cite the row) are read, a batch at a time.
"""

from collections.abc import Iterator, Mapping
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import Known
from neptune.model.provenance import EvidenceRef
from neptune.model.run import Stream
from neptune.model.series import LOCATOR, SEQ, time_column
from neptune.store.package import Content
from neptune.validate.engine import Context, Draft, evidence_of, plural, short


def _open(content: Content) -> Any:
    return pq.ParquetFile(pa.BufferReader(content) if isinstance(content, bytes) else content)


def _named(stream: Stream, details: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """``details`` with the stream's topic, when it states one."""
    if isinstance(stream.topic, Known):
        details["topic"] = stream.topic.value
    return details


def count_mismatch(context: Context) -> Iterator[Draft]:
    """A stream declares a message count its series does not hold (a cut or padded recording)."""
    for stream in context.records("stream"):
        if not isinstance(stream.message_count, Known):
            continue
        content = context.package.series.get(stream.id)
        stored = 0 if content is None else _open(content).metadata.num_rows
        declared = stream.message_count.value
        if stored == declared:
            continue
        yield Draft(
            subject=evidence_of(stream, stream.message_count),
            message=f"stream {short(stream.id)} declares {plural(declared, 'message')};"
            f" its series holds {plural(stored, 'row')}",
            details=_named(
                stream,
                {
                    "declared": declared,
                    "missing": max(declared - stored, 0),
                    "stored": stored,
                    "stream": stream.id,
                },
            ),
            records=(stream.id,),
        )


class _Clock:
    """One clock's walk through the file: falls counted, the first one's rows kept."""

    def __init__(self) -> None:
        self.previous: int | None = None
        self.previous_row: int | None = None  # global row index of ``previous``
        self.falls = 0
        self.first: tuple[int, int, int, int] | None = None  # (row, ticks, prior row, prior ticks)

    def walk(self, values: Any, offset: int) -> None:
        """Walk the next batch's cells (nulls skipped); ``offset`` is its first row's index."""
        valid = pc.indices_nonzero(pc.is_valid(values))
        if len(valid) == 0:
            return
        taken = pc.take(values, valid)
        lead = 0
        if self.previous is not None:
            taken = pa.concat_arrays([pa.array([self.previous], type=taken.type), taken])
            lead = 1
        falls = pc.indices_nonzero(pc.less(taken[1:], taken[:-1]))
        if len(falls):
            self.falls += len(falls)
            if self.first is None:
                at = falls[0].as_py()  # taken[at + 1] < taken[at]

                def row_of(i: int) -> int:
                    if lead and i == 0:
                        assert self.previous_row is not None
                        return self.previous_row
                    return offset + int(valid[i - lead].as_py())

                self.first = (
                    row_of(at + 1),
                    taken[at + 1].as_py(),
                    row_of(at),
                    taken[at].as_py(),
                )
        self.previous = taken[-1].as_py()
        self.previous_row = offset + int(valid[-1].as_py())


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _row(parquet: Any, index: int, columns: list[str]) -> Mapping[str, object]:
    """One row of the file by its index, the columns its evidence needs."""
    group_start = 0
    for group in range(parquet.metadata.num_row_groups):
        rows = parquet.metadata.row_group(group).num_rows
        if index < group_start + rows:
            table = parquet.read_row_group(group, columns=columns)
            row: Mapping[str, object] = table.slice(index - group_start, 1).to_pylist()[0]
            return row
        group_start += rows
    raise IndexError(index)


def _evidence(stream: Stream, parquet: Any, index: int, columns: list[str]) -> EvidenceRef:
    return stream.row_evidence(_row(parquet, index, columns))


def time_regression(context: Context) -> Iterator[Draft]:
    """A stream's samples step back in time on a clock, in the order the source wrote them."""
    domains = {domain.id: domain for domain in context.records("timestamp_domain")}
    for stream in context.records("stream"):
        content = context.package.series.get(stream.id)
        if content is None:
            continue
        parquet = _open(content)
        names = set(parquet.schema_arrow.names)
        clocks = [time_column(i) for i in range(len(stream.clocks))]
        locators = sorted(name for name in names if name.startswith(f"{LOCATOR}/"))
        cite = [SEQ, *locators]
        seq = _Clock()
        walks = [_Clock() for _ in clocks]
        offset = 0
        null_clock0 = False
        for batch in parquet.iter_batches(
            batch_size=context.bounds.batch_rows, columns=[SEQ, *clocks]
        ):
            seq.walk(batch.column(SEQ), offset)
            for clock, walk in enumerate(walks):
                walk.walk(batch.column(clocks[clock]), offset)
            null_clock0 = null_clock0 or batch.column(clocks[0]).null_count > 0
            offset += batch.num_rows
        # Rows whose clock 0 is not known sort last, by seq; a fall in seq is then a fall on clock 0
        # only among rows that have one. File order is source order iff seq never falls.
        in_source_order = seq.falls == 0
        for clock, walk in enumerate(walks):
            if clock == 0:
                falls, first = seq.falls, seq.first
                if null_clock0 and first is not None:
                    falls, first = _known_clock0_falls(parquet, context, clocks[0])
            elif in_source_order:
                falls, first = walk.falls, walk.first
            else:
                continue
            if not falls or first is None:
                continue
            row, _, prior_row, _ = first
            if clock == 0:  # walked on seq: report the sample written later and stamped earlier
                row, prior_row = prior_row, row
            values = _row(parquet, row, [SEQ, clocks[clock]])
            prior = _row(parquet, prior_row, [SEQ, clocks[clock]])
            domain = domains.get(stream.clocks[clock])
            declared = "no_clock_record"
            if domain is not None:
                monotonic = domain.declared_monotonic
                declared = (
                    str(monotonic.value).lower()
                    if isinstance(monotonic, Known)
                    else str(monotonic.state)
                )
            details: dict[str, JsonValue] = {
                "clock": clock,
                "declared_monotonic": declared,
                "domain": stream.clocks[clock],
                "descents": falls,
                "previous_seq": _int(prior[SEQ]),
                "previous_ticks": _int(prior[clocks[clock]]),
                "seq": _int(values[SEQ]),
                "stream": stream.id,
                "ticks": _int(values[clocks[clock]]),
            }
            _named(stream, details)
            if clock == 0 and len(clocks) > 1:
                details["other_clocks"] = "not_judged"  # file order is not source order
            yield Draft(
                subject=_evidence(stream, parquet, row, cite),
                message=f"stream {short(stream.id)} is not in time order on clock {clock}"
                f" in source order ({plural(falls, 'descent')})",
                details=details,
                related=(_evidence(stream, parquet, prior_row, cite),),
                records=(stream.id,),
            )


def _known_clock0_falls(
    parquet: Any, context: Context, column: str
) -> tuple[int, tuple[int, int, int, int] | None]:
    """Falls of seq among the rows with a known clock 0, which come first in the file."""
    seq = _Clock()
    offset = 0
    for batch in parquet.iter_batches(batch_size=context.bounds.batch_rows, columns=[SEQ, column]):
        known = pc.is_valid(batch.column(column))
        seq.walk(pc.if_else(known, batch.column(SEQ), pa.scalar(None, pa.int64())), offset)
        offset += batch.num_rows
    return seq.falls, seq.first
