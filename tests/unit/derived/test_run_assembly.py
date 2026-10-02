"""The evidence assembler (MVL-34, ADR 0066): stated file lists, edges, splits, merges, documents
and shared configuration, each explained and scored by the fixed formula.

Layouts are built in memory; evidence is what ``EvidenceBuilder`` would gather from committed
records, written out directly so each rule is tested on exactly the facts it reads. The job-level
test (``tests/integration/test_run_assembly_job.py``) reads the same facts from real files.
"""

import hashlib
import random
from fractions import Fraction

import pytest

from neptune.derived.assembly import (
    LISTED_PART_MISSING,
    MIXED_MACHINES_FINDING,
    SEVERAL_NAMED,
    UNLISTED_PART,
    Edge,
    Evidence,
    FileList,
    Interval,
    RunAssembler,
    SourceEvidence,
    score,
)
from neptune.derived.grouping import (
    CONTESTED,
    NO_SESSION,
    SHARED_REFERENCE,
    Grouping,
    GroupingConfig,
    LayoutGrouper,
    Rule,
)
from neptune.derived.sessions import Placement, SessionProposal, Status
from neptune.discovery.layout import Layout, LayoutFile, layout_of
from neptune.identity.ids import record_id
from neptune.model.alignment import MemberRole
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.knowledge import AssertionKind, NotApplicable
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import LocalPath

UNIX = ("unix", "posix")


def content(path: str) -> ContentId:
    return ContentId("sha256:" + hashlib.sha256(path.encode()).hexdigest())


def revision(path: str) -> RecordId:
    return record_id("source_revision", {"path": path})


def layout(*paths: str) -> Layout:
    return layout_of(LayoutFile(revision(p), LocalPath(p), content(p)) for p in paths)


def cite(path: str, offset: int = 0) -> EvidenceRef:
    return EvidenceRef(content(path), (ByteRange(offset, 1),))


def run_id(path: str) -> RecordId:
    return record_id("run", {"path": path})


def file_list(metadata: str, *listed: str) -> SourceEvidence:
    entries = tuple((name, cite(metadata, 10 + n)) for n, name in enumerate(listed))
    stated = FileList(run_id(metadata), cite(metadata, 0), cite(metadata, 5), entries)
    return SourceEvidence(runs=[run_id(metadata)], file_lists=[stated])


def machine(value: str, *intervals: Interval) -> SourceEvidence:
    return SourceEvidence(machines={LogicalId("px4.sys_uuid", value)}, intervals=list(intervals))


def by_rule(proposals: tuple[SessionProposal, ...], rule: str) -> list[SessionProposal]:
    return [p for p in proposals if p.rule == rule]


def paths(proposal: SessionProposal) -> set[str]:
    return {m.location.raw.decode() for m in proposal.members}


# --- the score ---------------------------------------------------------------------------------


def test_the_score_is_the_fixed_formula() -> None:
    assert score(0.6, []) == 0.6
    # 1 - 0.4 * 0.5 = 0.8, then 1 - 0.2 * 0.5 = 0.9
    assert score(0.6, [Edge.SAME_MACHINE]) == 0.8
    assert score(0.6, [Edge.SAME_MACHINE, Edge.TIMES_OVERLAP]) == 0.9
    # a contradiction multiplies: 0.6 * (1 - 3/5) = 0.24
    assert score(0.6, [Edge.MIXED_MACHINES]) == 0.24
    assert score(0.3, [Edge.MIXED_MACHINES, Edge.TIMES_APART, Edge.SOFTWARE_DIFFERS]) == 0.042
    assert score(0.01, [Edge.MIXED_MACHINES] * 9) == 0.01  # the floor
    # order never matters
    edges = [Edge.SAME_MACHINE, Edge.MIXED_MACHINES, Edge.SAME_SOFTWARE, Edge.TIMES_APART]
    assert len({score(0.5, random.Random(n).sample(edges, 4)) for n in range(20)}) == 1


