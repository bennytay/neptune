"""Snapshot bindings: the configuration, software, hardware and calibration each run ran with.

ADR 0064. The pass relates every ``Run`` in a package to the machine-context snapshots of ADR
0050 §8 (``configuration_snapshot``, ``software_configuration``, ``hardware_configuration``,
``calibration``), and says so only as far as evidence says it:

- **Stated** (canonical ``SnapshotBinding`` records, ``records/``): the run's own source names the
  snapshot by a value that reads the same wherever the tree is rooted. A text a recording declares
  (an MCAP metadata entry, any ``StructuredRecord`` cell of the run's source) equals, verbatim, the
  snapshot's content id (``sha256:<hex>`` or the bare hex), or a full git commit or a checkpoint
  or image digest one of the run's own snapshots declares. The join adds no reading (ADR 0050
  §2). A snapshot the run's own source declares is stated too. Provenance cites the declaring
  cell's row, or the snapshot's own declaration.
- **Inferred** (``InferredSnapshotBinding``, ``derived/snapshot_binding``), by a named rule.
  ``declared_by_run``: a text of the run's source is a path, relative to the recording's
  directory, that the snapshot's bytes are at (which assumes the robot's working directory), or a
  firmware version one of the run's own snapshots declares (a release, not an image's identity).
  ``session_nearest``: the snapshot is one of the run's own and nearest of its slot.
- **A run's own snapshots** belong to its *recording unit*: the run's source, joined with the files
  a ``RunAssembly`` lists for its run (MVL-34). A snapshot file belongs to the unit a
  ``RunAssembly`` places it in; else to the recording in its directory whose name stem its own
  name extends (``ep_7_hw.yaml`` beside ``ep_7.mcap``); else to the one unit below the nearest
  directory above it that holds any. It must also share a session the grouper proposes with the
  unit (the ``Grouping`` interface: a directory boundary is never crossed by a guess). A file as
  near to several units is no unit's own: it is bound to none, and one ``shared_snapshot`` finding
  names it and the runs below it. Other recordings' sidecars are never a run's candidates. Within
  a unit, snapshots compete by *slot*, their kind and file name; the one sharing the deepest
  directory with the recording wins its slot, and candidates tied for nearest are a conflict.
- **Unresolved** is explicit: a run with no binding of a kind gets a finding naming the run and
  the kind (``snapshot_unresolved``; for software, ``no_software_identity``, MVL-38's comment).
  A conflict is an ``ambiguous`` finding naming the run and every candidate, with no binding.

Identical bytes at two paths are one content: one snapshot, bound once, never two identities
merged; nothing here edits a run or a snapshot. Ids derive from content, every table is sorted
by id, and cost is linear in files and records: one index of units by directory, one owner per
snapshot file, and per run its unit's candidates and its own source's declared cells.
"""

import posixpath
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, TypeAlias

from neptune.derived.provenance import DERIVED_SCHEMA_VERSION as DERIVED_SCHEMA_VERSION
from neptune.derived.provenance import INFERRED, InferredProvenance, derived_object
from neptune.discovery.layout import Layout, ancestors, basename, name_signals, parent
from neptune.identity.findings import ingest_finding
from neptune.identity.ids import record_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model._fields import check_type, enum_decoder, json_array, json_str
from neptune.model.alignment import (
    MemberRole,
    RunAssembly,
    SnapshotBinding,
    SnapshotKind,
    ValidityWindow,
    validity_window_from_json,
)
from neptune.model.configuration import ConfigurationSnapshot
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Grounding,
    Knowledge,
    Known,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.machine import Calibration, HardwareConfiguration, SoftwareConfiguration
from neptune.model.provenance import (
    EvidenceRef,
    Provenance,
    TransformRecord,
    evidence_ref_from_json,
)
from neptune.model.run import Run
from neptune.model.versions import (
    ContainerImageDigest,
    FirmwareVersion,
    GitCommit,
    ModelCheckpointHash,
)
from neptune.model.world import StructuredRecord, StructuredTable

if TYPE_CHECKING:  # sessions.py reads this module's table, so these are imported for types only
    from neptune.derived.grouping import Grouping
    from neptune.derived.sessions import SessionProposal

BINDINGS_ID: Final = "neptune.bindings"
BINDINGS_VERSION: Final = "0.1.0"
BINDING_KIND: Final = SnapshotBinding.kind

