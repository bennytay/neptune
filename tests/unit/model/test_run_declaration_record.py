"""The run declaration kind of schema version 9 (ADR 0072 §2): shape, rules, JSON and schema."""

from dataclasses import replace
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.ids import LogicalId, RecordId
from neptune.model.kinds import KIND_SINCE, kinds_at, package_version
from neptune.model.knowledge import Ambiguous, AssertionKind, Candidate, Known, Unknown
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, adapter_locator
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.run import (
    RUN_DECLARATION_SINCE,
    RunDeclaration,
    run_declaration_from_json,
)
from neptune.model.schema import canonical_schema

MANIFEST: Final = content_id(b"neptune: 1\nruns:\n  - {name: pick, paths: [arm], machine: ur5e}\n")
TRANSFORM: Final = transform_record(
    adapter_id="neptune.manifest", adapter_version="0.2.0", config={}
)
RUN: Final = RecordId("rec:sha256:" + "5" * 64)
VALIDATOR: Final = Draft202012Validator(canonical_schema())


def cite(pointer: str, *steps: Any) -> Provenance:
    evidence = EvidenceRef(MANIFEST, (JsonPointer(pointer), *steps))
    return Provenance(evidence, TRANSFORM.id, AssertionKind.STATED)


def declared(value: str, pointer: str) -> Known[LogicalId]:
    return Known(LogicalId("manifest", value), cite(pointer))


def declaration(run: RecordId = RUN, **fields: Any) -> RunDeclaration:
    provenance = cite("/runs/0", adapter_locator("neptune.manifest:run", {"run": run}))
    values: dict[str, Any] = {
        "logical_id": declared("pick", "/runs/0/name"),
        "machine": declared("ur5e", "/runs/0/machine"),
        "site": Unknown(),
        "task": Unknown(),
        **fields,
    }
    return RunDeclaration(
        id=evidence_record_id(RunDeclaration.kind, provenance.evidence, TRANSFORM),
        provenance=provenance,
        run=run,
        **values,
    )


def test_it_is_a_run_family_kind_from_version_9() -> None:
    assert RunDeclaration.family is Family.RUN
    assert KIND_SINCE["run_declaration"] == RUN_DECLARATION_SINCE == 9 <= SCHEMA_VERSION
    assert "run_declaration" in kinds_at(9) and "run_declaration" not in kinds_at(8)
    assert package_version(["run", "run_declaration"]) == 9


def test_it_reads_back_exactly_and_validates_against_the_schema() -> None:
    record = declaration(task=declared("bin-pick", "/runs/0/task"))
    line = canonical_json.loads(canonical_json.dumps(record.to_json()))
    assert isinstance(line, dict)
    assert run_declaration_from_json(line) == record
    assert line["schema_version"] == 9 and line["kind"] == "run_declaration"
    assert sorted(line) == [
        "id",
        "kind",
        "logical_id",
        "machine",
        "provenance",
        "run",
        "schema_version",
        "site",
        "task",
    ]
    assert list(VALIDATOR.iter_errors(line)) == []


def test_one_entry_covering_two_runs_gives_two_records() -> None:
    other = RecordId("rec:sha256:" + "6" * 64)
    assert declaration().id != declaration(other).id


def test_an_ambiguous_machine_keeps_every_reading() -> None:
    both = Ambiguous(
        (
            Candidate(LogicalId("manifest", "arm-a"), cite("/runs/0/machine")),
            Candidate(LogicalId("manifest", "arm-b"), cite("/runs/1/machine")),
        )
    )
    record = declaration(machine=both)
    assert run_declaration_from_json(record.to_json()) == record


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"machine": Known("ur5e")}, ValueError),
        ({"run": "run-7"}, ValueError),
    ],
)
def test_wrong_values_are_refused(change: dict[str, Any], problem: type[Exception]) -> None:
    with pytest.raises(problem):
        replace(declaration(), **change)


def test_reading_is_strict() -> None:
    data = declaration().to_json()
    with pytest.raises(ValueError, match="unexpected"):
        run_declaration_from_json({**data, "software": []})
    missing = {key: value for key, value in data.items() if key != "site"}
    with pytest.raises(ValueError):
        run_declaration_from_json(missing)
    with pytest.raises(SchemaVersionError):
        run_declaration_from_json({**data, "schema_version": SCHEMA_VERSION + 1})
    with pytest.raises(SchemaVersionError):
        run_declaration_from_json({**data, "schema_version": 8})
