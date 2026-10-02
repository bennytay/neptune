"""Golden examples for the graph-schema contract, produced by neptune-memory itself.

``scripts/contracts.py bump graph-schema <version>`` runs this file and stores its output under
``v<version>/golden/``. It reads the compiler's four worked examples (a drone, a manipulator, a
mobile robot and a quadruped; ``tests/fixtures/model/``) as plain record lines, hands them to
``neptune_memory.contract.golden.build_golden`` (a stub Ledger over five transactions, the
golden consolidators, the identity policy and the superseding resolver), and keeps the resulting
graph document, the core vocabulary and the reference reader's answers to four queries at the
head (``result.*.json``: one per ``MemoryReader`` result type). Nothing is hand-written, and the
same code gives the same bytes; ``packages/neptune-memory/tests/test_golden_graph_memory.py``
re-runs it.

Prints one JSON object: golden file name -> {"target": JSON pointer into the schema, "value"}.
"""

import json
import sys
from pathlib import Path
from typing import Any, Final

from neptune_memory.contract.golden import WORKED_EXAMPLES, build_golden
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.reader import EpisodeFilter, result_to_json
from neptune_memory.schema.reference import ReferenceReader

EXAMPLES: Final = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "model"


def worked_examples(root: Path = EXAMPLES) -> dict[str, list[Any]]:
    """Each worked example's record lines, in file-name then line order."""
    return {
        name: [
            json.loads(line)
            for path in sorted((root / name / "records").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        for name in WORKED_EXAMPLES
    }


def _run_recorded_by(golden: GraphDocument, machine: str) -> NodeRef:
    """The run an operator or log says ``machine`` recorded."""
    (run,) = {
        c.subject
        for c in golden.resolution.claims
        if c.predicate == "recorded_by"
        and isinstance(c.object, NodeRef)
        and c.object.node_id == machine
    }
    return run


def goldens() -> dict[str, dict[str, Any]]:
    golden = build_golden(worked_examples())
    reader, head = ReferenceReader(golden), golden.head
    drone = _run_recorded_by(golden, "asset-tag:UAV-0043")
    manipulator = _run_recorded_by(golden, "asset-tag:ARM-06")
    return {
        "graph.json": {"target": "#/$defs/Graph", "value": golden.to_json()},
        "result.claims.json": {
            "target": "#/$defs/ClaimsResult",
            "value": reader.claims(manipulator, "recorded_by", head).to_json(),
        },
        "result.episodes.json": {
            "target": "#/$defs/EpisodesResult",
            "value": result_to_json(reader.episodes(EpisodeFilter(as_of=head)), list),
        },
        "result.neighbours.json": {
            "target": "#/$defs/NeighboursResult",
            "value": reader.neighbours(drone, 2, head).to_json(),
        },
        "result.node.json": {
            "target": "#/$defs/NodeResult",
            "value": result_to_json(reader.node(drone, head), lambda view: view.to_json()),
        },
        "vocabulary.json": {
            "target": "#/$defs/PredicateRegistry",
            "value": CORE_PREDICATES.to_json(),
        },
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(goldens(), sort_keys=True, ensure_ascii=False))
