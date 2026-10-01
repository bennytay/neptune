"""Run/session grouping: the interface, and v0's rules over a layout (ADR 0036).

A ``Grouper`` turns a ``Layout`` (``neptune.discovery.layout``: where files and links sit and what
their names say) into a ``Grouping``: session proposals, the files no proposal holds, and findings
for what it could not decide. Everything it says is inferred, so it lives here, in ``derived/``,
and reaches a package as derived tables beside the evidence, never as ``Run`` records.

``LayoutGrouper`` is v0: filesystem-level signals only, by named rules with named confidences.

1. **Declared sessions** (``GroupingConfig.sessions``, the manual override) take their files first.
2. **Recording units.** A rosbag2 directory (``metadata.yaml`` beside ``.db3``/``.mcap`` storage) is
   one recording; so are the parts ``<prefix>_<n>`` of a recording whose prefix states a start time
   (rosbag1 ``--split``). Parts whose prefix states no time are contested: one recording split, or
   several numbered recordings. Any other ``.mcap``/``.bag``/``.ulg``/``.db3`` is one recording.
3. **Session directories.** A directory whose name states a date-time or a session keyword with a
   number (``run_007``, ``episode-3``) is one session holding everything below it, unless it holds
   such directories itself: with two or more it is a collection, with exactly one (and files of its
   own) its reading as one session is contested with the readings inside it. A session directory
   whose recordings' names state times more than ``gap_seconds`` apart is contested with a reading
   split at those gaps.
4. **Loose recordings** (in no session directory) are each a session, except that recordings in
   one directory whose names state the same time are one session, and times within
   ``gap_seconds`` but not equal are contested between one session and several.
5. **Context files** beside loose recordings join the session whose name time or name stem they
   share, else the only session in their directory; with several there they are unassigned and
   ambiguous, with none unassigned and unknown. Files above session directories stay unassigned:
   a directory boundary is never crossed by a guess.

Nothing is merged by content: identical bytes at two locations stay two members, and each
proposal says so (``same_bytes``). Links are never followed; a proposal lists those whose target,
read lexically, names it (``alias``) or that sit inside its directory (``inside``). No file
contents, modification times or probe verdicts are read, so the same tree gives the same grouping
wherever and whenever it is walked, in any walk order.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from neptune.derived.sessions import (
    PROPOSAL_KIND,
    UNASSIGNED_KIND,
    LinkRelation,
    Reason,
    Role,
    SessionLink,
    SessionMember,
    SessionProposal,
    Status,
    UnassignedFile,
    bytes_json,
    directory_location,
    proposal_id,
    session_proposal,
    unassigned_file,
)
from neptune.discovery.layout import (
    ROOT,
    CivilTime,
    Layout,
    LayoutLink,
    NameSignals,
    ancestors,
    basename,
    inside,
    name_signals,
    parent,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.model._fields import exact_object, json_array, json_int, json_str
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId, check_text
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import TransformRecord
from neptune.model.source import LocalPath, local_location

GROUPING_ID: Final = "neptune.grouping"
GROUPING_VERSION: Final = "0.1.0"
DEFAULT_GAP_SECONDS: Final = 60

# Recording formats by extension: each file is one recording. v0 reads names only; MVL-34 reads
# the ``Run`` records adapters emit instead.
RECORDING_EXTENSIONS: Final = frozenset({"bag", "db3", "mcap", "ulg"})
BAG_STORAGE: Final = frozenset({"db3", "mcap"})
BAG_METADATA: Final = b"metadata.yaml"

# Finding codes, ``<producer>.<name>``.
CONTESTED: Final = f"{GROUPING_ID}.contested"
AMBIGUOUS_MEMBER: Final = f"{GROUPING_ID}.ambiguous_member"
DECLARATION_UNMATCHED: Final = f"{GROUPING_ID}.declaration_unmatched"
FINDING_CODES: Final = (AMBIGUOUS_MEMBER, CONTESTED, DECLARATION_UNMATCHED)

# Unassigned reasons.
NO_SESSION: Final = "no_session"
SEVERAL_SESSIONS: Final = "several_sessions"
SEVERAL_STEMS: Final = "several_stems"
TOO_MANY_SESSIONS: Final = "too_many_sessions"

# How many locations a reason or finding lists before it only counts; a duplicate's twins are
# listed more briefly, since every proposal holding one says so.
_LISTED: Final = 64
_SAME_LISTED: Final = 8
_EMPTY: Final = content_id(b"")


class Rule(StrEnum):
    """Every rule v0 applies. A proposal names the rule that formed it; a member the one that
    placed it; a reason the one that fired."""

    DECLARED = "declared"
    ROSBAG2_DIRECTORY = "rosbag2_directory"
    SPLIT_SEQUENCE = "split_sequence"
    NUMBERED_SEQUENCE = "numbered_sequence"
    RECORDING_FILE = "recording_file"
    SESSION_DIRECTORY = "session_directory"
    NAME_TIME_CLUSTERS = "name_time_clusters"
    SHARED_NAME_TIME = "shared_name_time"
    NAME_TIME_PROXIMITY = "name_time_proximity"
    SHARED_STEM = "shared_stem"
    SOLE_SESSION = "sole_session_in_directory"
    SAME_BYTES = "same_bytes"


# The confidence each rule lends what it forms or places, as named bands (ADR 0036 §3): a ranking,
# not a probability. Contested readings keep their band; their status says they compete.
CONFIDENCE: Final[Mapping[Rule, float]] = {
    Rule.DECLARED: 1.0,
    Rule.ROSBAG2_DIRECTORY: 0.9,
    Rule.SPLIT_SEQUENCE: 0.8,
    Rule.RECORDING_FILE: 0.7,
    Rule.SESSION_DIRECTORY: 0.6,
    Rule.SHARED_STEM: 0.6,
    Rule.SHARED_NAME_TIME: 0.5,
    Rule.SOLE_SESSION: 0.4,
    Rule.NAME_TIME_CLUSTERS: 0.3,
    Rule.NAME_TIME_PROXIMITY: 0.3,
    Rule.NUMBERED_SEQUENCE: 0.3,
}

# A link aliases a session by naming its directory only when that directory is the session's own.
_OWN_DIRECTORY: Final = frozenset(
    {Rule.ROSBAG2_DIRECTORY, Rule.SESSION_DIRECTORY, Rule.NAME_TIME_CLUSTERS}
)

_PLACED: Final[Mapping[Rule, str]] = {
    Rule.SHARED_NAME_TIME: "context files whose names state one of the session's times",
    Rule.SHARED_STEM: "context files whose name stem is a member's",
    Rule.SOLE_SESSION: "context files beside the only session in their directory",
}


# --- Config: the override hook -----------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredSession:
    """A session the user declares: every file at or below ``paths`` (root-relative, ``/``-
    separated, as ``LocalPath`` text). It wins over every rule; paths are kept sorted."""

    name: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        check_text("name", self.name)
        if not isinstance(self.paths, tuple) or not self.paths:
            raise ValueError("a declared session names at least one path")
        for path in self.paths:
            LocalPath(path)  # relative, no '.', '..' or empty parts, no NUL
        if len(set(self.paths)) != len(self.paths):
            raise ValueError(f"declared session {self.name!r} names a path twice")
        object.__setattr__(self, "paths", tuple(sorted(self.paths)))

    def to_json(self) -> JsonObject:
        return {"name": self.name, "paths": list(self.paths)}


@dataclass(frozen=True)
class GroupingConfig:
    """The grouper's config, and so its transform's: ``gap_seconds`` is the widest difference
    between two name times read as one moment; ``sessions`` are declared sessions."""

    gap_seconds: int = DEFAULT_GAP_SECONDS
    sessions: tuple[DeclaredSession, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.gap_seconds, bool) or not isinstance(self.gap_seconds, int):
            raise ValueError(f"gap_seconds must be an integer, got {self.gap_seconds!r}")
        if self.gap_seconds < 0:
            raise ValueError(f"gap_seconds must not be negative, got {self.gap_seconds}")
        if not isinstance(self.sessions, tuple):
            raise TypeError("sessions must be a tuple of DeclaredSession")
        for session in self.sessions:
            if not isinstance(session, DeclaredSession):
                raise TypeError(f"sessions must be DeclaredSessions, got {session!r}")
        names = [session.name for session in self.sessions]
        if len(set(names)) != len(names):
            raise ValueError("declared sessions must have distinct names")
        object.__setattr__(self, "sessions", tuple(sorted(self.sessions, key=lambda s: s.name)))

    def to_json(self) -> JsonObject:
        return {
            "gap_seconds": self.gap_seconds,
            "sessions": [session.to_json() for session in self.sessions],
        }


def grouping_config_from_json(data: JsonValue) -> GroupingConfig:
    obj = exact_object(data, "grouping config", {"gap_seconds", "sessions"})
    sessions = []
    for item in json_array(obj["sessions"], "sessions"):
        entry = exact_object(item, "declared session", {"name", "paths"})
        paths = json_array(entry["paths"], "paths")
        sessions.append(
            DeclaredSession(
                json_str(entry["name"], "name"), tuple(json_str(p, "path") for p in paths)
            )
        )
    return GroupingConfig(json_int(obj["gap_seconds"], "gap_seconds"), tuple(sessions))


# --- The interface -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Grouping:
    """What a grouper proposed for one layout: proposals and unassigned files sorted by id, and
    findings, under ``transform``. ``tables`` is what a package holds of it."""

    transform: TransformRecord
    proposals: tuple[SessionProposal, ...]
    unassigned: tuple[UnassignedFile, ...]
    findings: tuple[IngestFinding, ...]

    def ranked(self) -> tuple[SessionProposal, ...]:
        """Most confident first; uncontested before contested at equal confidence; then id."""
        return tuple(
            sorted(
                self.proposals,
                key=lambda p: (-p.confidence, p.status is Status.CONTESTED, p.id),
            )
        )

    def tables(self) -> dict[str, list[JsonObject]]:
        """The package's derived tables, by kind (ADR 0036 §7)."""
        return {
            PROPOSAL_KIND: [proposal.to_json() for proposal in self.proposals],
            UNASSIGNED_KIND: [entry.to_json() for entry in self.unassigned],
        }

    def summary(self) -> JsonObject:
        return {
            "ambiguous": sum(1 for u in self.unassigned if u.candidates),
            "contested": sum(1 for p in self.proposals if p.status is Status.CONTESTED),
            "findings": len(self.findings),
            "proposals": len(self.proposals),
            "unknown": sum(1 for u in self.unassigned if not u.candidates),
        }


