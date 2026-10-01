"""v0 session grouping over messy layouts: rules, conflicts, overrides, determinism (ADR 0036)."""

import importlib.util
import json
import os
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.derived.grouping import (
    AMBIGUOUS_MEMBER,
    CONFIDENCE,
    CONTESTED,
    DECLARATION_UNMATCHED,
    DECLARED_CONTRADICTS_LAYOUT,
    DEPTH_LIMIT,
    OUTER_NESTING_LIMIT,
    OUTSIDE_DECLARATION,
    DeclaredSession,
    Grouping,
    GroupingConfig,
    LayoutGrouper,
    Rule,
    check_grouping,
    grouping_config_from_json,
)
from neptune.derived.sessions import (
    ROOT_DIRECTORY,
    LinkRelation,
    Placement,
    Role,
    SessionProposal,
    Status,
    session_proposal,
    session_proposal_from_json,
)
from neptune.discovery.layout import Layout, LayoutFile, layout_from_scan, layout_of
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.revisions import SourceLedger, revision_id
from neptune.model.jsonvalue import JsonValue
from neptune.model.source import LocalPath, RawLocalPath, local_location


def _generator() -> ModuleType:
    path = Path(__file__).parents[2] / "fixtures" / "grouping" / "make_layouts.py"
    spec = importlib.util.spec_from_file_location("make_layouts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAYOUTS: Final[ModuleType] = _generator()


def walk(root: Path) -> Layout:
    result = scan(LocalSource(root), SourceLedger())
    return layout_from_scan(result.observations, result.symlinks)


def group(root: Path, config: GroupingConfig | None = None) -> Grouping:
    return LayoutGrouper(config).propose(walk(root))


def built(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    for name in names:
        LAYOUTS.build(root, name)
    return root


def text(location: LocalPath | RawLocalPath) -> str:
    return location.raw.decode("utf-8", "backslashreplace")


def where(proposal: SessionProposal) -> str:
    place = proposal.directory
    return text(place) if isinstance(place, (LocalPath, RawLocalPath)) else ""


def members(proposal: SessionProposal) -> set[str]:
    return {text(member.location) for member in proposal.members}


def recordings(proposal: SessionProposal) -> set[str]:
    return {text(m.location) for m in proposal.members if m.role is Role.RECORDING}


def extent(grouping: Grouping, proposal: SessionProposal) -> set[str]:
    """Every file the proposal reads as one session: its members and its includes'."""
    return {text(member.location) for member in grouping.extent(proposal)}


def by_members(grouping: Grouping) -> dict[frozenset[str], SessionProposal]:
    """Each proposal by the files it groups: its extent."""
    return {frozenset(extent(grouping, p)): p for p in grouping.proposals}


def unassigned(grouping: Grouping) -> dict[str, tuple[Placement, str, int]]:
    return {
        text(u.location): (u.placement, u.reason, len(u.candidates)) for u in grouping.unassigned
    }


def rules_of(details: Mapping[str, JsonValue]) -> list[str]:
    """The rules of the readings a reason or finding lists."""
    readings = details["readings"]
    assert isinstance(readings, list)
    return [str(reading["rule"]) for reading in readings if isinstance(reading, dict)]


def codes(grouping: Grouping) -> list[str]:
    return sorted(finding.code for finding in grouping.findings)


# --- Each convention ---------------------------------------------------------------------------


def test_each_rosbag2_directory_is_one_recording_and_a_note_beside_two_is_ambiguous(
    tmp_path: Path,
) -> None:
    grouping = group(built(tmp_path, "ros2_bags"))
    first = "ros2_bags/rosbag2_2024_05_01-12_30_00/"
    second = "ros2_bags/rosbag2_2024_05_01-14_00_00/"
    found = by_members(grouping)
    assert set(found) == {
        frozenset(
            {
                first + "metadata.yaml",
                first + "rosbag2_2024_05_01-12_30_00_0.db3",
                first + "rosbag2_2024_05_01-12_30_00_1.db3",
            }
        ),
        frozenset({second + "metadata.yaml", second + "rosbag2_2024_05_01-14_00_00_0.mcap"}),
    }
    for proposal in grouping.proposals:
        assert proposal.rule == Rule.ROSBAG2_DIRECTORY and proposal.status is Status.PROPOSED
        assert proposal.confidence == CONFIDENCE[Rule.ROSBAG2_DIRECTORY]
        assert recordings(proposal) == members(proposal)
    assert unassigned(grouping) == {
        "ros2_bags/notes.txt": (Placement.AMBIGUOUS, "several_sessions", 2)
    }
    assert codes(grouping) == [AMBIGUOUS_MEMBER]


def test_px4_date_folders_are_days_and_every_log_is_its_own_flight(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "px4"))
    assert sorted(map(sorted, by_members(grouping))) == [
        ["px4/log/2024-05-01/12_30_00.ulg"],
        ["px4/log/2024-05-01/13_45_10.ulg"],
        ["px4/log/2024-05-02/09_00_00.ulg"],
    ]
    assert {p.rule for p in grouping.proposals} == {Rule.RECORDING_FILE}
    assert unassigned(grouping) == {"px4/log/desktop.ini": (Placement.UNKNOWN, "no_session", 0)}
    assert grouping.findings == ()


def test_a_session_named_directory_holds_everything_below_it(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "runs"))
    found = by_members(grouping)
    run_001 = frozenset(
        {"runs/run_001/robot.mcap", "runs/run_001/config.yaml", "runs/run_001/camera/front.mp4"}
    )
    run_002 = frozenset(
        {"runs/run_002/robot.mcap", "runs/run_002/config.yaml", "runs/run_002/Thumbs.db"}
    )
    assert set(found) == {run_001, run_002}
    proposal = found[run_001]
    assert proposal.rule == Rule.SESSION_DIRECTORY and proposal.directory == LocalPath(
        "runs/run_001"
    )
    assert recordings(proposal) == {"runs/run_001/robot.mcap"}
    assert proposal.reasons[0].details == {"keyword": True, "name": "run_001"}
    # The README sits above the sessions: a directory boundary is never crossed by a guess.
    assert unassigned(grouping) == {"runs/README.md": (Placement.UNKNOWN, "no_session", 0)}


