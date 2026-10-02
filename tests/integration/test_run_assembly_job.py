"""MVL-34 end to end: the run assembler over real files from several embodiments (ADR 0066).

The tree is assembled at test time from the repository's real fixtures, so every source is read by
its real adapter:

- ``rosbag2/mobile_base_mcap``: a rosbag2 bag stored as MCAP (the D1 regression: one run, not two);
- ``rosbag2/split_missing``: a split sqlite3 bag with one listed part deleted, and a stray part;
- ``fleet``: two AMRs' bags below a shared site configuration;
- ``cell``: a manipulator cell session directory with its notes and its configuration;
- ``drone``: a PX4 flight log and a ground-station log named for the same time;
- ``trap/run_009``: a session directory holding two vehicles' logs (contamination trap);
- ``notes``: a debrief that names the drone's log, and a standalone to-do note.
"""

import shutil
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.derived.assembly import (
    ASSEMBLY_ID,
    LISTED_PART_MISSING,
    MIXED_MACHINES_FINDING,
    UNLISTED_PART,
    RunAssembler,
    evidence_of,
)
from neptune.derived.grouping import NO_SESSION, SHARED_REFERENCE, Rule
from neptune.derived.sessions import (
    Placement,
    SessionProposal,
    Status,
    UnassignedFile,
    read_derived,
)
from neptune.discovery.layout import LayoutFile, layout_of
from neptune.model.alignment import MemberRole, RunAssembly
from neptune.model.finding import IngestFinding
from neptune.model.run import Run
from neptune.model.source import LocalPath, SourceRevision
from neptune.runtime import IngestJob, JobState
from neptune.store.package import IngestPackage, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
DEBRIEF: Final = """# Flight debrief

The hover test is in flight_2024-05-03_14-00-00.ulg; the ground station logged it too.
"""
TODO: Final = "# To do\n\nOrder spare batteries and re-tape the cell's floor markings.\n"
GCS: Final = "time,mode,battery\n14:00:00,MANUAL,16.4\n14:00:05,POSCTL,16.3\n"


def build(root: Path) -> None:
    def copy(source: str, target: str) -> None:
        destination = root / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (FIXTURES / source).is_dir():
            shutil.copytree(FIXTURES / source, destination)
        else:
            shutil.copyfile(FIXTURES / source, destination)

    def write(target: str, text: str) -> None:
        destination = root / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)

    copy("rosbag2/mobile_base_mcap", "rosbag2/mobile_base_mcap")
    copy("rosbag2/split_sqlite3", "rosbag2/split_missing")
    (root / "rosbag2/split_missing/split_sqlite3_1.db3").unlink()
    copy("rosbag2/mobile_base_sqlite3/mobile_base_sqlite3_0.db3", "rosbag2/split_missing/stray.db3")
    copy("config/nav2_params.yaml", "fleet/site_config.yaml")
    copy("rosbag2/mobile_base_mcap", "fleet/amr_01/rosbag2_2024_05_01-09_00_00")
    copy("rosbag2/mobile_base_sqlite3", "fleet/amr_02/rosbag2_2024_05_01-09_30_00")
    copy("mcap/robot.mcap", "cell/session_2024-05-02_09-00-00/arm.mcap")
    copy("markdown/notes.md", "cell/session_2024-05-02_09-00-00/cell_notes.md")
    copy("config/run_a_params.yaml", "cell/session_2024-05-02_09-00-00/cell_config.yaml")
    copy("ulog/copter.ulg", "drone/flight_2024-05-03_14-00-00.ulg")
    write("drone/gcs_2024-05-03_14-00-00.csv", GCS)
    copy("ulog/copter_appended.ulg", "trap/run_009/copter.ulg")
    copy("ulog/rover.ulg", "trap/run_009/rover.ulg")
    write("notes/debrief.md", DEBRIEF)
    write("notes/todo.md", TODO)


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> IngestPackage:
    base = tmp_path_factory.mktemp("assembly")
    root = base / "tree"
    build(root)
    job = IngestJob(root, base / "package", Workspace(base / "home"), default_registry())
    assert job.run().state is JobState.COMMITTED
    return read_package(base / "package")