# --- 1. stated file lists ----------------------------------------------------------------------


def test_a_rosbag2_mcap_bag_is_one_run_by_its_stated_file_list() -> None:
    tree = layout("bag/metadata.yaml", "bag/bag_0.mcap")
    found = Evidence(
        {content("bag/metadata.yaml"): file_list("bag/metadata.yaml", "bag_0.mcap")}, ()
    )
    assembly = RunAssembler(evidence=found).assemble(tree)
    [proposal] = assembly.grouping.proposals
    assert proposal.rule == Rule.ROSBAG2_FILE_LIST and proposal.confidence == 0.95
    assert proposal.status is Status.PROPOSED
    assert paths(proposal) == {"bag/metadata.yaml", "bag/bag_0.mcap"}
    assert not assembly.grouping.findings
    [stated] = assembly.records
    assert stated.provenance.assertion_kind is AssertionKind.STATED
    assert stated.rule == "rosbag2.metadata" and stated.run == run_id("bag/metadata.yaml")
    assert isinstance(stated.validity, NotApplicable)
    roles = {m.revision: m.role for m in stated.members}
    assert roles == {
        revision("bag/metadata.yaml"): MemberRole.DESCRIPTION,
        revision("bag/bag_0.mcap"): MemberRole.RECORDING,
    }
    reason = next(r for r in proposal.reasons if r.rule == Rule.ROSBAG2_FILE_LIST)
    assert reason.details["run_assembly"] == stated.id


def test_listed_but_missing_and_present_but_unlisted_parts_are_findings() -> None:
    tree = layout("bag/metadata.yaml", "bag/bag_0.mcap", "bag/stray.mcap", "bag/notes.txt")
    listed = file_list("bag/metadata.yaml", "bag_0.mcap", "bag_1.mcap", "../escape.mcap")
    assembly = RunAssembler(evidence=Evidence({content("bag/metadata.yaml"): listed}, ())).assemble(
        tree
    )
    grouping = assembly.grouping
    [bag] = by_rule(grouping.proposals, Rule.ROSBAG2_FILE_LIST)
    assert paths(bag) == {"bag/metadata.yaml", "bag/bag_0.mcap", "bag/notes.txt"}
    [stray] = by_rule(grouping.proposals, Rule.RECORDING_FILE)
    assert paths(stray) == {"bag/stray.mcap"} and stray.status is Status.PROPOSED
    codes = {f.code: f for f in grouping.findings}
    assert codes[LISTED_PART_MISSING].details["missing"] == [
        {"kind": "local", "path": "bag/bag_1.mcap"}
    ]
    assert codes[UNLISTED_PART].details["unlisted"] == [{"kind": "local", "path": "bag/stray.mcap"}]
    [stated] = assembly.records
    assert {m.revision for m in stated.members} == {
        revision("bag/metadata.yaml"),
        revision("bag/bag_0.mcap"),
    }


def test_a_bag_inside_a_session_directory_keeps_its_directory_reading() -> None:
    tree = layout("run_007/bag/metadata.yaml", "run_007/bag/a_0.db3", "run_007/notes.md")
    listed = file_list("run_007/bag/metadata.yaml", "a_0.db3")
    assembly = RunAssembler(
        evidence=Evidence({content("run_007/bag/metadata.yaml"): listed}, ())
    ).assemble(tree)
    [session] = assembly.grouping.proposals
    assert session.rule == Rule.SESSION_DIRECTORY and len(session.members) == 3
    assert any(r.rule == Rule.ROSBAG2_FILE_LIST for r in session.reasons)
    assert len(assembly.records) == 1


def shape(grouping: Grouping) -> list[tuple[str, float, list[str]]]:
    return sorted((p.rule, p.confidence, sorted(paths(p))) for p in grouping.proposals)


