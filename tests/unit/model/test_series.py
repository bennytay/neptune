"""The series column contract and row locator templates, rule by rule (ADR 0018 §4 to §7).

End to end on real MCAP bytes: tests/integration/test_series_provenance.py.
"""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import RecordId
from neptune.model.knowledge import AssertionKind, KnowledgeState
from neptune.model.provenance import (
    NO_HEADER,
    ByteRange,
    EvidenceRef,
    Locator,
    RecordRange,
    Row,
    RowCell,
    VideoFrame,
    adapter_locator,
)
from neptune.model.series import (
    SEQ,
    SeriesProvenance,
    StepTemplate,
    cell_state,
    check_columns,
    locator_column,
    row_order,
    seq_of,
    series_provenance_from_json,
    state_column,
    step_template,
    time_column,
    value_column,
)
from neptune.model.time import INT64_MAX, INT64_MIN, Timestamp

LOG = content_id(b"\x89MCAP0\r\n" + bytes(64))
DOMAIN = RecordId("rec:sha256:" + "d" * 64)
OBSERVED = AssertionKind.OBSERVED
BYTES = step_template("byte_range", per_row=("length", "offset"))


def series(*steps: StepTemplate, kind: AssertionKind = OBSERVED) -> SeriesProvenance:
    return SeriesProvenance(LOG, steps, kind)


def cells(step: int, **values: Any) -> dict[str, object]:
    return {locator_column(step, name): value for name, value in values.items()}


# --- Column names ------------------------------------------------------------------------------


def test_neptune_columns_live_in_namespaces_that_source_names_cannot_reach() -> None:
    assert (SEQ, time_column(2), locator_column(1, "offset")) == (
        "seq",
        "time/2",
        "locator/1/offset",
    )
    # A source field called "seq" or "time/0" is a value column, never Neptune's own.
    assert value_column("seq") == "value/seq"
    assert value_column("time/0") == "value/time/0"
    assert state_column(value_column("x")) == "state/value/x"
    assert state_column(time_column(0)) == "state/time/0"
    with pytest.raises(ValueError, match="non-empty"):
        value_column("")


# --- Locator templates -------------------------------------------------------------------------

SHAPES: list[tuple[str, tuple[StepTemplate, ...], dict[str, object], tuple[Locator, ...]]] = [
    (
        "message in a compressed chunk",
        (BYTES, BYTES),
        {**cells(0, offset=512, length=4096), **cells(1, offset=88, length=60)},
        (ByteRange(512, 4096), ByteRange(88, 60)),
    ),
    (
        "csv row",
        (
            step_template("byte_range", {"offset": 0, "length": 120}),
            step_template("row", {}, ["row"]),
        ),
        cells(1, row=3),
        (ByteRange(0, 120), Row(3)),
    ),
    (
        "video frame",
        (
            step_template("byte_range", {"offset": 0, "length": 10_000}),
            step_template("video_frame", {"track": 0, "domain_id": DOMAIN}, ["index", "pts"]),
        ),
        cells(1, index=7, pts=7007),
        (ByteRange(0, 10_000), VideoFrame(0, 7, Timestamp(7007, DOMAIN))),
    ),
    (
        "one record of a log",
        (
            step_template(
                "record_range", {"channel": "/imu", "domain_id": DOMAIN}, ["end", "start"]
            ),
        ),
        cells(0, start=5, end=6),
        (RecordRange("/imu", Timestamp(5, DOMAIN), Timestamp(6, DOMAIN)),),
    ),
    (
        "cell under a header",
        (step_template("row_cell", {"column": 2, "column_name": "t [ms]"}, ["row"]),),
        cells(0, row=4),
        (RowCell(4, 2, "t [ms]"),),
    ),
    (
        "cell with no header",
        (step_template("row_cell", {"column": 2}, ["row"]),),
        cells(0, row=4),
        (RowCell(4, 2, NO_HEADER),),
    ),
    (
        "adapter step",
        (step_template("sqlite:row", {"table": "messages"}, ["rowid"]),),
        cells(0, rowid=17),
        (adapter_locator("sqlite:row", {"rowid": 17, "table": "messages"}),),
    ),
]


