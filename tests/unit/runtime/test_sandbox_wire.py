"""The wire: what a sandboxed call returns crosses as JSON and comes back exactly, or not at all.

Round trips use real adapter output (the text reference adapter for records and findings, the
tally fixture for series); series cells are generated for every column type, floats bit for bit.
Every decoder is fed malformed replies, since its input comes from a process that read hostile
bytes.
"""

import importlib.util
import json
import math
import struct
from pathlib import Path
from types import ModuleType
from typing import Any, Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.adapters.check import check_chunk_output
from neptune.adapters.contract import (
    VERIFIED,
    ChunkOutput,
    ContractError,
    InspectResult,
    Plan,
    ProbeReason,
    ProbeResult,
    configure,
)
from neptune.adapters.text import TextAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.ids import RecordId
from neptune.model.series import Cell, ColumnType, SeriesBatch, SeriesColumn
from neptune.runtime import wire
from neptune.runtime.sandbox import ReplyTooLarge

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
STREAM: Final = RecordId("rec:sha256:" + "ab" * 32)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "adapters" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter")


def outputs(adapter: Any, data: bytes) -> tuple[Plan, list[ChunkOutput]]:
    reader = BytesReader(data)
    config = configure(adapter.descriptor)
    plan = adapter.plan(reader, config)
    return plan, [adapter.ingest(reader, chunk, config) for chunk in plan.chunks]


def bits(value: object) -> object:
    """A cell compared by its bytes, so NaN equals itself and -0.0 differs from 0.0."""
    if isinstance(value, float):
        return struct.pack("<d", value)
    if isinstance(value, tuple):
        return tuple(bits(item) for item in value)
    return value


# --- Round trips -------------------------------------------------------------------------------


def test_text_output_and_its_findings_cross_unchanged() -> None:
    data = (FIXTURES / "text" / "corrupted.txt").read_bytes()
    plan, chunks = outputs(TextAdapter(), data)
    assert any(output.findings for output in chunks)  # invalid UTF-8: the adapter's findings
    for output in chunks:
        assert wire.decode_output(wire.encode_output(output)) == output
    assert wire.decode_plan(wire.encode_plan(plan)) == plan


def test_series_output_crosses_unchanged() -> None:
    plan, chunks = outputs(TALLY.TallyAdapter(rows_per_chunk=2), b"TALLY1\n10 1\n20 2\noops\n")
    assert any(output.series for output in chunks)
    for output in chunks:
        again = wire.decode_output(wire.encode_output(output))
        assert again == output
        assert [b.schema() for b in again.series] == [b.schema() for b in output.series]
    assert wire.decode_plan(wire.encode_plan(plan)) == plan


def test_a_probe_result_crosses_unchanged() -> None:
    for result in (
        ProbeResult(0.0, ()),
        ProbeResult(VERIFIED, (ProbeReason("text.utf8", "decodes as UTF-8"),), "2.0"),
    ):
        assert wire.decode_probe(wire.encode_probe(result)) == result


def test_an_inspect_result_crosses_unchanged() -> None:
    """``inspect`` crosses the sandbox for dry runs (MVL-15), summary and findings exact."""
    adapter, corrupted = TextAdapter(), (FIXTURES / "text" / "corrupted.txt").read_bytes()
    result = adapter.inspect(BytesReader(corrupted), configure(adapter.descriptor))
    assert wire.decode_inspect(wire.encode_inspect(result)) == result
    _, chunks = outputs(adapter, corrupted)
    found = tuple(finding for chunk in chunks for finding in chunk.findings)
    assert found  # the fixture's invalid UTF-8
    with_findings = InspectResult(result.summary, found)
    assert wire.decode_inspect(wire.encode_inspect(with_findings)) == with_findings
    for data in (b"{}", b'{"findings": [], "summary": []}', b'{"findings": {}, "summary": {}}'):
        with pytest.raises((ValueError, TypeError, ContractError)):
            wire.decode_inspect(data)


def test_the_encoding_is_deterministic() -> None:
    _, chunks = outputs(TALLY.TallyAdapter(rows_per_chunk=2), b"TALLY1\n10 1\n20 2\n30 3\n")
    for output in chunks:
        assert wire.encode_output(output) == wire.encode_output(output)