def test_without_evidence_the_readings_are_v0s() -> None:
    tree = layout("bag/metadata.yaml", "bag/bag_0.mcap", "run_1/a.mcap", "run_1/n.md", "x.md")
    assembled = RunAssembler().propose(tree)
    plain = LayoutGrouper().propose(tree)
    assert shape(assembled) == shape(plain)
    assert assembled.transform.adapter_id == "neptune.grouping"
    assert assembled.transform.adapter_version == "0.2.0"
    assert assembled.transform.id != plain.transform.id


# --- 2 and 3. edges and splits -----------------------------------------------------------------


def test_a_session_mixing_machines_is_contested_with_a_split_per_machine() -> None:
    tree = layout("run_007/copter.ulg", "run_007/rover.ulg", "run_007/notes.md")
    found = Evidence(
        {
            content("run_007/copter.ulg"): machine("0123456789abcdef"),
            content("run_007/rover.ulg"): machine("rover-0123456789"),
        },
        (),
    )
    grouping = RunAssembler(evidence=found).propose(tree)
    [session] = by_rule(grouping.proposals, Rule.SESSION_DIRECTORY)
    splits = by_rule(grouping.proposals, Rule.MACHINE_SPLIT)
    assert sorted(sorted(paths(s)) for s in splits) == [
        ["run_007/copter.ulg"],
        ["run_007/rover.ulg"],
    ]
    assert session.status is Status.CONTESTED and session.confidence == 0.24
    assert all(s.status is Status.CONTESTED for s in splits)
    assert {s.id for s in splits} <= set(session.contested)
    codes = [f.code for f in grouping.findings]
    assert MIXED_MACHINES_FINDING in codes and CONTESTED in codes
    reason = next(r for r in session.reasons if r.rule == Edge.MIXED_MACHINES)
    assert reason.details["namespace"] == "px4.sys_uuid"
    # The note stays with the directory's reading only: never silently in a split.
    assert all("run_007/notes.md" not in paths(s) for s in splits)


def test_machines_in_different_namespaces_are_never_compared() -> None:
    tree = layout("run_007/a.ulg", "run_007/b.mcap")
    found = Evidence(
        {
            content("run_007/a.ulg"): machine("0123"),
            content("run_007/b.mcap"): SourceEvidence(machines={LogicalId("serial", "X1")}),
        },
        (),
    )
    [session] = RunAssembler(evidence=found).propose(tree).proposals
    assert session.confidence == 0.6 and session.status is Status.PROPOSED


def test_runs_apart_on_one_clock_lower_a_reading_and_overlap_raises_it() -> None:
    near = Interval(UNIX, Fraction(1000), Fraction(1100))
    later = Interval(UNIX, Fraction(1130), Fraction(1200))  # within 60 s of the first's end
    far = Interval(UNIX, Fraction(9000), Fraction(9100))
    tree = layout("run_1/a.mcap", "run_1/b.mcap", "run_2/a.mcap", "run_2/b.mcap")
    found = Evidence(
        {
            content("run_1/a.mcap"): SourceEvidence(intervals=[near]),
            content("run_1/b.mcap"): SourceEvidence(intervals=[later]),
            content("run_2/a.mcap"): SourceEvidence(intervals=[near]),
            content("run_2/b.mcap"): SourceEvidence(intervals=[far]),
        },
        (),
    )
    grouping = RunAssembler(evidence=found).propose(tree)
    by_dir = {p.directory.to_json()["path"]: p for p in grouping.proposals}
    assert by_dir["run_1"].confidence == 0.8
    assert any(r.rule == Edge.TIMES_OVERLAP for r in by_dir["run_1"].reasons)
    assert by_dir["run_2"].confidence == 0.3
    assert any(r.rule == Edge.TIMES_APART for r in by_dir["run_2"].reasons)


