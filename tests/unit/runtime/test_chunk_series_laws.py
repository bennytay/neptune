"""The per-chunk series laws name facts of the chunk's content, never of its emission order.

The workspace keeps a chunk's rows as one sorted run per stream, not as the adapter emitted them,
and a job of a new runtime version judges a kept chunk from that form (ADR 0031 §2). So the
failure a law names (which stream, which ``seq``) must be the same however the adapter ordered or
batched the same rows, or a judged chunk and a fresh one would write different findings
(ADR 0033 §5).
"""

import itertools

import pytest

from neptune.adapters.contract import ChunkOutput
from neptune.model.ids import RecordId
from neptune.model.series import SEQ, ColumnType, SeriesBatch, SeriesColumn
from neptune.runtime.job import _chunk_series_failure
from neptune.runtime.lineage import Failure, Law, Step

A = RecordId("rec:sha256:" + "a" * 64)
B = RecordId("rec:sha256:" + "b" * 64)


def batch(stream: RecordId, *seqs: int, extra: bool = False) -> SeriesBatch:
    columns = [SeriesColumn(SEQ, ColumnType.INT64, seqs)]
    if extra:
        columns.append(SeriesColumn("value/extra", ColumnType.INT8, (0,) * len(seqs)))
    return SeriesBatch(stream, tuple(columns))


def failure_of(*batches: SeriesBatch) -> Failure | None:
    return _chunk_series_failure(ChunkOutput(series=batches))


def test_a_clean_chunk_has_no_failure() -> None:
    assert failure_of(batch(A, 0, 1), batch(B, 0), batch(A, 2)) is None


def test_the_least_stream_and_the_least_repeated_seq_are_named_in_any_order() -> None:
    batches = (batch(B, 1, 2, 1), batch(A, 9, 9), batch(A, 3, 4, 3), batch(B, 0))
    expected = Failure(
        Step.CHUNK_SERIES, "ContractError", {"law": "seq_repeated", "seq": 3, "stream": A}
    )
    for order in itertools.permutations(batches):
        assert failure_of(*order) == expected


def test_the_same_rows_batched_otherwise_fail_the_same() -> None:
    whole = failure_of(batch(A, 5, 7, 5, 7))
    split = failure_of(batch(A, 7), batch(A, 5, 7), batch(A, 5))
    stored = failure_of(batch(A, 5, 5, 7, 7))  # one sorted run, as the workspace keeps it
    assert whole == split == stored
    assert whole is not None and whole.facts["seq"] == 5


@pytest.mark.parametrize("first", [True, False])
def test_columns_that_disagree_are_named_before_any_seq_of_that_stream(first: bool) -> None:
    disagreeing = (batch(B, 0, 0), batch(B, 1, extra=True))
    batches = (*disagreeing, batch(A, 2)) if first else (batch(A, 2), *reversed(disagreeing))
    assert failure_of(*batches) == Failure(
        Step.CHUNK_SERIES, "ContractError", {"law": str(Law.BATCH_COLUMNS_DISAGREE), "stream": B}
    )