def test_split_parts_sharing_a_start_time_are_one_recording_and_a_gap_is_said(
    tmp_path: Path,
) -> None:
    grouping = group(built(tmp_path, "split"))
    found = by_members(grouping)
    split = found[
        frozenset(
            {
                "split/patrol_2024-05-01-12-30-00_0.bag",
                "split/patrol_2024-05-01-12-30-00_1.bag",
                "split/patrol_2024-05-01-12-30-00_3.bag",
            }
        )
    ]
    assert split.rule == Rule.SPLIT_SEQUENCE and split.confidence == 0.8
    details = split.reasons[0].details
    assert details["indices"] == [0, 1, 3] and details["missing"] == [2]
    assert details["missing_count"] == 1 and details["prefix"] == "patrol_2024-05-01-12-30-00"
    assert found[frozenset({"split/patrol_2024-05-01-15-00-00_0.bag"})].rule == Rule.RECORDING_FILE


def test_numbered_parts_without_a_start_time_are_contested_never_merged(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "parts"))
    found = by_members(grouping)
    whole = found[frozenset({"parts/x_0.mcap", "parts/x_1.mcap", "parts/x.yaml"})]
    first = found[frozenset({"parts/x_0.mcap"})]
    second = found[frozenset({"parts/x_1.mcap"})]
    assert whole.rule == Rule.NUMBERED_SEQUENCE
    assert {m.rule for m in whole.members if m.role is Role.CONTEXT} == {Rule.SHARED_STEM}
    # Proposals contest each other exactly when they share a file: the whole contests each part;
    # the parts, together one reading, do not contest each other.
    assert all(p.status is Status.CONTESTED for p in (whole, first, second))
    assert set(whole.contested) == {first.id, second.id}
    assert first.contested == (whole.id,) and second.contested == (whole.id,)
    [finding] = grouping.findings
    assert finding.code == CONTESTED and finding.subject == LocalPath("parts/x.yaml")
    assert finding.details["rules"] == ["numbered_sequence", "recording_file"]
    assert finding.category == "ambiguous" and finding.severity == "warning"


def test_parts_numbered_by_a_session_keyword_are_separate_runs(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "episodes"))
    assert set(by_members(grouping)) == {
        frozenset({"episodes/episode_1.mcap"}),
        frozenset({"episodes/episode_2.mcap"}),
    }
    assert all(p.status is Status.PROPOSED for p in grouping.proposals)


def test_split_parts_of_a_keyword_named_recording_are_contested_never_separated(
    tmp_path: Path,
) -> None:
    # ``rosbag record -O run_3 --split``: the keyword names the recording, not each part.
    grouping = group(built(tmp_path, "named_split"))
    found = by_members(grouping)
    whole = found[frozenset({"named_split/run_3_0.bag", "named_split/run_3_1.bag"})]
    parts = [
        found[frozenset({"named_split/run_3_0.bag"})],
        found[frozenset({"named_split/run_3_1.bag"})],
    ]
    assert whole.rule == Rule.NUMBERED_SEQUENCE and whole.status is Status.CONTESTED
    assert {p.rule for p in parts} == {Rule.RECORDING_FILE}
    assert set(whole.contested) == {p.id for p in parts}
    assert all(p.contested == (whole.id,) for p in parts)
    [finding] = grouping.findings
    assert finding.code == CONTESTED
    assert finding.details["rules"] == ["numbered_sequence", "recording_file"]


def test_media_named_by_the_same_time_are_one_session(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "dump"))
    assert set(by_members(grouping)) == {
        frozenset({"dump/2024-05-01_12-30-00_front.mp4", "dump/2024-05-01_12-30-00_imu.csv"}),
        frozenset({"dump/2024-05-01_14-02-11_front.mp4", "dump/2024-05-01_14-02-11_imu.csv"}),
    }
    assert {p.rule for p in grouping.proposals} == {Rule.SHARED_NAME_TIME}


def test_a_session_directory_whose_recordings_are_days_apart_is_contested(
    tmp_path: Path,
) -> None:
    grouping = group(built(tmp_path, "session_dir"))
    found = by_members(grouping)
    whole = found[
        frozenset({"session_04/2024-05-01_10-00-00.mcap", "session_04/2024-05-03_09-00-00.mcap"})
    ]
    assert whole.rule == Rule.SESSION_DIRECTORY and whole.status is Status.CONTESTED
    clusters = [p for p in grouping.proposals if p.rule == Rule.NAME_TIME_CLUSTERS]
    assert sorted(map(sorted, map(members, clusters))) == [
        ["session_04/2024-05-01_10-00-00.mcap"],
        ["session_04/2024-05-03_09-00-00.mcap"],
    ]
    assert all(p.contested == (whole.id,) for p in clusters)
    assert set(whole.contested) == {c.id for c in clusters}
    assert codes(grouping) == [CONTESTED]


def test_the_same_bytes_in_two_runs_stay_two_sessions_and_each_says_so(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "copies"))
    assert set(by_members(grouping)) == {
        frozenset({"copies/run_1/flight.ulg"}),
        frozenset({"copies/run_2/flight.ulg"}),
    }
    for proposal in grouping.proposals:
        [same] = [r for r in proposal.reasons if r.rule == Rule.SAME_BYTES]
        other = (
            "copies/run_2/flight.ulg"
            if "copies/run_1/flight.ulg" in members(proposal)
            else ("copies/run_1/flight.ulg")
        )
        assert same.details["same_as"] == [{"kind": "local", "path": other}]
        assert proposal.status is Status.PROPOSED


def test_recordings_seconds_apart_are_contested_between_one_session_and_two(
    tmp_path: Path,
) -> None:
    grouping = group(built(tmp_path, "trials"))
    rules = sorted(p.rule for p in grouping.proposals)
    assert rules == [Rule.NAME_TIME_PROXIMITY, Rule.RECORDING_FILE, Rule.RECORDING_FILE]
    assert all(p.status is Status.CONTESTED for p in grouping.proposals)
    assert codes(grouping) == [CONTESTED]
    # Tightening the gap below 25 s makes them two sessions, uncontested.
    tight = group(built(tmp_path, "trials"), GroupingConfig(gap_seconds=10))
    assert [p.status for p in tight.proposals] == [Status.PROPOSED, Status.PROPOSED]
    assert tight.transform.id != grouping.transform.id  # the gap is the transform's config


