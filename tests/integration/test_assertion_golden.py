"""The assertion adapter's golden packages: three assertion files, ingested and packaged (ADR 0062).

An identity confirmation between two robots of a warehouse fleet, a baseline acceptance for a
manipulator cell, and a retraction of the fleet confirmation. Their records are
compatibility-sensitive output (ADR 0003): any change to them is a new adapter version and an
explained golden diff. Each is a schema version 4 package (ADR 0037 §1).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.model.assertion import Assertion, AssertionType
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known
from neptune.model.schema import canonical_schema
from neptune.store.package import MANIFEST, read_files

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "assertion"


def _load() -> ModuleType:
    path = GOLDEN / "make_assertion_golden.py"
    spec = importlib.util.spec_from_file_location("make_assertion_golden", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_assertion_golden"] = module
    spec.loader.exec_module(module)
    return module


MAKER: Final = _load()


def committed() -> dict[str, bytes]:
    return {
        path.relative_to(GOLDEN).as_posix(): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def assertions(name: str) -> list[Assertion]:
    prefix = f"{name}/"
    files = {
        path.removeprefix(prefix): data
        for path, data in committed().items()
        if path.startswith(prefix)
    }
    manifest = canonical_json.loads(files[MANIFEST])
    assert isinstance(manifest, dict)
    for kind in manifest["tables"]:
        files.setdefault(f"records/{kind}.jsonl", b"")  # empty tables are not kept as files
    package = read_files(files)
    assert package.manifest.version == 4
    validator = Draft202012Validator(canonical_schema())
    for record in package.records:
        line = canonical_json.loads(canonical_json.dumps(record.to_json()))
        assert list(validator.iter_errors(line)) == []
    receipt = files["receipt.md"].decode()
    assert "assertion 0.1.0" in receipt and f"ops/assertions/{name}.json" in receipt
    found = [r for r in package.records if isinstance(r, Assertion)]
    return sorted(found, key=lambda r: str(r.provenance.evidence.locator[0]))


def test_the_golden_packages_are_what_ingesting_the_fixtures_gives() -> None:
    # On failure: run `make examples`, bump the assertion adapter's version if its output
    # changed, and explain the diff in the PR.
    assert committed() == MAKER.build()


def kind_of(record: Assertion) -> AssertionType:
    assert isinstance(record.assertion_type, Known)
    return record.assertion_type.value


def test_an_identity_confirmation_between_two_robots_of_a_fleet() -> None:
    same, distinct = assertions("fleet_identity")
    assert kind_of(same) is AssertionType.SAME_IDENTITY
    assert isinstance(same.scope, Known) and len(same.scope.value) == 2
    assert kind_of(distinct) is AssertionType.DISTINCT_IDENTITY


def test_a_baseline_acceptance_for_a_manipulator_cell() -> None:
    accept, reject = assertions("cell_baseline")
    assert kind_of(accept) is AssertionType.ACCEPT_BASELINE
    assert isinstance(accept.scope, Known) and isinstance(accept.scope.value[0], str)
    assert isinstance(accept.signature, Known)
    assert kind_of(reject) is AssertionType.REJECT_BASELINE


def test_a_retraction_names_the_confirmation_and_leaves_its_package_alone() -> None:
    retract, annotate = assertions("retraction")
    (confirmed, _) = assertions("fleet_identity")
    assert kind_of(retract) is AssertionType.RETRACT
    assert isinstance(retract.retracts, Known) and isinstance(confirmed.identifier, Known)
    assert retract.retracts.value == confirmed.identifier.value
    assert confirmed.identifier.value == LogicalId("dc-north.fleet-console", "ASR-2026-0107")
    assert kind_of(annotate) is AssertionType.ANNOTATE