def test_clocks_of_different_families_are_never_compared() -> None:
    tree = layout("run_1/a.mcap", "run_1/b.mcap")
    found = Evidence(
        {
            content("run_1/a.mcap"): SourceEvidence(
                intervals=[Interval(UNIX, Fraction(0), Fraction(1))]
            ),
            content("run_1/b.mcap"): SourceEvidence(
                intervals=[Interval(("gps", "gps"), Fraction(9999), Fraction(10000))]
            ),
        },
        (),
    )
    [session] = RunAssembler(evidence=found).propose(tree).proposals
    assert session.confidence == 0.6


def test_software_versions_support_or_contradict_a_reading() -> None:
    tree = layout("run_1/a.ulg", "run_1/b.ulg", "run_2/a.ulg", "run_2/b.ulg")
    found = Evidence(
        {
            content("run_1/a.ulg"): SourceEvidence(software={("PX4", "v1.14.0")}),
            content("run_1/b.ulg"): SourceEvidence(software={("PX4", "v1.14.0")}),
            content("run_2/a.ulg"): SourceEvidence(software={("PX4", "v1.14.0")}),
            content("run_2/b.ulg"): SourceEvidence(software={("PX4", "v1.15.0")}),
        },
        (),
    )
    by_dir = {
        p.directory.to_json()["path"]: p
        for p in RunAssembler(evidence=found).propose(tree).proposals
    }
    assert by_dir["run_1"].confidence == 0.68  # 1 - 0.4 * 0.8
    assert by_dir["run_2"].confidence == 0.42  # 0.6 * 0.7


# --- 4. merges ---------------------------------------------------------------------------------


def test_loose_recordings_of_one_machine_overlapping_on_one_clock_are_offered_merged() -> None:
    tree = layout("logs/arm_a.mcap", "logs/arm_b.mcap", "logs/other.mcap")
    span = Interval(UNIX, Fraction(100), Fraction(200))
    found = Evidence(
        {
            content("logs/arm_a.mcap"): machine("arm-7", span),
            content("logs/arm_b.mcap"): machine(
                "arm-7", Interval(UNIX, Fraction(150), Fraction(300))
            ),
            content("logs/other.mcap"): machine("arm-8", span),
        },
        (),
    )
    grouping = RunAssembler(evidence=found).propose(tree)
    [merged] = by_rule(grouping.proposals, Rule.MACHINE_TIME_MERGE)
    assert paths(merged) == {"logs/arm_a.mcap", "logs/arm_b.mcap"}
    assert merged.status is Status.CONTESTED
    assert merged.confidence == 0.875  # 1 - 0.5 * 0.5 * 0.5
    singles = by_rule(grouping.proposals, Rule.RECORDING_FILE)
    assert len(singles) == 3
    other = next(p for p in singles if paths(p) == {"logs/other.mcap"})
    assert other.status is Status.PROPOSED


def test_one_machine_on_unrelated_clocks_is_not_merged() -> None:
    tree = layout("logs/a.ulg", "logs/b.ulg")
    found = Evidence({content("logs/a.ulg"): machine("x"), content("logs/b.ulg"): machine("x")}, ())
    grouping = RunAssembler(evidence=found).propose(tree)
    assert not by_rule(grouping.proposals, Rule.MACHINE_TIME_MERGE)


# --- 5. documents ------------------------------------------------------------------------------


def test_a_document_joins_the_session_its_text_names_and_only_that() -> None:
    tree = layout(
        "flights/flight_0503.ulg",
        "flights/flight_0504.ulg",
        "notes/debrief.md",
        "notes/both.md",
        "notes/todo.md",
    )
    found = Evidence(
        {
            content("notes/debrief.md"): SourceEvidence(words={"see", "flight_0503.ulg"}),
            content("notes/both.md"): SourceEvidence(words={"flight_0503", "flight_0504"}),
            content("notes/todo.md"): SourceEvidence(words={"buy", "batteries"}),
        },
        (),
    )
    grouping = RunAssembler(evidence=found).propose(tree)
    [named] = [p for p in grouping.proposals if "flights/flight_0503.ulg" in paths(p)]
    assert "notes/debrief.md" in paths(named)
    member = next(m for m in named.members if m.location == LocalPath("notes/debrief.md"))
    assert member.rule == Rule.NAMED_IN_DOCUMENT
    unassigned = {u.location.raw.decode(): u for u in grouping.unassigned}
    assert unassigned["notes/both.md"].placement is Placement.AMBIGUOUS
    assert unassigned["notes/both.md"].reason == SEVERAL_NAMED
    assert unassigned["notes/todo.md"].placement is Placement.UNKNOWN
    assert unassigned["notes/todo.md"].reason == NO_SESSION