def test_a_directory_holding_two_session_directories_is_a_collection(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "campaign"))
    assert set(by_members(grouping)) == {
        frozenset({"campaign_2024-05-01_08-00-00/run_1/a.mcap"}),
        frozenset({"campaign_2024-05-01_08-00-00/run_2/b.mcap"}),
    }
    assert unassigned(grouping) == {
        "campaign_2024-05-01_08-00-00/plan.pdf": (Placement.UNKNOWN, "no_session", 0)
    }


def test_a_session_directory_holding_one_more_is_contested_with_the_readings_inside(
    tmp_path: Path,
) -> None:
    grouping = group(built(tmp_path, "drive"))
    camera = "drive_07/camera_2024-05-01_12-30-00/"
    found = by_members(grouping)
    outer = found[
        frozenset({"drive_07/robot.mcap", camera + "frame_0001.png", camera + "frame_0002.png"})
    ]
    inner = found[frozenset({camera + "frame_0001.png", camera + "frame_0002.png"})]
    robot = found[frozenset({"drive_07/robot.mcap"})]
    assert outer.directory == LocalPath("drive_07") and inner.directory == LocalPath(camera[:-1])
    # The outer reading holds its own file and includes the inner reading, never its files.
    assert members(outer) == {"drive_07/robot.mcap"} and outer.includes == (inner.id,)
    assert set(outer.contested) == {inner.id, robot.id}
    assert outer.reasons[0].details["inner"] == "camera_2024-05-01_12-30-00"
    assert codes(grouping) == [CONTESTED]


def test_recordings_with_nothing_to_join_them_are_never_merged(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "flat"))
    found = by_members(grouping)
    assert set(found) == {
        frozenset({"flat/front.mcap", "flat/front.yaml"}),
        frozenset({"flat/rear.mcap"}),
    }
    [yaml] = [
        m
        for m in found[frozenset({"flat/front.mcap", "flat/front.yaml"})].members
        if m.role is Role.CONTEXT
    ]
    assert yaml.rule == Rule.SHARED_STEM and yaml.confidence == 0.6
    assert unassigned(grouping) == {"flat/robot.yaml": (Placement.AMBIGUOUS, "several_sessions", 2)}
    [finding] = grouping.findings
    assert finding.code == AMBIGUOUS_MEMBER and finding.details["count"] == 1


def test_unicode_and_raw_names_are_grouped_byte_for_byte(tmp_path: Path) -> None:
    root = built(tmp_path, "unicode")
    grouping = group(root)
    [proposal] = grouping.proposals
    assert members(proposal) == {
        "séance_2024-05-01T12-00-00/données.mcap",
        "séance_2024-05-01T12-00-00/notes_ü.txt",
    }
    assert proposal.reasons[0].details["time"] == "2024-05-01T12-00-00"
    if not LAYOUTS.raw_names_supported(tmp_path):
        pytest.skip("this filesystem does not store names that are not UTF-8")
    LAYOUTS.build(root, "raw")
    raw = [p for p in group(root).proposals if isinstance(p.directory, RawLocalPath)]
    [proposal] = raw
    assert proposal.directory == RawLocalPath(b"raw/\xffrun_3\xfe")
    assert isinstance(proposal.members[0].location, RawLocalPath)


def test_links_are_aliases_or_inside_never_members(tmp_path: Path) -> None:
    grouping = group(built(tmp_path, "runs", "links"))
    found = {p.directory: p for p in grouping.proposals}
    run_001, run_002 = found[LocalPath("runs/run_001")], found[LocalPath("runs/run_002")]
    assert [(text(link.location), link.relation) for link in run_002.links] == [
        ("latest", LinkRelation.ALIAS)
    ]
    [inside] = run_001.links
    assert (text(inside.location), inside.relation) == ("runs/run_001/calib.yaml", "inside")
    assert inside.target == b"../../shared/calib.yaml"
    assert all("escape" not in text(link.location) for p in grouping.proposals for link in p.links)
    assert unassigned(grouping)["shared/calib.yaml"] == (Placement.UNKNOWN, "no_session", 0)


# --- The acceptance: grouped correctly or marked ambiguous; no silent cross-run merge ----------