@pytest.mark.parametrize(
    ("steps", "row", "locator"), [shape[1:] for shape in SHAPES], ids=[s[0] for s in SHAPES]
)
def test_a_rows_evidence_is_the_template_filled_from_its_columns(
    steps: tuple[StepTemplate, ...], row: dict[str, object], locator: tuple[Locator, ...]
) -> None:
    hoisted = series(*steps)
    assert hoisted.evidence(row) == EvidenceRef(LOG, locator)
    assert set(hoisted.columns) == set(row)
    line = canonical_json.dumps(hoisted.to_json())
    assert series_provenance_from_json(canonical_json.loads(line)) == hoisted


@given(offset=st.integers(0, 2**62 - 1), length=st.integers(0, 2**62 - 1))
def test_a_rows_locator_is_exactly_what_its_columns_hold(offset: int, length: int) -> None:
    row = cells(0, offset=offset, length=length)
    assert series(BYTES).evidence(row) == EvidenceRef(LOG, (ByteRange(offset, length),))


@pytest.mark.parametrize(
    ("build", "error"),
    [
        (lambda: step_template("frame", {}, ["ref"]), ValueError),  # a frame is not a place
        (lambda: step_template("no_such_step", {}, ["offset"]), ValueError),
        (lambda: step_template("Bad:kind", {}, ["offset"]), ValueError),
        (lambda: step_template("byte_range", {}, ["offset"]), ValueError),  # length missing
        (lambda: step_template("byte_range", {}, ["length", "offset", "size"]), ValueError),
        (lambda: step_template("byte_range", {"offset": 0}, ["length", "offset"]), ValueError),
        (lambda: step_template("byte_range", {}, ["kind", "length", "offset"]), ValueError),
        (lambda: step_template("byte_range", {}, ["Length", "offset"]), ValueError),
        (lambda: StepTemplate("byte_range", (), ("offset", "length")), ValueError),  # unsorted
        (lambda: StepTemplate("byte_range", (("offset", 0), ("length", 1)), ()), ValueError),
        (lambda: StepTemplate("byte_range", (("length", [1]),), ("offset",)), TypeError),  # type: ignore[arg-type]
        (lambda: step_template("byte_range", {"offset": -1, "length": 1}), ValueError),
        (lambda: step_template("sqlite:row", {"table": float("nan")}, ["rowid"]), ValueError),
    ],
)
def test_a_template_that_could_not_cite_anything_is_refused(build: Any, error: type) -> None:
    with pytest.raises(error):
        build()


@pytest.mark.parametrize(
    "row",
    [
        {},  # no locator columns at all
        cells(0, offset=8),  # length missing
        cells(0, offset=None, length=4),  # a null locator
        cells(0, offset="8", length=4),  # text where the step has an integer
        cells(0, offset=True, length=4),
        cells(0, offset=-8, length=4),
        cells(0, offset=8.0, length=4),
    ],
)
def test_a_row_whose_locator_columns_do_not_fill_the_template_is_an_error(
    row: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        series(BYTES).evidence(row)


def test_every_row_must_cite_its_own_sample() -> None:
    whole_file = step_template("byte_range", {"offset": 0, "length": 72})
    with pytest.raises(ValueError, match="varies per row"):
        series(whole_file)
    with pytest.raises(ValueError, match="non-empty"):
        series()
    with pytest.raises(TypeError, match="template"):
        SeriesProvenance(LOG, (ByteRange(0, 1),), OBSERVED)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="content id"):
        SeriesProvenance("rec:sha256:" + "0" * 64, (BYTES,), OBSERVED)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="derived/"):
        SeriesProvenance(LOG, (BYTES,), "inferred")  # type: ignore[arg-type]


def test_hoisted_provenance_json_is_read_strictly() -> None:
    step = BYTES.to_json()
    good = dict(series(BYTES, kind=AssertionKind.STATED).to_json())
    assert series_provenance_from_json(good) == series(BYTES, kind=AssertionKind.STATED)
    for broken in (
        {**good, "transform": "rec:sha256:" + "0" * 64},
        {key: value for key, value in good.items() if key != "source"},
        {**good, "assertion_kind": "inferred"},
        {**good, "locator": []},
        {**good, "locator": [{**step, "per_row": ["offset", "length"]}]},
        {**good, "locator": [{**step, "fixed": [0]}]},
        {**good, "locator": [{**step, "fixed": {"x": {"y": 1}}}]},
        {**good, "locator": [{**step, "extra": 1}]},
    ):
        with pytest.raises(ValueError):
            series_provenance_from_json(broken)


# --- Cells, states and nulls -------------------------------------------------------------------


