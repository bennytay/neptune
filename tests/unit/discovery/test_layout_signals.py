"""The observed layout: name grammars, link targets, and layout shape (ADR 0036 §1-§2)."""

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.discovery.layout import (
    ROOT,
    CivilTime,
    Layout,
    LayoutFile,
    LayoutLink,
    ancestors,
    inside,
    layout_of,
    name_signals,
)
from neptune.identity.hashing import content_id
from neptune.identity.revisions import revision_id
from neptune.model.ids import RecordId
from neptune.model.source import LocalPath, RawLocalPath, local_location


def file(path: bytes) -> LayoutFile:
    location = local_location(path)
    content = content_id(path)
    return LayoutFile(revision_id(location, content, ()), location, content)


# --- Civil date-times in names -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (b"2024-05-01_12-30-00.mcap", "2024-05-01_12-30-00"),
        (b"patrol_2024-05-01-12-30-00_0.bag", "2024-05-01-12-30-00"),  # rosbag1 --split
        (b"rosbag2_2024_05_01-12_30_00", "2024_05_01-12_30_00"),  # rosbag2's default
        (b"20240501T123000.mcap", "20240501T123000"),
        (b"20240501_123000", "20240501_123000"),
        (b"2024-05-01T12:30:00Z.json", "2024-05-01T12:30:00"),
        (b"s\xc3\xa9ance_2024-05-01T12-30-00", "2024-05-01T12-30-00"),
        (b"cam 2024.05.01 12.30.00.mp4", "2024.05.01 12.30.00"),
    ],
)
def test_a_name_states_a_civil_time_in_many_spellings(name: bytes, text: str) -> None:
    time = name_signals(name).time
    assert time is not None and time.text == text
    assert (time.year, time.month, time.day, time.hour, time.minute) == (2024, 5, 1, 12, 30)


@pytest.mark.parametrize(
    "name",
    [
        b"2024-13-01_12-30-00",  # no month 13
        b"2023-02-29_12-30-00",  # not a leap year
        b"2024-05-01_24-00-00",  # no hour 24
        b"2024-05-01_12-60-00",
        b"0000-01-01_00-00-00",  # no year 0
        b"12345678901234",  # a serial number is not a date
        b"x2024-05-01_12-30-001",  # digits touching both ends
        b"2024-05_01_12-30-00",  # separators that disagree
        b"2024-05-01",  # a date alone is a day, not a time
        b"12_30_00.ulg",  # a time of day alone
    ],
)
def test_what_is_not_a_valid_civil_time_is_not_read_as_one(name: bytes) -> None:
    assert name_signals(name).time is None


def test_civil_seconds_count_on_the_names_own_clock() -> None:
    time = name_signals(b"1970-01-02_00-00-01").time
    assert time is not None and time.seconds == 86_401
    leap = name_signals(b"2024-02-29_00-00-00").time
    assert leap == CivilTime("2024-02-29_00-00-00", 2024, 2, 29, 0, 0, 0)


# --- Parts, keywords, stems --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "part"),
    [
        (b"patrol_2024-05-01-12-30-00_0.bag", (b"patrol_2024-05-01-12-30-00", b"0")),
        (b"x_1.mcap", (b"x", b"1")),
        (b"take-0012.mcap", (b"take", b"0012")),
        (b"a_1_2.bag", (b"a_1", b"2")),
        (b"2024-05-01-12-30-00.mcap", None),  # the time's own seconds are no part number
        (b"2024-05-01.mcap", None),  # nor a date's day
        (b"12_30_00.ulg", None),  # nor a PX4 time of day's seconds
        (b"robot.mcap", None),
        (b"x_.mcap", None),
    ],
)
def test_a_part_number_never_eats_a_date_or_time(
    name: bytes, part: tuple[bytes, bytes] | None
) -> None:
    assert name_signals(name).part == part


@pytest.mark.parametrize(
    ("name", "keyword"),
    [
        (b"run_007", True),
        (b"Episode-12", True),
        (b"flight3", True),
        (b"session 2", True),
        (b"\xffrun_3\xfe", True),
        (b"session_A", False),  # a keyword needs a number
        (b"log", False),
        (b"log_2024-05-01", False),  # a date is not a session number
        (b"run_20240501", False),
        (b"prune_1", False),  # inside a word
        (b"rosbag2_2024_05_01-12_30_00", False),
    ],
)
def test_a_session_keyword_needs_its_own_number(name: bytes, keyword: bool) -> None:
    assert name_signals(name).keyword is keyword


