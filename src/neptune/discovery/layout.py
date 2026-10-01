"""The observed layout of one scan: where files and links sit, and what their names say (ADR 0036).

Grouping (stage 5) reads this and nothing else in v0. Everything here is an observation about
locations, never a claim about sessions: which directory a file sits in, what a link's target
says, and the tokens a name carries by fixed grammars (a civil date-time, a numeric part suffix,
a session keyword followed by a number). What they mean is the derived grouper's to say
(``neptune.derived.grouping``).

The layout holds no file contents, no modification times and no probe verdicts. It is a function
of a scan's locations and links alone, so it recomputes from a package's revision table and its
symlink findings, and the same tree gives the same layout wherever, whenever and in whatever
order it is walked. Paths are root-relative bytes throughout, ``b""`` being the root, so names
that are not UTF-8 are read exactly as the filesystem holds them (ADR 0010).
"""

import calendar
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from neptune.discovery.source import SymlinkEntry
from neptune.identity.revisions import Observation
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.source import LocalPath, RawLocalPath

ROOT: Final = b""

# A civil date and time of day, written with or without separators: 2024-05-01_12-30-00,
# 2024-05-01-12-30-00 (rosbag1), rosbag2_2024_05_01-12_30_00, 20240501T123000, 2024-05-01T12:30:00.
# The date's two separators match each other, and so do the time's. No digit may touch either end,
# so a longer number is never read as a date.
_CIVIL_TIME: Final = re.compile(
    rb"(?<![0-9])([0-9]{4})([-_.]?)([0-9]{2})\2([0-9]{2})"
    rb"(?:[Tt_ \-])?"
    rb"([0-9]{2})([-_:.]?)([0-9]{2})\6([0-9]{2})(?![0-9])"
)
# A civil date alone (2024-05-01, 20240501) and a time of day alone (12_30_00, the stem of a PX4
# log): neither is a session time, but a part number or a keyword's number must never be read out
# of one.
_CIVIL_DATE: Final = re.compile(rb"(?<![0-9])([0-9]{4})([-_.]?)([0-9]{2})\2([0-9]{2})(?![0-9])")
_TIME_OF_DAY: Final = re.compile(rb"(?<![0-9])([0-9]{2})([-_:.])([0-9]{2})\2([0-9]{2})(?![0-9])")
# A trailing part number on a stem: the ``_3`` of ``patrol_3`` or ``-0012`` of ``take-0012``.
_PART: Final = re.compile(rb"^(.+?)[_\-]([0-9]+)$", re.DOTALL)
# A session keyword followed by a number: run_007, Episode-12, flight3, session 2.
SESSION_KEYWORDS: Final = (
    "drive",
    "episode",
    "experiment",
    "flight",
    "log",
    "mission",
    "rec",
    "recording",
    "run",
    "seq",
    "sequence",
    "session",
    "sortie",
    "take",
    "test",
    "trial",
)
_KEYWORD: Final = re.compile(
    rb"(?:^|[^a-z])(?:"
    + b"|".join(keyword.encode() for keyword in SESSION_KEYWORDS)
    + rb")[-_ ]?([0-9]+)(?![0-9])",
    re.IGNORECASE,
)
# A part's prefix that ends in a session keyword: the part number is then the keyword's own
# (``episode_2``), not a part of a recording a keyword names (``run_3_0`` of ``-O run_3 --split``).
_KEYWORD_END: Final = re.compile(
    rb"(?:^|[^a-z])(?:" + b"|".join(keyword.encode() for keyword in SESSION_KEYWORDS) + rb")$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CivilTime:
    """A date and time of day a name states, with no zone (ADR 0023 §2).

    ``text`` is the matched characters, verbatim. ``seconds`` counts POSIX-style seconds from
    1970-01-01T00:00:00 on the name's own civil clock: two names compare only if one clock wrote
    both, which is the grouper's inference to make, never this type's.
    """

    text: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int

    @property
    def seconds(self) -> int:
        moment = datetime(self.year, self.month, self.day, self.hour, self.minute, self.second)
        return int((moment - datetime(1970, 1, 1)).total_seconds())


@dataclass(frozen=True)
class NameSignals:
    """What one name (a file's or a directory's) says by the grammars above; observations only.

    - ``base`` is the name up to its first dot (``flight_03`` of ``flight_03.params.yaml``); a
      leading dot belongs to the name (``.DS_Store``).
    - ``extension`` is the last extension in lowercase ASCII, ``""`` if there is none or it is
      not ASCII. ``stem`` is the name without it.
    - ``time`` is the first civil date-time the name states, if any.
    - ``part`` is ``(prefix, digits)`` when the stem ends in ``_<digits>`` or ``-<digits>`` that
      are not the end of ``time``.
    - ``keyword`` is whether the name holds a session keyword followed by a number, anywhere.
    - ``part_keyword`` is whether ``part``'s number is a session keyword's own: its prefix ends
      in the keyword (``episode_2``). A keyword earlier in the name (``run_3_0``) is not.
    """

    name: bytes
    base: bytes
    stem: bytes
    extension: str
    time: CivilTime | None
    part: tuple[bytes, bytes] | None
    keyword: bool
    part_keyword: bool


def name_signals(name: bytes) -> NameSignals:
    """Read one path component by the fixed grammars. Pure: the same bytes, the same signals."""
    if not isinstance(name, bytes) or not name or b"/" in name:
        raise ValueError(f"a name is one non-empty path component: {name!r}")
    leading = len(name) - len(name.lstrip(b"."))
    body = name[leading:]
    base = name[:leading] + body.split(b".", 1)[0]
    stem, extension = name, ""
    if b"." in body:
        head, _, tail = name.rpartition(b".")
        if head and head.strip(b"."):
            stem = head
            extension = tail.decode("ascii").lower() if tail.isascii() else ""
    temporal = _temporal_spans(name)
    part = None
    if (match := _PART.match(stem)) and not _within(match.start(2), temporal):
        part = (match[1], match[2])
    keyword = any(not _within(m.start(1), temporal) for m in _KEYWORD.finditer(name))
    return NameSignals(
        name=name,
        base=base,
        stem=stem,
        extension=extension,
        time=_civil_time(name),
        part=part,
        keyword=keyword,
        part_keyword=part is not None and _KEYWORD_END.search(part[0]) is not None,
    )


def _civil_time(name: bytes) -> CivilTime | None:
    for match in _CIVIL_TIME.finditer(name):
        year, month, day = int(match[1]), int(match[3]), int(match[4])
        hour, minute, second = int(match[5]), int(match[7]), int(match[8])
        if _valid_date(year, month, day) and _valid_time(hour, minute, second):
            return CivilTime(match[0].decode("ascii"), year, month, day, hour, minute, second)
    return None


def _valid_date(year: int, month: int, day: int) -> bool:
    return year >= 1 and 1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]


