"""Run involvement from the compiler's own ``run_declaration`` records (ADR 0020, root ADR 0072).

The packages are the compiler's manifest goldens (``tests/golden/manifest/``): three embodiments
ingested under a ``neptune.yaml`` by the compiler itself, and checked by its integration test to be
exactly what ingesting gives. Nothing here is hand-written JSON.

- A manipulator cell: one run, its machine, site and task declared.
- An AMR fleet: two runs in one session, each declared with its own machine at one site.
- An aerial survey: the flight log states the drone's ``sys_uuid``, the manifest a manifest id;
  aliasing them is identity's, so the run's machine stays two candidates, never one.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from memory_identity_records import Record, ledger
from neptune.identity import canonical_json
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Ambiguous, Candidate, Known, Unknown
from neptune.model.run import RunDeclaration, run_declaration_from_json
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.runs import RunConsolidator, involvement
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune_memory.schema.claim import Claim

GOLDEN: Final = Path(__file__).resolve().parents[3] / "tests" / "golden" / "manifest"
TX = ledger_tx(9)


def package(name: str) -> list[Record]:
    """A compiler manifest golden's record lines, in file-name then line order."""
    return [
        json.loads(line)
        for path in sorted((GOLDEN / name / "records").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def kind(records: Sequence[Record], name: str) -> list[Record]:
    return [r for r in records if r["kind"] == name]


def declarations(records: Sequence[Record]) -> list[RunDeclaration]:
    return [run_declaration_from_json(r) for r in kind(records, "run_declaration")]  # type: ignore[arg-type]


def consolidate(packages: Mapping[str, Sequence[Record]]) -> Consolidation:
    return run_consolidator(
        RunConsolidator(), ledger(packages), (), {}, recorded_at=TX, registry=CORE_PREDICATES
    )


def manifest(value: str, node_type: NodeType) -> NodeRef:
    return NodeRef(node_type, f"manifest:{value}")


def run_of(declaration: RunDeclaration) -> NodeRef:
    """A run that declares no logical id is its record: the declaration's name never keys it."""
    return NodeRef(NodeType.RUN, f"record:{declaration.run}")


def about(result: Consolidation, subject: NodeRef) -> list[Claim]:
    return [c for c in result.claims if c.subject == subject]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("manipulator_cell", {"ur5e-cell-3": ("plant-2", "bin-pick")}),
        ("amr_fleet", {"AMR-01": ("DC-7", None), "AMR-02": ("DC-7", None)}),
    ],
)
def test_declared_machine_site_and_task_are_known_and_cite_the_declaration(
    name: str, expected: Mapping[str, tuple[str, str | None]]
) -> None:
    records = package(name)
    result = consolidate({name: records})
    assert codes(result) == []
    found = declarations(records)
    assert len(found) == len(expected)
    for declaration in found:
        assert isinstance(declaration.machine, Known)
        site, task = expected[declaration.machine.value.value]
        subject = run_of(declaration)
        current = about(result, subject)
        machine = manifest(declaration.machine.value.value, NodeType.MACHINE)
        assert involvement(current, subject, "recorded_by") == Known((machine,))
        assert involvement(current, subject, "at_site") == Known((manifest(site, NodeType.SITE),))
        assert involvement(current, subject, "executes_task") == (
            Known((manifest(task, NodeType.TASK),)) if task else Unknown()
        )
        for claim in current:
            if claim.predicate in ("recorded_by", "at_site", "executes_task"):
                assert declaration.id in claim.provenance.records
                assert declaration.provenance.evidence in claim.provenance.evidence
    # The user's run name is never a node.
    assert not any(c.subject.node_id.startswith("manifest:") for c in result.claims)


def test_a_declared_machine_beside_the_logs_own_id_is_never_definite() -> None:
    records = package("aerial_survey")
    result = consolidate({"survey": records})
    (declaration,) = declarations(records)
    subject = run_of(declaration)
    current = about(result, subject)
    assert codes(result) == ["runs.declarations_disagree"]
    machine = involvement(current, subject, "recorded_by")
    assert isinstance(machine, Ambiguous)
    assert {c.value for c in machine.candidates} == {
        (manifest("survey-quad-7", NodeType.MACHINE),),
        (NodeRef(NodeType.MACHINE, "px4.sys_uuid:000200000000343233345117003a0027"),),
    }
    assert involvement(current, subject, "at_site") == Known(
        (manifest("north-field", NodeType.SITE),)
    )


def test_an_ambiguous_declared_machine_gives_candidates_only() -> None:
    records = package("manipulator_cell")
    (real,) = declarations(records)
    assert isinstance(real.machine, Known)
    other = LogicalId("manifest", "ur5e-cell-4")
    ambiguous = dataclasses.replace(
        real,
        machine=Ambiguous(
            (Candidate(real.machine.value, real.provenance), Candidate(other, real.provenance))
        ),
    )
    rest = [r for r in records if r["kind"] != "run_declaration"]
    result = consolidate({"cell": [*rest, ambiguous.to_json()]})  # type: ignore[list-item]
    subject = run_of(real)
    current = about(result, subject)
    assert [c.predicate for c in current if c.predicate == "recorded_by"] == []
    machine = involvement(current, subject, "recorded_by")
    assert isinstance(machine, Ambiguous)
    assert {c.value for c in machine.candidates} == {
        (manifest("ur5e-cell-3", NodeType.MACHINE),),
        (manifest("ur5e-cell-4", NodeType.MACHINE),),
    }
    assert involvement(current, subject, "at_site") == Known((manifest("plant-2", NodeType.SITE),))


def test_a_declaration_whose_run_record_is_absent_is_a_finding_not_a_guess() -> None:
    records = package("manipulator_cell")
    (declaration,) = declarations(records)
    result = consolidate({"cell": [r for r in records if r["kind"] != "run"]})
    assert codes(result) == ["runs.dangling_declaration"]
    (finding,) = result.findings
    assert finding.details["run"] == declaration.run
    assert finding.records == (declaration.id,)
    assert not [c for c in result.claims if c.predicate in ("at_site", "executes_task")]


def test_a_declaration_in_another_package_reaches_its_run_and_output_is_byte_identical() -> None:
    records = package("amr_fleet")
    together = consolidate({"fleet": records})
    apart = consolidate(
        {
            "logs": [r for r in records if r["kind"] != "run_declaration"],
            "manifest": kind(records, "run_declaration")[::-1],
        }
    )
    again = consolidate({"fleet": records})
    assert canonical_json.dumps(together.to_json()) == canonical_json.dumps(again.to_json())
    assert {(c.subject, c.predicate, c.object) for c in together.claims} == {
        (c.subject, c.predicate, c.object) for c in apart.claims
    }
    assert codes(apart) == []