def test_a_plain_column_is_known_in_every_row_and_never_null() -> None:
    assert cell_state({"value/x": 1.5}, "value/x") is KnowledgeState.KNOWN
    with pytest.raises(ValueError, match="exactly where"):
        cell_state({"value/x": None}, "value/x")
    with pytest.raises(ValueError, match="no column"):
        cell_state({}, "value/x")


@pytest.mark.parametrize(
    ("value", "state", "expected"),
    [
        (0.83, "known", KnowledgeState.KNOWN),
        (None, "unknown", KnowledgeState.UNKNOWN),
        (None, "not_covered", KnowledgeState.NOT_COVERED),
        (None, "not_applicable", KnowledgeState.NOT_APPLICABLE),
    ],
)
def test_a_wrapped_column_is_null_exactly_where_its_state_is_not_known(
    value: object, state: str, expected: KnowledgeState
) -> None:
    assert cell_state({"value/p": value, "state/value/p": state}, "value/p") is expected


@pytest.mark.parametrize(
    ("value", "state"),
    [
        (None, "known"),  # a known value that is not there
        (-1.0, "not_covered"),  # a sentinel kept beside its state: consumers could read it
        (None, "known_absent"),  # needs a citation of the definition: not per row
        (None, "ambiguous"),  # needs its candidates: not per row
        (None, "missing"),
        (None, None),
        (None, 1),
    ],
)
def test_states_that_do_not_fit_one_cell_are_refused(value: object, state: object) -> None:
    with pytest.raises(ValueError):
        cell_state({"value/p": value, "state/value/p": state}, "value/p")


# --- Ordering ----------------------------------------------------------------------------------


def row(seq: int, ticks: int | None) -> dict[str, object]:
    out: dict[str, object] = {SEQ: seq, time_column(0): ticks}
    out[state_column(time_column(0))] = "unknown" if ticks is None else "known"
    return out


def test_rows_sort_by_clock_zero_then_source_order_with_unknown_times_last() -> None:
    rows = [row(0, 30), row(1, None), row(2, 10), row(3, 30), row(4, INT64_MIN), row(5, None)]
    assert [r[SEQ] for r in sorted(rows, key=row_order)] == [4, 2, 0, 3, 1, 5]


@given(
    st.permutations(range(8)),
    st.lists(st.none() | st.integers(INT64_MIN, INT64_MAX), min_size=8, max_size=8),
)
def test_the_stored_order_does_not_depend_on_the_order_rows_arrive_in(
    arrival: list[int], ticks: list[int | None]
) -> None:
    rows = [row(seq, ticks[seq]) for seq in range(8)]
    assert sorted((rows[i] for i in arrival), key=row_order) == sorted(rows, key=row_order)


@pytest.mark.parametrize("seq", [None, -1, True, 1.0, "1", INT64_MAX + 1])
def test_seq_is_a_non_negative_int64(seq: object) -> None:
    with pytest.raises(ValueError):
        seq_of({SEQ: seq})
    with pytest.raises(ValueError, match="no 'seq'"):
        seq_of({})


# --- The whole row -----------------------------------------------------------------------------


REQUIRED = (SEQ, time_column(0), *series(BYTES).columns)
GOOD: dict[str, object] = {
    SEQ: 0,
    time_column(0): 10,
    **cells(0, offset=8, length=40),
    "value/voltage": 24.1,
    "value/percentage": None,
    "state/value/percentage": "unknown",
    "state/time/0": "known",
}


def test_a_row_may_carry_values_and_their_states_on_top_of_the_required_columns() -> None:
    check_columns(GOOD, REQUIRED)


@pytest.mark.parametrize(
    "change",
    [
        {"mystery": 1},  # outside every namespace
        {"value/": 1},  # a value with no name
        {"state/seq": "known"},  # only times and values have states
        {"state/locator/0/offset": "known"},
        {"state/state/value/percentage": "known"},
        {"state/value/other": "unknown"},  # the state of a column that is not there
        {"value/voltage": None},  # null without a state column
    ],
)
def test_columns_outside_the_contract_are_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        check_columns({**GOOD, **change}, REQUIRED)


def test_a_missing_required_column_is_named() -> None:
    incomplete = {key: value for key, value in GOOD.items() if key != locator_column(0, "offset")}
    with pytest.raises(ValueError, match="locator/0/offset"):
        check_columns(incomplete, REQUIRED)