def _valid_time(hour: int, minute: int, second: int) -> bool:
    return hour <= 23 and minute <= 59 and second <= 59


def _temporal_spans(name: bytes) -> list[tuple[int, int]]:
    """Where ``name`` states a valid date-time, date or time of day, as ``[start, end)`` spans."""
    spans: list[tuple[int, int]] = []
    for match in _CIVIL_TIME.finditer(name):
        if _valid_date(int(match[1]), int(match[3]), int(match[4])) and _valid_time(
            int(match[5]), int(match[7]), int(match[8])
        ):
            spans.append(match.span())
    for match in _CIVIL_DATE.finditer(name):
        if _valid_date(int(match[1]), int(match[3]), int(match[4])):
            spans.append(match.span())
    for match in _TIME_OF_DAY.finditer(name):
        if _valid_time(int(match[1]), int(match[3]), int(match[4])):
            spans.append(match.span())
    return spans


def _within(offset: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in spans)


def parent(path: bytes) -> bytes:
    """The directory holding ``path``; ``ROOT`` for a top-level entry."""
    return path.rpartition(b"/")[0]


def basename(path: bytes) -> bytes:
    return path.rpartition(b"/")[2]


def ancestors(path: bytes) -> tuple[bytes, ...]:
    """Every directory above ``path``, nearest first, the root last."""
    found: list[bytes] = []
    while path:
        path = parent(path)
        found.append(path)
    return tuple(found)


def inside(path: bytes, directory: bytes) -> bool:
    """Whether ``path`` lies strictly below ``directory`` (everything lies below the root)."""
    return directory == ROOT or path.startswith(directory + b"/")