def derived(package: IngestPackage) -> tuple[list[SessionProposal], list[UnassignedFile]]:
    records = read_derived({k: v for k, v in package.derived.items() if k.startswith("session_")})
    return (
        [r for r in records if isinstance(r, SessionProposal)],
        [r for r in records if isinstance(r, UnassignedFile)],
    )


def paths(proposal: SessionProposal) -> set[str]:
    return {member.location.raw.decode() for member in proposal.members}


def holding(proposals: list[SessionProposal], path: str) -> list[SessionProposal]:
    return [p for p in proposals if path in paths(p)]


def findings(package: IngestPackage, code: str) -> list[IngestFinding]:
    return [r for r in package.records if isinstance(r, IngestFinding) and r.code == code]


def revisions(package: IngestPackage) -> dict[str, SourceRevision]:
    return {
        r.location.raw.decode(): r
        for r in package.records
        if isinstance(r, SourceRevision) and isinstance(r.location, LocalPath)
    }


def local(revision: SourceRevision) -> LocalPath:
    assert isinstance(revision.location, LocalPath)
    return revision.location


def test_a_rosbag2_bag_stored_as_mcap_is_one_run(package: IngestPackage) -> None:
    proposals, _ = derived(package)
    bag = "rosbag2/mobile_base_mcap"
    [proposal] = holding(proposals, f"{bag}/mobile_base_mcap_0.mcap")
    assert paths(proposal) == {f"{bag}/metadata.yaml", f"{bag}/mobile_base_mcap_0.mcap"}
    assert proposal.rule == Rule.ROSBAG2_FILE_LIST and proposal.status is Status.PROPOSED
    # Each source keeps its own Run (evidence is never merged); the bag's statement joins them.
    at = revisions(package)
    runs = {r.provenance.evidence.source: r for r in package.records if isinstance(r, Run)}
    assert at[f"{bag}/metadata.yaml"].content_id in runs
    assert at[f"{bag}/mobile_base_mcap_0.mcap"].content_id in runs
    [assembly] = [
        r
        for r in package.records
        if isinstance(r, RunAssembly)
        and at[f"{bag}/metadata.yaml"].id in {m.revision for m in r.members}
    ]
    assert assembly.provenance.assertion_kind.value == "stated"
    assert assembly.run == runs[at[f"{bag}/metadata.yaml"].content_id].id
    # The fleet's first AMR holds a copy of this bag: one statement, so one record, listing the
    # files of both copies; each copy stays its own session reading.
    copy = "fleet/amr_01/rosbag2_2024_05_01-09_00_00"
    assert {m.revision: m.role for m in assembly.members} == {
        at[f"{bag}/metadata.yaml"].id: MemberRole.DESCRIPTION,
        at[f"{bag}/mobile_base_mcap_0.mcap"].id: MemberRole.RECORDING,
        at[f"{copy}/metadata.yaml"].id: MemberRole.DESCRIPTION,
        at[f"{copy}/mobile_base_mcap_0.mcap"].id: MemberRole.RECORDING,
    }


def test_listed_parts_are_set_against_present_ones(package: IngestPackage) -> None:
    proposals, _ = derived(package)
    bag = "rosbag2/split_missing"
    [missing] = [
        f
        for f in findings(package, LISTED_PART_MISSING)
        if f.subject == LocalPath(f"{bag}/metadata.yaml")
    ]
    assert missing.details["missing"] == [{"kind": "local", "path": f"{bag}/split_sqlite3_1.db3"}]
    [unlisted] = findings(package, UNLISTED_PART)
    assert unlisted.subject == LocalPath(f"{bag}/stray.db3")
    [stray] = holding(proposals, f"{bag}/stray.db3")
    assert paths(stray) == {f"{bag}/stray.db3"} and stray.rule == Rule.RECORDING_FILE
    [listed] = holding(proposals, f"{bag}/split_sqlite3_0.db3")
    assert listed.rule == Rule.ROSBAG2_FILE_LIST and f"{bag}/stray.db3" not in paths(listed)


