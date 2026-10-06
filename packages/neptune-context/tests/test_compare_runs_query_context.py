"""The ``compare_runs`` explain clause in the query language (ADR 0015): read, written, refused."""

from __future__ import annotations

from typing import Any

import pytest

from neptune_context.mcp.server import COMPARE_ITEMS, COMPARE_TOKENS, query_from_arguments
from neptune_context.query import CompareRuns, from_json, to_json
from neptune_context.sdk import SdkError

RUN_A = {"kind": "run", "declared_id": "record:rec:sha256:" + "a" * 64, "same_as_depth": 0}
RUN_B = {"kind": "run", "declared_id": "record:rec:sha256:" + "b" * 64, "same_as_depth": 0}


def _compare(before: dict[str, Any], after: dict[str, Any]) -> Any:
    return query_from_arguments(
        "neptune_compare_runs", {"before": before, "after": after, "include_inferred": False}
    )


def test_a_comparison_round_trips() -> None:
    query = _compare(RUN_A, RUN_B)
    (clause,) = query.explain
    assert isinstance(clause, CompareRuns)
    assert from_json(to_json(query)) == query
    assert to_json(query)["explain"] == [{"after": RUN_B, "before": RUN_A, "kind": "compare_runs"}]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({**RUN_A, "kind": "machine"}, RUN_B),  # a machine is not a run
        ({"kind": "run", "same_as_depth": 0}, RUN_B),  # every run, not one
        (RUN_A, RUN_A),  # a run against itself
    ],
)
def test_a_comparison_of_anything_but_two_runs_is_refused(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    with pytest.raises(SdkError):
        _compare(before, after)


def test_the_tool_builds_the_clause_with_its_default_budget() -> None:
    query = query_from_arguments(
        "neptune_compare_runs", {"before": RUN_A, "after": RUN_B, "include_inferred": False}
    )
    assert (query.budget.items, query.budget.tokens) == (COMPARE_ITEMS, COMPARE_TOKENS)
    assert not query.subjects  # the comparison's claims, nothing else
    with pytest.raises(SdkError):
        query_from_arguments(
            "neptune_compare_runs",
            {"before": RUN_A, "after": RUN_B, "include_inferred": False, "max_tokens": 0},
        )
