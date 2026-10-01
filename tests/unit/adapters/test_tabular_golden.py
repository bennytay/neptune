"""What the tabular adapter emits for its fixtures is kept as golden files (compatibility)."""

from pathlib import Path
from types import ModuleType
from typing import Final

GOLDEN: Final = Path(__file__).parents[2] / "golden" / "tabular"


def test_the_committed_golden_files_are_what_the_adapter_emits(tabular_golden: ModuleType) -> None:
    built = tabular_golden.build()
    assert set(built) == {p.name for p in GOLDEN.glob("*.golden")}
    for name, data in built.items():
        assert (GOLDEN / name).read_bytes() == data, f"{name} changed: explain it in the PR"