# The true sessions of the messy tree, each with every file it holds: recordings and context.
# Where the names cannot tell (numbered parts, a session directory days apart, trials seconds
# apart, a camera directory inside a drive) the grouper must contest, and one of the contested
# readings must be the true session, context included.
TRUE_RUNS: Final = [
    {
        "ros2_bags/rosbag2_2024_05_01-12_30_00/metadata.yaml",
        "ros2_bags/rosbag2_2024_05_01-12_30_00/rosbag2_2024_05_01-12_30_00_0.db3",
        "ros2_bags/rosbag2_2024_05_01-12_30_00/rosbag2_2024_05_01-12_30_00_1.db3",
    },
    {
        "ros2_bags/rosbag2_2024_05_01-14_00_00/metadata.yaml",
        "ros2_bags/rosbag2_2024_05_01-14_00_00/rosbag2_2024_05_01-14_00_00_0.mcap",
    },
    {"px4/log/2024-05-01/12_30_00.ulg"},
    {"px4/log/2024-05-01/13_45_10.ulg"},
    {"px4/log/2024-05-02/09_00_00.ulg"},
    {"runs/run_001/robot.mcap", "runs/run_001/config.yaml", "runs/run_001/camera/front.mp4"},
    {"runs/run_002/robot.mcap", "runs/run_002/config.yaml", "runs/run_002/Thumbs.db"},
    {
        "split/patrol_2024-05-01-12-30-00_0.bag",
        "split/patrol_2024-05-01-12-30-00_1.bag",
        "split/patrol_2024-05-01-12-30-00_3.bag",
    },
    {"split/patrol_2024-05-01-15-00-00_0.bag"},
    {"parts/x_0.mcap", "parts/x_1.mcap", "parts/x.yaml"},
    {"named_split/run_3_0.bag", "named_split/run_3_1.bag"},
    {"episodes/episode_1.mcap"},
    {"episodes/episode_2.mcap"},
    {"dump/2024-05-01_12-30-00_front.mp4", "dump/2024-05-01_12-30-00_imu.csv"},
    {"dump/2024-05-01_14-02-11_front.mp4", "dump/2024-05-01_14-02-11_imu.csv"},
    {"session_04/2024-05-01_10-00-00.mcap"},
    {"session_04/2024-05-03_09-00-00.mcap"},
    {"copies/run_1/flight.ulg"},
    {"copies/run_2/flight.ulg"},
    {"trials/trial_2024-05-01_12-30-00.mcap"},
    {"trials/trial_2024-05-01_12-30-25.mcap"},
    {"campaign_2024-05-01_08-00-00/run_1/a.mcap"},
    {"campaign_2024-05-01_08-00-00/run_2/b.mcap"},
    {
        "drive_07/robot.mcap",
        "drive_07/camera_2024-05-01_12-30-00/frame_0001.png",
        "drive_07/camera_2024-05-01_12-30-00/frame_0002.png",
    },
    {"flat/front.mcap", "flat/front.yaml"},
    {"flat/rear.mcap"},
    {"séance_2024-05-01T12-00-00/données.mcap", "séance_2024-05-01T12-00-00/notes_ü.txt"},
]
# The run under a directory whose name is not UTF-8, where the filesystem takes one.
RAW_RUN: Final = {"raw/\\xffrun_3\\xfe/log.ulg"}
# Files no true session holds, or that the names cannot place: each must stay unassigned.
STRAYS: Final = {
    ".DS_Store",
    "campaign_2024-05-01_08-00-00/plan.pdf",
    "flat/robot.yaml",
    "px4/log/desktop.ini",
    "ros2_bags/notes.txt",
    "runs/README.md",
    "shared/calib.yaml",
}


def assert_grouped_correctly_or_contested(grouping: Grouping, runs: list[set[str]]) -> None:
    """Every file is in one true session or a stray; no uncontested reading spans two sessions
    or holds a stray; and every true session is a reading, exactly (context too): the only one
    holding its files, or one of readings that all contest."""
    run_of = {path: index for index, run in enumerate(runs) for path in run}
    extents = {p.id: extent(grouping, p) for p in grouping.proposals}
    unplaced = {text(u.location) for u in grouping.unassigned}
    assert set(run_of).isdisjoint(STRAYS)
    assert set(run_of) | STRAYS == set().union(*extents.values()) | unplaced
    assert unplaced == STRAYS
    for proposal in grouping.proposals:
        held = extents[proposal.id]
        assert held.isdisjoint(STRAYS), f"a stray in a session: {sorted(held & STRAYS)}"
        if proposal.status is Status.PROPOSED:
            spanned = {run_of[path] for path in held}
            assert len(spanned) == 1, f"silent cross-run merge: {sorted(held)}"
    for run in runs:
        holders = [p for p in grouping.proposals if extents[p.id] & run]
        exact = [p for p in holders if extents[p.id] == run]
        assert exact, f"no reading is exactly {sorted(run)}"
        alone = holders == exact[:1] and exact[0].status is Status.PROPOSED
        assert alone or all(p.status is Status.CONTESTED for p in holders), sorted(run)


def test_the_messy_tree_is_grouped_correctly_or_marked_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "messy"
    root.mkdir()
    LAYOUTS.build_all(root)
    grouping = group(root)
    raw = [RAW_RUN] if LAYOUTS.raw_names_supported(root) else []
    assert_grouped_correctly_or_contested(grouping, [*TRUE_RUNS, *raw])
    contested = {f.subject for f in grouping.findings if f.code == CONTESTED}
    assert contested == {
        LocalPath("drive_07/camera_2024-05-01_12-30-00/frame_0001.png"),
        LocalPath("named_split/run_3_0.bag"),
        LocalPath("parts/x.yaml"),
        LocalPath("session_04/2024-05-01_10-00-00.mcap"),
        LocalPath("trials/trial_2024-05-01_12-30-00.mcap"),
    }
    assert len(grouping.ranked()) == len(grouping.proposals)
    confidences = [(-p.confidence, p.status is Status.CONTESTED) for p in grouping.ranked()]
    assert confidences == sorted(confidences)


# --- Overrides ---------------------------------------------------------------------------------


def test_a_declared_session_is_stated_and_resolves_the_readings_it_holds_whole(
    tmp_path: Path,
) -> None:
    root = built(tmp_path, "parts", "trials")
    take = DeclaredSession("calibration take", ("parts",))
    grouping = group(root, GroupingConfig(sessions=(take,)))
    [declared] = [p for p in grouping.proposals if p.rule == Rule.DECLARED]
    assert members(declared) == {"parts/x_0.mcap", "parts/x_1.mcap", "parts/x.yaml"}
    assert declared.confidence == 1.0 and declared.status is Status.PROPOSED
    assert declared.directory == LocalPath("parts")
    # Stated, with the declaration as its provenance, under the transform whose config holds it.
    data = declared.to_json()
    assert data["assertion_kind"] == "stated" and declared.declared == (take,)
    assert data["declared"] == [{"name": "calibration take", "paths": ["parts"]}]
    assert session_proposal_from_json(data) == declared
    assert grouping.transform.config["sessions"] == [take.to_json()]
    # The rules read the parts too: the declaration holds each of their readings whole, so it
    # resolves them, and says which.
    stated, held = [r for r in declared.reasons if r.rule == Rule.DECLARED]
    assert stated.details == take.to_json()
    assert held.details["count"] == 3
    assert sorted(rules_of(held.details)) == [
        "numbered_sequence",
        "recording_file",
        "recording_file",
    ]
    assert not [
        p for p in grouping.proposals if p is not declared and members(p) & members(declared)
    ]
    assert all(p.assertion_kind == "inferred" for p in grouping.proposals if p is not declared)
    # The trials are still the rules' to read.
    assert {p.rule for p in grouping.proposals} - {Rule.DECLARED} == {
        Rule.NAME_TIME_PROXIMITY,
        Rule.RECORDING_FILE,
    }
    assert codes(grouping) == [CONTESTED]  # the trials' own


