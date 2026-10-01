"""``run_seq_range``: a run's ``seq`` bounds from its Parquet statistics, never from its rows."""

from pathlib import Path

import pytest

from neptune.model.ids import RecordId
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn
from neptune.store.series import SeriesError, run_seq_range, write_run

STREAM = RecordId("rec:sha256:" + "1" * 64)


def batch(seqs: list[int]) -> SeriesBatch:
    return SeriesBatch(
        STREAM,
        (
            SeriesColumn("locator/0/offset", ColumnType.INT64, tuple(7 * s for s in seqs)),
            SeriesColumn("seq", ColumnType.INT64, tuple(seqs)),
            SeriesColumn("time/0", ColumnType.INT64, tuple(1000 - s for s in seqs)),
            SeriesColumn("value/v", ColumnType.FLOAT64, tuple(float(s) for s in seqs)),
        ),
    )


def test_the_range_is_the_least_and_greatest_seq_whatever_the_order(tmp_path: Path) -> None:
    run = tmp_path / "run.parquet"
    write_run([batch([5, 3, 9]), batch([4])], run)  # sorted by time, so seq order is reversed
    assert run_seq_range(run) == (3, 9)


def test_an_empty_run_has_no_range(tmp_path: Path) -> None:
    run = tmp_path / "run.parquet"
    write_run([batch([])], run)
    assert run_seq_range(run) is None


def test_a_run_of_one_row_has_a_point_range(tmp_path: Path) -> None:
    run = tmp_path / "run.parquet"
    write_run([batch([42])], run)
    assert run_seq_range(run) == (42, 42)


def test_a_file_that_is_not_a_run_is_refused(tmp_path: Path) -> None:
    (tmp_path / "junk.parquet").write_bytes(b"PAR1 not really")
    with pytest.raises(SeriesError, match="not a readable Parquet file"):
        run_seq_range(tmp_path / "junk.parquet")