def test_a_bounded_decode_refuses_a_reply_packed_with_values() -> None:
    """A reply that stays under the byte cap but holds more containers or elements than a decode
    may build is refused with ``ReplyTooLarge``; the count ignores strings and short-circuits, so
    a reply no larger than the cap is parsed without a scan."""
    cap = wire._MAX_REPLY_NODES
    flat = b"[" + b"0," * (cap + 8) + b"0]"  # millions of bare zeros
    nested = b"[" * (cap + 8) + b"]" * (cap + 8)  # a very deep nesting
    for data in (flat, nested):
        with pytest.raises(ReplyTooLarge):
            wire._loads(data)
    # Commas and brackets inside a string never count: a big text value with many of each is fine.
    text = b'"' + b",[{" * cap + b'"'
    assert len(text) > cap and wire._loads(text) == ",[{" * cap
    assert wire._loads(b"[]") == [] and wire._loads(b'{"a":1}') == {"a": 1}


SCALARS: Final = {
    ColumnType.BOOL: st.booleans(),
    ColumnType.INT8: st.integers(-(2**7), 2**7 - 1),
    ColumnType.INT16: st.integers(-(2**15), 2**15 - 1),
    ColumnType.INT32: st.integers(-(2**31), 2**31 - 1),
    ColumnType.INT64: st.integers(-(2**63), 2**63 - 1),
    ColumnType.UINT8: st.integers(0, 2**8 - 1),
    ColumnType.UINT16: st.integers(0, 2**16 - 1),
    ColumnType.UINT32: st.integers(0, 2**32 - 1),
    ColumnType.UINT64: st.integers(0, 2**64 - 1),
    ColumnType.FLOAT32: st.floats(width=32, allow_nan=True, allow_infinity=True),
    ColumnType.FLOAT64: st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(0, 2**64 - 1).map(lambda n: struct.unpack("<d", struct.pack("<Q", n))[0]),
    ),
    ColumnType.STRING: st.text(),
    ColumnType.BINARY: st.binary(),
}


@st.composite
def batches(draw: st.DrawFn) -> SeriesBatch:
    rows = draw(st.integers(0, 6))
    kind = draw(st.sampled_from(sorted(SCALARS)))
    repeated = draw(st.booleans())
    scalar = SCALARS[kind]
    cell: st.SearchStrategy[Cell] = st.lists(scalar, max_size=3).map(tuple) if repeated else scalar
    values = tuple(draw(st.one_of(st.none(), cell)) for _ in range(rows))
    return SeriesBatch(
        STREAM,
        (
            SeriesColumn("seq", ColumnType.INT64, tuple(range(rows))),
            SeriesColumn("value/x", kind, values, repeated),
        ),
    )


@given(batches())
def test_every_cell_of_every_type_crosses_bit_for_bit(batch: SeriesBatch) -> None:
    (again,) = wire.decode_output(wire.encode_output(ChunkOutput(series=(batch,)))).series
    assert again.schema() == batch.schema()
    for before, after in zip(batch.columns, again.columns, strict=True):
        assert [bits(v) for v in after.values] == [bits(v) for v in before.values]


def test_nan_payloads_and_signed_zero_survive() -> None:
    payload = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000ABC))[0]
    values = (payload, -0.0, math.inf, -math.inf, None)
    batch = SeriesBatch(
        STREAM,
        (
            SeriesColumn("seq", ColumnType.INT64, tuple(range(len(values)))),
            SeriesColumn("value/x", ColumnType.FLOAT64, values),
        ),
    )
    (again,) = wire.decode_output(wire.encode_output(ChunkOutput(series=(batch,)))).series
    assert [bits(v) for v in again.columns[1].values] == [bits(v) for v in values]


# --- Malformed replies -------------------------------------------------------------------------


def encoded_batch(column: dict[str, object]) -> bytes:
    seq = {"name": "seq", "repeated": False, "type": "int64", "values": [0]}
    reply = {
        "findings": [],
        "records": [],
        "series": [{"columns": [seq, column], "stream": STREAM}],
    }
    return json.dumps(reply).encode()