def test_a_stale_declaration_that_cuts_a_session_directory_is_contested_and_said(
    tmp_path: Path,
) -> None:
    # Declared before the camera directory was copied in: it no longer covers the run.
    stale = DeclaredSession("run one", ("runs/run_001/config.yaml", "runs/run_001/robot.mcap"))
    grouping = group(built(tmp_path, "runs"), GroupingConfig(sessions=(stale,)))
    found = by_members(grouping)
    declared = found[frozenset({"runs/run_001/config.yaml", "runs/run_001/robot.mcap"})]
    run_001 = found[
        frozenset(
            {"runs/run_001/robot.mcap", "runs/run_001/config.yaml", "runs/run_001/camera/front.mp4"}
        )
    ]
    assert declared.assertion_kind == "stated" and run_001.rule == Rule.SESSION_DIRECTORY
    assert declared.contested == (run_001.id,) and run_001.contested == (declared.id,)
    assert codes(grouping) == [CONTESTED, DECLARED_CONTRADICTS_LAYOUT]
    [said] = [f for f in grouping.findings if f.code == DECLARED_CONTRADICTS_LAYOUT]
    assert said.category == "inconsistent" and said.subject == LocalPath(stale.paths[0])
    assert said.details["declared"] == stale.to_json() and said.details["count"] == 1
    assert said.details["readings"] == [
        {"directory": run_001.directory.to_json(), "files": 3, "rule": "session_directory"}
    ]
    # The other run is untouched.
    run_002 = {"runs/run_002/robot.mcap", "runs/run_002/config.yaml", "runs/run_002/Thumbs.db"}
    assert found[frozenset(run_002)].status is Status.PROPOSED


def test_a_mistyped_declaration_that_splits_a_rosbag2_directory_is_contested_and_said(
    tmp_path: Path,
) -> None:
    bag = "ros2_bags/rosbag2_2024_05_01-12_30_00"
    typo = DeclaredSession("morning bag", (f"{bag}/metadata.yaml",))  # meant the directory
    grouping = group(built(tmp_path, "ros2_bags"), GroupingConfig(sessions=(typo,)))
    [declared] = [p for p in grouping.proposals if p.rule == Rule.DECLARED]
    [split] = [
        p for p in grouping.proposals if p.rule == Rule.ROSBAG2_DIRECTORY and where(p) == bag
    ]
    assert members(declared) == {f"{bag}/metadata.yaml"} and len(split.members) == 3
    assert declared.contested == (split.id,) and split.contested == (declared.id,)
    [said] = [f for f in grouping.findings if f.code == DECLARED_CONTRADICTS_LAYOUT]
    assert rules_of(said.details) == ["rosbag2_directory"]
    assert unassigned(grouping)["ros2_bags/notes.txt"] == (
        Placement.AMBIGUOUS,
        "several_sessions",
        2,
    )


def test_overlapping_declarations_are_contested_and_an_empty_one_is_a_finding(
    tmp_path: Path,
) -> None:
    root = built(tmp_path, "flat")
    config = GroupingConfig(
        sessions=(
            DeclaredSession("front", ("flat/front.mcap", "flat/robot.yaml")),
            DeclaredSession("all", ("flat",)),
            DeclaredSession("ghost", ("nowhere/at_all",)),
        )
    )
    grouping = group(root, config)
    declared = [p for p in grouping.proposals if p.rule == Rule.DECLARED]
    assert len(declared) == 2 and all(p.status is Status.CONTESTED for p in declared)
    # "front" leaves front.yaml out of the front recording's reading: it cuts through it.
    assert codes(grouping) == [CONTESTED, DECLARATION_UNMATCHED, DECLARED_CONTRADICTS_LAYOUT]
    [ghost] = [f for f in grouping.findings if f.code == DECLARATION_UNMATCHED]
    assert ghost.subject == LocalPath("nowhere/at_all") and ghost.category == "missing"
    front = by_members(grouping)[frozenset({"flat/front.mcap", "flat/front.yaml"})]
    assert front.rule == Rule.RECORDING_FILE and front.status is Status.CONTESTED
    assert grouping.unassigned == ()


def test_a_file_a_declaration_leaves_out_of_every_session_it_could_join_is_said() -> None:
    declared = DeclaredSession("pair", ("d/front.mcap", "d/front.yaml", "d/rear.mcap"))
    grouping = LayoutGrouper(GroupingConfig(sessions=(declared,))).propose(
        synthetic(b"d/front.mcap", b"d/front.yaml", b"d/rear.mcap", b"d/robot.yaml")
    )
    [proposal] = grouping.proposals
    assert proposal.declared == (declared,) and proposal.status is Status.PROPOSED
    # robot.yaml could join either recording's session; the declaration holds both but not it.
    assert unassigned(grouping) == {"d/robot.yaml": (Placement.UNKNOWN, OUTSIDE_DECLARATION, 0)}


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (lambda: DeclaredSession("x", ()), "at least one path"),
        (lambda: DeclaredSession("x", ("../up",)), "path must be relative"),
        (lambda: DeclaredSession("x", ("/abs",)), "path must be relative"),
        (lambda: DeclaredSession("x", ("a", "a")), "twice"),
        (lambda: DeclaredSession("", ("a",)), "non-empty"),
        (lambda: GroupingConfig(gap_seconds=-1), "negative"),
        (lambda: GroupingConfig(gap_seconds=True), "integer"),
        (lambda: GroupingConfig(gap_seconds=1.5), "integer"),  # type: ignore[arg-type]
        (
            lambda: GroupingConfig(
                sessions=(DeclaredSession("a", ("x",)), DeclaredSession("a", ("y",)))
            ),
            "distinct names",
        ),
    ],
)
def test_a_bad_config_is_refused(make: object, error: str) -> None:
    with pytest.raises((ValueError, TypeError), match=error):
        make()  # type: ignore[operator]


