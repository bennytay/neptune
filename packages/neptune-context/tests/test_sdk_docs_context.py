"""docs/sdk.md's ten worked queries in SDK form: each block runs and sends the query it states."""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

import pytest

import neptune_context.query as q
from neptune_context.query import Query, query_id
from neptune_context.sdk import Client
from sdk_testing_context import answering

PACKAGE = Path(__file__).resolve().parents[1]
DOC = PACKAGE / "docs" / "sdk.md"
ADR = PACKAGE / "docs" / "adr" / "0002-query-language.md"
_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
_ID_LINE = re.compile(r"^# query_id: (query:sha256:[0-9a-f]{64})$")


def _section(path: Path, start: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return _BLOCK.findall(text.split(start, 1)[1].split("\n## ", 1)[0])


DOC_BLOCKS = _section(DOC, "## The ten worked queries in SDK form")
ADR_BLOCKS = _section(ADR, "## Worked queries")


class Echo:
    """Answers any query with a valid packet addressed to it, and remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[Query] = []

    def query(self, query: Query) -> object:
        self.asked.append(query)
        return answering(query)

    def hydrate(self, evidence: object, *, as_of: int | None) -> object:
        raise AssertionError("not used")


def test_the_page_has_ten_worked_queries_matching_the_adrs() -> None:
    assert len(DOC_BLOCKS) == len(ADR_BLOCKS) == 10
    for doc, adr in zip(DOC_BLOCKS, ADR_BLOCKS, strict=True):
        assert doc.startswith(adr), "the SDK page must keep the ADR's query verbatim"


@pytest.mark.parametrize("number", range(1, 11), ids=[f"q{n:02d}" for n in range(1, 11)])
def test_each_block_sends_exactly_its_stated_query(number: int) -> None:
    block = DOC_BLOCKS[number - 1]
    match = _ID_LINE.match(block.splitlines()[0])
    assert match, "first line is '# query_id: query:sha256:<hex>'"
    engine = Echo()
    namespace: dict[str, object] = {name: getattr(q, name) for name in q.__all__}
    namespace["Fraction"] = Fraction
    namespace["client"] = Client(engine)  # type: ignore[arg-type]
    exec(block, namespace)  # the page's own code, checked in alongside this test
    assert engine.asked, "the block sends a query"
    assert {query_id(asked) for asked in engine.asked} == {match.group(1)}
    assert namespace["packet"].query_id == match.group(1)  # type: ignore[attr-defined]


def test_q04_is_sent_both_ways_and_the_ids_agree() -> None:
    assert "client.diff(" in DOC_BLOCKS[3]
    engine = Echo()
    namespace: dict[str, object] = {name: getattr(q, name) for name in q.__all__}
    namespace["client"] = Client(engine)  # type: ignore[arg-type]
    exec(DOC_BLOCKS[3], namespace)
    assert len(engine.asked) == 2
    assert engine.asked[0] == engine.asked[1]
