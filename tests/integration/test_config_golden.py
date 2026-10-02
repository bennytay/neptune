"""The config adapter's golden package: the Nav2 parameters fixture, ingested and packaged.

Its records are compatibility-sensitive output (ADR 0003): any change to them is a new adapter
version and an explained golden diff. The package is a schema version 2 package (ADR 0037 §1).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.model.configuration import ConfigurationSnapshot, ConfigurationValue
from neptune.model.schema import canonical_schema
from neptune.store.package import MANIFEST, read_files

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "config"


def _load() -> ModuleType:
    path = GOLDEN / "make_config_golden.py"
    spec = importlib.util.spec_from_file_location("make_config_golden", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_config_golden"] = module
    spec.loader.exec_module(module)
    return module


MAKER: Final = _load()


def committed() -> dict[str, bytes]:
    return {
        path.relative_to(GOLDEN).as_posix(): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def test_the_golden_package_is_what_ingesting_the_fixture_gives() -> None:
    # On failure: run `make examples`, bump the config adapter's version if its output changed,
    # and explain the diff in the PR.
    assert committed() == MAKER.build()


def test_the_golden_package_reads_back_and_every_line_validates() -> None:
    prefix = f"{MAKER.NAME}/"
    files = {path.removeprefix(prefix): data for path, data in committed().items()}
    manifest = canonical_json.loads(files[MANIFEST])
    assert isinstance(manifest, dict)
    for kind in manifest["tables"]:
        files.setdefault(f"records/{kind}.jsonl", b"")  # empty tables are not kept as files
    package = read_files(files)
    assert package.manifest.version == 2
    snapshots = [r for r in package.records if isinstance(r, ConfigurationSnapshot)]
    values = [r for r in package.records if isinstance(r, ConfigurationValue)]
    assert len(snapshots) == 1 and snapshots[0].values == len(values) == 107
    validator = Draft202012Validator(canonical_schema())
    checked: list[ConfigurationSnapshot | ConfigurationValue] = [*snapshots, *values]
    for record in checked:
        assert (
            list(
                validator.iter_errors(canonical_json.loads(canonical_json.dumps(record.to_json())))
            )
            == []
        )
    receipt = files["receipt.md"].decode()
    assert "config 0.1.0" in receipt and "bringup/params/nav2_params.yaml" in receipt
