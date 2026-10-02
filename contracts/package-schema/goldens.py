"""Golden examples for the package-schema contract, produced by the compiler itself.

``scripts/contracts.py bump package-schema <version>`` runs this file and stores its output under
``v<version>/golden/``. It packages the compiler's four worked examples (a drone, a manipulator, a
mobile robot and a quadruped; ``tests/fixtures/model/``) with ``neptune.store.package_files`` and
keeps, per example, the manifest, the receipt and the first line of every non-empty record table.
Nothing is hand-written, and the same compiler gives the same bytes.

Prints one JSON object: golden file name -> {"target": JSON pointer into the schema, "value"}.
"""

import json
import sys
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.model.kinds import RECORD_KINDS
from neptune.store.package import MANIFEST, RECEIPT, package_files

EXAMPLES: Final = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "model"
NAMES: Final = (
    "drone",
    "manipulator",
    "mobile_robot",
    "quadruped",
    "warehouse_amr",
    "manipulator_cell",
)
DOCUMENTS: Final = {MANIFEST: "#/$defs/PackageManifest", RECEIPT: "#/$defs/IngestReceipt"}


def records(name: str) -> list[Any]:
    """Every record of one worked example, read through the compiler's strict readers."""
    found: list[Any] = []
    for path in sorted((EXAMPLES / name / "records").glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        found += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return found


def goldens() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in NAMES:
        files = package_files(records(name))
        for document, target in DOCUMENTS.items():
            value = json.loads(files[document])
            out[f"{name}.{document}"] = {"target": target, "value": value}
        for path, content in sorted(files.items()):
            if not (path.startswith("records/") and path.endswith(".jsonl")):
                continue
            lines = content.splitlines()
            if lines:
                kind = path.removeprefix("records/").removesuffix(".jsonl")
                out[f"{name}.record.{kind}.json"] = {"target": "#", "value": json.loads(lines[0])}
    return out


if __name__ == "__main__":
    sys.stdout.write(json.dumps(goldens(), sort_keys=True, ensure_ascii=False))
