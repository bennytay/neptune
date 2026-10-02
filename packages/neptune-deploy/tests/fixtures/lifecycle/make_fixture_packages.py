"""Regenerate the compiler packages the lifecycle tests map (Deploy ADR 0002, consequences).

Each folder here (``warehouse_amr``, ``manipulator_cell``, ``inspection_quadruped``) holds real
small exports and a ``neptune.yaml`` telling the compiler's tabular adapter to read CSV headers.
This script ingests each with the compiler's own command line and writes the package to
``packages/<folder>``, without ``volatile/`` (wall clock and host). Deploy's tests read those
packages and never run ingestion, so a compiler adapter change reaches them only through a
deliberate regeneration PR. Run from the repository root::

    uv run python packages/neptune-deploy/tests/fixtures/lifecycle/make_fixture_packages.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCHETYPES = ("inspection_quadruped", "manipulator_cell", "warehouse_amr")


def main() -> int:
    command = Path(sys.executable).parent / "neptune"
    for name in ARCHETYPES:
        out = HERE / "packages" / name
        shutil.rmtree(out, ignore_errors=True)
        out.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as workspace:
            subprocess.run(
                [str(command), "ingest", str(HERE / name), "--out", str(out), "-w", workspace],
                check=True,
            )
        shutil.rmtree(out / "volatile", ignore_errors=True)
        sys.stdout.write(f"wrote {out.relative_to(HERE)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
