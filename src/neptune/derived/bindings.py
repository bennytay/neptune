"""Snapshot bindings: the configuration, software, hardware and calibration each run ran with.

ADR 0064. The pass relates every ``Run`` in a package to the machine-context snapshots of ADR
0050 §8 (``configuration_snapshot``, ``software_configuration``, ``hardware_configuration``,
``calibration``), and says so only as far as evidence says it:

- **Stated** (canonical ``SnapshotBinding`` records, ``records/``): the run's own source names the
  snapshot. A text a recording declares (an MCAP metadata entry, any ``StructuredRecord`` cell of
  the run's source) equals, verbatim, the snapshot's content id (``sha256:<hex>`` or the bare hex),
  a path the snapshot's bytes are at (root-relative, or relative to the recording's directory),
  or an identity the snapshot declares (a git commit, a stated digest, a firmware version). The
  join adds no reading (ADR 0050 §2). A snapshot that the run's own source declares is stated
  too. Provenance cites the declaring cell's row, or the snapshot's own declaration.
- **Inferred** (``InferredSnapshotBinding``, ``derived/snapshot_binding``): the snapshot's file is
  in a session the grouper proposes for the run's recording (the ``Grouping`` interface, never a
  filesystem rule of this module's own). Snapshots compete by *slot*: their kind and file name
  (two ``params.yaml`` are two candidates for one slot; ``params.yaml`` and ``nav.yaml`` two
  slots). The candidate whose file shares the deepest directory with the recording wins its slot;
  candidates tied for nearest are a conflict, never a choice.
- **Unresolved** is explicit: a run with no binding of a kind gets a finding naming the run and
  the kind (``snapshot_unresolved``; for software, ``no_software_identity``, MVL-38's comment).
  A conflict is an ``ambiguous`` finding naming the run and every candidate, with no binding.

Identical bytes at two paths are one content: one snapshot, bound once, never two identities
merged; nothing here edits a run or a snapshot. Ids derive from content, every table is sorted
by id, and cost is the size of each run's sessions plus its source's declared cells.
"""

import posixpath
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeAlias

