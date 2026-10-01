"""Series batches (ADR 0024 §5): typed columns, Neptune's fixed column types, rows."""

from typing import Any

import pytest

from neptune.model.ids import RecordId
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn

STREAM = RecordId("rec:sha256:" + "1" * 64)


def column(name: str, kind: ColumnType, *values: Any, repeated: bool = False) -> SeriesColumn:
    return SeriesColumn(name, kind, tuple(values), repeated)


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        (ColumnType.INT8, (-128, 127), (128, -129, True, 1.0)),
        (ColumnType.UINT8, (0, 255), (-1, 256)),
        (ColumnType.INT16, (-(2**15), 2**15 - 1), (2**15,)),
        (ColumnType.UINT16, (0, 2**16 - 1), (2**16,)),
        (ColumnType.INT32, (-(2**31), 2**31 - 1), (2**31,)),
        (ColumnType.UINT32, (0, 2**32 - 1), (2**32,)),
        (ColumnType.INT64, (-(2**63), 2**63 - 1), (2**63, False)),
        (ColumnType.UINT64, (0, 2**64 - 1), (2**64, -1)),
        (ColumnType.FLOAT64, (0.1, -0.0, float("nan"), float("inf")), (1, "1.0")),
        (ColumnType.FLOAT32, (0.5, 1.5, float("nan"), float("-inf")), (0.1, 1e39, 1)),
        (ColumnType.BOOL, (True, False), (0, 1, "true")),
        (ColumnType.STRING, ("", "é", "\x00"), (b"a", 1, "\ud800")),
        (ColumnType.BINARY, (b"", b"\x00\xff"), ("a", bytearray(b"a"))),
    ],
)
def test_cells_are_exactly_their_column_type(
    kind: ColumnType, good: tuple[Any, ...], bad: tuple[Any, ...]
) -> None:
    assert column("value/x", kind, *good, None).values == (*good, None)
    for value in bad:
        with pytest.raises((ValueError, TypeError)):
            column("value/x", kind, value)


def test_a_repeated_column_holds_tuples_of_its_type() -> None:
    covariance = column("value/cov", ColumnType.FLOAT64, (0.0, 1.0), (), None, repeated=True)
    assert covariance.values == ((0.0, 1.0), (), None)
    with pytest.raises((ValueError, TypeError)):
        column("value/cov", ColumnType.FLOAT64, [0.0, 1.0], repeated=True)
    with pytest.raises((ValueError, TypeError)):
        column("value/cov", ColumnType.FLOAT64, (0.0, 1), repeated=True)
    with pytest.raises((ValueError, TypeError)):
        column("value/cov", ColumnType.FLOAT64, 0.0, repeated=True)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("seq", ColumnType.INT32),
        ("seq", ColumnType.UINT64),
        ("time/0", ColumnType.FLOAT64),
        ("locator/0/offset", ColumnType.BOOL),
        ("locator/0/offset", ColumnType.BINARY),
        ("state/value/x", ColumnType.INT64),
    ],
)
def test_neptunes_own_columns_have_fixed_types(name: str, kind: ColumnType) -> None:
    with pytest.raises((ValueError, TypeError)):
        SeriesColumn(name, kind, ())


def test_neptunes_own_columns_are_never_repeated() -> None:
    with pytest.raises((ValueError, TypeError)):
        SeriesColumn("time/0", ColumnType.INT64, (), repeated=True)


@pytest.mark.parametrize(
    "name", ["value", "value/", "state/", "time/x", "time/01", "locator/0", "extra", "Seq"]
)
def test_a_column_lives_in_a_series_namespace(name: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        SeriesColumn(name, ColumnType.INT64, ())


def test_a_batch_may_hold_no_rows_and_still_types_its_columns() -> None:
    empty = SeriesBatch(
        STREAM, (column("seq", ColumnType.INT64), column("value/v", ColumnType.BOOL))
    )
    assert (empty.length, list(empty.rows())) == (0, [])
    assert empty.schema()[1] == ("value/v", ColumnType.BOOL, False)


def test_a_batch_has_seq_unique_names_and_equal_lengths() -> None:
    seq = column("seq", ColumnType.INT64, 0, 1)
    value = column("value/v", ColumnType.STRING, "a", "b")
    batch = SeriesBatch(STREAM, (seq, value))
    assert batch.length == 2
    assert list(batch.rows()) == [{"seq": 0, "value/v": "a"}, {"seq": 1, "value/v": "b"}]
    assert batch.schema() == (
        ("seq", ColumnType.INT64, False),
        ("value/v", ColumnType.STRING, False),
    )
    with pytest.raises((ValueError, TypeError)):
        SeriesBatch(STREAM, (value,))
    with pytest.raises((ValueError, TypeError)):
        SeriesBatch(STREAM, (seq, seq))
    with pytest.raises((ValueError, TypeError)):
        SeriesBatch(STREAM, (seq, column("value/v", ColumnType.STRING, "a")))
    with pytest.raises(ValueError):
        SeriesBatch(RecordId("stream"), (seq,))