def test_a_site_config_shared_by_two_amrs_is_held_by_neither(package: IngestPackage) -> None:
    proposals, unassigned = derived(package)
    assert not holding(proposals, "fleet/site_config.yaml")
    [shared] = [u for u in unassigned if u.location == LocalPath("fleet/site_config.yaml")]
    assert shared.reason == SHARED_REFERENCE and shared.placement is Placement.AMBIGUOUS
    amr_01 = holding(proposals, "fleet/amr_01/rosbag2_2024_05_01-09_00_00/metadata.yaml")
    amr_02 = holding(proposals, "fleet/amr_02/rosbag2_2024_05_01-09_30_00/metadata.yaml")
    assert len(amr_01) == len(amr_02) == 1 and amr_01[0].id != amr_02[0].id
    assert {amr_01[0].id, amr_02[0].id} <= set(shared.candidates)
    assert not set(paths(amr_01[0])) & set(paths(amr_02[0]))


def test_the_manipulator_cell_session_holds_its_notes_and_config(package: IngestPackage) -> None:
    proposals, _ = derived(package)
    cell = "cell/session_2024-05-02_09-00-00"
    [session] = holding(proposals, f"{cell}/arm.mcap")
    assert paths(session) == {
        f"{cell}/arm.mcap",
        f"{cell}/cell_notes.md",
        f"{cell}/cell_config.yaml",
    }
    assert session.status is Status.PROPOSED


def test_the_drone_flight_holds_its_ground_station_log_and_debrief(package: IngestPackage) -> None:
    proposals, unassigned = derived(package)
    [flight] = holding(proposals, "drone/flight_2024-05-03_14-00-00.ulg")
    assert {"drone/gcs_2024-05-03_14-00-00.csv", "notes/debrief.md"} <= paths(flight)
    debrief = next(m for m in flight.members if m.location == LocalPath("notes/debrief.md"))
    assert debrief.rule == Rule.NAMED_IN_DOCUMENT
    [todo] = [u for u in unassigned if u.location == LocalPath("notes/todo.md")]
    assert todo.placement is Placement.UNKNOWN and todo.reason == NO_SESSION


def test_two_vehicles_in_one_run_folder_are_flagged_never_merged_silently(
    package: IngestPackage,
) -> None:
    proposals, _ = derived(package)
    readings = holding(proposals, "trap/run_009/rover.ulg")
    rules = sorted(p.rule for p in readings)
    assert rules == [Rule.MACHINE_SPLIT, Rule.SESSION_DIRECTORY]
    assert all(p.status is Status.CONTESTED for p in readings)
    whole = next(p for p in readings if p.rule == Rule.SESSION_DIRECTORY)
    assert whole.confidence < 0.6
    [finding] = findings(package, MIXED_MACHINES_FINDING)
    assert finding.details["machines"] == ["0123456789abcdef", "rover-0123456789"]


def test_the_package_recomputes_from_its_own_records(package: IngestPackage) -> None:
    # The tree has no links, so its layout is each location's revision.
    tree = layout_of(LayoutFile(r.id, local(r), r.content_id) for r in revisions(package).values())
    assembler = RunAssembler(evidence=evidence_of(package.records))
    assembly = assembler.assemble(tree)
    proposals, unassigned = derived(package)
    assert proposals == list(assembly.grouping.proposals)
    assert unassigned == list(assembly.grouping.unassigned)
    stated = sorted((r for r in package.records if isinstance(r, RunAssembly)), key=lambda r: r.id)
    assert stated == list(assembly.records)
    transforms = {t.adapter_id: t for t in package.receipt.transforms}
    assert transforms[ASSEMBLY_ID].id == assembler.transform.id