class Grouper(Protocol):
    """Proposes sessions for a layout. v0 is ``LayoutGrouper``; MVL-34's evidence-graph assembler
    implements the same interface, under its own transform, as a new lineage."""

    @property
    def transform(self) -> TransformRecord: ...

    def propose(self, layout: Layout) -> Grouping: ...


def check_grouping(grouping: Grouping, layout: Layout) -> None:
    """The laws every grouping keeps (ADR 0036 §4); ``ValueError`` names the first one broken.

    - Every file of the layout is a member of some proposal or unassigned, never both, and
      unassigned once; nothing else is.
    - A file in two proposals is only ever in contested ones: no silent cross-session merge.
    - ``contested`` is symmetric, and names and candidates are proposals of this grouping.
    - Everything names the grouping's transform.
    """
    files = {file.revision: file.path for file in layout.files}
    proposals = {proposal.id: proposal for proposal in grouping.proposals}
    if len(proposals) != len(grouping.proposals):
        raise ValueError("two proposals share an id")
    holders: dict[RecordId, list[SessionProposal]] = defaultdict(list)
    for proposal in grouping.proposals:
        if proposal.transform != grouping.transform.id:
            raise ValueError(f"proposal {proposal.id} names another transform")
        for member in proposal.members:
            if files.get(member.revision) != member.location.raw:
                raise ValueError(f"proposal {proposal.id} holds a file the layout does not")
            holders[member.revision].append(proposal)
        for other in proposal.contested:
            if other not in proposals or proposal.id not in proposals[other].contested:
                raise ValueError(f"proposal {proposal.id} contests {other} one-sidedly")
    for revision, held in holders.items():
        if len(held) > 1 and any(p.status is not Status.CONTESTED for p in held):
            raise ValueError(f"{files[revision]!r} is in two proposals, not all contested")
    unassigned: set[RecordId] = set()
    for entry in grouping.unassigned:
        if entry.transform != grouping.transform.id:
            raise ValueError(f"unassigned file {entry.id} names another transform")
        if files.get(entry.revision) != entry.location.raw:
            raise ValueError(f"unassigned file {entry.id} is not a file of the layout")
        if entry.revision in unassigned or entry.revision in holders:
            raise ValueError(f"{files[entry.revision]!r} is placed twice")
        if any(candidate not in proposals for candidate in entry.candidates):
            raise ValueError(f"unassigned file {entry.id} names a proposal that does not exist")
        unassigned.add(entry.revision)
    if missing := set(files) - unassigned - set(holders):
        raise ValueError(f"{len(missing)} files are neither proposed nor unassigned")
    for finding in grouping.findings:
        if finding.transform != grouping.transform.id:
            raise ValueError(f"finding {finding.id} names another transform")


