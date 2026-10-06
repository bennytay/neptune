"""Upstream vocabularies come from the pinned registry versions, not live code (ADR 0006 §9).

The snapshot matches the registry at the pins, and an upstream addition that Context has not
pinned (a Memory predicate or node type, a Ledger thread kind) changes nothing Context publishes:
not the query schema, not the query-packet export, not a query id, not the planner's prompt.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from neptune_context import pinned, pins
from neptune_context.query.validate import PREDICATES, SUBJECT_KINDS

PACKAGE = Path(__file__).resolve().parents[1]
CONTRACTS = PACKAGE.parents[1] / "contracts"
SNAPSHOT = PACKAGE / "src" / "neptune_context" / "pinned.json"
WORKED = PACKAGE / "tests" / "golden" / "worked-queries"


def test_snapshot_is_fresh_against_the_registry_at_the_pins() -> None:
    assert SNAPSHOT.read_bytes() == pinned.build(CONTRACTS), (
        "regenerate: uv run python -m neptune_context.pinned contracts "
        "packages/neptune-context/src/neptune_context/pinned.json"
    )


def test_snapshot_records_the_pinned_versions() -> None:
    snapshot = json.loads(SNAPSHOT.read_bytes())
    assert snapshot["graph-schema"]["version"] == pins.GRAPH_SCHEMA_VERSION
    assert snapshot["catalog-api"]["version"] == pins.CATALOG_API_VERSION


def test_validate_reads_the_pinned_vocabularies() -> None:
    vocabulary = json.loads(
        (
            CONTRACTS
            / "graph-schema"
            / f"v{pins.GRAPH_SCHEMA_VERSION}"
            / "golden"
            / "vocabulary.json"
        ).read_bytes()
    )
    assert {p["name"] for p in vocabulary["predicates"]} == PREDICATES
    assert pinned.node_types() | pinned.thread_kinds() == SUBJECT_KINDS


def test_defs_are_a_copy() -> None:
    defs = pinned.graph_schema_defs()
    defs.clear()
    assert "NodeType" in pinned.graph_schema_defs()


def test_main_writes_the_snapshot_and_checks_usage(tmp_path: Path) -> None:
    out = tmp_path / "pinned.json"
    assert pinned.main([str(CONTRACTS), str(out)]) == 0
    assert out.read_bytes() == SNAPSHOT.read_bytes()
    assert pinned.main([]) == 2


# Runs in a fresh interpreter: with "patched", Memory gains a predicate and a node type and the
# Ledger a thread kind before Context is imported, as an upstream minor release would. The node
# type is added both to the exported schema and to Memory's ``NodeType`` enum itself, so a
# Context module that read the live enum (``query/validate.py``'s ``SUBJECT_KINDS``, say) would
# change what Context publishes even while live Memory is not ahead of the pin.
_PROBE = """
import enum, hashlib, json, sys
from pathlib import Path
from typing import Literal, get_args

import neptune_ledger.api as ledger
import neptune_memory.schema.export as export
import neptune_memory.schema.nodes as nodes
import neptune_memory.schema.predicates as predicates
from neptune_memory.schema.nodes import NodeType

if sys.argv[1] == "patched":
    predicates.CORE_PREDICATES = predicates.CORE_PREDICATES.extend(
        predicates.PredicateSpec(
            "novel_predicate", 1, frozenset({NodeType.RUN}), frozenset({NodeType.SITE}),
            predicates.Cardinality.MANY, "a predicate Memory added after Context's pin",
        )
    )
    live = export.graph_schema

    def widened():
        schema = live()
        schema["$defs"]["NodeType"]["enum"].append("novel_node")
        return schema

    export.graph_schema = widened
    nodes.NodeType = enum.StrEnum(
        "NodeType", {**{m.name: m.value for m in NodeType}, "NOVEL_NODE": "novel_node"}
    )
    assert "novel_node" in {str(m) for m in nodes.NodeType}
    ledger.ThreadKind = Literal[(*get_args(ledger.ThreadKind), "novel_thread")]
    assert "novel_predicate" in {s.name for s in predicates.CORE_PREDICATES.specs}

from neptune_context.contract import contract_schema, query_id
from neptune_context.query import Query, from_json
from neptune_context.query.plan.prompt import output_schema, system_prompt, template_sha256
from neptune_context.query.schema import schema_bytes
from neptune_context.query.validate import PREDICATES, SUBJECT_KINDS

def sha(data):
    return hashlib.sha256(data).hexdigest()

ids = []
for path in sorted(Path(sys.argv[2]).glob("q*.json")):
    query = from_json(json.loads(path.read_bytes()))
    assert isinstance(query, Query), path
    ids.append(query_id(query))
print(json.dumps({
    "contract": sha(json.dumps(contract_schema(), sort_keys=True).encode()),
    "ids": ids,
    "kinds": sorted(SUBJECT_KINDS),
    "planner": [
        sha(system_prompt().encode()),
        sha(json.dumps(output_schema(), sort_keys=True).encode()),
        template_sha256(),
    ],
    "predicates": sorted(PREDICATES),
    "query_schema": sha(schema_bytes()),
}))
"""


def _probe(mode: str) -> dict[str, object]:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, mode, str(WORKED)],
        capture_output=True,
        check=True,
        text=True,
    )
    result: dict[str, object] = json.loads(done.stdout)
    return result


def test_an_unpinned_upstream_addition_changes_nothing_context_publishes() -> None:
    baseline, patched = _probe("baseline"), _probe("patched")
    assert patched == baseline
    assert "novel_predicate" not in baseline["predicates"]  # type: ignore[operator]
    assert baseline["ids"]
