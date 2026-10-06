"""The golden status packages (ADR 0071): five embodiments' logs, each its adapter's records byte
for byte, holding the status and safety-state records the log's declared types state.

A difference is a changed output of an adapter. Regenerate with
``uv run python tests/golden/status/make_status_golden.py``, explain the change in the PR, and
bump the adapter's version when records change (ADR 0003).
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.store.package import read_files

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "status"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_status_golden", GOLDEN / "make_status_golden.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR: Final = _generator()
BUILT: Final = GENERATOR.build()


def test_the_golden_packages_are_what_the_adapters_write() -> None:
    committed = {
        str(path.relative_to(GOLDEN)): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    assert sorted(committed) == sorted(BUILT)
    for relative, data in BUILT.items():
        assert committed[relative] == data, relative


@pytest.mark.parametrize(
    ("stem", "statuses", "safety"),
    [
        ("arm_cell", 4, 9),
        ("mobile_base", 4, 2),
        ("av_shuttle", 3, 3),
        ("quad_killswitch", 4, 3),
        ("boat_failsafe", 6, 0),
    ],
)
def test_each_embodiment_states_its_statuses_and_safety_states(
    stem: str, statuses: int, safety: int
) -> None:
    """Each embodiment's log states its own statuses and safety states, as records."""
    names = [name for name in BUILT if name.startswith(f"{stem}/")]
    files = {name.removeprefix(f"{stem}/"): BUILT[name] for name in names}
    counts = {
        path: data.count(b"\n")
        for path, data in files.items()
        if path in ("records/status_report.jsonl", "records/safety_state.jsonl")
    }
    assert counts.get("records/status_report.jsonl", 0) == statuses
    assert counts.get("records/safety_state.jsonl", 0) == safety


def test_every_package_reads_back_and_names_its_reader() -> None:
    readers = {Path(name).stem: reader for name, reader, _ in GENERATOR.SOURCES}
    versions = {"mcap": "0.3.0", "rosbag1": "0.3.0", "rosbag2": "0.3.0", "flightlog": "0.2.0"}
    for stem, files in GENERATOR.packages().items():
        package = read_files(files)
        (transform,) = package.receipt.transforms
        assert transform.adapter_id == readers[stem]
        assert transform.adapter_version == versions[readers[stem]]
        assert package.manifest.version == 9
