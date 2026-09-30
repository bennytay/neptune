"""The four worked examples as ingest packages (ADR 0022): golden documents, determinism, and
what each receipt tells a developer without reading any log.
"""

import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.package import IngestReceipt, ingest_receipt_from_json
from neptune.model.schema import canonical_schema
from neptune.store.package import (
    MANIFEST,
    RECEIPT,
    package_files,
    package_id,
    read_package,
    write_package,
)

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "packages"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_packages", GOLDEN / "make_packages.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_packages"] = module
    spec.loader.exec_module(module)
    return module


PACKAGES: Final = _load()
NAMES: Final = PACKAGES.NAMES


def test_the_golden_documents_are_what_packaging_the_examples_gives() -> None:
    # On failure, run `make examples` and explain the diff in the PR (ADR 0003).
    built = PACKAGES.build()
    committed = {
        path.relative_to(GOLDEN).as_posix(): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    assert committed == built


@pytest.mark.parametrize("name", NAMES)
def test_each_example_writes_reads_and_writes_back_identically(name: str, tmp_path: Path) -> None:
    records = PACKAGES.example_records(name)
    files = package_files(records)
    shuffled = list(records)
    random.Random(name).shuffle(shuffled)
    assert package_files(shuffled) == files  # the input's order never shows
    write_package(tmp_path / name, files)
    package = read_package(tmp_path / name)
    assert package.id == package_id(files)
    assert package.files() == files


@pytest.mark.parametrize("name", NAMES)
def test_the_documents_validate_against_the_schema(name: str) -> None:
    schema = canonical_schema()
    files = package_files(PACKAGES.example_records(name))
    for definition, document in (("PackageManifest", MANIFEST), ("IngestReceipt", RECEIPT)):
        validator = Draft202012Validator(
            {
                "$defs": schema["$defs"],
                "$ref": f"#/$defs/{definition}",
                "$schema": schema["$schema"],
            }
        )
        assert list(validator.iter_errors(canonical_json.loads(files[document]))) == []


def receipt(name: str) -> IngestReceipt:
    return ingest_receipt_from_json(canonical_json.loads((GOLDEN / name / RECEIPT).read_bytes()))


def test_a_receipt_answers_what_neptune_did() -> None:
    drone = receipt("drone")
    assert [(t.adapter_id, t.adapter_version) for t in drone.transforms] == [("ulog", "1.0.0")]
    assert {f.code for f in drone.findings} == {"ulog.dropout", "ulog.software_identity_missing"}
    assert {f.severity for f in drone.findings} == {Severity.WARNING}
    assert {clock.field for clock in drone.clocks} == {
        "timestamp",
        "timestamp_sample",
        "time_utc_usec",
    }  # boot time and GPS time, side by side and unconverted
    manipulator = receipt("manipulator")
    assert [field.pointer for field in manipulator.ambiguous] == ["/direction"]
    mobile = receipt("mobile_robot")
    assert sorted(i.value for e in mobile.entities for i in e.identifiers) == ["S-007", "S-008"]
    quadruped = receipt("quadruped")
    assert all(source.read_by for source in quadruped.sources)  # every file was read
    assert len(quadruped.sources) == 4 and len(quadruped.streams) == 2