from neptune.derived.provenance import DERIVED_SCHEMA_VERSION as DERIVED_SCHEMA_VERSION
from neptune.derived.provenance import INFERRED, InferredProvenance, derived_object
from neptune.discovery.layout import Layout, basename, parent
from neptune.identity.findings import ingest_finding
from neptune.identity.ids import record_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model._fields import check_type, enum_decoder, json_array, json_str
from neptune.model.alignment import (
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
FINDING_CODES: Final = (
    CONFLICTING_SNAPSHOTS,
    NO_SOFTWARE_IDENTITY,
    SNAPSHOT_UNRESOLVED,
    STATED_DIFFERS,
)

# The rules, by name: what put a snapshot in a run's binding (ADR 0064 §2).
DECLARED_BY_RUN: Final = "declared_by_run"  # stated: the run's source names the snapshot
SAME_SOURCE: Final = "same_source"  # stated: the run's source declares the snapshot itself
SESSION_NEAREST: Final = "session_nearest"  # inferred: nearest of its slot in the run's sessions

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
    """The records the pass reads of a package: runs, snapshots and canonical bindings. The
    declared cells of runs' sources are passed apart (``statements``), so a caller loads them
    only for sources that declare a run."""
    for record in records:
        if isinstance(record, Run | SnapshotBinding) or type(record) in SNAPSHOT_KINDS:
            yield record


# --- The derived record -------------------------------------------------------------------------


def _inherited_only(data: JsonObject) -> Grounding:
    """A derived record's states inherit its inferred provenance; none carries a canonical one."""
    raise ValueError("a state of a derived record inherits the record's provenance")


@dataclass(frozen=True)
class InferredSnapshotBinding:
    """``SnapshotBinding``'s fields exactly (ADR 0050 §2, §8), inferred: the run ``run`` ran with
    ``snapshot`` because the snapshot's file is nearest of its slot in the run's sessions.

    ``evidence`` cites the run's declaration, then the snapshot's; ``transform`` is the binding
    pass. ``validity`` is ``Unknown``: a file beside a recording states no window.
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


def _common_depth(a: bytes, b: bytes) -> int:
    """How many directories the two files' paths share, from the root."""
    left, right = parent(a).split(b"/"), parent(b).split(b"/")
    depth = 0
    for x, y in zip(left, right, strict=False):
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


def _identities(snapshot: Snapshot) -> Iterator[str]:
    """The identities a snapshot declares that a run's declaration can name verbatim: a git
    commit, a stated digest, a firmware version. Other versions are too common to join on."""
    if not isinstance(snapshot, SoftwareConfiguration):
        return
    for item in snapshot.software:
        for state in (item.commit, item.digest, item.release):
            if not isinstance(state, Known):
                continue
            value = state.value
            if isinstance(value, GitCommit) and not value.abbreviated:
                yield value.sha
            elif isinstance(value, ModelCheckpointHash | ContainerImageDigest):
                yield value.digest
            elif isinstance(value, FirmwareVersion):
                yield value.value


@dataclass
class _Run:
    """One run's view: its recording's paths and the files of the sessions holding it."""

    run: Run
    source: ContentId | None
    paths: tuple[bytes, ...]
    session_paths: tuple[bytes, ...]
    sessions: tuple[RecordId, ...]


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
        upstream = {grouping.transform.id}
        self.runs_in: dict[ContentId, int] = defaultdict(int)
        for record in records:
            if isinstance(record, Run):
                self.runs.append(record)
                if isinstance(source := record.provenance.evidence.source, str):
                    self.runs_in[ContentId(source)] += 1
            elif isinstance(record, SnapshotBinding):
                self.declared[record.run].add(record.snapshot_kind)
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
            config={"slot": "kind_and_file_name", "join": "verbatim"},
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
        # The files of each proposal's extent, computed once: includes are followed by id.
        by_id = {proposal.id: proposal for proposal in grouping.proposals}
        self.extents: dict[RecordId, tuple[bytes, ...]] = {}
        self.sessions_of: dict[ContentId, set[RecordId]] = defaultdict(set)
        for proposal in grouping.proposals:
            paths = self._extent(proposal, by_id)
            self.extents[proposal.id] = paths
            for path in paths:
                if (content := self.content_at.get(path)) is not None:
                    self.sessions_of[content].add(proposal.id)
        # Global joins: by content id and by path, for every content holding a snapshot.
        self.by_name: dict[bytes, set[ContentId]] = defaultdict(set)
        for content in self.snapshots:
            digest = content.removeprefix("sha256:").encode()
            self.by_name[content.encode()].add(content)
            self.by_name[digest].add(content)
            for path in self.paths.get(content, ()):
                self.by_name[path].add(content)
        self.findings: list[IngestFinding] = []
        self.stated: dict[RecordId, SnapshotBinding] = {}
        self.inferred: dict[RecordId, InferredSnapshotBinding] = {}

    def _extent(
        self, proposal: "SessionProposal", by_id: Mapping[RecordId, "SessionProposal"]
    ) -> tuple[bytes, ...]:
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
        return tuple(sorted(found))

    def view(self, run: Run) -> _Run:
        source = run.provenance.evidence.source
        if not isinstance(source, str):
            return _Run(run, None, (), (), ())
        content = ContentId(source)
        sessions = tuple(sorted(self.sessions_of.get(content, ())))
        paths = {path for session in sessions for path in self.extents[session]}
        return _Run(
            run, content, tuple(sorted(self.paths.get(content, ()))), tuple(sorted(paths)), sessions
        )

    # --- stated ---------------------------------------------------------------------------------

    def _named(
        self, view: _Run, text: str, local: Mapping[bytes, set[ContentId]]
    ) -> set[ContentId]:
        """The contents ``text`` names, verbatim: by content id or path anywhere in the package,
        by a declared identity among the run's session files."""
        raw = text.encode("utf-8", "surrogatepass")
        named = set(self.by_name.get(raw, ()))
        for path in view.paths:
            if (resolved := _resolve(parent(path), raw)) is not None:
                named |= self.by_name.get(resolved, set())
        named |= local.get(raw, set())
        named.discard(view.source)
        return named

    def stated_for(self, view: _Run) -> tuple[dict[ContentId, EvidenceRef | None], set[ContentId]]:
        """What the run's own source names or declares: content to the citing cell (``None``
        for a snapshot its source declares itself); and the contents a declared value named
        among others, which a conflict finding reports and nothing may then bind."""
        found: dict[ContentId, EvidenceRef | None] = {}
        conflicted: set[ContentId] = set()
        if view.source is None or self.runs_in[view.source] != 1:
            return found, conflicted  # several runs: which one a row is about is not stated
        if view.source in self.snapshots:
            found[view.source] = None
        local: dict[bytes, set[ContentId]] = defaultdict(set)
        for path in view.session_paths:
            content = self.content_at[path]
            for snapshot, _ in self.snapshots.get(content, ()):
                for identity in _identities(snapshot):
                    local[identity.encode()].add(content)
        for row in self.statements.get(view.source, ()):
            table = self.tables.get(row.table)
            for column, cell in enumerate(row.cells):
                if not isinstance(cell, Known) or not isinstance(cell.value, str):
                    continue
                named = self._named(view, cell.value, local)
                if not named:
                    continue
                cited = (
                    row.cell_evidence(table, column)
                    if table is not None
                    else row.provenance.evidence
                )
                if len(named) > 1:
                    conflicted |= named
                    self._conflict(view, DECLARED_BY_RUN, None, sorted(named), cited)
                else:
                    (content,) = named
                    found.setdefault(content, cited)
        for content in conflicted:
            found.pop(content, None)  # named elsewhere alone, and among others here: undecided
        return found, conflicted

    # --- inferred -------------------------------------------------------------------------------

    def slots(self, view: _Run) -> dict[tuple[SnapshotKind, bytes], dict[ContentId, int]]:
        """Each slot's candidates in the run's sessions, with each one's nearness."""
        slots: dict[tuple[SnapshotKind, bytes], dict[ContentId, int]] = defaultdict(dict)
        for path in view.session_paths:
            content = self.content_at[path]
            if content == view.source or content not in self.snapshots:
                continue
            near = max((_common_depth(path, mine) for mine in view.paths), default=0)
            for kind in sorted({kind for _, kind in self.snapshots[content]}):
                slot = slots[kind, basename(path)]
                slot[content] = max(slot.get(content, -1), near)
        return slots

    # --- records --------------------------------------------------------------------------------

    def _bind_stated(self, view: _Run, content: ContentId, cited: EvidenceRef | None) -> None:
        """A stated binding per snapshot of ``content``. A canonical record's id derives from its
        evidence (ADR 0017 §5), so one naming row gives one record: a file of several snapshots
        (a YAML stream, a multi-camera calibration) that a row names is bound document by
        document in the derived table instead, citing the row first (ADR 0064 §3)."""
        snapshots = self.snapshots[content]
        for snapshot, kind in snapshots:
            if cited is not None and len(snapshots) > 1:
                self._bind_inferred(view, snapshot, kind, DECLARED_BY_RUN, cited)
                continue
            evidence = cited if cited is not None else snapshot.provenance.evidence
            binding_id = evidence_record_id(BINDING_KIND, evidence, self.transform)
            if binding_id in self.stated:  # one citation, one record: the rest are derived
                self._bind_inferred(view, snapshot, kind, DECLARED_BY_RUN, evidence)
                continue
            binding = SnapshotBinding(
                id=binding_id,
                provenance=Provenance(evidence, self.transform.id, AssertionKind.STATED),
                run=view.run.id,
                snapshot=snapshot.id,
                snapshot_kind=kind,
                validity=Unknown(),
            )
            self.stated[binding.id] = binding

    def _bind_inferred(
        self,
        view: _Run,
        snapshot: Snapshot,
        kind: SnapshotKind,
        rule: str = SESSION_NEAREST,
        cited: EvidenceRef | None = None,
    ) -> None:
        inputs: dict[str, JsonValue] = {
            "rule": rule,
            "run": view.run.id,
            "snapshot": snapshot.id,
            "transform": self.transform.id,
        }
        first = cited if cited is not None else view.run.provenance.evidence
        evidence = tuple(dict.fromkeys((first, snapshot.provenance.evidence)))
        binding = InferredSnapshotBinding(
            id=record_id(BINDING_KIND, inputs),
            transform=self.transform.id,
            evidence=evidence,
            run=view.run.id,
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
        view: _Run,
        rule: str,
        slot: tuple[SnapshotKind, bytes] | None,
        contents: Sequence[ContentId],
        subject: EvidenceRef,
    ) -> None:
        kind = slot[0] if slot is not None else None
        records = [view.run.id] + [
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
            "run": view.run.id,
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

    def _unresolved(self, view: _Run, kind: SnapshotKind) -> None:
        software = kind is SnapshotKind.SOFTWARE_CONFIGURATION
        message = (
            "the run has no software identity: no software, build, firmware or checkpoint"
            " record is bound to it"
            if software
            else f"no {kind} record is bound to the run: the binding is unresolved"
        )
        self.findings.append(
            ingest_finding(
                code=NO_SOFTWARE_IDENTITY if software else SNAPSHOT_UNRESOLVED,
                category=FindingCategory.MISSING,
                severity=_UNRESOLVED_SEVERITY[kind],
                subject=view.run.provenance.evidence,
                transform=self.transform,
                message=message,
                details={
                    "run": view.run.id,
                    "sessions": list(view.sessions),
                    "snapshot_kind": str(kind),
                },
                records=(view.run.id,),
            )
        )

    def bind(self, run: Run) -> None:
        view = self.view(run)
        stated, conflicted = self.stated_for(view)
        for content, cited in sorted(stated.items(), key=lambda item: item[0]):
            self._bind_stated(view, content, cited)
        bound = set(self.declared.get(run.id, ()))
        bound |= {kind for content in stated for _, kind in self.snapshots[content]}
        settled: dict[tuple[SnapshotKind, bytes], set[ContentId]] = defaultdict(set)
        for content in stated:
            for path in self.paths.get(content, ()):
                for _, kind in self.snapshots[content]:
                    settled[kind, basename(path)].add(content)
        for slot, candidates in sorted(self.slots(view).items()):
            kind = slot[0]
            nearest = max(candidates.values())
            winners = sorted(c for c, near in candidates.items() if near == nearest)
            if slot in settled:  # the run's own statement settles its slot, wherever it points
                said = sorted(settled[slot])
                if winners != said:
                    self._differs(view, slot, said, winners)
                continue
            if conflicted & set(candidates):
                continue  # a declared value named these among others: reported, not chosen
            if len(winners) > 1:
                self._conflict(view, SESSION_NEAREST, slot, winners, run.provenance.evidence)
                continue
            for snapshot, snapshot_kind in self.snapshots[winners[0]]:
                if snapshot_kind is kind:
                    self._bind_inferred(view, snapshot, kind)
            bound.add(kind)
        for kind in SnapshotKind:
            if kind not in bound:
                self._unresolved(view, kind)

    def _differs(
        self,
        view: _Run,
        slot: tuple[SnapshotKind, bytes],
        stated: Sequence[ContentId],
        nearest: Sequence[ContentId],
    ) -> None:
        name = slot[1].decode("utf-8", "backslashreplace")
        self.findings.append(
            ingest_finding(
                code=STATED_DIFFERS,
                category=FindingCategory.INCONSISTENT,
                severity=Severity.WARNING,
                subject=view.run.provenance.evidence,
                transform=self.transform,
                message=(
                    f"the run names a {slot[0]} {name!r} that is not the nearest one in its"
                    " session; the run's statement is bound"
                ),
                details={
                    "file_name": name,
                    "nearest": self._candidates(nearest, slot[0]),
                    "run": view.run.id,
                    "snapshot_kind": str(slot[0]),
                    "stated": self._candidates(stated, slot[0]),
                },
                related=self._related([*stated, *nearest], view.run.provenance.evidence),
                records=[view.run.id],
            )
        )


def bind_snapshots(
    records: Iterable[object],
    statements: Iterable[StructuredRecord | StructuredTable],
    layout: Layout,
    grouping: "Grouping",
) -> Bindings | None:
    """Bind every run in ``records`` to the snapshots evidence relates it to (ADR 0064).

    ``records`` may hold anything; only runs, snapshots and canonical bindings are read.
    ``statements`` are the declared rows of the runs' own sources, and their tables. ``None``
    when there is no run: no transform, no table, no finding. The same inputs give the same
    result in any order.
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