NO_SOFTWARE_IDENTITY: Final = f"{BINDINGS_ID}.no_software_identity"
SNAPSHOT_UNRESOLVED: Final = f"{BINDINGS_ID}.snapshot_unresolved"
CONFLICTING_SNAPSHOTS: Final = f"{BINDINGS_ID}.conflicting_snapshots"
STATED_DIFFERS: Final = f"{BINDINGS_ID}.stated_differs_from_nearest"
SHARED_SNAPSHOT: Final = f"{BINDINGS_ID}.shared_snapshot"
FINDING_CODES: Final = (
    CONFLICTING_SNAPSHOTS,
    NO_SOFTWARE_IDENTITY,
    SHARED_SNAPSHOT,
    SNAPSHOT_UNRESOLVED,
    STATED_DIFFERS,
)

# The rules, by name: what put a snapshot in a run's binding (ADR 0064 §2).
DECLARED_BY_RUN: Final = "declared_by_run"  # the run's source names the snapshot
SAME_SOURCE: Final = "same_source"  # stated: the run's source declares the snapshot itself
SESSION_NEAREST: Final = "session_nearest"  # inferred: nearest of its slot among the run's own

# How much a missing binding costs: configuration and software are what a run is replayed and
# compared by; hardware and calibration records are rarer and their absence is common.
_UNRESOLVED_SEVERITY: Final[Mapping[SnapshotKind, Severity]] = {
    SnapshotKind.HARDWARE_CONFIGURATION: Severity.INFO,
    SnapshotKind.SOFTWARE_CONFIGURATION: Severity.WARNING,
    SnapshotKind.CALIBRATION: Severity.INFO,
    SnapshotKind.CONFIGURATION_SNAPSHOT: Severity.WARNING,
}
# Candidates a finding lists; the rest are counted.
_LISTED: Final = 64

Snapshot: TypeAlias = (
    ConfigurationSnapshot | SoftwareConfiguration | HardwareConfiguration | Calibration
)
SNAPSHOT_KINDS: Final[Mapping[type, SnapshotKind]] = {
    ConfigurationSnapshot: SnapshotKind.CONFIGURATION_SNAPSHOT,
    SoftwareConfiguration: SnapshotKind.SOFTWARE_CONFIGURATION,
    HardwareConfiguration: SnapshotKind.HARDWARE_CONFIGURATION,
    Calibration: SnapshotKind.CALIBRATION,
}


def binding_inputs(records: Iterable[object]) -> Iterator[object]:
    """The records the pass reads of a package: runs, snapshots, canonical bindings and run
    assemblies. The declared cells of runs' sources are passed apart (``statements``), so a caller
    keeps them only for sources that declare a run."""
    for record in records:
        if (
            isinstance(record, Run | SnapshotBinding | RunAssembly)
            or type(record) in SNAPSHOT_KINDS
        ):
            yield record


# --- The derived record -------------------------------------------------------------------------


def _inherited_only(data: JsonObject) -> Grounding:
    """A derived record's states inherit its inferred provenance; none carries a canonical one."""
    raise ValueError("a state of a derived record inherits the record's provenance")


@dataclass(frozen=True)
class InferredSnapshotBinding:
    """``SnapshotBinding``'s fields exactly (ADR 0050 §2, §8), inferred: the run ``run`` ran with
    ``snapshot`` because its source names the snapshot by a path or a firmware version, or the
    snapshot is nearest of its slot among the run's own (module docstring).

    ``evidence`` cites the naming row or the run's declaration, then the snapshot's;
    ``transform`` is the binding pass. ``validity`` is ``Unknown``: a file beside a recording
    states no window.
    """

    kind = BINDING_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    run: RecordId
    snapshot: RecordId
    snapshot_kind: SnapshotKind
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        InferredProvenance(self.evidence, self.transform)  # checks both
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("evidence repeats a citation")
        parse_record_id(self.run)
        parse_record_id(self.snapshot)
        if not isinstance(self.snapshot_kind, SnapshotKind):
            raise TypeError(f"snapshot_kind must be a SnapshotKind, got {self.snapshot_kind!r}")
        check_type("validity", self.validity, ValidityWindow)

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": INFERRED,
            "evidence": [ref.to_json() for ref in self.evidence],
            "id": self.id,
            "kind": self.kind,
            "run": self.run,
            "schema_version": DERIVED_SCHEMA_VERSION,
            "snapshot": self.snapshot,
            "snapshot_kind": str(self.snapshot_kind),
            "transform": self.transform,
            "validity": to_json(self.validity, ValidityWindow.to_json),
        }


