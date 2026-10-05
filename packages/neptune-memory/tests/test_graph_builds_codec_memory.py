"""The graph document's ``builds`` (graph-schema 1.9.0, ADR 0016): round trip and hostile input."""

import json
from copy import deepcopy
from typing import Any

import pytest

from memory_golden_fixtures import PUBLISHED, published
from neptune.identity import canonical_json
from neptune_memory.schema.codec import GraphDocument, build_from_json, graph_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import resolve

RAW: dict[str, Any] = json.loads((PUBLISHED / "golden" / "graph.json").read_text(encoding="utf-8"))


def test_the_golden_graph_carries_one_build_per_consolidator_and_transaction() -> None:
    golden = published()
    keys = [(b.recorded_at, b.consolidator_id) for b in golden.builds]
    assert keys == sorted(set(keys)) and keys
    every = {i for b in golden.builds for i in b.claims}
    assert every <= {c.id for c in golden.resolution.claims}


def test_builds_round_trip_byte_for_byte() -> None:
    document = graph_from_json(RAW)
    assert canonical_json.dumps(document.to_json()) == canonical_json.dumps(RAW)
    for build in document.builds:
        assert build_from_json(build.to_json()) == build


def test_resolving_the_document_with_its_builds_reproduces_it() -> None:
    golden = published()
    priorities = golden.resolver_config["priorities"]
    assert isinstance(priorities, dict)
    ranks = {cid: int(str(p)) for cid, p in priorities.items()}
    again = resolve(golden.resolution.claims, CORE_PREDICATES, ranks, golden.builds)
    assert again == golden.resolution


def test_a_document_without_builds_writes_no_builds_key() -> None:
    golden = published()
    bare = GraphDocument(golden.resolution, golden.resolver_config, golden.head)
    assert "builds" not in bare.to_json()
    assert ReferenceReader(bare).head == ledger_tx(golden.head)


def _widest(raw: dict[str, Any]) -> dict[str, Any]:
    widest: dict[str, Any] = max(raw["builds"], key=lambda b: len(b["claims"]))
    assert len(widest["claims"]) > 1
    return widest


def _mutated(change: Any) -> dict[str, Any]:
    raw = deepcopy(RAW)
    change(raw)
    return raw


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.update(builds=[]),
        lambda raw: raw.update(builds=list(reversed(raw["builds"]))),
        lambda raw: raw["builds"].append(raw["builds"][-1]),
        lambda raw: raw["builds"][0].update(recorded_at=raw["head"] + 1),
        lambda raw: raw["builds"][0].update(claims=["claim:sha256:" + "f" * 64]),
        lambda raw: _widest(raw).update(claims=list(reversed(_widest(raw)["claims"]))),
        lambda raw: raw["builds"][0].update(extra=1),
        lambda raw: raw["builds"][0].pop("version"),
        lambda raw: raw["builds"][0].update(consolidator_id="memory.supersede"),
        lambda raw: raw["builds"][0].update(recorded_at="1"),
        lambda raw: raw.update(builds={}),
    ],
    ids=[
        "empty",
        "unordered",
        "twice",
        "after-head",
        "dangling",
        "unsorted-claims",
        "unknown-key",
        "missing-key",
        "resolver",
        "string-tx",
        "not-a-list",
    ],
)
def test_hostile_builds_are_refused(change: Any) -> None:
    raw = _mutated(change)
    with pytest.raises((TypeError, ValueError)):
        graph_from_json(raw)
