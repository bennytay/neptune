"""MVL-13 end to end: a real job over a messy tree writes its session proposals into the package.

The job groups at the end of inspect (ADR 0036 §5) and the package carries the proposals as
derived tables beside the evidence, with the grouping's findings and transform in its records.
What the package holds is exactly what the grouper proposes for the tree, and it recomputes from
the package's own revision table and symlink findings, so anyone holding the package can check it.
"""

import importlib.util
import shutil
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.derived.assembly import ASSEMBLY_ID, RunAssembler, evidence_of
from neptune.derived.grouping import (
    CONTESTED,
    DeclaredSession,
    GroupingConfig,
    LayoutGrouper,
    Rule,
)
from neptune.derived.sessions import SessionProposal, Status, read_derived
from neptune.discovery.layout import Layout, LayoutFile, LayoutLink, layout_of
from neptune.discovery.policy import SYMLINK_NOT_FOLLOWED
from neptune.discovery.source import LocalSource
from neptune.model.finding import IngestFinding
from neptune.model.source import LocalPath, RawLocalPath, SourceAbsence, SourceRevision
from neptune.runtime import IngestJob, JobEvent, JobOptions, JobState, Phase
from neptune.store.assemble import export
from neptune.store.package import IngestPackage, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration


def _generator() -> ModuleType:
    path = Path(__file__).parents[1] / "fixtures" / "grouping" / "make_layouts.py"
    spec = importlib.util.spec_from_file_location("make_layouts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAYOUTS: Final = _generator()


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    root = tmp_path / "messy"
    root.mkdir()
    LAYOUTS.build_all(root)
    return root


def ingest(
    root: Path, destination: Path, home: Path, options: JobOptions | None = None
) -> tuple[IngestPackage, list[JobEvent]]:
    seen: list[JobEvent] = []
    job = IngestJob(
        root, destination, Workspace(home), default_registry(), options, on_event=seen.append
    )
    assert job.run().state is JobState.COMMITTED
    return read_package(destination), seen


def proposals(package: IngestPackage) -> list[SessionProposal]:
    return [r for r in read_derived(package.derived) if isinstance(r, SessionProposal)]


def layout_from_package(records: Iterable[Any]) -> Layout:
    """The layout the job grouped, rebuilt from the package alone: each location's latest
    revision, and every link discovery recorded with its target."""
    chain = [r for r in records if isinstance(r, (SourceRevision, SourceAbsence))]
    superseded = {previous for entry in chain for previous in entry.supersedes}
    files = [
        LayoutFile(r.id, r.location, r.content_id)
        for r in chain
        if isinstance(r, SourceRevision)
        and r.id not in superseded
        and isinstance(r.location, (LocalPath, RawLocalPath))
    ]
    links = []
    for record in records:
        if isinstance(record, IngestFinding) and record.code == SYMLINK_NOT_FOLLOWED:
            details = record.details
            if "target" in details:
                target = str(details["target"]).encode()
            elif "target_hex" in details:
                target = bytes.fromhex(str(details["target_hex"]))
            else:
                continue  # refused at open, not a link the walk recorded
            assert isinstance(record.subject, (LocalPath, RawLocalPath))
            links.append(LayoutLink(record.subject, target))
    return layout_of(files, links)


def test_a_job_writes_the_grouping_into_the_package_and_it_recomputes(
    messy: Path, tmp_path: Path
) -> None:
    package, seen = ingest(messy, tmp_path / "package", tmp_path / "home")
    # The package's proposals are the assembler's (ADR 0066) over what the package itself
    # records: its layout, and the evidence its adapters committed.
    grouper = RunAssembler(evidence=evidence_of(package.records))
    tree = layout_from_package(package.records)
    recomputed = grouper.propose(tree)
    sessions = {k: v for k, v in package.derived.items() if k.startswith("session_")}
    assert sessions == {k: tuple(v) for k, v in recomputed.tables().items()}
    assert proposals(package) == list(recomputed.proposals)
    # Its findings and transform are in the evidence tables; its proposals are not.
    transforms = {t.adapter_id: t for t in package.receipt.transforms}
    assert transforms[ASSEMBLY_ID].id == grouper.transform.id
    findings = {r.id for r in package.records if isinstance(r, IngestFinding)}
    assert {f.id for f in recomputed.findings} <= findings
    assert any(f.code == CONTESTED for f in recomputed.findings)
    assert not any(getattr(r, "kind", "").startswith("session") for r in package.records)
    # One event says what the layout alone proposed, in the inspect phase (a dry run's view);
    # another what the assembly proposed over the records, in assemble.
    [event] = [e for e in seen if e.kind == "sessions_proposed"]
    assert event.phase is Phase.INSPECT
    assert event.details == LayoutGrouper().propose(tree).summary()
    [assembled] = [e for e in seen if e.kind == "runs_assembled"]
    assert assembled.phase is Phase.ASSEMBLE
    assert assembled.details == {**recomputed.summary(), "run_assemblies": 0}


def test_the_package_holds_the_runs_of_the_tree(messy: Path, tmp_path: Path) -> None:
    package, _ = ingest(messy, tmp_path / "package", tmp_path / "home")
    found = {p.directory: p for p in proposals(package) if p.rule == Rule.SESSION_DIRECTORY}
    run_001 = found[LocalPath("runs/run_001")]
    assert {m.location for m in run_001.members} == {
        LocalPath("runs/run_001/robot.mcap"),
        LocalPath("runs/run_001/config.yaml"),
        LocalPath("runs/run_001/camera/front.mp4"),
    }
    assert run_001.status is Status.PROPOSED
    # Every member is a revision the package holds.
    revisions = {r.id for r in package.records if isinstance(r, SourceRevision)}
    assert all(m.revision in revisions for p in proposals(package) for m in p.members)


def test_the_same_tree_gives_the_same_package_wherever_it_is_and_however_often(
    messy: Path, tmp_path: Path
) -> None:
    first, _ = ingest(messy, tmp_path / "first", tmp_path / "home")
    again, _ = ingest(messy, tmp_path / "again", tmp_path / "home")  # every chunk reused
    moved = tmp_path / "elsewhere" / "copy"
    shutil.copytree(messy, moved, symlinks=True)
    fresh, _ = ingest(moved, tmp_path / "fresh", tmp_path / "other-home")
    assert first.id == again.id == fresh.id
    assert first.derived == fresh.derived


def test_a_declared_session_reaches_the_package_under_its_own_transform(
    messy: Path, tmp_path: Path
) -> None:
    plain, _ = ingest(messy, tmp_path / "plain", tmp_path / "home")
    config = GroupingConfig(sessions=(DeclaredSession("trial pair", ("trials",)),))
    declared, _ = ingest(
        messy, tmp_path / "declared", tmp_path / "home", JobOptions(grouping=config)
    )
    [proposal] = [p for p in proposals(declared) if p.rule == Rule.DECLARED]
    assert proposal.confidence == 1.0 and len(proposal.members) == 2
    assert proposal.assertion_kind == "stated" and proposal.status is Status.PROPOSED
    assert not any(p.rule == Rule.NAME_TIME_PROXIMITY for p in proposals(declared))
    transforms = {t.adapter_id: t for t in declared.receipt.transforms}
    expected = RunAssembler(config, evidence_of(declared.records)).transform
    assert transforms[ASSEMBLY_ID].id == expected.id
    assert plain.id != declared.id
    # The evidence is the same; only the interpretation and its transform differ.
    assert {r.id for r in plain.records if isinstance(r, SourceRevision)} == {
        r.id for r in declared.records if isinstance(r, SourceRevision)
    }


def test_an_export_carries_the_derived_tables(messy: Path, tmp_path: Path) -> None:
    package, _ = ingest(messy, tmp_path / "package", tmp_path / "home")
    export(tmp_path / "package", tmp_path / "export", LocalSource(messy))
    assert read_package(tmp_path / "export").derived == package.derived