def inferred_snapshot_binding_from_json(data: JsonValue) -> InferredSnapshotBinding:
    """Parse strictly; no state may carry provenance of its own."""
    keys = {"evidence", "id", "run", "snapshot", "snapshot_kind", "transform", "validity"}
    obj = derived_object(data, BINDING_KIND, keys)
    return InferredSnapshotBinding(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        evidence=tuple(evidence_ref_from_json(r) for r in json_array(obj["evidence"], "evidence")),
        run=parse_record_id(json_str(obj["run"], "run")),
        snapshot=parse_record_id(json_str(obj["snapshot"], "snapshot")),
        snapshot_kind=enum_decoder(SnapshotKind)(obj["snapshot_kind"]),
        validity=from_json(obj["validity"], validity_window_from_json, _inherited_only),
    )


# --- The result ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bindings:
    """What the pass found for one package: stated bindings (canonical records), inferred ones
    (a derived table) and findings, each sorted by id, under ``transform``."""

    transform: TransformRecord
    stated: tuple[SnapshotBinding, ...]
    inferred: tuple[InferredSnapshotBinding, ...]
    findings: tuple[IngestFinding, ...]

    def tables(self) -> dict[str, Iterator[JsonObject]]:
        """The package's derived table: present, and empty when nothing was inferred."""
        return {BINDING_KIND: (binding.to_json() for binding in self.inferred)}

    def summary(self) -> JsonObject:
        codes: dict[str, int] = defaultdict(int)
        for finding in self.findings:
            codes[finding.code.removeprefix(f"{BINDINGS_ID}.")] += 1
        return {
            "findings": dict(sorted(codes.items())),
            "inferred": len(self.inferred),
            "stated": len(self.stated),
        }


# --- The pass -----------------------------------------------------------------------------------


def _depth(a: bytes, b: bytes) -> int:
    """How many directories two directories share, from the root."""
    depth = 0
    for x, y in zip(a.split(b"/"), b.split(b"/"), strict=False):
        if x != y or not x:
            break
        depth += 1
    return depth


def _resolve(directory: bytes, text: bytes) -> bytes | None:
    """``text`` as a path relative to ``directory``, collapsed lexically; ``None`` if absolute
    or leaving the root (as ``LayoutLink.resolved`` reads a link)."""
    if not text or text.startswith(b"/"):
        return None
    joined = posixpath.normpath(directory + b"/" + text if directory else text)
    return None if joined == b".." or joined.startswith(b"../") or joined == b"." else joined


def _stems(base: bytes) -> Iterator[bytes]:
    """``base``, then each prefix of it that ends before a ``_`` or ``-``, longest first: the
    recording stems a sidecar's name may extend (``ep_7_hw`` extends ``ep_7``)."""
    yield base
    end = len(base)
    while (cut := max(base.rfind(b"_", 0, end), base.rfind(b"-", 0, end))) > 0:
        yield base[:cut]
        end = cut


def _identities(snapshot: Snapshot) -> Iterator[tuple[str, bool]]:
    """The identities a snapshot declares that a run's declaration can name verbatim, each with
    whether naming it is stated: a full git commit or a stated checkpoint or image digest names
    one build (stated); a firmware version names a release many images may carry (inferred).
    Other versions are too common to join on."""
    if not isinstance(snapshot, SoftwareConfiguration):
        return
    for item in snapshot.software:
        for state in (item.commit, item.digest, item.release):
            if not isinstance(state, Known):
                continue
            value = state.value
            if isinstance(value, GitCommit) and not value.abbreviated:
                yield value.sha, True
            elif isinstance(value, ModelCheckpointHash | ContainerImageDigest):
                yield value.digest, True
            elif isinstance(value, FirmwareVersion):
                yield value.value, False


Slot: TypeAlias = tuple[SnapshotKind, bytes]


@dataclass
class _Unit:
    """One recording unit (module docstring): its contents and paths, the sessions holding them,
    its runs, and the snapshots that are its own, by slot with each one's nearness and by the
    identities they declare."""

    contents: tuple[ContentId, ...]
    paths: tuple[bytes, ...]
    directories: tuple[bytes, ...]
    sessions: frozenset[RecordId]
    runs: list[Run] = field(default_factory=list)
    slots: dict[Slot, dict[ContentId, int]] = field(default_factory=lambda: defaultdict(dict))
    identities: dict[bytes, set[ContentId]] = field(default_factory=lambda: defaultdict(set))
    versions: dict[bytes, set[ContentId]] = field(default_factory=lambda: defaultdict(set))


