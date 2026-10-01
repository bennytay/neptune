"""Golden examples for the graph-schema contract, produced by neptune-memory itself.

``scripts/contracts.py bump graph-schema <version>`` runs this file and stores its output under
``v<version>/golden/``. It reads the compiler's four worked examples (a drone, a manipulator, a
mobile robot and a quadruped; ``tests/fixtures/model/``) as plain record lines, hands them to
``neptune_memory.contract.golden.build_golden`` (a stub Ledger over three transactions, the
golden consolidators, the identity policy and the superseding resolver), and keeps the resulting
graph document and the core vocabulary. Nothing is hand-written, and the same code gives the
same bytes; ``packages/neptune-memory/tests/test_golden_graph_memory.py`` re-runs it.

Prints one JSON object: golden file name -> {"target": JSON pointer into the schema, "value"}.
"""

import json
import sys
from pathlib import Path
from typing import Any, Final

from neptune_memory.contract.golden import WORKED_EXAMPLES, build_golden
from neptune_memory.schema.predicates import CORE_PREDICATES

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


def goldens() -> dict[str, dict[str, Any]]:
    return {
        "graph.json": {
            "target": "#/$defs/Graph",
            "value": build_golden(worked_examples()).to_json(),
        },
        "vocabulary.json": {
            "target": "#/$defs/PredicateRegistry",
            "value": CORE_PREDICATES.to_json(),
        },
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(goldens(), sort_keys=True, ensure_ascii=False))