def test_two_declarations_of_the_same_files_are_one_proposal_naming_both() -> None:
    config = GroupingConfig(
        sessions=(
            DeclaredSession("A", ("x",)),
            DeclaredSession("B", ("x/a.bag", "x/b.bag")),
        )
    )
    grouping = LayoutGrouper(config).propose(synthetic(b"x/a.bag", b"x/b.bag"))
    [proposal] = grouping.proposals
    names = [r.details["name"] for r in proposal.reasons if "name" in r.details]
    assert names == ["A", "B"] and proposal.status is Status.PROPOSED
    assert [d.name for d in proposal.declared] == ["A", "B"]
    assert proposal.to_json()["assertion_kind"] == "stated"


def test_a_sidecar_goes_with_its_recording_in_every_reading() -> None:
    grouping = LayoutGrouper().propose(
        synthetic(b"d/patrol_1.bag", b"d/patrol_2.bag", b"d/patrol_1.yaml")
    )
    holding = [p for p in grouping.proposals if LocalPath("d/patrol_1.yaml") in locations(p)]
    assert sorted(p.rule for p in holding) == [Rule.NUMBERED_SEQUENCE, Rule.RECORDING_FILE]
    assert grouping.unassigned == ()
    # Two different recordings sharing a stem are not readings of one file: still ambiguous.
    apart = LayoutGrouper().propose(synthetic(b"d/run.mcap", b"d/run.bag", b"d/run.yaml"))
    assert [(u.placement, u.reason) for u in apart.unassigned] == [
        (Placement.AMBIGUOUS, "several_stems")
    ]
    assert all(p.status is Status.PROPOSED for p in apart.proposals)


def locations(proposal: SessionProposal) -> set[LocalPath | RawLocalPath]:
    return {member.location for member in proposal.members}


def test_a_config_round_trips_and_its_order_never_shows() -> None:
    config = GroupingConfig(30, (DeclaredSession("b", ("z", "a")), DeclaredSession("a", ("q",))))
    same = GroupingConfig(30, (DeclaredSession("a", ("q",)), DeclaredSession("b", ("a", "z"))))
    assert config == same and config.to_json() == same.to_json()
    assert grouping_config_from_json(config.to_json()) == config
    assert LayoutGrouper(config).transform == LayoutGrouper(same).transform
    with pytest.raises(ValueError):
        grouping_config_from_json({"gap_seconds": 1})


# --- Boundaries --------------------------------------------------------------------------------


def synthetic(*paths: bytes) -> Layout:
    files = []
    for path in paths:
        location = local_location(path)
        content = content_id(path)
        files.append(LayoutFile(revision_id(location, content, ()), location, content))
    return layout_of(files)


def test_an_empty_layout_proposes_nothing() -> None:
    grouping = LayoutGrouper().propose(Layout(()))
    assert grouping.proposals == () and grouping.unassigned == () and grouping.findings == ()
    assert {kind: list(lines) for kind, lines in grouping.tables().items()} == {
        "session_proposal": [],
        "session_unassigned": [],
    }


def test_one_recording_at_the_root_is_one_session_at_the_root() -> None:
    [proposal] = LayoutGrouper().propose(synthetic(b"a.mcap")).proposals
    assert proposal.directory is ROOT_DIRECTORY and proposal.rule == Rule.RECORDING_FILE


def test_one_note_at_the_root_is_unknown() -> None:
    grouping = LayoutGrouper().propose(synthetic(b"README"))
    assert grouping.proposals == ()
    assert [u.placement for u in grouping.unassigned] == [Placement.UNKNOWN]


def test_a_root_that_is_a_rosbag2_directory_is_one_recording() -> None:
    grouping = LayoutGrouper().propose(synthetic(b"metadata.yaml", b"x_0.db3", b"notes.txt"))
    [proposal] = grouping.proposals
    assert proposal.rule == Rule.ROSBAG2_DIRECTORY and proposal.directory is ROOT_DIRECTORY
    assert {m.role for m in proposal.members} == {Role.RECORDING, Role.CONTEXT}


def test_a_broken_grouping_is_caught() -> None:
    layout = synthetic(b"a.mcap", b"b.mcap")
    grouping = LayoutGrouper().propose(layout)
    with pytest.raises(ValueError, match="neither proposed nor unassigned"):
        check_grouping(Grouping(grouping.transform, grouping.proposals[:1], (), ()), layout)
    with pytest.raises(ValueError, match="does not"):
        check_grouping(grouping, synthetic(b"a.mcap"))


# --- Determinism -------------------------------------------------------------------------------


def as_json(grouping: Grouping) -> bytes:
    return canonical_json.dumps(
        {
            "findings": [f.to_json() for f in grouping.findings],
            "tables": {kind: list(lines) for kind, lines in grouping.tables().items()},
            "transform": grouping.transform.to_json(),
        }
    )


