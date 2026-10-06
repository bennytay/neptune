"""graph-schema 2.2.0's ``declared_value`` literal in a frozen snapshot (Memory ADR 0025).

The pack compiler reads Memory's acceptance snapshot, which holds declared configuration values,
work-order events and stated causes, and renders a declared value as declared. A malformed
declared value is refused with its pointer, like any other literal.
"""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any, Final

import pytest

from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.snapshot import GRAPH_SCHEMA_PIN, read_snapshot
from neptune_deploy.packs.text import claim_object

REPO: Final = Path(__file__).resolve().parents[3]
SNAPSHOT: Final = (
    REPO / "packages" / "neptune-memory" / "tests" / "fixtures" / "acceptance_corpus.graph.json.gz"
)


def document() -> dict[str, Any]:
    return json.loads(gzip.decompress(SNAPSHOT.read_bytes()))  # type: ignore[no-any-return]


def _declared(doc: dict[str, Any]) -> int:
    return next(
        i
        for i, c in enumerate(doc["claims"])
        if c["object"].get("datatype") == "declared_value"
        and c["object"]["value"]["path"] == ["reprojection_error"]
    )


def test_the_acceptance_snapshot_reads_with_its_declared_values() -> None:
    doc = document()
    assert doc["graph_schema"] == GRAPH_SCHEMA_PIN == "2.2.0"
    snapshot = read_snapshot(doc)
    assert not snapshot.unread
    predicates = {c.predicate for c in snapshot.claims}
    assert {"declared_value", "stated_cause", "has_name"} <= predicates


def test_a_declared_value_renders_as_declared() -> None:
    doc = document()
    literal = doc["claims"][_declared(doc)]["object"]
    assert claim_object(literal) == 'declared "reprojection_error" = [1.86]'
    text: dict[str, Any] = {
        "datatype": "declared_value",
        "kind": "literal",
        "unit": {"knowledge": "not_applicable"},
    }
    board = {**text, "value": {"path": ["board"], "type": "text", "value": "ChArUco 7x5"}}
    assert claim_object(board) == 'declared "board" = "ChArUco 7x5"'


@pytest.mark.parametrize(
    "value",
    [
        {"path": [], "type": "text", "value": "x"},
        {"path": [-1], "type": "text", "value": "x"},
        {"path": ["a"], "type": "real", "value": 1},
        {"path": ["a"], "type": "reals", "value": []},
        {"path": ["a"], "type": "boolean", "value": "true"},
        {"path": ["a"], "type": "complex", "value": "x"},
        {"path": ["a"], "type": "text"},
    ],
)
def test_a_malformed_declared_value_is_refused(value: dict[str, Any]) -> None:
    doc = copy.deepcopy(document())
    doc["claims"][_declared(doc)]["object"]["value"] = value
    with pytest.raises(PackError, match="snapshot_malformed"):
        read_snapshot(doc)