@pytest.mark.parametrize(
    ("column", "why"),
    [
        ({"name": "value/x", "repeated": False, "type": "float64", "values": [1.5]}, "float"),
        ({"name": "value/x", "repeated": False, "type": "float64", "values": ["3FF0"]}, "short"),
        (
            {"name": "value/x", "repeated": False, "type": "float64", "values": ["3FF0" * 4]},
            "upper",
        ),
        ({"name": "value/x", "repeated": False, "type": "binary", "values": ["abc"]}, "odd"),
        ({"name": "value/x", "repeated": False, "type": "binary", "values": ["ab cd"]}, "space"),
        ({"name": "value/x", "repeated": False, "type": "int8", "values": [True]}, "bool"),
        ({"name": "value/x", "repeated": False, "type": "int8", "values": [300]}, "range"),
        ({"name": "value/x", "repeated": False, "type": "bool", "values": [1]}, "int"),
        ({"name": "value/x", "repeated": False, "type": "string", "values": [1]}, "text"),
        ({"name": "value/x", "repeated": True, "type": "int8", "values": [1]}, "unlisted"),
        ({"name": "value/x", "repeated": False, "type": "decimal", "values": [1]}, "type"),
        ({"name": "value/x", "repeated": False, "type": "int8", "values": [1, 2]}, "rows"),
        ({"name": "value/x", "repeated": False, "type": "int8"}, "keys"),
        ({"name": "nonsense", "repeated": False, "type": "int8", "values": [1]}, "name"),
    ],
)
def test_a_malformed_series_reply_is_refused(column: dict[str, object], why: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        wire.decode_output(encoded_batch(column))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"records": [], "series": []}',
        b'{"findings": [], "records": [], "series": [], "extra": []}',
        b'{"findings": {}, "records": [], "series": []}',
        b'{"findings": [], "records": [{"kind": "source_artifact"}], "series": []}',
        b'{"findings": [], "records": [{"kind": "no_such_kind"}], "series": []}',
        b'{"findings": [], "records": [["not", "an", "object"]], "series": []}',
        b'{"findings": [], "records": [{"kind": "document_record"}], "series": []}',
        b'{"findings": [{"kind": "ingest_finding"}], "records": [], "series": []}',
        b'{"findings": [], "records": [], "series": [{"columns": [], "stream": "x"}]}',
        b"[" * 100_000 + b"]" * 100_000,
    ],
)
def test_a_malformed_output_reply_is_refused(data: bytes) -> None:
    with pytest.raises((ValueError, TypeError, KeyError, RecursionError, ContractError)):
        wire.decode_output(data)


def test_a_forged_record_parses_only_to_be_refused_by_the_contract_check() -> None:
    """Two layers: the decoder refuses what is not a record; the job's contract check, which
    every sandboxed output goes through, refuses a record whose id its evidence does not give."""
    adapter, data = TextAdapter(), (FIXTURES / "text" / "notes.txt").read_bytes()
    reader, config = BytesReader(data), configure(TextAdapter().descriptor)
    plan, chunks = outputs(adapter, data)
    record: dict[str, Any] = cast("Any", chunks[0].records[0]).to_json()
    broken = {key: value for key, value in record.items() if key != "provenance"}
    with pytest.raises((ValueError, TypeError, KeyError)):
        wire.decode_output(json.dumps({"findings": [], "records": [broken], "series": []}).encode())
    forged = dict(record) | {"id": "rec:sha256:" + "0" * 64}
    reply = json.dumps({"findings": [], "records": [forged], "series": []}).encode()
    output = wire.decode_output(reply)
    with pytest.raises(ContractError, match="id does not match"):
        check_chunk_output(adapter.descriptor, reader, config, plan.chunks[0], output)


@pytest.mark.parametrize(
    "data",
    [
        b"{}",
        b'{"confidence": 1, "reasons": []}',
        b'{"confidence": 1.5, "reasons": []}',
        b'{"confidence": NaN, "reasons": []}',
        b'{"confidence": 0.5, "reasons": [{"code": "x"}]}',
        b'{"confidence": 0.5, "reasons": [{"code": "bad code", "message": "m"}]}',
        b'{"confidence": 0.5, "reasons": [], "version": 2}',
        b'{"confidence": 0.5, "reasons": [], "extra": 2}',
    ],
)
def test_a_malformed_probe_reply_is_refused(data: bytes) -> None:
    with pytest.raises((ValueError, TypeError, KeyError, ContractError)):
        wire.decode_probe(data)


@pytest.mark.parametrize(
    "data",
    [
        b'{"chunks": []}',
        b'{"chunks": [], "findings": []}',  # a plan has at least one chunk
        b'{"chunks": [{"id": "chunk:sha256:00"}], "findings": []}',
    ],
)
def test_a_malformed_plan_reply_is_refused(data: bytes) -> None:
    with pytest.raises((ValueError, TypeError, KeyError, ContractError)):
        wire.decode_plan(data)


def test_a_plan_whose_chunk_id_does_not_match_its_content_is_refused() -> None:
    plan, _ = outputs(TextAdapter(), b"one\n\ntwo\n")
    chunk = plan.chunks[0].to_json()
    forged = dict(chunk) | {"context": {"forged": True}}
    reply = {"chunks": [forged], "findings": []}
    with pytest.raises(ContractError, match="does not match"):
        wire.decode_plan(json.dumps(reply).encode())