# --- v0 ----------------------------------------------------------------------------------------


class LayoutGrouper:
    """v0: filesystem-level signals only (module docstring; ADR 0036 §3)."""

    def __init__(self, config: GroupingConfig | None = None) -> None:
        self.config = config if config is not None else GroupingConfig()
        self.transform = transform_record(
            adapter_id=GROUPING_ID, adapter_version=GROUPING_VERSION, config=self.config.to_json()
        )

    def propose(self, layout: Layout) -> Grouping:
        grouping = _Proposer(self.config, self.transform, layout).run()
        check_grouping(grouping, layout)
        return grouping


@dataclass
class _Unit:
    """One recording: a rosbag2 directory, a split sequence, a numbered sequence or one file.

    ``position`` is the directory it sits in (a rosbag2 directory sits in its parent); ``anchor``
    is the directory a proposal of it alone names. A numbered sequence keeps each part as a unit
    of its own (``parts``): the other reading.
    """

    rule: Rule
    position: bytes
    anchor: bytes
    recordings: tuple[bytes, ...]
    context: tuple[bytes, ...]
    time: CivilTime | None
    stems: frozenset[bytes]
    reasons: tuple[Reason, ...]
    parts: tuple["_Unit", ...] = ()

    @property
    def paths(self) -> tuple[bytes, ...]:
        return self.recordings + self.context


@dataclass
class _Draft:
    """A proposal being built: members by path, with their role and the rule that placed them."""

    rule: Rule
    directory: bytes
    members: dict[bytes, tuple[Role, Rule]] = field(default_factory=dict)
    reasons: list[Reason] = field(default_factory=list)
    times: dict[int, str] = field(default_factory=dict)
    stems: set[bytes] = field(default_factory=set)
    links: set[tuple[LayoutLink, LinkRelation]] = field(default_factory=set)

    def add(self, path: bytes, role: Role, rule: Rule) -> None:
        self.members.setdefault(path, (role, rule))

    def take(self, unit: _Unit, rule: Rule) -> None:
        for path in unit.recordings:
            self.add(path, Role.RECORDING, rule)
        for path in unit.context:
            self.add(path, Role.CONTEXT, rule)
        if unit.time is not None:
            self.times.setdefault(unit.time.seconds, unit.time.text)
        self.stems.update(unit.stems)


def _name(key: str, path: bytes) -> dict[str, JsonValue]:
    return dict(bytes_json(key, basename(path)))


def _location_json(path: bytes) -> JsonObject:
    return local_location(path).to_json()


