"""Golden examples for the package-schema contract, produced by the compiler itself.

``scripts/contracts.py bump package-schema <version>`` runs this file and stores its output under
``v<version>/golden/``. It packages the compiler's worked examples (a drone, a manipulator, a
mobile robot, a quadruped and two deployments; ``tests/fixtures/model/``), the assertion
adapter's three golden packages (``tests/golden/assertion/``, ADR 0062), the manifest's three
(``tests/golden/manifest/``, ADR 0072: run declarations, machines, sites and pins) and the five
status packages (``tests/golden/status/``, ADR 0071: an arm, a mobile base, an autonomous shuttle,
a multicopter and a boat) with
``neptune.store.package_files`` and keeps, per package, the manifest, the receipt and the first
line of every non-empty record table. Nothing is hand-written, and the same compiler gives the
same bytes.

Prints one JSON object: golden file name -> {"target": JSON pointer into the schema, "value"}.
"""

import json
import sys
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.model.kinds import RECORD_KINDS
from neptune.store.package import MANIFEST, RECEIPT, package_files

TESTS: Final = Path(__file__).resolve().parents[2] / "tests"
# Golden name prefix -> the directory holding the package's records/ tables.
PACKAGES: Final = {
    **{
        name: TESTS / "fixtures" / "model" / name
        for name in (
            "drone",
            "manipulator",
            "mobile_robot",
            "quadruped",
            "warehouse_amr",
            "manipulator_cell",
        )
    },
    **{
        f"assertion_{name}": TESTS / "golden" / "assertion" / name
        for name in ("cell_baseline", "fleet_identity", "retraction")
    },
    **{
        f"manifest_{name}": TESTS / "golden" / "manifest" / name
        for name in ("aerial_survey", "amr_fleet", "manipulator_cell")
    },
    **{
        f"status_{name}": TESTS / "golden" / "status" / name
        for name in ("arm_cell", "mobile_base", "av_shuttle", "quad_killswitch", "boat_failsafe")
    },
}
DOCUMENTS: Final = {MANIFEST: "#/$defs/PackageManifest", RECEIPT: "#/$defs/IngestReceipt"}


def records(root: Path) -> list[Any]:
    """Every record of one package's tables, read through the compiler's strict readers."""
    found: list[Any] = []
    for path in sorted((root / "records").glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        found += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return found


def goldens() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, root in PACKAGES.items():
        files = package_files(records(root))
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
