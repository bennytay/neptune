"""The committed DataFlash fixtures are exactly what their generator writes, and stay small."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "ardupilot"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_dataflash_fixtures_check", FIXTURES / "make_dataflash_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILT: Final = _generator().build()


@pytest.mark.parametrize("name", sorted(BUILT))
def test_the_committed_file_is_what_the_generator_writes(name: str) -> None:
    assert (FIXTURES / name).read_bytes() == BUILT[name]


def test_every_fixture_is_small() -> None:
    assert all(len(content) < 512 * 1024 for content in BUILT.values())
    assert all(p.stat().st_size < 512 * 1024 for p in FIXTURES.iterdir())