def _sequence_details(prefix: bytes, indices: Sequence[int]) -> JsonObject:
    """The parts' indices and the gaps between the least and greatest. Names choose the indices,
    so the gaps are counted, and listed only up to ``_LISTED``: work is bounded by the number of
    parts, never by how far apart a hostile name puts two indices."""
    present = set(indices)
    low, high = min(present), max(present)
    missing: list[int] = []
    index = low
    while index <= high and len(missing) < _LISTED:
        if index not in present:
            missing.append(index)
        index += 1
    return {
        **bytes_json("prefix", prefix),
        "indices": sorted(indices)[:_LISTED],
        "missing": missing,
        "missing_count": high - low + 1 - len(present),
        "parts": len(indices),
    }


def _chains(times: Sequence[int], gap: int) -> list[list[int]]:
    """``times`` (sorted, distinct) split wherever two neighbours are more than ``gap`` apart."""
    chains: list[list[int]] = []
    for moment in times:
        if chains and moment - chains[-1][-1] <= gap:
            chains[-1].append(moment)
        else:
            chains.append([moment])
    return chains


class _Proposer:
    """One run of v0 over one layout. Every loop is over sorted keys, so the result depends on
    the layout alone, never on the order its files were found in."""

    def __init__(self, config: GroupingConfig, transform: TransformRecord, layout: Layout) -> None:
        self.config = config
        self.transform = transform
        self.layout = layout
        self.files = {file.path: file for file in layout.files}
        self.drafts: list[_Draft] = []
        self.unassigned: dict[bytes, tuple[str, list[int]]] = {}
        self.findings: list[IngestFinding] = []
        self._signals: dict[bytes, NameSignals] = {}

    def run(self) -> Grouping:
        remaining = self._declared()
        bags = self._bag_directories(remaining)
        leaves, singles = self._session_directories(remaining, bags)
        owned: set[bytes] = set()
        under_leaf: dict[bytes, list[bytes]] = defaultdict(list)
        for path in remaining:
            for directory in ancestors(path):
                if directory in leaves:
                    under_leaf[directory].append(path)
                    break
        for leaf in sorted(under_leaf):
            self._session(leaf, under_leaf[leaf], bags)
            owned.update(under_leaf[leaf])
        self._loose([path for path in remaining if path not in owned], bags)
        under_single: dict[bytes, list[bytes]] = defaultdict(list)
        for path in remaining:
            for directory in ancestors(path):
                if directory in singles:
                    under_single[directory].append(path)
        for node in sorted(singles):
            self._outer(node, singles[node], under_single[node], bags)
        self._links()
        self._same_bytes()
        return self._build()

    # --- signals -------------------------------------------------------------------------------

    def _sig(self, path: bytes) -> NameSignals:
        signals = self._signals.get(path)
        if signals is None:
            signals = self._signals[path] = name_signals(basename(path))
        return signals

    def _role(self, path: bytes) -> Role:
        return Role.RECORDING if self._sig(path).extension in RECORDING_EXTENSIONS else Role.CONTEXT

    def _draft(self, rule: Rule, directory: bytes) -> int:
        self.drafts.append(_Draft(rule, directory))
        return len(self.drafts) - 1

    # --- 1. declared sessions ------------------------------------------------------------------

    def _declared(self) -> list[bytes]:
        claims: dict[bytes, list[int]] = defaultdict(list)
        for declared in self.config.sessions:
            prefixes = [path.encode("utf-8") for path in declared.paths]
            matched = [
                path
                for path in sorted(self.files)
                if any(path == p or path.startswith(p + b"/") for p in prefixes)
            ]
            if not matched:
                self.findings.append(
                    ingest_finding(
                        code=DECLARATION_UNMATCHED,
                        category=FindingCategory.MISSING,
                        severity=Severity.WARNING,
                        subject=LocalPath(declared.paths[0]),
                        transform=self.transform,
                        message="a declared session names no file this scan saw",
                        details=declared.to_json(),
                    )
                )
                continue
            index = self._draft(Rule.DECLARED, _common_directory(matched))
            draft = self.drafts[index]
            for path in matched:
                draft.add(path, self._role(path), Rule.DECLARED)
                claims[path].append(index)
            draft.reasons.append(
                Reason(
                    Rule.DECLARED,
                    "declared in the grouping config; it overrides every rule",
                    declared.to_json(),
                )
            )
        # Two declarations claiming one file share it, so they contest each other (_build).
        return [path for path in sorted(self.files) if path not in claims]

    # --- 2. recording units --------------------------------------------------------------------

    def _bag_directories(self, paths: Iterable[bytes]) -> dict[bytes, list[bytes]]:
        by_directory: dict[bytes, list[bytes]] = defaultdict(list)
        for path in paths:
            by_directory[parent(path)].append(path)
        bags: dict[bytes, list[bytes]] = {}
        for directory, held in sorted(by_directory.items()):
            names = {basename(path) for path in held}
            storage = [p for p in held if self._sig(p).extension in BAG_STORAGE]
            if BAG_METADATA in names and storage:
                bags[directory] = sorted(held)
        return bags

    def _units(self, paths: Iterable[bytes], bags: Mapping[bytes, list[bytes]]) -> list[_Unit]:
        """The recordings among ``paths``, in path order; context files are not units."""
        wanted = set(paths)
        units: list[_Unit] = []
        in_bags: set[bytes] = set()
        for directory in sorted({parent(path) for path in wanted}.intersection(bags)):
            files = [path for path in bags[directory] if path in wanted]
            if not files:
                continue
            in_bags.update(files)
            storage = [p for p in files if self._sig(p).extension in BAG_STORAGE]
            recordings = tuple(p for p in files if p in storage or basename(p) == BAG_METADATA)
            context = tuple(p for p in files if p not in recordings)
            name = basename(directory)
            details: dict[str, JsonValue] = {"storage": len(storage)}
            if directory != ROOT:
                details |= _name("directory", directory)
            units.append(
                _Unit(
                    rule=Rule.ROSBAG2_DIRECTORY,
                    position=parent(directory) if directory != ROOT else ROOT,
                    anchor=directory,
                    recordings=recordings,
                    context=context,
                    time=self._sig(directory).time if name else None,
                    stems=frozenset({name} if name else set()),
                    reasons=(
                        Reason(
                            Rule.ROSBAG2_DIRECTORY,
                            "a rosbag2 directory: metadata.yaml beside its storage files",
                            details,
                        ),
                    ),
                )
            )
        by_directory: dict[bytes, list[bytes]] = defaultdict(list)
        for path in sorted(wanted - in_bags):
            if self._sig(path).extension in RECORDING_EXTENSIONS:
                by_directory[parent(path)].append(path)
        for directory, found in sorted(by_directory.items()):
            units.extend(self._recordings_in(directory, found))
        return units

    def _recordings_in(self, directory: bytes, recordings: list[bytes]) -> list[_Unit]:
        sequences: dict[tuple[str, bytes], list[bytes]] = defaultdict(list)
        for path in recordings:
            signals = self._sig(path)
            if signals.part is not None:
                sequences[signals.extension, signals.part[0]].append(path)
        units: list[_Unit] = []
        grouped: set[bytes] = set()
        for (_, prefix), parts in sorted(sequences.items()):
            if len(parts) < 2:
                continue
            first = self._sig(parts[0])
            indices = [_part_index(self._sig(p)) for p in parts]
            details = _sequence_details(prefix, indices)
            singles = tuple(self._recording(directory, part) for part in parts)
            started = name_signals(prefix).time
            if started is not None:
                grouped.update(parts)
                units.append(
                    _Unit(
                        rule=Rule.SPLIT_SEQUENCE,
                        position=directory,
                        anchor=directory,
                        recordings=tuple(parts),
                        context=(),
                        time=started,
                        stems=frozenset({prefix, *(self._sig(p).base for p in parts)}),
                        reasons=(
                            Reason(
                                Rule.SPLIT_SEQUENCE,
                                "parts <prefix>_<n> of one split recording whose prefix states"
                                " its start time",
                                details,
                            ),
                        ),
                    )
                )
            elif not first.keyword:
                grouped.update(parts)
                units.append(
                    _Unit(
                        rule=Rule.NUMBERED_SEQUENCE,
                        position=directory,
                        anchor=directory,
                        recordings=tuple(parts),
                        context=(),
                        time=None,
                        stems=frozenset({prefix, *(self._sig(p).base for p in parts)}),
                        reasons=(
                            Reason(
                                Rule.NUMBERED_SEQUENCE,
                                "numbered parts <prefix>_<n> with no start time: one recording"
                                " split, or several numbered recordings",
                                details,
                            ),
                        ),
                        parts=singles,
                    )
                )
            # Numbered by a session keyword (run_1, episode_2): each part is its own recording.
        units.extend(self._recording(directory, p) for p in recordings if p not in grouped)
        return units

    def _recording(self, directory: bytes, path: bytes) -> _Unit:
        signals = self._sig(path)
        return _Unit(
            rule=Rule.RECORDING_FILE,
            position=directory,
            anchor=directory,
            recordings=(path,),
            context=(),
            time=signals.time,
            stems=frozenset({signals.base}),
            reasons=(
                Reason(
                    Rule.RECORDING_FILE,
                    "a recording by its extension",
                    {"extension": signals.extension, **_name("name", path)},
                ),
            ),
        )

    # --- 3. session directories ----------------------------------------------------------------

    def _session_directories(
        self, paths: Sequence[bytes], bags: Mapping[bytes, list[bytes]]
    ) -> tuple[set[bytes], dict[bytes, bytes]]:
        """Leaf session directories, and those holding exactly one (with that one)."""
        directories = {directory for path in paths for directory in ancestors(path)}
        candidates = {
            d
            for d in directories
            if d != ROOT and d not in bags and (self._sig(d).time or self._sig(d).keyword)
        }
        children: dict[bytes, list[bytes]] = defaultdict(list)
        for candidate in sorted(candidates):
            for directory in ancestors(candidate):
                if directory in candidates:
                    children[directory].append(candidate)
                    break
        leaves = {c for c in candidates if not children[c]}
        singles = {c: children[c][0] for c in sorted(candidates) if len(children[c]) == 1}
        return leaves, singles

    def _signal_details(self, directory: bytes) -> tuple[str, dict[str, JsonValue]]:
        signals = self._sig(directory)
        details = _name("name", directory)
        if signals.time is not None:
            return "the directory's name states a time", details | {"time": signals.time.text}
        return "the directory's name is a session keyword with a number", details | {
            "keyword": True
        }

    def _session(self, leaf: bytes, paths: list[bytes], bags: Mapping[bytes, list[bytes]]) -> None:
        units = self._units(paths, bags)
        recordings = {path for unit in units for path in unit.recordings}
        index = self._draft(Rule.SESSION_DIRECTORY, leaf)
        draft = self.drafts[index]
        for path in sorted(paths):
            role = Role.RECORDING if path in recordings else Role.CONTEXT
            draft.add(path, role, Rule.SESSION_DIRECTORY)
        message, details = self._signal_details(leaf)
        draft.reasons.append(Reason(Rule.SESSION_DIRECTORY, message, details))
        timed = sorted(
            (unit.time.seconds, unit.paths[0], unit) for unit in units if unit.time is not None
        )
        moments = sorted({seconds for seconds, _, _ in timed})
        chains = _chains(moments, self.config.gap_seconds)
        if len(chains) < 2:
            return
        # Each cluster shares its recordings with the whole directory, so they contest (_build).
        chain_of = {moment: number for number, chain in enumerate(chains) for moment in chain}
        clusters: list[list[_Unit]] = [[] for _ in chains]
        for seconds, _, unit in timed:
            clusters[chain_of[seconds]].append(unit)
        for within in clusters:
            cluster = self._draft(Rule.NAME_TIME_CLUSTERS, leaf)
            for unit in within:
                self.drafts[cluster].take(unit, Rule.NAME_TIME_CLUSTERS)
            self.drafts[cluster].reasons.append(
                Reason(
                    Rule.NAME_TIME_CLUSTERS,
                    "the directory's recordings state times more than gap_seconds apart; this"
                    " reading splits it at those gaps",
                    {
                        "gap_seconds": self.config.gap_seconds,
                        "times": sorted({_time_text(unit) for unit in within}),
                    },
                )
            )

    def _outer(
        self, node: bytes, child: bytes, paths: list[bytes], bags: Mapping[bytes, list[bytes]]
    ) -> None:
        """A session directory holding exactly one other: if it has files of its own, its
        reading as one session holds every file below it, so it contests every reading there."""
        if all(inside(path, child) for path in paths):
            return  # it adds nothing to the one inside it
        recordings = {path for unit in self._units(paths, bags) for path in unit.recordings}
        index = self._draft(Rule.SESSION_DIRECTORY, node)
        draft = self.drafts[index]
        for path in sorted(paths):
            role = Role.RECORDING if path in recordings else Role.CONTEXT
            draft.add(path, role, Rule.SESSION_DIRECTORY)
            self.unassigned.pop(path, None)
        message, details = self._signal_details(node)
        draft.reasons.append(
            Reason(
                Rule.SESSION_DIRECTORY,
                f"{message}; it also holds one session-named directory",
                details | _name("inner", child),
            )
        )

    # --- 4 and 5. loose recordings and their context -------------------------------------------

    def _loose(self, paths: list[bytes], bags: Mapping[bytes, list[bytes]]) -> None:
        units = self._units(paths, bags)
        in_units = {path for unit in units for path in unit.paths}
        positions: dict[bytes, list[_Unit]] = defaultdict(list)
        for unit in units:
            positions[unit.position].append(unit)
        context: dict[bytes, list[bytes]] = defaultdict(list)
        for path in paths:
            if path not in in_units:
                context[parent(path)].append(path)
        for position in sorted(set(positions) | set(context)):
            self._position(position, positions.get(position, []), context.get(position, []))

    def _unit_draft(self, unit: _Unit) -> int:
        index = self._draft(unit.rule, unit.anchor)
        self.drafts[index].take(unit, unit.rule)
        self.drafts[index].reasons.extend(unit.reasons)
        return index

    def _position(self, position: bytes, units: list[_Unit], context: list[bytes]) -> None:
        here: list[int] = []
        by_time: dict[int, list[_Unit]] = defaultdict(list)
        numbered: list[_Unit] = []
        for unit in units:
            if unit.rule is Rule.NUMBERED_SEQUENCE:
                numbered.append(unit)
            elif unit.time is not None:
                by_time[unit.time.seconds].append(unit)
            else:
                here.append(self._unit_draft(unit))
        at_time: dict[int, int] = {}
        for seconds, group in sorted(by_time.items()):
            if len(group) == 1:
                at_time[seconds] = self._unit_draft(group[0])
            else:
                index = self._draft(Rule.SHARED_NAME_TIME, position)
                for unit in group:
                    self.drafts[index].take(unit, Rule.SHARED_NAME_TIME)
                    self.drafts[index].reasons.extend(unit.reasons)
                self.drafts[index].reasons.insert(
                    0,
                    Reason(
                        Rule.SHARED_NAME_TIME,
                        "recordings whose names state the same time",
                        {"recordings": len(group), "time": _time_text(group[0])},
                    ),
                )
                at_time[seconds] = index
            here.append(at_time[seconds])
        for chain in _chains(sorted(by_time), self.config.gap_seconds):
            if len(chain) < 2:
                continue
            merged = self._draft(Rule.NAME_TIME_PROXIMITY, position)
            for seconds in chain:
                for unit in by_time[seconds]:
                    self.drafts[merged].take(unit, Rule.NAME_TIME_PROXIMITY)
            self.drafts[merged].reasons.append(
                Reason(
                    Rule.NAME_TIME_PROXIMITY,
                    "recordings whose names state times within gap_seconds but not equal: one"
                    " session started in steps, or several",
                    {
                        "gap_seconds": self.config.gap_seconds,
                        "times": [_time_text(by_time[seconds][0]) for seconds in chain],
                    },
                )
            )
            here.append(merged)  # it shares every file with the chain's drafts: contested
        for unit in numbered:
            whole = self._unit_draft(unit)
            parts = [self._unit_draft(part) for part in unit.parts]
            here.extend([whole, *parts])  # the whole shares each part's file: contested
        self._place(position, here, context)

    def _place(self, position: bytes, here: list[int], context: list[bytes]) -> None:
        """Context files at ``position``: by name time, then name stem, then the only session.
        Each step looks its candidates up by key, so a directory of many files costs linear time;
        a file that more than ``_LISTED`` sessions could hold is unknown, not ambiguous among
        them all, since no one resolves a choice that wide by hand."""
        at_time: dict[int, list[int]] = defaultdict(list)
        for index in here:
            for seconds in self.drafts[index].times:
                at_time[seconds].append(index)
        pending: list[bytes] = []
        alone: dict[int, list[bytes]] = defaultdict(list)
        for path in sorted(context):
            time = self._sig(path).time
            matches = at_time.get(time.seconds, []) if time is not None else []
            for index in matches:
                self.drafts[index].add(path, Role.CONTEXT, Rule.SHARED_NAME_TIME)
            if matches:
                continue
            if time is not None:
                alone[time.seconds].append(path)
            pending.append(path)
        joined: set[bytes] = set()
        for seconds, paths in sorted(alone.items()):
            if len(paths) < 2:
                continue
            index = self._draft(Rule.SHARED_NAME_TIME, position)
            draft = self.drafts[index]
            for path in paths:
                draft.add(path, Role.CONTEXT, Rule.SHARED_NAME_TIME)
                draft.stems.add(self._sig(path).base)
            joined.update(paths)
            text = next(t.text for t in (self._sig(paths[0]).time,) if t is not None)
            draft.times[seconds] = text
            draft.reasons.append(
                Reason(
                    Rule.SHARED_NAME_TIME,
                    "files whose names state the same time, with no recording among them",
                    {"files": len(paths), "time": text},
                )
            )
            here.append(index)
        by_stem: dict[bytes, list[int]] = defaultdict(list)
        for index in here:
            for stem in self.drafts[index].stems:
                by_stem[stem].append(index)
        for path in pending:
            if path in joined:
                continue
            stems = by_stem.get(self._sig(path).base, [])
            if len(stems) == 1:
                self.drafts[stems[0]].add(path, Role.CONTEXT, Rule.SHARED_STEM)
            elif stems:
                self._ambiguous(path, SEVERAL_STEMS, stems)
            elif len(here) == 1:
                self.drafts[here[0]].add(path, Role.CONTEXT, Rule.SOLE_SESSION)
            elif here:
                self._ambiguous(path, SEVERAL_SESSIONS, here)
            else:
                self.unassigned[path] = (NO_SESSION, [])

    def _ambiguous(self, path: bytes, reason: str, candidates: list[int]) -> None:
        if len(candidates) > _LISTED:
            self.unassigned[path] = (TOO_MANY_SESSIONS, [])
        else:
            self.unassigned[path] = (reason, list(candidates))

    # --- links and duplicates ------------------------------------------------------------------

    def _links(self) -> None:
        by_member: dict[bytes, list[int]] = defaultdict(list)
        by_directory: dict[bytes, list[int]] = defaultdict(list)
        for index, draft in enumerate(self.drafts):
            for path in draft.members:
                by_member[path].append(index)
            if draft.rule in _OWN_DIRECTORY and draft.directory != ROOT:
                by_directory[draft.directory].append(index)
        for link in self.layout.links:
            target = link.resolved
            if target is not None and target != ROOT:
                for index in (*by_member.get(target, ()), *by_directory.get(target, ())):
                    self.drafts[index].links.add((link, LinkRelation.ALIAS))
            for directory in ancestors(link.path):
                for index in by_directory.get(directory, ()):
                    self.drafts[index].links.add((link, LinkRelation.INSIDE))

    def _same_bytes(self) -> None:
        """One reason per proposal per content it shares with files outside it. Empty files
        are all equal and say nothing, so they are left out. Work is one pass over the files
        and, per proposal, its members plus a bounded listing."""
        by_content: dict[str, list[bytes]] = defaultdict(list)
        for path, file in sorted(self.files.items()):
            if file.content_id != _EMPTY:
                by_content[file.content_id].append(path)
        for draft in self.drafts:
            held: dict[str, list[bytes]] = defaultdict(list)
            for path in sorted(draft.members):
                held[self.files[path].content_id].append(path)
            for content, inside_draft in sorted(held.items()):
                everywhere = by_content.get(content, [])
                outside = len(everywhere) - len(inside_draft)
                if outside <= 0:
                    continue
                listed: list[JsonValue] = []
                for other in everywhere:
                    if len(listed) == _SAME_LISTED:
                        break
                    if other not in draft.members:
                        listed.append(_location_json(other))
                draft.reasons.append(
                    Reason(
                        Rule.SAME_BYTES,
                        "members hold the same bytes as files outside this session;"
                        " equal bytes are never merged",
                        {
                            "count": outside,
                            "locations": [_location_json(p) for p in inside_draft[:_LISTED]],
                            "same_as": listed,
                        },
                    )
                )

    # --- records -------------------------------------------------------------------------------

    def _build(self) -> Grouping:
        transform = self.transform.id
        ids: list[RecordId] = []
        for draft in self.drafts:
            revisions = [self.files[path].revision for path in draft.members]
            ids.append(
                proposal_id(transform, draft.rule, directory_location(draft.directory), revisions)
            )
        first: dict[RecordId, int] = {}
        for index, record in enumerate(ids):
            first.setdefault(record, index)
        # Two proposals contest each other exactly when they share a file: each reading that
        # holds a file another reading also holds was offered beside it, and none was chosen.
        # A finding names each connected set of such readings once.
        holders: dict[bytes, list[int]] = defaultdict(list)
        for index, draft in enumerate(self.drafts):
            if first[ids[index]] == index:
                for path in draft.members:
                    holders[path].append(index)
        contested: dict[RecordId, set[RecordId]] = defaultdict(set)
        joined = list(range(len(self.drafts)))

        def find(index: int) -> int:
            while joined[index] != index:
                joined[index] = joined[joined[index]]
                index = joined[index]
            return index

        shared: set[int] = set()
        for held in holders.values():
            if len(held) < 2:
                continue
            shared.update(held)
            for index in held:
                contested[ids[index]].update(ids[other] for other in held if other != index)
                joined[find(index)] = find(held[0])
        components: dict[int, list[int]] = defaultdict(list)
        for index in sorted(shared):
            components[find(index)].append(index)
        groups = sorted(components.values())
        proposals: dict[RecordId, SessionProposal] = {}
        for index, draft in enumerate(self.drafts):
            if first[ids[index]] != index:
                continue  # the same proposal, reached twice
            proposals[ids[index]] = session_proposal(
                transform=transform,
                rule=draft.rule,
                confidence=CONFIDENCE[draft.rule],
                directory=directory_location(draft.directory),
                members=[
                    SessionMember(
                        revision=self.files[path].revision,
                        location=self.files[path].location,
                        role=role,
                        rule=rule,
                        confidence=CONFIDENCE[rule],
                    )
                    for path, (role, rule) in draft.members.items()
                ],
                links=[
                    SessionLink(link.location, link.target, relation)
                    for link, relation in draft.links
                ],
                reasons=[*draft.reasons, *_placed(draft)],
                contested=contested[ids[index]],
            )
        unassigned = []
        ambiguous: dict[bytes, list[bytes]] = defaultdict(list)
        for path, (reason, candidates) in sorted(self.unassigned.items()):
            named = {ids[first[ids[i]]] for i in candidates}
            file = self.files[path]
            unassigned.append(
                unassigned_file(
                    transform=transform,
                    revision=file.revision,
                    location=file.location,
                    reason=reason if len(named) > 1 or not candidates else NO_SESSION,
                    candidates=named if len(named) > 1 else (),
                )
            )
            if len(named) > 1:
                ambiguous[parent(path)].append(path)
        for group in groups:
            self.findings.append(self._contested_finding(group))
        for directory, paths in sorted(ambiguous.items()):
            self.findings.append(self._ambiguous_finding(directory, paths))
        return Grouping(
            transform=self.transform,
            proposals=tuple(sorted(proposals.values(), key=lambda p: p.id)),
            unassigned=tuple(sorted(unassigned, key=lambda u: u.id)),
            findings=tuple(sorted({f.id: f for f in self.findings}.values(), key=lambda f: f.id)),
        )

    def _contested_finding(self, group: list[int]) -> IngestFinding:
        drafts = [self.drafts[i] for i in group]
        subject = min(path for draft in drafts for path in draft.members)
        ordered = sorted(drafts, key=lambda d: (str(d.rule), d.directory, min(d.members)))
        readings: list[JsonValue] = [
            {
                "directory": directory_location(draft.directory).to_json(),
                "files": len(draft.members),
                "rule": str(draft.rule),
            }
            for draft in ordered
        ]
        rules = sorted({str(draft.rule) for draft in drafts})
        return ingest_finding(
            code=CONTESTED,
            category=FindingCategory.AMBIGUOUS,
            severity=Severity.WARNING,
            subject=local_location(subject),
            transform=self.transform,
            message=f"files support {len(group)} session readings ({', '.join(rules)});"
            " none was chosen",
            details={
                "files": len({path for draft in drafts for path in draft.members}),
                "readings": readings,
                "rules": rules,
            },
        )

    def _ambiguous_finding(self, directory: bytes, paths: list[bytes]) -> IngestFinding:
        return ingest_finding(
            code=AMBIGUOUS_MEMBER,
            category=FindingCategory.AMBIGUOUS,
            severity=Severity.WARNING,
            subject=local_location(paths[0]),
            transform=self.transform,
            message=f"{len(paths)} file(s) here could each belong to several sessions;"
            " none was chosen",
            details={
                "count": len(paths),
                "directory": directory_location(directory).to_json(),
                "files": [_location_json(path) for path in paths[:_LISTED]],
            },
        )


def _placed(draft: _Draft) -> list[Reason]:
    """One reason per rule that placed context files in ``draft`` after it formed."""
    counts: dict[Rule, int] = defaultdict(int)
    for _, rule in draft.members.values():
        if rule in _PLACED and rule is not draft.rule:
            counts[rule] += 1
    return [Reason(rule, _PLACED[rule], {"files": n}) for rule, n in sorted(counts.items())]


def _part_index(signals: NameSignals) -> int:
    if signals.part is None:
        raise ValueError(f"{signals.name!r} has no part number")
    return int(signals.part[1])


def _time_text(unit: _Unit) -> str:
    if unit.time is None:
        raise ValueError("a unit grouped by its time has one")
    return unit.time.text


def _common_directory(paths: Sequence[bytes]) -> bytes:
    """The deepest directory holding every one of ``paths``."""
    parts = [parent(path).split(b"/") if parent(path) else [] for path in paths]
    common: list[bytes] = []
    for level in zip(*parts, strict=False):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    return b"/".join(common)
