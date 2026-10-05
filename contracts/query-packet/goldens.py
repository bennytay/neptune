"""Golden examples for the query-packet contract, produced by neptune-context itself.

``scripts/contracts.py bump query-packet <version>`` runs this file and stores its output under
``v<version>/golden/``. It reads the goldens neptune-context generates and tests
(``packages/neptune-context/tests/golden/``): the ten persona queries and the packets answering
them over Memory's golden graph and the compiler's worked examples (context ADR 0003 §10), and the
ten worked queries of context ADR 0002. Nothing is hand-written here; the package's tests fail when
those files drift from the code that builds them, and decode every query with the query reader and
every packet with the packet reader.

Prints one JSON object: golden file name -> {"target": JSON pointer into the schema, "value"}.
"""

import json
import sys
from pathlib import Path
from typing import Any, Final

GOLDEN: Final = Path(__file__).resolve().parents[2] / "packages/neptune-context/tests/golden"


def goldens(root: Path = GOLDEN) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "packets").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        out[f"packet.{path.name}"] = {"target": "#/$defs/ContextPacket", "value": value}
    for path in sorted((root / "queries").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        out[f"query.{path.name}"] = {"target": "#/$defs/Query", "value": value}
    for path in sorted((root / "worked-queries").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        out[f"query.worked-{path.name}"] = {"target": "#/$defs/Query", "value": value}
    return out


if __name__ == "__main__":
    json.dump(goldens(), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
