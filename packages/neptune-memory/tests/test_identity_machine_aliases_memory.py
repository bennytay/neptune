"""A machine record's declared ids are ``same_as`` (ADR 0021, root ADR 0072 §1).

Every package here is the compiler's own output: the manifest goldens (``tests/golden/manifest/``)
and small folders ingested under a ``neptune.yaml`` by the compiler in the test. The one record
built in code is an ``Ambiguous`` alias, which no manifest can state: it is the compiler's
``Machine`` class over a compiler-produced record, never hand JSON.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import tempfile
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from memory_identity_records import Record, ledger
from neptune.identity import canonical_json
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Ambiguous, AssertionKind, Candidate
from neptune.model.machine import machine_from_json
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.identity import SAME_AS, SAME_AS_CANDIDATE, IdentityConsolidator
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    from neptune_memory.schema.claim import Claim

ROOT: Final = Path(__file__).resolve().parents[3]
GOLDEN: Final = ROOT / "tests" / "golden" / "manifest"
TX = ledger_tx(9)


@cache
def compiler() -> ModuleType:
    """The manifest goldens' generator: its MCAP writer and its ``Neptune.ingest`` call."""
    path = GOLDEN / "make_manifest_golden.py"
    spec = importlib.util.spec_from_file_location("memory_machine_alias_compiler", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def golden(name: str) -> list[Record]:
    return [
        json.loads(line)
        for path in sorted((GOLDEN / name / "records").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@cache
def ingested(manifest: str, *runs: str) -> tuple[Record, ...]:
    """What the compiler commits for a folder with ``manifest`` and one recording per run path."""
    gen = compiler()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = work / "source"
        (source).mkdir()
        (source / "neptune.yaml").write_text(manifest, encoding="utf-8")
        for n, path in enumerate(runs):
            target = source / path / "drive.mcap"
            target.parent.mkdir(parents=True)
            target.write_bytes(gen.recording(f"/{path.replace('/', '_')}", (n + 1) * 10**9))
        out = work / "package"
        gen.ingest(source, out, work / "workspace")
        return tuple(
            json.loads(line)
            for table in sorted((out / "records").glob("*.jsonl"))
            for line in table.read_text(encoding="utf-8").splitlines()
        )


def consolidate(packages: Mapping[str, Sequence[Record]]) -> Consolidation:
    return run_consolidator(
        IdentityConsolidator(), ledger(packages), (), {}, recorded_at=TX, registry=CORE_PREDICATES
    )


def machine(node_id: str) -> NodeRef:
    return NodeRef(NodeType.MACHINE, node_id)


def pairs(result: Consolidation, predicate: str) -> set[tuple[str, str]]:
    return {
        (c.subject.node_id, c.object.node_id)  # type: ignore[union-attr]
        for c in result.claims
        if c.predicate == predicate
    }


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def of_kind(records: Sequence[Record], kind: str) -> list[Record]:
    return [r for r in records if r["kind"] == kind]


ARM_CELL: Final = """\
neptune: 1
machines:
  - id: ARM-3A
    embodiment: manipulator
    aliases: {cmms.asset: ARM-3A, servicenow.ci: CI0012345, record: arm-3a}
  - {id: ARM-4, embodiment: manipulator, aliases: {cmms.asset: ARM-4}}
runs:
  - {name: arm3a-shift-1, paths: [cell/arm_3a], machine: ARM-3A}
"""


def test_manifest_aliases_are_stated_same_as_citing_the_machine_record_and_its_first_run() -> None:
    records = ingested(ARM_CELL, "cell/arm_3a")
    result = consolidate({"cell": records})
    hub = "cmms.asset:ARM-3A"  # the lowest id in canonical order joins the others
    assert pairs(result, SAME_AS) == {
        (hub, "manifest:ARM-3A"),
        (hub, "servicenow.ci:CI0012345"),
    }
    assert pairs(result, SAME_AS_CANDIDATE) == set()
    (arm,) = [m for m in of_kind(records, "machine") if "ARM-3A" in json.dumps(m)]
    (run,) = of_kind(records, "run")
    (declaration,) = of_kind(records, "run_declaration")
    for claim in result.claims:
        assert claim.assertion_kind is AssertionKind.STATED
        assert claim.provenance.consolidator_version == "3"
        assert set(claim.provenance.records) == {arm["id"], run["id"], declaration["id"]}
        assert claim.valid_from.ticks > 0  # the run's first instant: a convention, not a lifetime
    # ARM-4 has an alias but no run or thread places it; the reserved namespace is refused.
    assert codes(result) == [
        "identity.machine_identifier_unrepresentable",
        "identity.machine_unplaced",
    ]


def test_compiler_goldens_join_a_serial_and_a_flight_logs_own_id_to_the_manifest_id() -> None:
    cell = consolidate({"cell": golden("manipulator_cell")})
    assert pairs(cell, SAME_AS) == {("manifest:ur5e-cell-3", "serial:20235400123")}
    aerial = consolidate({"aerial": golden("aerial_survey")})
    (pair,) = pairs(aerial, SAME_AS)
    assert pair[0] == "manifest:survey-quad-7" and pair[1].startswith("px4.sys_uuid:")
    # Placed by the log's own Run.machine (the sys_uuid) as well as the declaration: first by id.
    assert codes(cell) == codes(aerial) == []
    fleet = consolidate({"fleet": golden("amr_fleet")})
    assert fleet.claims == () and codes(fleet) == []  # one id per machine: nothing to join


def test_an_ambiguous_alias_is_candidates_both_ways_never_same_as() -> None:
    records = list(golden("manipulator_cell"))
    (index,) = [i for i, r in enumerate(records) if r["kind"] == "machine"]
    parsed = machine_from_json(records[index])  # type: ignore[arg-type]
    serial = parsed.identifiers[1]
    either = Ambiguous(
        (
            Candidate(LogicalId("serial", "20235400123"), serial.provenance),  # type: ignore[union-attr]
            Candidate(LogicalId("serial", "20235400128"), serial.provenance),  # type: ignore[union-attr]
        )
    )
    changed = dataclasses.replace(parsed, identifiers=(parsed.identifiers[0], either))
    records[index] = dict(changed.to_json())
    result = consolidate({"cell": records})
    assert pairs(result, SAME_AS) == set()
    manifest = "manifest:ur5e-cell-3"
    assert pairs(result, SAME_AS_CANDIDATE) == {
        (manifest, "serial:20235400123"),
        ("serial:20235400123", manifest),
        (manifest, "serial:20235400128"),
        ("serial:20235400128", manifest),
    }


SHARED_ALIAS: Final = """\
neptune: 1
machines:
  - {id: ARM-3A, aliases: {cmms.asset: ASSET-77}}
  - {id: ARM-4, aliases: {cmms.asset: ASSET-77}}
runs:
  - {name: a, paths: [cell/arm_3a], machine: ARM-3A}
  - {name: b, paths: [cell/arm_4], machine: ARM-4}
"""

ALIAS_IS_ANOTHER_MACHINE: Final = """\
neptune: 1
machines:
  - {id: ARM-3A, aliases: {manifest: ARM-4}}
  - {id: ARM-4}
runs:
  - {name: a, paths: [cell/arm_3a], machine: ARM-3A}
"""


def test_an_alias_colliding_with_another_machine_is_a_conflict_never_a_merge() -> None:
    for manifest, runs in (
        (SHARED_ALIAS, ("cell/arm_3a", "cell/arm_4")),
        (ALIAS_IS_ANOTHER_MACHINE, ("cell/arm_3a",)),
    ):
        result = consolidate({"cell": ingested(manifest, *runs)})
        assert pairs(result, SAME_AS) == set()
        assert pairs(result, SAME_AS_CANDIDATE)  # each pair still offered, both ways
        assert "identity.machine_conflict" in codes(result)


def test_two_documents_join_by_a_shared_id_unless_they_disagree_within_a_namespace() -> None:
    first = ingested(
        "neptune: 1\nmachines:\n  - {id: ARM-3A, aliases: {cmms.asset: ARM-3A}}\nruns:\n"
        "  - {name: a, paths: [cell/a], machine: ARM-3A}\n",
        "cell/a",
    )
    agrees = ingested(
        "neptune: 1\nmachines:\n  - {id: ARM-3A, aliases: {serial: SN-1}}\nruns:\n"
        "  - {name: b, paths: [cell/b], machine: ARM-3A}\n",
        "cell/b",
    )
    joined = consolidate({"one": first, "two": agrees})
    assert pairs(joined, SAME_AS) == {
        ("cmms.asset:ARM-3A", "manifest:ARM-3A"),
        ("manifest:ARM-3A", "serial:SN-1"),
    }
    assert codes(joined) == []
    disagrees = ingested(
        "neptune: 1\nmachines:\n  - {id: ARM-9, aliases: {cmms.asset: ARM-3A}}\nruns:\n"
        "  - {name: c, paths: [cell/c], machine: ARM-9}\n",
        "cell/c",
    )
    contested = consolidate({"one": first, "two": disagrees})
    assert pairs(contested, SAME_AS) == set()
    assert codes(contested) == ["identity.machine_conflict"]


def test_machine_identity_is_deterministic_and_independent_of_package_layout() -> None:
    records = ingested(ARM_CELL, "cell/arm_3a")

    def text(result: Consolidation) -> list[bytes]:
        return sorted(canonical_json.dumps(c.to_json()) for c in result.claims)

    once, again = consolidate({"cell": records}), consolidate({"cell": records})
    assert text(once) == text(again)
    machines = [r for r in records if r["kind"] == "machine"]
    rest = [r for r in records if r["kind"] != "machine"]
    split = consolidate({"a-rest": rest, "b-machines": machines, "c-again": machines})
    assert text(split) == text(once)


def claims_about(result: Consolidation, node: NodeRef) -> list[Claim]:
    return [c for c in result.claims if node in (c.subject, c.object)]
