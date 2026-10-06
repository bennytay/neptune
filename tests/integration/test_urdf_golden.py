"""The golden package of the diff-drive Xacro: the adapter's output, byte for byte (MVL-24).

A difference is a changed output of the ``urdf`` adapter. Regenerate with
``uv run python tests/golden/urdf/make_urdf_golden.py``, explain the change in the PR, and bump
the adapter's version when records change (ADR 0003).
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.store.package import read_files

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "urdf"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_urdf_golden", GOLDEN / "make_urdf_golden.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_golden_package_is_what_the_adapter_writes() -> None:
    built = _generator().build()
    committed = {
        str(path.relative_to(GOLDEN)): path.read_bytes()
        for path in sorted((GOLDEN / "diff_drive_xacro").rglob("*"))
        if path.is_file()
    }
    assert sorted(committed) == sorted(built)
    for relative, data in built.items():
        assert committed[relative] == data, relative


def test_the_golden_package_reads_back_and_its_receipt_names_the_reader() -> None:
    prefix = "diff_drive_xacro/"
    files = {path.removeprefix(prefix): data for path, data in _generator().build().items()}
    package = read_files(files)
    (transform,) = package.receipt.transforms
    assert (transform.adapter_id, transform.adapter_version) == ("urdf", "0.1.0")
    counts = dict(package.receipt.records)
    assert counts["hardware_configuration"] == 1 and counts["description_expansion"] == 1