@pytest.mark.parametrize(
    ("name", "base", "stem", "extension"),
    [
        (b"flight_03.params.yaml", b"flight_03", b"flight_03.params", "yaml"),
        (b"ROBOT.MCAP", b"ROBOT", b"ROBOT", "mcap"),
        (b".DS_Store", b".DS_Store", b".DS_Store", ""),
        (b".hidden.yaml", b".hidden", b".hidden", "yaml"),
        (b"...", b"...", b"...", ""),
        (b"x.", b"x", b"x", ""),
        (b"README", b"README", b"README", ""),
        (b"log.\xd0\xb4", b"log", b"log", ""),  # an extension that is not ASCII is none
    ],
)
def test_base_stem_and_extension(name: bytes, base: bytes, stem: bytes, extension: str) -> None:
    signals = name_signals(name)
    assert (signals.base, signals.stem, signals.extension) == (base, stem, extension)


@pytest.mark.parametrize("name", [b"", b"a/b", "text"])
def test_a_name_is_one_non_empty_component(name: object) -> None:
    with pytest.raises(ValueError, match="one non-empty path component"):
        name_signals(name)  # type: ignore[arg-type]


@given(st.binary(min_size=1, max_size=40).filter(lambda b: b"/" not in b))
def test_any_name_reads_without_error_and_the_same_every_time(name: bytes) -> None:
    assert name_signals(name) == name_signals(name)


# --- Paths and links ---------------------------------------------------------------------------


def test_paths_walk_up_to_the_root() -> None:
    assert ancestors(b"a/b/c.mcap") == (b"a/b", b"a", ROOT)
    assert ancestors(b"top.mcap") == (ROOT,)
    assert inside(b"a/b", ROOT) and inside(b"a/b", b"a") and not inside(b"ab/c", b"a")


@pytest.mark.parametrize(
    ("link", "target", "resolved"),
    [
        (b"latest", b"runs/run_002", b"runs/run_002"),
        (b"runs/run_001/calib.yaml", b"../../shared/calib.yaml", b"shared/calib.yaml"),
        (b"a/self", b".", b"a"),
        (b"a/up", b"..", ROOT),
        (b"escape", b"/etc/passwd", None),  # absolute: host state
        (b"a/out", b"../../x", None),  # leaves the root
        (b"loop", b"loop", b"loop"),
    ],
)
def test_a_link_resolves_lexically_and_never_on_disk(
    link: bytes, target: bytes, resolved: bytes | None
) -> None:
    assert LayoutLink(local_location(link), target).resolved == resolved


def test_a_link_needs_a_target() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        LayoutLink(LocalPath("x"), b"")


# --- The layout --------------------------------------------------------------------------------


def test_a_layout_is_the_same_whatever_order_its_entries_came_in() -> None:
    files = [file(p) for p in (b"b/x.mcap", b"a/y.mcap", b"a/b/z.txt", b"\xff/raw.bag")]
    links = [LayoutLink(LocalPath("l2"), b"a"), LayoutLink(LocalPath("l1"), b"b")]
    first = layout_of(files, links)
    shuffled = list(files)
    random.Random(7).shuffle(shuffled)
    assert layout_of(shuffled, reversed(links)) == first
    assert [f.path for f in first.files] == sorted(f.path for f in files)
    assert isinstance(first.files[-1].location, RawLocalPath)
    assert first.directories() == (ROOT, b"a", b"a/b", b"b", b"\xff")


def test_a_layout_refuses_what_no_walk_could_see() -> None:
    a, b = file(b"a"), file(b"b")
    with pytest.raises(ValueError, match="sorted"):
        Layout((b, a))
    with pytest.raises(ValueError, match="sorted"):
        Layout((a, a))
    with pytest.raises(ValueError, match="both a file and a link"):
        Layout((a,), (LayoutLink(LocalPath("a"), b"b"),))
    with pytest.raises(ValueError, match="which is a file"):
        layout_of([a, file(b"a/inner")])
    with pytest.raises(TypeError):
        Layout([a])  # type: ignore[arg-type]


def test_a_layout_file_is_a_local_revision() -> None:
    good = file(b"x")
    with pytest.raises(ValueError):
        LayoutFile(RecordId("not-an-id"), good.location, good.content_id)
    with pytest.raises(TypeError):
        LayoutFile(good.revision, "x", good.content_id)  # type: ignore[arg-type]
