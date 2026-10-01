"""The contract's laws as checks (ADR 0024 §6): each broken law is caught before storing."""

import importlib.util
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.check import (
    check_chunk_output,
    check_plan,
    check_record,
    check_source_output,
)
from neptune.adapters.contract import (
    ChunkOutput,
    ContractError,
    Plan,
    configure,
    make_chunk,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.text import DESCRIPTOR, TextAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, Severity
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    Provenance,
    Span,
    adapter_locator,
)
from neptune.model.run import Stream
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn
from neptune.model.source import LocalPath
from neptune.model.world import DocumentBlock

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "adapters"
TEXT: Final = BytesReader(b"first paragraph\n\nsecond\n")
OTHER: Final = BytesReader(b"another source\n")


def _tally() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tally_adapter", FIXTURES / "tally_adapter.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _tally()
TALLY_SOURCE: Final = BytesReader(b"TALLY1\n100 5\n200 6\n300 7\n")


def text_output() -> SourceOutput:
    return ingest_source(TextAdapter(chunk_bytes=4), TEXT)


def tally_output() -> SourceOutput:
    return ingest_source(TALLY.TallyAdapter(), TALLY_SOURCE)


def check(output: SourceOutput, outputs: list[ChunkOutput], source: Any = TEXT) -> None:
    descriptor = TALLY.DESCRIPTOR if output.config.transform.adapter_id == "tally" else DESCRIPTOR
    check_source_output(descriptor, source, output.config, output.plan, outputs)


def a_block(output: SourceOutput) -> DocumentBlock:
    return next(r for r in output.records() if isinstance(r, DocumentBlock))


# --- What passes -------------------------------------------------------------------------------


def test_well_behaved_adapters_pass_every_check() -> None:
    text, tally = text_output(), tally_output()
    assert len(text.plan.chunks) == 3
    assert {r.kind for r in tally.records()} == {"run", "stream", "timestamp_domain"}
    assert [batch.length for batches in tally.series().values() for batch in batches] == [0, 2, 1]


# --- Records -----------------------------------------------------------------------------------


def test_a_record_citing_another_source_is_refused() -> None:
    output = text_output()
    block = a_block(output)
    evidence = EvidenceRef(OTHER.content_id, (Span(0, 5),))
    foreign = replace(
        block,
        id=evidence_record_id(DocumentBlock.kind, evidence, output.config.transform),
        provenance=replace(block.provenance, evidence=evidence),
    )
    with pytest.raises(ContractError, match="not its source"):
        check_record(DESCRIPTOR, TEXT, output.config, foreign)


def test_a_record_with_an_id_from_other_evidence_is_refused() -> None:
    output = text_output()
    block = a_block(output)
    moved = replace(block.provenance, evidence=EvidenceRef(TEXT.content_id, (Span(0, 1),)))
    with pytest.raises(ContractError, match="id does not match"):
        check_record(DESCRIPTOR, TEXT, output.config, replace(block, provenance=moved))


def test_a_record_from_another_transform_is_refused() -> None:
    output = text_output()
    other = configure(DESCRIPTOR, {"block_rule": "line"})
    with pytest.raises(ContractError, match="not produced by transform"):
        check_record(DESCRIPTOR, TEXT, other, a_block(output))


def test_a_nested_provenance_naming_another_transform_is_refused() -> None:
    output = text_output()
    block = a_block(output)
    stranger = configure(DESCRIPTOR, {"block_rule": "line"}).transform.id
    nested = Provenance(block.provenance.evidence, stranger, AssertionKind.OBSERVED)
    with pytest.raises(ContractError, match="provenance names transform"):
        check_record(DESCRIPTOR, TEXT, output.config, replace(block, text=Known("x", nested)))


def test_a_record_of_an_undeclared_kind_is_refused() -> None:
    output = text_output()
    narrow = replace(DESCRIPTOR, record_kinds=("document_record",))
    with pytest.raises(ContractError, match="does not declare kind"):
        check_record(narrow, TEXT, output.config, a_block(output))


def test_an_undeclared_adapter_locator_step_is_refused() -> None:
    output = text_output()
    block = a_block(output)
    step = adapter_locator("text:line", {"index": 0})
    evidence = EvidenceRef(TEXT.content_id, (step,))
    stepped = replace(
        block,
        id=evidence_record_id(DocumentBlock.kind, evidence, output.config.transform),
        provenance=replace(block.provenance, evidence=evidence),
    )
    with pytest.raises(ContractError, match="undeclared locator step"):
        check_record(DESCRIPTOR, TEXT, output.config, stepped)


# --- Findings ----------------------------------------------------------------------------------