# --- 6. shared configuration -------------------------------------------------------------------


def test_a_configuration_above_several_sessions_is_shared_never_merged() -> None:
    tree = layout(
        "fleet/site_config.yaml",
        "fleet/README.md",
        "fleet/amr_01/run_1/a.mcap",
        "fleet/amr_02/run_1/a.mcap",
    )
    found = Evidence(
        {
            content("fleet/site_config.yaml"): SourceEvidence(configuration=True),
            content("fleet/README.md"): SourceEvidence(words={"fleet"}),
        },
        (),
    )
    grouping = RunAssembler(evidence=found).propose(tree)
    sessions = by_rule(grouping.proposals, Rule.SESSION_DIRECTORY)
    assert len(sessions) == 2
    assert all("fleet/site_config.yaml" not in paths(p) for p in grouping.proposals)
    assert all(p.status is Status.PROPOSED for p in sessions)
    unassigned = {u.location.raw.decode(): u for u in grouping.unassigned}
    shared = unassigned["fleet/site_config.yaml"]
    assert shared.reason == SHARED_REFERENCE
    assert set(shared.candidates) == {p.id for p in sessions}
    assert all(any(r.rule == SHARED_REFERENCE for r in p.reasons) for p in sessions)
    assert unassigned["fleet/README.md"].reason == NO_SESSION
    # A shared reference is not a doubt: no ambiguous-member finding names it.
    assert not any(f.code.endswith("ambiguous_member") for f in grouping.findings)


# --- determinism -------------------------------------------------------------------------------


def test_the_same_tree_and_evidence_give_the_same_assembly() -> None:
    files = [
        "bag/metadata.yaml",
        "bag/bag_0.mcap",
        "run_007/copter.ulg",
        "run_007/rover.ulg",
        "site.yaml",
        "notes.md",
    ]
    sources = {
        content("bag/metadata.yaml"): file_list("bag/metadata.yaml", "bag_0.mcap"),
        content("run_007/copter.ulg"): machine("c"),
        content("run_007/rover.ulg"): machine("r"),
        content("site.yaml"): SourceEvidence(configuration=True),
        content("notes.md"): SourceEvidence(words={"copter"}),
    }
    first = RunAssembler(GroupingConfig(gap_seconds=30), Evidence(sources, ())).assemble(
        layout(*files)
    )
    for seed in range(5):
        order = random.Random(seed).sample(sorted(sources), len(sources))
        again = RunAssembler(
            GroupingConfig(gap_seconds=30), Evidence({k: sources[k] for k in order}, ())
        ).assemble(layout(*random.Random(seed).sample(files, len(files))))
        assert again == first
    assert first.grouping.transform.config == {"gap_seconds": 30, "sessions": []}


@pytest.mark.parametrize("text", ["", "/abs.mcap", "..", "a\\b.mcap", "../x.mcap"])
def test_unsafe_listed_paths_are_never_resolved(text: str) -> None:
    tree = layout("bag/metadata.yaml", "bag/bag_0.mcap", "x.mcap")
    listed = file_list("bag/metadata.yaml", "bag_0.mcap", text)
    assembly = RunAssembler(evidence=Evidence({content("bag/metadata.yaml"): listed}, ())).assemble(
        tree
    )
    [stated] = assembly.records
    assert revision("x.mcap") not in {m.revision for m in stated.members}
    assert not [f for f in assembly.grouping.findings if f.code == LISTED_PART_MISSING]
