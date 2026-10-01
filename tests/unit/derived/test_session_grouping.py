"""v0 session grouping over messy layouts: rules, conflicts, overrides, determinism (ADR 0036)."""

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterable
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
)
from neptune.discovery.layout import Layout, LayoutFile, layout_from_scan, layout_of
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.revisions import SourceLedger, revision_id
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


def members(proposal: SessionProposal) -> set[str]:
    return {text(member.location) for member in proposal.members}


def recordings(proposal: SessionProposal) -> set[str]:
    return {text(m.location) for m in proposal.members if m.role is Role.RECORDING}


def by_members(grouping: Grouping) -> dict[frozenset[str], SessionProposal]:
    return {frozenset(members(p)): p for p in grouping.proposals}


def unassigned(grouping: Grouping) -> dict[str, tuple[Placement, str, int]]:
    return {
        text(u.location): (u.placement, u.reason, len(u.candidates)) for u in grouping.unassigned
    }


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
    for proposal in (whole, first, second):
        assert proposal.status is Status.CONTESTED
        others = {p.id for p in (whole, first, second)} - {proposal.id}
        assert set(proposal.contested) == others
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
    assert all(set(p.contested) == {whole.id, *(c.id for c in clusters)} - {p.id} for p in clusters)
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

# The true runs of the messy tree, by their recordings. Where the names cannot tell (numbered
# parts, a session directory days apart, trials seconds apart, a camera directory inside a drive)
# the grouper must contest, not choose.
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
    {"runs/run_001/robot.mcap"},
    {"runs/run_002/robot.mcap"},
    {
        "split/patrol_2024-05-01-12-30-00_0.bag",
        "split/patrol_2024-05-01-12-30-00_1.bag",
        "split/patrol_2024-05-01-12-30-00_3.bag",
    },
    {"split/patrol_2024-05-01-15-00-00_0.bag"},
    {"parts/x_0.mcap", "parts/x_1.mcap"},
    {"episodes/episode_1.mcap"},
    {"episodes/episode_2.mcap"},
    {"session_04/2024-05-01_10-00-00.mcap"},
    {"session_04/2024-05-03_09-00-00.mcap"},
    {"copies/run_1/flight.ulg"},
    {"copies/run_2/flight.ulg"},
    {"trials/trial_2024-05-01_12-30-00.mcap"},
    {"trials/trial_2024-05-01_12-30-25.mcap"},
    {"campaign_2024-05-01_08-00-00/run_1/a.mcap"},
    {"campaign_2024-05-01_08-00-00/run_2/b.mcap"},
    {"drive_07/robot.mcap"},
    {"flat/front.mcap"},
    {"flat/rear.mcap"},
    {"séance_2024-05-01T12-00-00/données.mcap"},
]


def assert_grouped_correctly_or_contested(grouping: Grouping) -> None:
    run_of = {path: index for index, run in enumerate(TRUE_RUNS) for path in run}
    for proposal in grouping.proposals:
        runs = {run_of[path] for path in recordings(proposal) if path in run_of}
        if proposal.status is Status.PROPOSED:
            assert len(runs) <= 1, f"silent cross-run merge: {sorted(recordings(proposal))}"
    for run in TRUE_RUNS:
        holders = [p for p in grouping.proposals if recordings(p) & run]
        exact = [p for p in holders if p.status is Status.PROPOSED and recordings(p) == run]
        assert exact or all(p.status is Status.CONTESTED for p in holders), sorted(run)


def test_the_messy_tree_is_grouped_correctly_or_marked_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "messy"
    root.mkdir()
    LAYOUTS.build_all(root)
    grouping = group(root)
    assert_grouped_correctly_or_contested(grouping)
    contested = {f.subject for f in grouping.findings if f.code == CONTESTED}
    assert contested == {
        LocalPath("drive_07/camera_2024-05-01_12-30-00/frame_0001.png"),
        LocalPath("parts/x.yaml"),
        LocalPath("session_04/2024-05-01_10-00-00.mcap"),
        LocalPath("trials/trial_2024-05-01_12-30-00.mcap"),
    }
    assert len(grouping.ranked()) == len(grouping.proposals)
    confidences = [(-p.confidence, p.status is Status.CONTESTED) for p in grouping.ranked()]
    assert confidences == sorted(confidences)


# --- Overrides ---------------------------------------------------------------------------------


def test_a_declared_session_overrides_every_rule(tmp_path: Path) -> None:
    root = built(tmp_path, "parts", "trials")
    config = GroupingConfig(sessions=(DeclaredSession("calibration take", ("parts",)),))
    grouping = group(root, config)
    [declared] = [p for p in grouping.proposals if p.rule == Rule.DECLARED]
    assert members(declared) == {"parts/x_0.mcap", "parts/x_1.mcap", "parts/x.yaml"}
    assert declared.confidence == 1.0 and declared.status is Status.PROPOSED
    assert declared.directory == LocalPath("parts")
    assert declared.reasons[0].details == {"name": "calibration take", "paths": ["parts"]}
    assert grouping.transform.config["sessions"] == [
        {"name": "calibration take", "paths": ["parts"]}
    ]
    # The trials are still the rules' to read.
    assert {p.rule for p in grouping.proposals} - {Rule.DECLARED} == {
        Rule.NAME_TIME_PROXIMITY,
        Rule.RECORDING_FILE,
    }


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
    assert codes(grouping) == [CONTESTED, DECLARATION_UNMATCHED]
    [ghost] = [f for f in grouping.findings if f.code == DECLARATION_UNMATCHED]
    assert ghost.subject == LocalPath("nowhere/at_all") and ghost.category == "missing"
    assert grouping.unassigned == ()


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
    assert grouping.tables() == {"session_proposal": [], "session_unassigned": []}


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
            "tables": grouping.tables(),
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
tables = {"f": [f.to_json() for f in grouping.findings], "t": grouping.tables()}
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