def finding(output: SourceOutput, **changes: Any) -> Any:
    arguments: dict[str, Any] = {
        "code": "text.invalid_utf8",
        "category": FindingCategory.UNREPRESENTABLE,
        "severity": Severity.WARNING,
        "subject": EvidenceRef(TEXT.content_id, (ByteRange(0, 5),)),
        "transform": output.config.transform,
        "message": "a problem",
    }
    return ingest_finding(**{**arguments, **changes})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"code": "text.undeclared"}, "not declared"),
        ({"subject": LocalPath("a.txt")}, "cites bytes"),
        ({"subject": EvidenceRef(OTHER.content_id, (ByteRange(0, 5),))}, "not its source"),
        ({"related": (EvidenceRef(OTHER.content_id, (ByteRange(0, 1),)),)}, "not its source"),
        ({"transform": configure(DESCRIPTOR, {"block_rule": "line"}).transform}, "names transform"),
    ],
)
def test_a_bad_finding_is_refused(changes: dict[str, Any], message: str) -> None:
    output = text_output()
    bad = ChunkOutput(findings=(finding(output, **changes),))
    with pytest.raises(ContractError, match=message):
        check_chunk_output(DESCRIPTOR, TEXT, output.config, output.plan.chunks[0], bad)


def test_a_plan_finding_is_checked_too() -> None:
    output = text_output()
    plan = Plan(output.plan.chunks, (finding(output, code="text.undeclared"),))
    with pytest.raises(ContractError, match="not declared"):
        check_plan(DESCRIPTOR, TEXT, output.config, plan)


# --- Plans and sources -------------------------------------------------------------------------


def test_a_chunk_of_another_source_or_transform_is_refused() -> None:
    output = text_output()
    foreign = make_chunk(OTHER, output.config, {"part": "document"}, 0)
    with pytest.raises(ContractError, match="not of this source"):
        check_plan(DESCRIPTOR, TEXT, output.config, Plan((foreign,)))
    other = configure(DESCRIPTOR, {"block_rule": "line"})
    with pytest.raises(ContractError, match="not of this source"):
        check_plan(DESCRIPTOR, TEXT, other, output.plan)


def test_every_chunk_has_exactly_one_output() -> None:
    output = text_output()
    with pytest.raises(ContractError, match="outputs"):
        check(output, list(output.outputs[:-1]))


def test_a_record_emitted_by_two_chunks_is_refused() -> None:
    output = text_output()
    outputs = list(output.outputs)
    outputs[2] = replace(outputs[2], records=outputs[1].records)
    with pytest.raises(ContractError, match="emitted twice"):
        check(output, outputs)


def test_saying_nothing_about_a_source_is_refused() -> None:
    output = text_output()
    with pytest.raises(ContractError, match="said nothing"):
        check(output, [ChunkOutput() for _ in output.outputs])


# --- Series ------------------------------------------------------------------------------------


def batches(output: SourceOutput) -> list[SeriesBatch]:
    return [batch for found in output.series().values() for batch in found]


def with_series(output: SourceOutput, chunk: int, *series: SeriesBatch) -> list[ChunkOutput]:
    outputs = list(output.outputs)
    outputs[chunk] = replace(outputs[chunk], series=series)
    return outputs


def test_a_batch_for_a_stream_the_source_did_not_declare_is_refused() -> None:
    output = tally_output()
    first = batches(output)[1]
    stranger = replace(first, stream=a_block(text_output()).id)
    with pytest.raises(ContractError, match="not a stream of this source"):
        check(output, with_series(output, 1, stranger), TALLY_SOURCE)


def test_a_row_that_breaks_its_streams_contract_is_refused() -> None:
    output = tally_output()
    first = batches(output)[1]
    columns = tuple(
        replace(c, values=(-1, 13)) if c.name == "locator/0/offset" else c for c in first.columns
    )
    with pytest.raises(ContractError, match="offset must be in"):
        check(output, with_series(output, 1, replace(first, columns=columns)), TALLY_SOURCE)


def test_a_seq_written_twice_is_refused() -> None:
    output = tally_output()
    _, _, second = batches(output)
    repeated = replace(
        second,
        columns=tuple(replace(c, values=(0,)) if c.name == "seq" else c for c in second.columns),
    )
    with pytest.raises(ContractError, match="seq 0 appears twice"):
        check(output, with_series(output, 2, repeated), TALLY_SOURCE)


def test_batches_of_one_stream_must_agree_on_their_columns() -> None:
    output = tally_output()
    _, _, second = batches(output)
    extra = SeriesColumn("value/extra", ColumnType.INT8, (1,))
    widened = replace(second, columns=(*second.columns, extra))
    with pytest.raises(ContractError, match="disagree"):
        check(output, with_series(output, 2, widened), TALLY_SOURCE)


def test_a_stream_without_a_batch_in_its_own_chunk_is_refused() -> None:
    output = tally_output()
    with pytest.raises(ContractError, match="no series batch in the chunk that declares it"):
        check(output, with_series(output, 0), TALLY_SOURCE)


def test_an_empty_batch_types_a_stream_with_no_samples() -> None:
    output = ingest_source(TALLY.TallyAdapter(), BytesReader(b"TALLY1\n"))
    ((empty,),) = output.series().values()
    assert empty.length == 0
    assert [name for name, _, _ in empty.schema()] == sorted(TALLY.COLUMNS)


def test_a_stream_record_is_what_series_rows_are_checked_against() -> None:
    output = tally_output()
    (stream,) = (r for r in output.records() if isinstance(r, Stream))
    for batch in batches(output):
        for row in batch.rows():
            stream.check_row(row)