class _Union:
    """Union-find over contents; a component's root is its least content id, so the result does
    not depend on the order unions are made in."""

    def __init__(self) -> None:
        self.up: dict[ContentId, ContentId] = {}

    def add(self, content: ContentId) -> None:
        self.up.setdefault(content, content)

    def find(self, content: ContentId) -> ContentId:
        root = content
        while self.up[root] != root:
            root = self.up[root]
        while self.up[content] != root:
            self.up[content], content = root, self.up[content]
        return root

    def union(self, a: ContentId, b: ContentId) -> None:
        left, right = self.find(a), self.find(b)
        if left != right:
            self.up[max(left, right)] = min(left, right)


@dataclass(frozen=True)
class _Shared:
    """Where a snapshot file is as near to several units: the directory, the first units below
    it (by root content id) and how many there are."""

    directory: bytes
    units: tuple[ContentId, ...]
    count: int


class _Binder:
    def __init__(
        self,
        records: Iterable[object],
        statements: Iterable[StructuredRecord | StructuredTable],
        layout: Layout,
        grouping: "Grouping",
    ) -> None:
        self.runs: list[Run] = []
        self.snapshots: dict[ContentId, list[tuple[Snapshot, SnapshotKind]]] = defaultdict(list)
        self.declared: dict[RecordId, set[SnapshotKind]] = defaultdict(set)
        assemblies: list[RunAssembly] = []
        upstream = {grouping.transform.id}
        self.runs_in: dict[ContentId, int] = defaultdict(int)
        for record in records:
            if isinstance(record, Run):
                self.runs.append(record)
                if isinstance(source := record.provenance.evidence.source, str):
                    self.runs_in[ContentId(source)] += 1
            elif isinstance(record, SnapshotBinding):
                self.declared[record.run].add(record.snapshot_kind)
            elif isinstance(record, RunAssembly):
                assemblies.append(record)
            elif (kind := SNAPSHOT_KINDS.get(type(record))) is not None:
                source = record.provenance.evidence.source  # type: ignore[attr-defined]
                if isinstance(source, str):
                    self.snapshots[ContentId(source)].append((record, kind))  # type: ignore[arg-type]
            else:
                continue
            upstream.add(record.provenance.transform)  # type: ignore[attr-defined]
        self.statements: dict[ContentId, list[StructuredRecord]] = defaultdict(list)
        self.tables: dict[RecordId, StructuredTable] = {}
        for row in statements:
            if isinstance(row, StructuredTable):
                self.tables[row.id] = row
                continue
            source = row.provenance.evidence.source
            if isinstance(source, str):
                self.statements[ContentId(source)].append(row)
                upstream.add(row.provenance.transform)
        self.transform = transform_record(
            adapter_id=BINDINGS_ID,
            adapter_version=BINDINGS_VERSION,
            config={"join": "verbatim", "scope": "recording_unit", "slot": "kind_and_file_name"},
            upstream=sorted(upstream),
        )
        for entries in self.snapshots.values():
            entries.sort(key=lambda entry: entry[0].id)
        for rows in self.statements.values():
            rows.sort(key=lambda row: row.id)
        self.paths: dict[ContentId, list[bytes]] = defaultdict(list)
        self.content_at: dict[bytes, ContentId] = {}
        content_of: dict[RecordId, ContentId] = {}
        for file in layout.files:
            self.paths[file.content_id].append(file.path)
            self.content_at[file.path] = file.content_id
            content_of[file.revision] = file.content_id
        for paths in self.paths.values():
            paths.sort()
        # The sessions holding each content, from each proposal's extent (includes followed by id).
        by_id = {proposal.id: proposal for proposal in grouping.proposals}
        self.sessions_of: dict[ContentId, set[RecordId]] = defaultdict(set)
        for proposal in grouping.proposals:
            for path in self._extent(proposal, by_id):
                if (content := self.content_at.get(path)) is not None:
                    self.sessions_of[content].add(proposal.id)
        # Content ids a run may name, whole or as the bare digest: the same under any root.
        self.by_content: dict[bytes, ContentId] = {}
        for content in self.snapshots:
            self.by_content[content.encode()] = content
            self.by_content[content.removeprefix("sha256:").encode()] = content
        self.findings: list[IngestFinding] = []
        self.stated: dict[RecordId, SnapshotBinding] = {}
        self.inferred: dict[RecordId, InferredSnapshotBinding] = {}
        self._units(assemblies, content_of)

    def _extent(
        self, proposal: "SessionProposal", by_id: Mapping[RecordId, "SessionProposal"]
    ) -> set[bytes]:
        found: set[bytes] = set()
        seen: set[RecordId] = set()
        pending = [proposal]
        while pending:
            current = pending.pop()
            if current.id in seen:
                continue
            seen.add(current.id)
            found.update(member.location.raw for member in current.members)
            pending.extend(by_id[inner] for inner in current.includes if inner in by_id)
        return found

    # --- units and their own snapshots ----------------------------------------------------------

    def _units(
        self, assemblies: Sequence[RunAssembly], content_of: Mapping[RecordId, ContentId]
    ) -> None:
        """Group contents into recording units, index them by directory once, and give each
        snapshot file its one owner, or a ``shared_snapshot`` finding."""
        union = _Union()
        for content in self.runs_in:
            union.add(content)
        run_source = {
            run.id: ContentId(source)
            for run in self.runs
            if isinstance(source := run.provenance.evidence.source, str)
        }
        context: dict[ContentId, set[ContentId]] = defaultdict(set)
        for assembly in sorted(assemblies, key=lambda record: record.id):
            if (anchor := run_source.get(assembly.run)) is None:
                continue
            for member in assembly.members:
                if (held := content_of.get(member.revision)) is None:
                    continue
                if member.role is MemberRole.CONTEXT:
                    context[held].add(anchor)
                else:
                    union.add(held)
                    union.union(anchor, held)
        members: dict[ContentId, list[ContentId]] = defaultdict(list)
        for content in list(union.up):
            members[union.find(content)].append(content)
        self.unit_of: dict[ContentId, ContentId] = {c: union.find(c) for c in union.up}
        self.units: dict[ContentId, _Unit] = {}
        for root, contents in sorted(members.items()):
            paths = tuple(sorted(p for c in contents for p in self.paths.get(c, ())))
            sessions = frozenset(s for c in contents for s in self.sessions_of.get(c, ()))
            self.units[root] = _Unit(
                tuple(sorted(contents)), paths, tuple(sorted({parent(p) for p in paths})), sessions
            )
        for run in self.runs:
            if (recording := run_source.get(run.id)) is not None:
                self.units[self.unit_of[recording]].runs.append(run)
        for unit in self.units.values():
            unit.runs.sort(key=lambda run: run.id)
        # One index, one pass: each directory's recording stems, and the units below it.
        stems: dict[bytes, dict[bytes, set[ContentId]]] = defaultdict(lambda: defaultdict(set))
        count: dict[bytes, int] = defaultdict(int)
        below: dict[bytes, list[ContentId]] = defaultdict(list)
        for root, unit in self.units.items():
            above: set[bytes] = set()
            for path in unit.paths:
                stems[parent(path)][name_signals(basename(path)).base].add(root)
                above.update(ancestors(path))
            for directory in above:
                count[directory] += 1
                if len(below[directory]) < _LISTED:
                    below[directory].append(root)
        for content in sorted(self.snapshots):
            if content in self.unit_of:
                continue  # a recording's own declarations: ``same_source``, never a candidate
            owned = False
            shared: _Shared | None = None
            for path in self.paths.get(content, ()):
                owner, where = self._owner(content, path, context, stems, count, below)
                if owner is not None:
                    if self.units[owner].sessions & self.sessions_of.get(content, set()):
                        self._own(self.units[owner], content, path)
                        owned = True
                elif shared is None:
                    shared = where
            if not owned and shared is not None:
                self._shared(content, shared)

    def _owner(
        self,
        content: ContentId,
        path: bytes,
        context: Mapping[ContentId, set[ContentId]],
        stems: Mapping[bytes, Mapping[bytes, set[ContentId]]],
        count: Mapping[bytes, int],
        below: Mapping[bytes, list[ContentId]],
    ) -> tuple[ContentId | None, _Shared | None]:
        """The unit a snapshot file belongs to, or where it is shared (module docstring)."""
        if anchors := context.get(content):  # a run assembly places it
            roots = sorted({self.unit_of[anchor] for anchor in anchors})
            if len(roots) == 1:
                return roots[0], None
            return None, _Shared(parent(path), tuple(roots[:_LISTED]), len(roots))
        directory = parent(path)
        if here := stems.get(directory):  # a recording's sidecar, by the stem its name extends
            for stem in _stems(name_signals(basename(path)).base):
                if roots := sorted(here.get(stem, ())):
                    if len(roots) == 1:
                        return roots[0], None
                    return None, _Shared(directory, tuple(roots[:_LISTED]), len(roots))
        for above in ancestors(path):  # the one unit below the nearest directory holding any
            if (found := count.get(above, 0)) == 1:
                return below[above][0], None
            if found > 1:
                return None, _Shared(above, tuple(below[above]), found)
        return None, None

    def _own(self, unit: _Unit, content: ContentId, path: bytes) -> None:
        """``content``, at ``path``, is one of ``unit``'s own snapshots."""
        directory = parent(path)
        near = max((_depth(directory, mine) for mine in unit.directories), default=0)
        for kind in sorted({kind for _, kind in self.snapshots[content]}):
            slot = unit.slots[kind, basename(path)]
            slot[content] = max(slot.get(content, -1), near)
        for snapshot, _ in self.snapshots[content]:
            for identity, stated in _identities(snapshot):
                (unit.identities if stated else unit.versions)[identity.encode()].add(content)

    def _shared(self, content: ContentId, shared: _Shared) -> None:
        """One finding for a snapshot file as near to several units: bound to none of them."""
        sessions = self.sessions_of.get(content, set())
        units = [self.units[root] for root in shared.units]
        if not any(unit.sessions & sessions for unit in units):
            return  # no session holds it with them: the grouping already reports it
        runs = [run.id for unit in units for run in unit.runs][:_LISTED]
        snapshots = self.snapshots[content]
        directory = shared.directory.decode("utf-8", "backslashreplace")
        kinds = sorted({str(kind) for _, kind in snapshots})
        where = repr(directory) if directory else "the root"
        self.findings.append(
            ingest_finding(
                code=SHARED_SNAPSHOT,
                category=FindingCategory.AMBIGUOUS,
                severity=Severity.INFO,
                subject=snapshots[0][0].provenance.evidence,
                transform=self.transform,
                message=(
                    f"the {', '.join(kinds)} file is as near to {shared.count} recordings in"
                    f" {where}, so it is no one run's own; it is bound to none"
                ),
                details={
                    "candidates": self._candidates([content], None),
                    "directory": directory,
                    "recordings": shared.count,
                    "runs": list(runs),
                },
                records=[*(s.id for s, _ in snapshots), *runs],
            )
        )

    # --- stated and declared --------------------------------------------------------------------

    def declared_by(
        self, run: Run, source: ContentId, unit: _Unit
    ) -> tuple[dict[ContentId, tuple[EvidenceRef | None, bool]], set[ContentId]]:
        """What the run's own source names or declares: content to the citing cell (``None`` for
        a snapshot its source declares itself) and whether the naming is stated; and the contents
        a declared value named among others, which a conflict finding reports and nothing may
        then bind."""
        found: dict[ContentId, tuple[EvidenceRef | None, bool]] = {}
        conflicted: set[ContentId] = set()
        if self.runs_in[source] != 1:
            return found, conflicted  # several runs: which one a row is about is not stated
        if source in self.snapshots:
            found[source] = (None, True)
        directories = sorted({parent(path) for path in self.paths.get(source, ())})
        for row in self.statements.get(source, ()):
            table = self.tables.get(row.table)
            for column, cell in enumerate(row.cells):
                if not isinstance(cell, Known) or not isinstance(cell.value, str):
                    continue
                raw = cell.value.encode("utf-8", "surrogatepass")
                stated = set(unit.identities.get(raw, ()))
                if (whole := self.by_content.get(raw)) is not None:
                    stated.add(whole)
                named = stated | unit.versions.get(raw, set())
                for directory in directories:  # a path from the recording's own directory
                    at = self.content_at.get(_resolve(directory, raw) or b"")
                    if at is not None and at in self.snapshots:
                        named.add(at)
                named.discard(source)
                if not named:
                    continue
                cited = (
                    row.cell_evidence(table, column)
                    if table is not None
                    else row.provenance.evidence
                )
                if len(named) > 1:
                    conflicted |= named
                    self._conflict(run, DECLARED_BY_RUN, None, sorted(named), cited)
                    continue
                (content,) = named
                previous = found.get(content)
                if previous is None or (content in stated and not previous[1]):
                    found[content] = (cited, content in stated)
        for content in conflicted:
            found.pop(content, None)  # named elsewhere alone, and among others here: undecided
        return found, conflicted

    # --- records --------------------------------------------------------------------------------

    def _bind_stated(self, run: Run, content: ContentId, cited: EvidenceRef | None) -> None:
        """A stated binding per snapshot of ``content``. A canonical record's id derives from its
        evidence (ADR 0017 §5), so one naming row gives one record: a file of several snapshots
        (a YAML stream, a multi-camera calibration) that a row names is bound document by
        document in the derived table instead, citing the row first (ADR 0064 §3)."""
        snapshots = self.snapshots[content]
        for snapshot, kind in snapshots:
            if cited is not None and len(snapshots) > 1:
                self._bind_inferred(run, snapshot, kind, DECLARED_BY_RUN, cited)
                continue
            evidence = cited if cited is not None else snapshot.provenance.evidence
            binding_id = evidence_record_id(BINDING_KIND, evidence, self.transform)
            if binding_id in self.stated:  # one citation, one record: the rest are derived
                self._bind_inferred(run, snapshot, kind, DECLARED_BY_RUN, evidence)
                continue
            binding = SnapshotBinding(
                id=binding_id,
                provenance=Provenance(evidence, self.transform.id, AssertionKind.STATED),
                run=run.id,
                snapshot=snapshot.id,
                snapshot_kind=kind,
                validity=Unknown(),
            )
            self.stated[binding.id] = binding

    def _bind_inferred(
        self,
        run: Run,
        snapshot: Snapshot,
        kind: SnapshotKind,
        rule: str = SESSION_NEAREST,
        cited: EvidenceRef | None = None,
    ) -> None:
        inputs: dict[str, JsonValue] = {
            "rule": rule,
            "run": run.id,
            "snapshot": snapshot.id,
            "transform": self.transform.id,
        }
        first = cited if cited is not None else run.provenance.evidence
        evidence = tuple(dict.fromkeys((first, snapshot.provenance.evidence)))
        binding = InferredSnapshotBinding(
            id=record_id(BINDING_KIND, inputs),
            transform=self.transform.id,
            evidence=evidence,
            run=run.id,
            snapshot=snapshot.id,
            snapshot_kind=kind,
            validity=Unknown(),
        )
        self.inferred[binding.id] = binding

    def _candidates(self, contents: Sequence[ContentId], kind: SnapshotKind | None) -> JsonValue:
        listed: list[JsonValue] = []
        for content in contents[:_LISTED]:
            listed.append(
                {
                    "content": content,
                    "paths": [p.decode("utf-8", "backslashreplace") for p in self.paths[content]],
                    "snapshots": [
                        s.id for s, k in self.snapshots[content] if kind is None or k is kind
                    ],
                }
            )
        return listed

    def _related(
        self, contents: Sequence[ContentId], subject: EvidenceRef
    ) -> tuple[EvidenceRef, ...]:
        refs = dict.fromkeys(
            snapshot.provenance.evidence
            for content in contents[:_LISTED]
            for snapshot, _ in self.snapshots[content]
        )
        refs.pop(subject, None)
        return tuple(refs)

    def _conflict(
        self,
        run: Run,
        rule: str,
        slot: Slot | None,
        contents: Sequence[ContentId],
        subject: EvidenceRef,
    ) -> None:
        kind = slot[0] if slot is not None else None
        records = [run.id] + [
            s.id
            for content in contents[:_LISTED]
            for s, k in self.snapshots[content]
            if kind is None or k is kind
        ]
        if slot is not None:
            name = slot[1].decode("utf-8", "backslashreplace")
            what = f"{len(contents)} {kind} candidates named {name!r} are equally near the run"
        else:
            what = f"one value the run declares names {len(contents)} different snapshots"
        details: dict[str, JsonValue] = {
            "candidates": self._candidates(contents, kind),
            "count": len(contents),
            "rule": rule,
            "run": run.id,
        }
        if slot is not None:
            details["file_name"] = slot[1].decode("utf-8", "backslashreplace")
            details["snapshot_kind"] = str(slot[0])
        self.findings.append(
            ingest_finding(
                code=CONFLICTING_SNAPSHOTS,
                category=FindingCategory.AMBIGUOUS,
                severity=Severity.WARNING,
                subject=subject,
                transform=self.transform,
                message=f"{what}; none is bound",
                details=details,
                related=self._related(contents, subject),
                records=records,
            )
        )

    def _unresolved(self, run: Run, unit: _Unit | None, kind: SnapshotKind) -> None:
        software = kind is SnapshotKind.SOFTWARE_CONFIGURATION
        message = (
            "the run has no software identity: no software, build, firmware or checkpoint"
            " record is bound to it"
            if software
            else f"no {kind} record is bound to the run: the binding is unresolved"
        )
        sessions: list[JsonValue] = [*sorted(unit.sessions)] if unit is not None else []
        self.findings.append(
            ingest_finding(
                code=NO_SOFTWARE_IDENTITY if software else SNAPSHOT_UNRESOLVED,
                category=FindingCategory.MISSING,
                severity=_UNRESOLVED_SEVERITY[kind],
                subject=run.provenance.evidence,
                transform=self.transform,
                message=message,
                details={"run": run.id, "sessions": sessions, "snapshot_kind": str(kind)},
                records=(run.id,),
            )
        )

    def bind(self, run: Run) -> None:
        bound = set(self.declared.get(run.id, ()))
        source = run.provenance.evidence.source
        if not isinstance(source, str):
            for kind in SnapshotKind:
                if kind not in bound:
                    self._unresolved(run, None, kind)
            return
        unit = self.units[self.unit_of[ContentId(source)]]
        found, conflicted = self.declared_by(run, ContentId(source), unit)
        for content, (cited, stated) in sorted(found.items(), key=lambda item: item[0]):
            if stated:
                self._bind_stated(run, content, cited)
            else:
                for snapshot, kind in self.snapshots[content]:
                    self._bind_inferred(run, snapshot, kind, DECLARED_BY_RUN, cited)
        bound |= {kind for content in found for _, kind in self.snapshots[content]}
        settled: dict[Slot, set[ContentId]] = defaultdict(set)
        for content in found:
            for path in self.paths.get(content, ()):
                for _, kind in self.snapshots[content]:
                    settled[kind, basename(path)].add(content)
        for slot, candidates in sorted(unit.slots.items()):
            kind = slot[0]
            nearest = max(candidates.values())
            winners = sorted(c for c, near in candidates.items() if near == nearest)
            if slot in settled:  # the run's own statement settles its slot, wherever it points
                said = sorted(settled[slot])
                if winners != said:
                    self._differs(run, slot, said, winners)
                continue
            if conflicted & set(candidates):
                continue  # a declared value named these among others: reported, not chosen
            if len(winners) > 1:
                self._conflict(run, SESSION_NEAREST, slot, winners, run.provenance.evidence)
                continue
            for snapshot, snapshot_kind in self.snapshots[winners[0]]:
                if snapshot_kind is kind:
                    self._bind_inferred(run, snapshot, kind)
            bound.add(kind)
        for kind in SnapshotKind:
            if kind not in bound:
                self._unresolved(run, unit, kind)

    def _differs(
        self,
        run: Run,
        slot: Slot,
        stated: Sequence[ContentId],
        nearest: Sequence[ContentId],
    ) -> None:
        name = slot[1].decode("utf-8", "backslashreplace")
        self.findings.append(
            ingest_finding(
                code=STATED_DIFFERS,
                category=FindingCategory.INCONSISTENT,
                severity=Severity.WARNING,
                subject=run.provenance.evidence,
                transform=self.transform,
                message=(
                    f"the run names a {slot[0]} {name!r} that is not the nearest of its own;"
                    " the run's statement is bound"
                ),
                details={
                    "file_name": name,
                    "nearest": self._candidates(nearest, slot[0]),
                    "run": run.id,
                    "snapshot_kind": str(slot[0]),
                    "stated": self._candidates(stated, slot[0]),
                },
                related=self._related([*stated, *nearest], run.provenance.evidence),
                records=[run.id],
            )
        )


def bind_snapshots(
    records: Iterable[object],
    statements: Iterable[StructuredRecord | StructuredTable],
    layout: Layout,
    grouping: "Grouping",
) -> Bindings | None:
    """Bind every run in ``records`` to the snapshots evidence relates it to (ADR 0064).

    ``records`` may hold anything; only runs, snapshots, canonical bindings and run assemblies are
    read. ``statements`` are the declared rows of the runs' own sources, and their tables.
    ``None`` when there is no run: no transform, no table, no finding. The same inputs give the
    same result in any order.
    """
    binder = _Binder(binding_inputs(records), statements, layout, grouping)
    if not binder.runs:
        return None
    for run in sorted(binder.runs, key=lambda run: run.id):
        binder.bind(run)
    findings = {finding.id: finding for finding in binder.findings}
    return Bindings(
        transform=binder.transform,
        stated=tuple(binder.stated[key] for key in sorted(binder.stated)),
        inferred=tuple(binder.inferred[key] for key in sorted(binder.inferred)),
        findings=tuple(findings[key] for key in sorted(findings)),
    )