def test_the_same_tree_built_in_another_order_gives_the_same_bytes(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    LAYOUTS.build_all(first)
    for name in reversed(list(LAYOUTS.LAYOUTS)):
        LAYOUTS.build(second, name)
    if LAYOUTS.raw_names_supported(second):
        LAYOUTS.build(second, "raw")
    assert as_json(group(first)) == as_json(group(second))


_SCRIPT: Final = """
import sys
from pathlib import Path
from neptune.derived.grouping import LayoutGrouper
from neptune.discovery.layout import layout_from_scan
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
from neptune.identity.revisions import SourceLedger

result = scan(LocalSource(Path(sys.argv[1])), SourceLedger())
grouping = LayoutGrouper().propose(layout_from_scan(result.observations, result.symlinks))
tables = {
    "f": [f.to_json() for f in grouping.findings],
    "t": {kind: list(lines) for kind, lines in grouping.tables().items()},
}
sys.stdout.buffer.write(canonical_json.dumps(tables))
"""


def test_the_grouping_never_depends_on_hash_order(tmp_path: Path) -> None:
    root = tmp_path / "messy"
    root.mkdir()
    LAYOUTS.build_all(root)
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run(
            [sys.executable, "-c", _SCRIPT, str(root)], env=env, capture_output=True, check=True
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1
    assert json.loads(outputs.pop())["t"]["session_proposal"]


_COMPONENTS: Final = [
    b"run_1",
    b"run_2",
    b"2024-05-01_12-00-00",
    b"rosbag2_2024_05_01-12_00_00",
    b"day",
    b"\xff",
]
_NAMES: Final = [
    b"a.mcap",
    b"a.yaml",
    b"x_0.bag",
    b"x_1.bag",
    b"metadata.yaml",
    b"s_0.db3",
    b"2024-05-01_12-00-00_cam.mp4",
    b"2024-05-01_12-00-30_cam.mp4",
    b"2024-05-01_12-00-00.ulg",
    b"notes.txt",
]


@st.composite
def trees(draw: st.DrawFn) -> list[bytes]:
    paths: set[bytes] = set()
    for _ in range(draw(st.integers(0, 14))):
        depth = draw(st.integers(0, 3))
        directories = [draw(st.sampled_from(_COMPONENTS)) for _ in range(depth)]
        paths.add(b"/".join([*directories, draw(st.sampled_from(_NAMES))]))
    return sorted(paths)


@settings(max_examples=150, deadline=None)
@given(trees(), st.randoms(use_true_random=False))
def test_any_tree_keeps_the_laws_whatever_order_it_is_given_in(
    paths: list[bytes], shuffle: object
) -> None:
    layout = synthetic(*paths)
    grouping = LayoutGrouper().propose(layout)  # check_grouping runs inside
    reordered = list(layout.files)
    shuffle.shuffle(reordered)  # type: ignore[attr-defined]
    again = LayoutGrouper().propose(layout_of(reordered))
    assert as_json(again) == as_json(grouping)
    held = [m.revision for p in grouping.proposals for m in p.members]
    placed = {u.revision for u in grouping.unassigned}
    assert set(held) | placed == {f.revision for f in layout.files}
    for proposal in grouping.proposals:
        assert proposal.confidence in CONFIDENCE.values()


@st.composite
def declared_trees(draw: st.DrawFn) -> tuple[list[bytes], GroupingConfig]:
    """A tree, and up to three sessions declared over its files and directories (UTF-8 only)."""
    paths = draw(trees())
    places = sorted(
        {
            text
            for path in paths
            for cut in range(path.count(b"/") + 1)
            if (text := b"/".join(path.split(b"/")[: cut + 1]).decode("utf-8", "replace"))
            and "�" not in text
        }
    )
    sessions = []
    if places:
        for number in range(draw(st.integers(0, 3))):
            chosen = draw(st.lists(st.sampled_from(places), min_size=1, max_size=3, unique=True))
            sessions.append(DeclaredSession(f"s{number}", tuple(chosen)))
    return paths, GroupingConfig(sessions=tuple(sessions))


@settings(max_examples=150, deadline=None)
@given(declared_trees())
def test_any_declarations_over_any_tree_keep_the_laws(
    case: tuple[list[bytes], GroupingConfig],
) -> None:
    paths, config = case
    layout = synthetic(*paths)
    grouping = LayoutGrouper(config).propose(layout)  # check_grouping runs inside
    assert as_json(LayoutGrouper(config).propose(layout_of(layout.files[::-1]))) == as_json(
        grouping
    )
    for proposal in grouping.proposals:
        assert (proposal.assertion_kind == "stated") == (proposal.rule == Rule.DECLARED)
    # A declaration contradicts the layout exactly when it shares files with a rule's reading
    # that also holds files outside it; then they contest.
    said = set()
    for finding in grouping.findings:
        if finding.code == DECLARED_CONTRADICTS_LAYOUT:
            declaration = finding.details["declared"]
            assert isinstance(declaration, dict)
            said.add(declaration["name"])
    for proposal in grouping.proposals:
        if proposal.rule != Rule.DECLARED:
            continue
        inside = set(members(proposal))
        cut = [
            p
            for p in grouping.proposals
            if p.rule != Rule.DECLARED
            and (whole := extent(grouping, p)) & inside
            and not whole <= inside
        ]
        names = {d.name for d in proposal.declared}
        assert bool(cut) == bool(names & said)
        assert all(p.id in proposal.contested for p in cut)


def paths_of(proposals: Iterable[SessionProposal]) -> list[list[str]]:
    return sorted(sorted(members(p)) for p in proposals)


# --- Hostile names and sizes -------------------------------------------------------------------


def test_part_numbers_far_apart_cost_nothing_and_are_counted() -> None:
    huge = 10**18
    layout = synthetic(b"x_1.bag", b"x_" + str(huge).encode() + b".bag")
    [whole] = [p for p in LayoutGrouper().propose(layout).proposals if len(p.members) == 2]
    details = whole.reasons[0].details
    assert details["missing_count"] == huge - 2
    assert details["missing"] == list(range(2, 66))


def test_duplicates_say_so_once_per_proposal_and_empty_files_never() -> None:
    files = []
    for index in range(300):
        for name, data in ((b"same.yaml", b"the same bytes"), (b".gitkeep", b"")):
            location = local_location(b"run_%d/" % index + name)
            content = content_id(data)
            files.append(LayoutFile(revision_id(location, content, ()), location, content))
    grouping = LayoutGrouper().propose(layout_of(files))
    assert len(grouping.proposals) == 300
    for proposal in grouping.proposals:
        [same] = [r for r in proposal.reasons if r.rule == Rule.SAME_BYTES]
        assert same.details["count"] == 299
        assert same.details["locations"] == [proposal.members[1].location.to_json()]
        listed = same.details["same_as"]
        assert isinstance(listed, list) and len(listed) == 8


def test_wide_directories_group_in_near_linear_time_with_bounded_candidates() -> None:
    n = 2000
    paths = [
        *(b"flat/a%05d.mcap" % i for i in range(n)),
        *(b"flat/n%05d.txt" % i for i in range(n)),
        *(b"drive_1/camera_2024-05-01_12-30-00/f%05d.png" % i for i in range(n)),
        *(b"drive_1/b%05d.mcap" % i for i in range(n)),
        *(b"run_%05d/x.mcap" % i for i in range(n)),
    ]
    started = time.perf_counter()
    grouping = LayoutGrouper().propose(synthetic(*paths))
    assert time.perf_counter() - started < 15  # about 0.5 s; all-pairs work would take minutes
    notes = [u for u in grouping.unassigned if text(u.location).startswith("flat/n")]
    assert len(notes) == n
    assert {(u.placement, u.reason) for u in notes} == {(Placement.UNKNOWN, "too_many_sessions")}
    [outer] = [
        p
        for p in grouping.proposals
        if p.directory == LocalPath("drive_1") and p.rule == Rule.SESSION_DIRECTORY
    ]
    assert len(outer.contested) == n + 1  # every recording beside the camera, and the camera
    inner = [p for p in grouping.proposals if outer.id in p.contested]
    assert all(p.contested == (outer.id,) for p in inner)


def chain(depth: int, name: bytes = b"notes.txt") -> Layout:
    """``run_1/`` nested ``depth`` deep, with a file of its own at every level."""
    return synthetic(*(b"run_1/" * level + name for level in range(1, depth + 1)))


def table_bytes(grouping: Grouping) -> int:
    lines = (line for table in grouping.tables().values() for line in table)
    return sum(len(canonical_json.dumps(line)) + 1 for line in lines)


def test_a_reading_of_nested_session_directories_includes_the_inner_one_by_id() -> None:
    depth = 400
    layout = chain(depth)
    started = time.perf_counter()
    grouping = LayoutGrouper().propose(layout)
    size = table_bytes(grouping)
    assert time.perf_counter() - started < 1  # about 0.02 s; a reading of every file below
    assert size < 1_000_000  # each level took minutes and ~depth^3 bytes
    outers = [p for p in grouping.proposals if p.includes]
    assert (
        len(outers) == OUTER_NESTING_LIMIT - 1
    )  # the leaf spans one directory, each outer one more
    by_id = {p.id: p for p in grouping.proposals}
    for outer in outers:
        [own] = outer.members
        assert text(own.location) == where(outer) + "/notes.txt"
        [inner] = outer.includes
        assert inner in outer.contested and outer.id in by_id[inner].contested
    deepest = max(outers, key=lambda p: len(p.members[0].location.raw))
    assert len(grouping.extent(deepest)) == 2
    [limit] = [f for f in grouping.findings if f.code == DEPTH_LIMIT]
    above = depth - OUTER_NESTING_LIMIT
    assert limit.subject == local_location(b"/".join([b"run_1"] * above))
    assert limit.category == "limit" and limit.details["limit"] == OUTER_NESTING_LIMIT
    # Above the limit, each level's note stays where the rules leave it: unassigned.
    assert len(grouping.unassigned) == above


def test_a_chain_far_past_the_limit_costs_its_files_not_its_depth_squared() -> None:
    layout = chain(2000)
    paths = sum(len(file.path) for file in layout.files)
    tracemalloc.start()
    try:
        grouping = LayoutGrouper().propose(layout)
        size = table_bytes(grouping)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # About 40 MB for 12 MB of path bytes; a reading of every file below every level would be
    # two million members of up to 12 KB each.
    assert peak < 200_000_000
    assert size < 2 * paths
    assert [f.code for f in grouping.findings].count(DEPTH_LIMIT) == 1
    assert len(grouping.proposals) == OUTER_NESTING_LIMIT


def test_a_directory_whose_inner_one_is_a_collection_is_a_collection_too() -> None:
    grouping = LayoutGrouper().propose(
        synthetic(
            b"drive_1/plan.pdf",
            b"drive_1/day_run_1/run_1/a.mcap",
            b"drive_1/day_run_1/run_2/b.mcap",
        )
    )
    assert sorted(where(p) for p in grouping.proposals) == [
        "drive_1/day_run_1/run_1",
        "drive_1/day_run_1/run_2",
    ]
    assert unassigned(grouping) == {"drive_1/plan.pdf": (Placement.UNKNOWN, "no_session", 0)}


def test_an_outer_reading_keeps_a_note_ambiguous_among_the_sessions_beside_it() -> None:
    grouping = LayoutGrouper().propose(
        synthetic(
            b"drive_1/a.mcap",
            b"drive_1/b.mcap",
            b"drive_1/notes.txt",
            b"drive_1/camera_2024-05-01_12-30-00/f.png",
        )
    )
    found = by_members(grouping)
    outer = next(p for p in grouping.proposals if p.includes)
    assert members(outer) == {"drive_1/a.mcap", "drive_1/b.mcap", "drive_1/notes.txt"}
    a, b = found[frozenset({"drive_1/a.mcap"})], found[frozenset({"drive_1/b.mcap"})]
    # The outer reading holds the note; the readings inside leave it ambiguous between a and b.
    # Both records stand, so a consumer choosing either reading knows where the note is.
    [note] = grouping.unassigned
    assert text(note.location) == "drive_1/notes.txt"
    assert note.placement is Placement.AMBIGUOUS and set(note.candidates) == {a.id, b.id}
    assert AMBIGUOUS_MEMBER in codes(grouping)
    assert {a.id, b.id} <= set(outer.contested)


def test_a_broken_include_or_contest_is_caught() -> None:
    layout = synthetic(b"drive_1/a.mcap", b"drive_1/camera_2024-05-01_12-30-00/f.png")
    grouping = LayoutGrouper().propose(layout)
    outer = next(p for p in grouping.proposals if p.includes)
    others = tuple(p for p in grouping.proposals if p is not outer)
    nowhere = others[0].members[0].revision  # a record id, but no proposal's
    ghost = session_proposal(
        transform=grouping.transform.id,
        rule=outer.rule,
        confidence=outer.confidence,
        directory=outer.directory,
        members=outer.members,
        includes=[nowhere],
        contested=[nowhere],
    )
    with pytest.raises(ValueError, match="no proposal of this grouping"):
        check_grouping(replace(grouping, proposals=(*others, ghost)), layout)
    parts = LayoutGrouper().propose(synthetic(b"x_0.bag", b"x_1.bag"))
    lonely = tuple(replace(p, contested=(), status=Status.PROPOSED) for p in parts.proposals)
    with pytest.raises(ValueError, match="contest exactly"):
        check_grouping(replace(parts, proposals=lonely), synthetic(b"x_0.bag", b"x_1.bag"))
