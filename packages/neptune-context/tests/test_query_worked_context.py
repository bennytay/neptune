"""ADR 0002's ten worked queries: each validates, matches its golden bytes and its stated id.

The ADR is the source: every ```python block under "## Worked queries" defines ``query`` and states
its ``query_id`` on its first line. A changed golden file is a compatibility change (AGENTS.md):
regenerate with ``uv run python packages/neptune-context/tests/test_query_worked_context.py`` only
when the encoding is meant to change, and explain it in the PR.
"""

from __future__ import annotations

import re
import sys
from fractions import Fraction
from pathlib import Path

import jsonschema
import pytest

import neptune_context.query as q
from neptune_context.query import Query, canonical_bytes, loads, query_id, to_json, validate
from neptune_context.query.schema import query_schema

PACKAGE = Path(__file__).resolve().parents[1]
ADR = PACKAGE / "docs" / "adr" / "0002-query-language.md"
GOLDEN = PACKAGE / "tests" / "golden" / "worked-queries"
_ID_LINE = re.compile(r"^# query_id: (query:sha256:(?:[0-9a-f]{64}|TBD))$")
_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _blocks() -> list[str]:
    text = ADR.read_text(encoding="utf-8")
    section = text.split("## Worked queries", 1)[1].split("\n## ", 1)[0]
    return _BLOCK.findall(section)


def _build(block: str) -> Query:
    namespace: dict[str, object] = {name: getattr(q, name) for name in q.__all__}
    namespace["Fraction"] = Fraction
    exec(block, namespace)  # the ADR's own code, checked in alongside this test
    query = namespace["query"]
    assert isinstance(query, Query)
    return query


def _stated_id(block: str) -> str:
    match = _ID_LINE.match(block.splitlines()[0])
    assert match, "a worked query's first line is '# query_id: query:sha256:<hex>'"
    return match.group(1)


WORKED = [(f"q{n:02d}", block) for n, block in enumerate(_blocks(), start=1)]


def test_the_adr_has_ten_worked_queries() -> None:
    assert len(WORKED) == 10


@pytest.mark.parametrize(("name", "block"), WORKED, ids=[name for name, _ in WORKED])
def test_worked_query_is_valid_golden_and_stable(name: str, block: str) -> None:
    query = _build(block)
    assert validate(query) == ()
    data = canonical_bytes(query)
    assert (GOLDEN / f"{name}.json").read_bytes() == data + b"\n"
    assert query_id(query) == _stated_id(block)
    assert loads(data) == query  # the golden bytes read back to the same query
    jsonschema.Draft202012Validator(query_schema()).validate(to_json(query))


def test_worked_queries_are_distinct() -> None:
    ids = {query_id(_build(block)) for _, block in WORKED}
    assert len(ids) == len(WORKED)


def _regenerate() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    text = ADR.read_text(encoding="utf-8")
    for name, block in WORKED:
        query = _build(block)
        (GOLDEN / f"{name}.json").write_bytes(canonical_bytes(query) + b"\n")
        first = block.splitlines()[0]
        text = text.replace(block, block.replace(first, f"# query_id: {query_id(query)}", 1), 1)
    ADR.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    _regenerate()
    sys.exit(0)
