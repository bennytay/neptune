"""Shared fixtures: loaders for the generator scripts under ``tests/fixtures``."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_generator(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def hostile() -> ModuleType:
    """The hostile fixture generator, ``tests/fixtures/hostile/make_hostile.py``."""
    return load_generator(FIXTURES / "hostile" / "make_hostile.py")


@pytest.fixture(scope="session")
def tabular_fixtures() -> ModuleType:
    """The tabular fixture generator, ``tests/fixtures/tabular/make_tabular_fixtures.py``."""
    return load_generator(FIXTURES / "tabular" / "make_tabular_fixtures.py")


@pytest.fixture(scope="session")
def tabular_golden() -> ModuleType:
    """The tabular golden-file generator, ``tests/golden/tabular/make_tabular_golden.py``."""
    return load_generator(Path(__file__).parent / "golden" / "tabular" / "make_tabular_golden.py")
