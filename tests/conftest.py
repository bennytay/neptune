"""Shared fixtures: loaders for the generator scripts under ``tests/fixtures``."""

import importlib.util
import os
import sys
from collections.abc import Iterator
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
def xlsx_fixtures() -> ModuleType:
    """The XLSX fixture generator, ``tests/fixtures/tabular/make_xlsx_fixtures.py``."""
    return load_generator(FIXTURES / "tabular" / "make_xlsx_fixtures.py")


@pytest.fixture(scope="session")
def tabular_golden() -> ModuleType:
    """The tabular golden-file generator, ``tests/golden/tabular/make_tabular_golden.py``."""
    return load_generator(Path(__file__).parent / "golden" / "tabular" / "make_tabular_golden.py")


@pytest.fixture(scope="session")
def plugin_dists() -> ModuleType:
    """The installed-distribution generator, ``tests/fixtures/plugins/make_plugin_dists.py``."""
    return load_generator(FIXTURES / "plugins" / "make_plugin_dists.py")


@pytest.fixture
def fake_store() -> ModuleType:
    """The in-process object store and connector, ``tests/fixtures/sources/fake_object_store.py``,
    loaded afresh (its read log empty)."""
    return load_generator(FIXTURES / "sources" / "fake_object_store.py")


@pytest.fixture
def forget_plugins() -> Iterator[None]:
    """Forget the test plugins' modules after the test, so the next one imports its own."""
    yield
    for name in [name for name in sys.modules if name.startswith("neptune_test_")]:
        del sys.modules[name]


@pytest.fixture
def plugin_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forget_plugins: None) -> Path:
    """An empty site directory on ``sys.path``, for ``make_plugin_dists.install``."""
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    return site


# In CI, an unmarked test slower than SLOW_BUDGET_S fails: mark it `slow` (the nightly runs it).
SLOW_BUDGET_S = 60.0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if (
        os.environ.get("CI")
        and report.when == "call"
        and report.passed
        and "slow" not in item.keywords
        and report.duration > SLOW_BUDGET_S
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} took {report.duration:.0f} s, over the {SLOW_BUDGET_S:.0f} s budget "
            "for an unmarked test: mark it @pytest.mark.slow"
        )