@dataclass(frozen=True)
class LayoutFile:
    """A regular file this scan saw: its location, and the revision that records it there."""

    revision: RecordId
    location: LocalPath | RawLocalPath
    content_id: ContentId

    def __post_init__(self) -> None:
        parse_record_id(self.revision)
        if not isinstance(self.location, (LocalPath, RawLocalPath)):
            raise TypeError(f"a layout file has a local location, got {self.location!r}")
        parse_content_id(self.content_id)

    @property
    def path(self) -> bytes:
        return self.location.raw


@dataclass(frozen=True)
class LayoutLink:
    """A symlink this scan recorded and did not follow; ``target`` is exactly as stored."""

    location: LocalPath | RawLocalPath
    target: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.location, (LocalPath, RawLocalPath)):
            raise TypeError(f"a layout link has a local location, got {self.location!r}")
        if not isinstance(self.target, bytes) or not self.target:
            raise ValueError("a link's target is its non-empty contents, as bytes")

    @property
    def path(self) -> bytes:
        return self.location.raw

    @property
    def resolved(self) -> bytes | None:
        """The root-relative path the target names, collapsed lexically against the link's own
        directory; ``ROOT`` for the root itself. ``None`` when the target is absolute or leaves
        the root: whether it lands back inside depends on where the root is mounted, which is
        host state (as ``discovery.policy.symlink_details`` judges it). Nothing is resolved on
        disk, and a link through another link is not followed.
        """
        if self.target.startswith(b"/"):
            return None
        here = parent(self.path)
        joined = here + b"/" + self.target if here else self.target
        collapsed = os.path.normpath(joined)
        if collapsed == b"..":
            return None
        if collapsed.startswith(b"../"):
            return None
        return ROOT if collapsed == b"." else collapsed


@dataclass(frozen=True)
class Layout:
    """The files and links of one scan, each sorted by path bytes and named once.

    No path is both a file and a link, and no file sits below another file. Build one with
    ``layout_of`` or ``layout_from_scan``, which sort; the order they were found in never shows.
    """

    files: tuple[LayoutFile, ...]
    links: tuple[LayoutLink, ...] = ()

    def __post_init__(self) -> None:
        for name, entries in (("files", self.files), ("links", self.links)):
            if not isinstance(entries, tuple):
                raise TypeError(f"{name} must be a tuple")
            paths = [entry.path for entry in entries]
            if paths != sorted(set(paths)):
                raise ValueError(f"layout {name} must be sorted by path, each path once")
        files = {file.path for file in self.files}
        if clash := sorted(files.intersection(link.path for link in self.links)):
            raise ValueError(f"a path cannot be both a file and a link: {clash[0]!r}")
        for path in files:
            for directory in ancestors(path)[:-1]:
                if directory in files:
                    raise ValueError(f"{path!r} lies below {directory!r}, which is a file")

    def directories(self) -> tuple[bytes, ...]:
        """Every directory holding a file or a link, with its ancestors, the root included."""
        found = {ROOT}
        for path in [*(file.path for file in self.files), *(link.path for link in self.links)]:
            found.update(ancestors(path))
        return tuple(sorted(found))


def layout_of(files: Iterable[LayoutFile], links: Iterable[LayoutLink] = ()) -> Layout:
    """A ``Layout`` of ``files`` and ``links`` in canonical order, whatever order they came in."""
    return Layout(
        tuple(sorted(files, key=lambda file: file.path)),
        tuple(sorted(links, key=lambda link: link.path)),
    )


def layout_from_scan(
    observations: Iterable[Observation], symlinks: Iterable[SymlinkEntry] = ()
) -> Layout:
    """The layout of one scan: each observed location's current revision, and every link.

    ``observations`` are a scan's (``ScanResult.observations``): the locations it saw holding
    bytes this time. Locations the ledger remembers but this scan did not see are not in it.
    """
    files: list[LayoutFile] = []
    for observation in observations:
        revision = observation.revision
        location = revision.location
        if not isinstance(location, (LocalPath, RawLocalPath)):
            raise ValueError(f"a local scan observed a non-local location: {location!r}")
        files.append(LayoutFile(revision.id, location, revision.content_id))
    links = [LayoutLink(entry.location, entry.target) for entry in symlinks]
    return layout_of(files, links)
