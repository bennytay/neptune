"""Commit the compiler's package of the acceptance corpus's lifecycle files (Deploy ADR 0016).

The acceptance corpus (``harness/acceptance``, Platform ADR 0007) is the Demo v1 hand-over. Deploy
maps five of its files with shipped presets and templates only: both sites' incident report PDFs,
both requalification sheets and PLANT-2's CMMS export (with the INSP work order WO-26-0709). This
script takes those files byte for byte from the corpus generator, ingests them as a subprocess
(a member never imports the compiler's runtime; root ``test_merge_freshness``) and commits the
package without ``volatile/`` as ``package/``, the precedent of Deploy ADR 0004 §3 and §4. Deploy's
tests run only the mapper over it, and check every source's bytes against ``corpus.lock.json``.

``downtime/downtime_log.csv`` is PLANT-2's CMMS downtime log from corpus 2.0.0 (``CORPUS_2``,
harness PR #143), committed as a source because that corpus version is not on main yet. It is
ingested on its own into ``downtime_package/``, so ``package/`` does not change when it lands.
Run from the repository root::

    uv run --all-packages --all-groups python \
        packages/neptune-deploy/tests/fixtures/demo_corpus/make_demo_corpus.py
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Final

HERE: Final = Path(__file__).resolve().parent
ROOT: Final = HERE.parents[4]
PACKAGE: Final = HERE / "package"
DOWNTIME: Final = HERE / "downtime" / "downtime_log.csv"
DOWNTIME_PACKAGE: Final = HERE / "downtime_package"
DOWNTIME_PATH: Final = "sites/PLANT-2/cmms/downtime_log.csv"
# Its content id in acceptance 2.0.0's corpus.lock.json.
CORPUS_2: Final = "sha256:3dc72a6d8664f88e5197d03dc16b955a7b439302b609e6d9b30114035bb3ba58"
FILES: Final = (
    "sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf",
    "sites/PLANT-2/cell3/requalification/requalification_tests.csv",
    "sites/PLANT-2/cmms/work_orders.csv",
    "sites/S-007/incidents/INC-0007.pdf",
    "sites/S-007/requalification/requalification_tests.csv",
)
# The corpus's own manifest names runs and machines whose files are not here; this one only says
# how to read a CSV, as the corpus's does (root ADR 0042 §2).
MANIFEST: Final = b"neptune: 1\nadapters:\n  tabular: {options: {csv_header: first_row}}\n"


def _corpus() -> ModuleType:
    path = ROOT / "harness" / "acceptance" / "generate.py"
    spec = importlib.util.spec_from_file_location("demo_corpus_generate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sources() -> dict[str, bytes]:
    """The five corpus files by their corpus path, and the manifest."""
    files = _corpus().build()
    return {"neptune.yaml": MANIFEST, **{path: files[path] for path in FILES}}


def ingest_command(folder: Path, out: Path, workspace: Path) -> list[str]:
    command = [str(Path(sys.executable).parent / "neptune"), "ingest", str(folder)]
    flags = ["--isolation", "in_process", "--job", "demo_corpus", "--no-plugins"]
    return [*command, "--out", str(out), "-w", str(workspace), *flags]


def _ingest(files: dict[str, bytes], target: Path) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        folder, out = Path(scratch) / "sources", Path(scratch) / "package"
        for relative, data in files.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        command = ingest_command(folder, out, Path(scratch) / "work")
        subprocess.run(command, check=True, capture_output=True)
        shutil.rmtree(target, ignore_errors=True)
        for written in sorted(out.rglob("*")):
            inside = written.relative_to(out)
            if written.is_file() and inside.parts[0] != "volatile":
                copy = target / inside
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(written.read_bytes())
    sys.stdout.write(f"wrote {target.relative_to(ROOT)}\n")


def main() -> int:
    _ingest(sources(), PACKAGE)
    _ingest({"neptune.yaml": MANIFEST, DOWNTIME_PATH: DOWNTIME.read_bytes()}, DOWNTIME_PACKAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
