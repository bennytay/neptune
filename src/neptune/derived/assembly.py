"""Run/experience assembly: grouping over evidence as well as names (ADR 0066, MVL-34).

``RunAssembler`` implements ADR 0036's ``Grouper`` interface. It reads the same ``Layout`` v0 reads
and, beside it, the records adapters committed for the job's sources (``Evidence``): the runs they
declare and the clocks those runs are on, the machine identifiers and software they state, the
files a rosbag2 bag lists, which sources are configuration, and the words documents hold. It runs
v0's rules first, under its own transform, then sets the evidence against their readings:

1. **Stated file lists** (``rosbag2_file_list``). A bag's ``metadata.yaml`` lists its storage
   files. That list is the recorder's statement, so it becomes a canonical, ``stated``
   ``RunAssembly`` (ADR 0050 §7): the metadata's ``Run``, the metadata as its description and every
   listed file present beside it as a recording, each citing the row that lists it. The reading
   of the bag directory then holds exactly the listed files. A listed file that is absent is a
   ``listed_part_missing`` finding; a storage file beside the metadata that it does not list is an
   ``unlisted_part`` finding and its own recording, never silently part of the bag.
2. **Edges** between the recordings of each reading, each a named rule with a fixed weight
   (``WEIGHTS``) and a reason citing the files and records it read:
   ``same_machine`` / ``mixed_machines`` (machine identifiers the sources declare, compared within
   one namespace only), ``times_overlap`` / ``times_apart`` (run times, compared only on clocks
   whose declared epoch and timescale make them one clock), ``same_software`` /
   ``software_differs`` (a software item's declared commit or release).
3. **Split.** A reading whose recordings declare different machines in one namespace is offered
   beside one ``machine_split`` reading per machine: contested, with a ``mixed_machines`` finding.
   A multi-robot session is real, so neither is chosen.
4. **Merge.** Loose recordings in one directory that declare the same machine and whose runs
   overlap on one clock are offered as one ``machine_time_merge`` reading, contested with the
   recordings' own readings.
5. **Documents.** A document no reading holds joins the reading whose recording or directory
   its text names (``named_in_document``); naming several unrelated readings, it stays
   unassigned and ambiguous among them. A document naming nothing stays where v0 left it.
6. **Shared configuration.** A configuration source above several sessions and in none is held by
   none: it stays unassigned (``shared_reference``, naming every session below it), and each of
   those sessions says it shares it. It is never merged into, or contaminates, any one of them.

A reading's confidence is ``score``: its rule's band, raised by supporting edges and lowered by
contradicting ones, by one fixed formula on exact fractions, rounded to four places. No model, no
randomness, no wall clock: the same layout, records and config give the same grouping.
"""

import os
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Final

from neptune.derived.grouping import (
    BAG_METADATA,
    BAG_STORAGE,
    CONFIDENCE,
    NO_SESSION,
    SHARED_REFERENCE,
    TOO_MANY_SESSIONS,
    Grouping,
    GroupingConfig,
    Rule,
    _Proposer,
    check_grouping,
)
from neptune.derived.sessions import Reason, Role
from neptune.discovery.layout import ROOT, Layout, basename, parent
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.alignment import MemberRole, RunAssembly, RunMember
from neptune.model.configuration import ConfigurationSnapshot
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
from neptune.model.machine import Machine, SoftwareConfiguration
from neptune.model.provenance import EvidenceRef, Provenance, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run
from neptune.model.source import local_location
from neptune.model.time import Epoch, Timescale
from neptune.model.world import DocumentBlock, StructuredRecord, StructuredTable

ASSEMBLY_ID: Final = "neptune.assembly"
ASSEMBLY_VERSION: Final = "0.1.0"

# Finding codes, ``<producer>.<name>``.
LISTED_PART_MISSING: Final = f"{ASSEMBLY_ID}.listed_part_missing"
UNLISTED_PART: Final = f"{ASSEMBLY_ID}.unlisted_part"
MIXED_MACHINES_FINDING: Final = f"{ASSEMBLY_ID}.mixed_machines"
FINDING_CODES: Final = (LISTED_PART_MISSING, MIXED_MACHINES_FINDING, UNLISTED_PART)

# Unassigned reason: a document naming several readings that share no file.
SEVERAL_NAMED: Final = "several_named"

# rosbag2's list of storage files, as the rosbag2 adapter tables it (ADR 0045 §4).
FILE_LIST_TABLE: Final = "relative_file_paths"

# Words a document must name for ``named_in_document``: at least this long, so ``a.bag`` or
# ``run_1`` in prose never places a note; and at most this many words are kept per document.
MIN_NAME: Final = 6
MAX_WORDS: Final = 100_000
_WORD: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*")
_LISTED: Final = 64

# Clocks whose declared epoch makes one clock of every domain that states it with one timescale.
_SHARED_EPOCHS: Final = frozenset({Epoch.UNIX, Epoch.GPS})
_CIVIL_TIMESCALES: Final = frozenset({Timescale.UTC, Timescale.TAI, Timescale.GPS, Timescale.POSIX})


class Edge(StrEnum):
    """Evidence between the recordings of one reading. Each names a reason; supports raise a
    reading's confidence and contradictions lower it (``score``)."""

    SAME_MACHINE = "same_machine"
    MIXED_MACHINES = "mixed_machines"
    TIMES_OVERLAP = "times_overlap"
    TIMES_APART = "times_apart"
    SAME_SOFTWARE = "same_software"
    SOFTWARE_DIFFERS = "software_differs"


# Fixed weights (ADR 0066 §4): a ranking, not a probability.
WEIGHTS: Final[Mapping[Edge, Fraction]] = {
    Edge.SAME_MACHINE: Fraction(1, 2),
    Edge.TIMES_OVERLAP: Fraction(1, 2),
    Edge.SAME_SOFTWARE: Fraction(1, 5),
    Edge.MIXED_MACHINES: Fraction(3, 5),
    Edge.TIMES_APART: Fraction(1, 2),
    Edge.SOFTWARE_DIFFERS: Fraction(3, 10),
}
SUPPORTS: Final = frozenset({Edge.SAME_MACHINE, Edge.TIMES_OVERLAP, Edge.SAME_SOFTWARE})
_FLOOR: Final = Fraction(1, 100)


def score(band: float, edges: Iterable[Edge]) -> float:
    """``c = 1 - (1 - band) * prod(1 - w_s)`` over supporting edges, then ``c * prod(1 - w_c)``
    over contradicting ones, at least 0.01, rounded half-even to four decimal places. Exact
    fractions throughout, so the result is the same on every platform."""
    exact = Fraction(str(band))
    found = list(edges)
    doubt = Fraction(1) - exact
    for edge in found:
        if edge in SUPPORTS:
            doubt *= 1 - WEIGHTS[edge]
    value = 1 - doubt
    for edge in found:
        if edge not in SUPPORTS:
            value *= 1 - WEIGHTS[edge]
    value = max(value, _FLOOR)
    return float(round(value, 4))


# --- Evidence ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FileList:
    """The storage files a rosbag2 ``metadata.yaml`` lists: its run, the table and each row."""

    run: RecordId
    run_evidence: EvidenceRef
    evidence: EvidenceRef
    entries: tuple[tuple[str, EvidenceRef], ...]


@dataclass(frozen=True)
class Interval:
    """A run's first and last instants on a clock that ``family`` (epoch, timescale) makes one
    clock across sources, in seconds."""

    family: tuple[str, str]
    start: Fraction
    end: Fraction


@dataclass
class SourceEvidence:
    """What the records committed from one source's bytes say that assembly reads."""

    runs: list[RecordId] = field(default_factory=list)
    machines: set[LogicalId] = field(default_factory=set)
    software: set[tuple[str, str]] = field(default_factory=set)
    intervals: list[Interval] = field(default_factory=list)
    file_lists: list[FileList] = field(default_factory=list)
    configuration: bool = False
    words: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Evidence:
    """Evidence by source content id, and the transforms that produced it (the assembler's
    upstream, so an adapter upgrade re-lineages the assembly)."""

    sources: Mapping[ContentId, SourceEvidence]
    transforms: tuple[RecordId, ...]

    @staticmethod
    def empty() -> "Evidence":
        return Evidence({}, ())


# The record kinds assembly reads; a job keeps only these while it collects evidence.
ASSEMBLY_INPUTS: Final = (
    Run,
    TimestampDomain,
    Machine,
    SoftwareConfiguration,
    ConfigurationSnapshot,
    StructuredTable,
    StructuredRecord,
    DocumentBlock,
)


def _source(record: object) -> ContentId | None:
    provenance = getattr(record, "provenance", None)
    if not isinstance(provenance, Provenance):
        return None
    source = provenance.evidence.source
    return ContentId(source) if isinstance(source, str) else None


class EvidenceBuilder:
    """Gathers evidence from records as they are read, chunk by chunk, keeping only what
    assembly reads: a document's words (at most ``MAX_WORDS`` per source), not its blocks; the
    rows of rosbag2 file-list tables (each in the batch of its table, as the adapter emits them
    in one chunk), not every table's rows. So memory grows with runs and names, never with a source's size."""

    def __init__(self) -> None:
        self.by_source: dict[ContentId, SourceEvidence] = defaultdict(SourceEvidence)
        self.transforms: set[RecordId] = set()
        self.domains: dict[RecordId, TimestampDomain] = {}
        self.runs: dict[ContentId, list[Run]] = defaultdict(list)
        self.tables: dict[RecordId, tuple[ContentId, StructuredTable]] = {}
        self.rows: dict[RecordId, list[StructuredRecord]] = defaultdict(list)

    def add(self, records: Iterable[object]) -> "EvidenceBuilder":
        """Add one batch (a chunk's output): its tables first, so a row finds its table
        whatever order the batch is in."""
        batch = [record for record in records if isinstance(record, ASSEMBLY_INPUTS)]
        for record in batch:
            if isinstance(record, StructuredTable):
                self._add(record)
        for record in batch:
            if not isinstance(record, StructuredTable):
                self._add(record)
        return self

    def _add(self, record: object) -> None:
        content = _source(record)
        if content is None:
            return
        provenance = record.provenance  # type: ignore[attr-defined]  # _source checked it
        found = self.by_source[content]
        if isinstance(record, TimestampDomain):
            self.domains[record.id] = record  # read through a run, under the run's transform
            return
        if isinstance(record, StructuredTable):
            if isinstance(record.name, Known) and record.name.value == FILE_LIST_TABLE:
                self.tables[record.id] = (content, record)
            return
        if isinstance(record, StructuredRecord):
            if record.table in self.tables:
                self.rows[record.table].append(record)
            return
        if isinstance(record, Run):
            self.runs[content].append(record)
            found.runs.append(record.id)
            if isinstance(record.machine, Known):
                found.machines.add(record.machine.value)
        elif isinstance(record, Machine):
            found.machines.update(_identifiers(record))
        elif isinstance(record, SoftwareConfiguration):
            if isinstance(record.machine, Known):
                found.machines.add(record.machine.value)
            for item in record.software:
                if isinstance(item.name, Known):
                    for version in (item.commit, item.release):
                        if isinstance(version, Known):
                            found.software.add((item.name.value, str(version.value)))
                            break
        elif isinstance(record, ConfigurationSnapshot):
            found.configuration = True
        elif isinstance(record, DocumentBlock):
            if not isinstance(record.text, Known) or len(found.words) >= MAX_WORDS:
                return
            for word in _WORD.findall(record.text.value):
                found.words.add(word.rstrip("._-"))
                if len(found.words) >= MAX_WORDS:
                    break
        self.transforms.add(provenance.transform)

    def build(self) -> Evidence:
        for content, runs in self.runs.items():
            found = self.by_source[content]
            for run in sorted(runs, key=lambda r: r.id):
                found.intervals.extend(_intervals(run, self.domains))
        for table_id, (content, table) in sorted(self.tables.items()):
            held = self.runs.get(content, [])
            if len(held) != 1:
                continue  # a file list belongs to the one run its metadata declares
            run = held[0]
            entries: list[tuple[str, EvidenceRef]] = []
            for row in sorted(self.rows.get(table_id, []), key=lambda r: r.row):
                cell = row.cells[0] if row.cells else None
                if isinstance(cell, Known) and isinstance(cell.value, str):
                    entries.append((cell.value, row.provenance.evidence))
            self.by_source[content].file_lists.append(
                FileList(run.id, run.provenance.evidence, table.provenance.evidence, tuple(entries))
            )
            self.transforms.add(table.provenance.transform)
        return Evidence(dict(self.by_source), tuple(sorted(self.transforms)))


def evidence_of(records: Iterable[object]) -> Evidence:
    """The evidence ``records`` hold, as one batch."""
    return EvidenceBuilder().add(records).build()


def _identifiers(machine: Machine) -> Iterator[LogicalId]:
    for identifier in machine.identifiers:
        if isinstance(identifier, Known):
            yield identifier.value


def _intervals(run: Run, domains: Mapping[RecordId, TimestampDomain]) -> Iterator[Interval]:
    """The run's span on a shared clock, when both ends are known on one such clock."""
    if not isinstance(run.first, Known) or not isinstance(run.last, Known):
        return
    first, last = run.first.value, run.last.value
    if first.domain_id != last.domain_id:
        return
    domain = domains.get(first.domain_id)
    if domain is None:
        return
    family = _family(domain)
    if family is None or not isinstance(domain.resolution, Known):
        return
    resolution = domain.resolution.value
    start, end = first.ticks * resolution, last.ticks * resolution
    yield Interval(family, min(start, end), max(start, end))


def _family(domain: TimestampDomain) -> tuple[str, str] | None:
    """(epoch, timescale) when the domain states a civil epoch and timescale: every domain
    stating the same pair counts the same instants the same way. Any other clock (boot,
    monotonic, unknown) is its own, and only a clock mapping (MVL-36) relates it."""
    epoch, timescale = domain.epoch, domain.timescale
    if not isinstance(epoch, Known) or not isinstance(timescale, Known):
        return None
    if epoch.value not in _SHARED_EPOCHS or timescale.value not in _CIVIL_TIMESCALES:
        return None
    return (str(epoch.value), str(timescale.value))


# --- The assembler -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Assembly:
    """What the assembler made of one layout: the grouping (derived) and the canonical run
    assemblies the evidence states (``records/run_assembly``)."""

    grouping: Grouping
    records: tuple[RunAssembly, ...]


class RunAssembler:
    """The evidence-graph grouper (module docstring; ADR 0066). ``propose`` keeps ADR 0036's
    ``Grouper`` interface; ``assemble`` also returns the stated run assemblies."""

    def __init__(
        self,
        config: GroupingConfig | None = None,
        evidence: Evidence | None = None,
        *,
        upstream: Sequence[RecordId] = (),
    ) -> None:
        self.config = config if config is not None else GroupingConfig()
        self.evidence = evidence if evidence is not None else Evidence.empty()
        self.transform: TransformRecord = transform_record(
            adapter_id=ASSEMBLY_ID,
            adapter_version=ASSEMBLY_VERSION,
            config=self.config.to_json(),
            upstream=tuple(sorted({*upstream, *self.evidence.transforms})),
        )

    def assemble(self, layout: Layout) -> Assembly:
        proposer = _Assembler(self.config, self.transform, layout, self.evidence)
        grouping = proposer.run()
        check_grouping(grouping, layout)
        records = {record.id: record for record in proposer.assemblies}
        return Assembly(grouping, tuple(records[key] for key in sorted(records)))

    def propose(self, layout: Layout) -> Grouping:
        return self.assemble(layout).grouping


def _location_json(path: bytes) -> JsonObject:
    return local_location(path).to_json()


def _locations(paths: Iterable[bytes]) -> list[JsonValue]:
    return [_location_json(path) for path in sorted(paths)[:_LISTED]]


def _text(path: bytes) -> str | None:
    try:
        return path.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _listed_path(directory: bytes, text: str) -> bytes | None:
    """A listed part's root-relative path, read lexically against the bag's directory; ``None``
    for one that is empty, absolute, holds a backslash or leaves the directory (the adapter's
    ``unsafe_part_path`` says why)."""
    if not text or text.startswith("/") or "\\" in text or "\x00" in text:
        return None
    joined = os.path.normpath((directory + b"/" if directory else b"") + text.encode("utf-8"))
    inside = directory + b"/" if directory else b""
    if not joined.startswith(inside) or joined == directory or joined.startswith(b".."):
        return None
    return joined


class _Assembler(_Proposer):
    """One run of the assembler: v0's readings of the layout, then the evidence (ADR 0066)."""

    def __init__(
        self,
        config: GroupingConfig,
        transform: TransformRecord,
        layout: Layout,
        evidence: Evidence,
    ) -> None:
        super().__init__(config, transform, layout)
        self.evidence = evidence
        self.assemblies: list[RunAssembly] = []

    def _of(self, path: bytes) -> SourceEvidence | None:
        return self.evidence.sources.get(ContentId(self.files[path].content_id))

    def _evidence(self) -> None:
        self._file_lists()
        self._merges()
        self._edges()
        self._mentions()
        self._shared()

    # --- 1. stated file lists ------------------------------------------------------------------

    def _file_lists(self) -> None:
        holders: dict[bytes, list[int]] = defaultdict(list)
        for index, draft in enumerate(self.drafts):
            for path in draft.members:
                holders[path].append(index)
        by_directory: dict[bytes, list[bytes]] = defaultdict(list)
        for path in self.files:
            by_directory[parent(path)].append(path)
        # One statement per metadata's bytes, so one canonical record: copies of a bag share
        # their metadata's Run, and the record lists every copy's files (ADR 0066 §1).
        members: dict[ContentId, dict[RecordId, RunMember]] = defaultdict(dict)
        stated: dict[ContentId, FileList] = {}
        for path in sorted(self.files):
            if basename(path) != BAG_METADATA:
                continue
            found = self._of(path)
            if found is None or len(found.file_lists) != 1:
                continue
            content = ContentId(self.files[path].content_id)
            stated[content] = found.file_lists[0]
            members[content].update(self._file_list(path, stated[content], holders, by_directory))
        for content, statement in sorted(stated.items()):
            held = members[content]
            self.assemblies.append(
                RunAssembly(
                    id=self._assembly_id(statement),
                    provenance=Provenance(
                        statement.evidence, self.transform.id, AssertionKind.STATED
                    ),
                    run=statement.run,
                    rule="rosbag2.metadata",
                    members=tuple(held[key] for key in sorted(held)),
                    validity=NotApplicable(),
                )
            )

    def _assembly_id(self, stated: FileList) -> RecordId:
        return evidence_record_id(RunAssembly.kind, stated.evidence, self.transform)

    def _file_list(
        self,
        metadata: bytes,
        stated: FileList,
        holders: Mapping[bytes, list[int]],
        by_directory: Mapping[bytes, list[bytes]],
    ) -> dict[RecordId, RunMember]:
        """One copy of a bag: its findings, its readings, and its members of the run."""
        directory = parent(metadata)
        listed: dict[bytes, EvidenceRef] = {}
        for text, evidence in stated.entries:
            path = _listed_path(directory, text)
            if path is not None:
                listed.setdefault(path, evidence)
        present = sorted(path for path in listed if path in self.files)
        missing = sorted(path for path in listed if path not in self.files)
        storage = sorted(
            path
            for path in by_directory.get(directory, ())
            if self._sig(path).extension in BAG_STORAGE
        )
        unlisted = [path for path in storage if path not in listed]
        assembly = self._assembly_id(stated)
        revision = self.files[metadata].revision
        members = {revision: RunMember(revision, MemberRole.DESCRIPTION, stated.run_evidence)}
        for path in present:
            member = self.files[path].revision
            members.setdefault(member, RunMember(member, MemberRole.RECORDING, listed[path]))
        if missing:
            self.findings.append(
                ingest_finding(
                    code=LISTED_PART_MISSING,
                    category=FindingCategory.MISSING,
                    severity=Severity.WARNING,
                    subject=local_location(metadata),
                    transform=self.transform,
                    message=f"the bag lists {len(missing)} storage file(s) this scan did not see;"
                    " its run is assembled from the files present",
                    details={"count": len(missing), "missing": _locations(missing)},
                    related=[listed[path] for path in missing[:_LISTED]],
                    records=[stated.run],
                )
            )
        if unlisted:
            self.findings.append(
                ingest_finding(
                    code=UNLISTED_PART,
                    category=FindingCategory.INCONSISTENT,
                    severity=Severity.WARNING,
                    subject=local_location(unlisted[0]),
                    transform=self.transform,
                    message=f"{len(unlisted)} storage file(s) beside the bag's metadata are not in"
                    " its file list; each is its own recording, not part of the bag",
                    details={"count": len(unlisted), "unlisted": _locations(unlisted)},
                    related=[stated.evidence],
                    records=[stated.run],
                )
            )
        details: JsonObject = {
            "listed": len(listed),
            "missing": len(missing),
            "present": len(present),
            "run": stated.run,
            "run_assembly": assembly,
            "unlisted": len(unlisted),
        }
        reason = Reason(
            Rule.ROSBAG2_FILE_LIST,
            "the bag's metadata.yaml lists its storage files (stated); the canonical run"
            " assembly cites each",
            details,
        )
        for index in sorted(set(holders.get(metadata, ()))):
            draft = self.drafts[index]
            draft.reasons.append(reason)
            if draft.rule is not Rule.ROSBAG2_DIRECTORY:
                continue  # a wider reading keeps every file its directory holds
            draft.rule = Rule.ROSBAG2_FILE_LIST
            for path in [metadata, *present]:
                if path in draft.members:
                    draft.members[path] = (Role.RECORDING, Rule.ROSBAG2_FILE_LIST)
            for path in unlisted:
                if draft.members.pop(path, None) is None:
                    continue
                alone = self._draft(Rule.RECORDING_FILE, parent(path))
                self.drafts[alone].add(path, Role.RECORDING, Rule.RECORDING_FILE)
                self.drafts[alone].reasons.append(
                    Reason(
                        Rule.RECORDING_FILE,
                        "a storage file the bag beside it does not list: its own recording",
                        {"run_assembly": assembly},
                    )
                )
        return members

    # --- 2 and 3. edges, and splits by machine -------------------------------------------------

    def _recordings(self, index: int) -> list[bytes]:
        extent = self._extent(index)
        return sorted(
            path
            for path in extent
            if self._role(path) is Role.RECORDING or basename(path) == BAG_METADATA
        )

    def _edges(self) -> None:
        self._extents.clear()
        index = 0
        while index < len(self.drafts):  # splits are appended, and scored in turn
            draft = self.drafts[index]
            if draft.rule is not Rule.DECLARED:
                self._score(index)
            index += 1

    def _score(self, index: int) -> None:
        draft = self.drafts[index]
        recordings = [p for p in self._recordings(index) if self._of(p) is not None]
        edges: list[Edge] = []
        mixed = self._machines(index, recordings, edges)
        self._times(recordings, edges, draft.reasons)
        if not mixed:
            self._software(recordings, edges, draft.reasons)
        if edges or draft.rule in (Rule.ROSBAG2_FILE_LIST, Rule.MACHINE_SPLIT):
            draft.confidence = score(CONFIDENCE[draft.rule], edges)

    def _machines(self, index: int, recordings: list[bytes], edges: list[Edge]) -> bool:
        draft = self.drafts[index]
        by_namespace: dict[str, dict[str, list[bytes]]] = defaultdict(lambda: defaultdict(list))
        for path in recordings:
            found = self._of(path)
            assert found is not None
            for machine in found.machines:
                by_namespace[machine.namespace][machine.value].append(path)
        mixed = sorted(ns for ns, values in by_namespace.items() if len(values) > 1)
        shared = sorted(
            ns
            for ns, values in by_namespace.items()
            if len(values) == 1 and len(next(iter(values.values()))) > 1
        )
        if mixed:
            namespace = mixed[0]
            values = by_namespace[namespace]
            edges.append(Edge.MIXED_MACHINES)
            details: JsonObject = {
                "machines": {value: _locations(paths) for value, paths in sorted(values.items())},
                "namespace": namespace,
                "weight": float(WEIGHTS[Edge.MIXED_MACHINES]),
            }
            draft.reasons.append(
                Reason(
                    Edge.MIXED_MACHINES,
                    "its recordings declare different machines; a split per machine is"
                    " offered beside it",
                    details,
                )
            )
            if draft.rule is not Rule.MACHINE_SPLIT:
                self._split(index, namespace, values)
        elif shared:
            namespace = shared[0]
            ((value, paths),) = by_namespace[namespace].items()
            edges.append(Edge.SAME_MACHINE)
            draft.reasons.append(
                Reason(
                    Edge.SAME_MACHINE,
                    "its recordings declare the same machine",
                    {
                        "files": _locations(paths),
                        "machine": {"namespace": namespace, "value": value},
                        "weight": float(WEIGHTS[Edge.SAME_MACHINE]),
                    },
                )
            )
        return bool(mixed)

    def _split(self, index: int, namespace: str, values: Mapping[str, list[bytes]]) -> None:
        draft = self.drafts[index]
        for value, paths in sorted(values.items()):
            split = self._draft(Rule.MACHINE_SPLIT, draft.directory)
            for path in paths:
                self.drafts[split].add(path, Role.RECORDING, Rule.MACHINE_SPLIT)
            self.drafts[split].reasons.append(
                Reason(
                    Rule.MACHINE_SPLIT,
                    "the recordings of one machine, split from a reading that mixes machines",
                    {
                        "from_rule": str(draft.rule),
                        "machine": {"namespace": namespace, "value": value},
                    },
                )
            )
        subject = min(path for paths in values.values() for path in paths)
        self.findings.append(
            ingest_finding(
                code=MIXED_MACHINES_FINDING,
                category=FindingCategory.AMBIGUOUS,
                severity=Severity.WARNING,
                subject=local_location(subject),
                transform=self.transform,
                message=f"a {draft.rule} reading holds recordings of {len(values)} machines:"
                " one multi-machine session, or one per machine; both are offered and neither"
                " is chosen",
                details={
                    "directory": (
                        {"kind": "root"}
                        if draft.directory == ROOT
                        else _location_json(draft.directory)
                    ),
                    "machines": sorted(values),
                    "namespace": namespace,
                    "rule": str(draft.rule),
                },
            )
        )

    def _times(self, recordings: list[bytes], edges: list[Edge], reasons: list[Reason]) -> None:
        by_family: dict[tuple[str, str], list[tuple[Fraction, Fraction, bytes]]] = defaultdict(list)
        for path in recordings:
            found = self._of(path)
            assert found is not None
            for interval in found.intervals:
                by_family[interval.family].append((interval.start, interval.end, path))
        gap = Fraction(self.config.gap_seconds)
        for family, spans in sorted(by_family.items()):
            if len({path for _, _, path in spans}) < 2:
                continue
            spans.sort()
            chains = 1
            reach = spans[0][1]
            for start, end, _ in spans[1:]:
                if start > reach + gap:
                    chains += 1
                reach = max(reach, end)
            edge = Edge.TIMES_OVERLAP if chains == 1 else Edge.TIMES_APART
            edges.append(edge)
            reasons.append(
                Reason(
                    edge,
                    "its recordings' runs overlap on one clock (within gap_seconds)"
                    if chains == 1
                    else "its recordings' runs fall apart on one clock, beyond gap_seconds",
                    {
                        "clock": {"epoch": family[0], "timescale": family[1]},
                        "files": _locations({path for _, _, path in spans}),
                        "gap_seconds": self.config.gap_seconds,
                        "spans": chains,
                        "weight": float(WEIGHTS[edge]),
                    },
                )
            )

    def _software(self, recordings: list[bytes], edges: list[Edge], reasons: list[Reason]) -> None:
        versions: dict[str, dict[str, set[bytes]]] = defaultdict(lambda: defaultdict(set))
        for path in recordings:
            found = self._of(path)
            assert found is not None
            for name, version in found.software:
                versions[name][version].add(path)
        differs = sorted(name for name, held in versions.items() if len(held) > 1)
        same = sorted(
            name
            for name, held in versions.items()
            if len(held) == 1 and len(next(iter(held.values()))) > 1
        )
        if differs:
            edges.append(Edge.SOFTWARE_DIFFERS)
            reasons.append(
                Reason(
                    Edge.SOFTWARE_DIFFERS,
                    "its recordings declare different versions of one piece of software",
                    {
                        "software": {
                            name: sorted(versions[name])[:_LISTED] for name in differs[:_LISTED]
                        },
                        "weight": float(WEIGHTS[Edge.SOFTWARE_DIFFERS]),
                    },
                )
            )
        elif same:
            edges.append(Edge.SAME_SOFTWARE)
            reasons.append(
                Reason(
                    Edge.SAME_SOFTWARE,
                    "its recordings declare the same software versions",
                    {
                        "software": {name: next(iter(versions[name])) for name in same[:_LISTED]},
                        "weight": float(WEIGHTS[Edge.SAME_SOFTWARE]),
                    },
                )
            )

    # --- 4. merges by machine and time ---------------------------------------------------------

    _UNITS: Final = frozenset(
        {Rule.RECORDING_FILE, Rule.ROSBAG2_DIRECTORY, Rule.ROSBAG2_FILE_LIST, Rule.SPLIT_SEQUENCE}
    )

    def _merges(self) -> None:
        """Loose recordings of one directory that declare one machine and overlap on one clock."""
        groups: dict[tuple[bytes, str, str, tuple[str, str]], list[tuple[Fraction, Fraction, int]]]
        groups = defaultdict(list)
        for index, draft in enumerate(self.drafts):
            if draft.rule not in self._UNITS:
                continue
            bag = draft.rule in (Rule.ROSBAG2_DIRECTORY, Rule.ROSBAG2_FILE_LIST)
            position = (
                parent(draft.directory) if bag and draft.directory != ROOT else draft.directory
            )
            machines: set[LogicalId] = set()
            intervals: list[Interval] = []
            for path in draft.members:
                found = self._of(path)
                if found is not None:
                    machines.update(found.machines)
                    intervals.extend(found.intervals)
            for machine in machines:
                for interval in intervals:
                    key = (position, machine.namespace, machine.value, interval.family)
                    groups[key].append((interval.start, interval.end, index))
        gap = Fraction(self.config.gap_seconds)
        made: set[tuple[int, ...]] = set()
        for (position, namespace, value, family), spans in sorted(groups.items()):
            spans.sort()
            chain: list[int] = []
            reach: Fraction | None = None
            for start, end, index in [*spans, (Fraction(0), Fraction(0), -1)]:
                if index >= 0 and reach is not None and start <= reach + gap:
                    chain.append(index)
                    reach = max(reach, end)
                    continue
                units = tuple(sorted(set(chain)))
                if len(units) > 1 and units not in made and not self._together(units):
                    made.add(units)
                    self._merge(position, units, namespace, value, family)
                chain, reach = [index], end
        return

    def _together(self, units: Sequence[int]) -> bool:
        """Whether some reading already holds all of these units' recordings."""
        wanted = {path for unit in units for path in self.drafts[unit].members}
        return any(
            index not in units and wanted <= draft.members.keys()
            for index, draft in enumerate(self.drafts)
        )

    def _merge(
        self,
        position: bytes,
        units: Sequence[int],
        namespace: str,
        value: str,
        family: tuple[str, str],
    ) -> None:
        merged = self._draft(Rule.MACHINE_TIME_MERGE, position)
        draft = self.drafts[merged]
        for unit in units:
            for path, (role, _) in self.drafts[unit].members.items():
                draft.add(path, role, Rule.MACHINE_TIME_MERGE)
        draft.reasons.append(
            Reason(
                Rule.MACHINE_TIME_MERGE,
                "loose recordings of one machine whose runs overlap on one clock: one session,"
                " offered beside each recording's own",
                {
                    "clock": {"epoch": family[0], "timescale": family[1]},
                    "machine": {"namespace": namespace, "value": value},
                    "recordings": len(units),
                },
            )
        )

    # --- 5. documents that name a session ------------------------------------------------------

    def _mentions(self) -> None:
        names: dict[str, set[int]] = defaultdict(set)
        for index, draft in enumerate(self.drafts):
            for path, (role, _) in draft.members.items():
                if role is not Role.RECORDING or basename(path) == BAG_METADATA:
                    continue
                for key in (basename(path), self._sig(path).stem):
                    if (text := _text(key)) is not None and len(text) >= MIN_NAME:
                        names[text].add(index)
            if (
                draft.rule
                in (
                    Rule.SESSION_DIRECTORY,
                    Rule.ROSBAG2_DIRECTORY,
                    Rule.ROSBAG2_FILE_LIST,
                )
                and draft.directory != ROOT
            ):
                text = _text(basename(draft.directory))
                if text is not None and len(text) >= MIN_NAME:
                    names[text].add(index)
        if not names:
            return
        for path, (reason, _) in sorted(self.unassigned.items()):
            if reason in (SHARED_REFERENCE, TOO_MANY_SESSIONS):
                continue
            found = self._of(path)
            if found is None or not found.words:
                continue
            hits = sorted(word for word in found.words if word in names)
            if not hits:
                continue
            holders = sorted({index for word in hits for index in names[word]})
            if len(holders) == 1 or self._overlap(holders):
                del self.unassigned[path]
                for index in holders:
                    self.drafts[index].add(path, Role.CONTEXT, Rule.NAMED_IN_DOCUMENT)
                    self.drafts[index].reasons.append(
                        Reason(
                            Rule.NAMED_IN_DOCUMENT,
                            "a document whose text names this session's files",
                            {"document": _location_json(path), "names": hits[:_LISTED]},
                        )
                    )
            else:
                self._ambiguous(path, SEVERAL_NAMED, holders)

    # --- 6. shared configuration ---------------------------------------------------------------

    def _shared(self) -> None:
        below: dict[bytes, list[int]] = defaultdict(list)
        for index, draft in enumerate(self.drafts):
            directory = draft.directory
            while True:
                below[directory].append(index)
                if directory == ROOT:
                    break
                directory = parent(directory)
        for path, (reason, candidates) in sorted(self.unassigned.items()):
            if reason != NO_SESSION or candidates:
                continue
            found = self._of(path)
            if found is None or not found.configuration:
                continue
            sessions = sorted(set(below.get(parent(path), ())))
            if len(sessions) < 2:
                continue
            self._ambiguous(path, SHARED_REFERENCE, sessions)
            if len(sessions) > _LISTED:
                continue  # too many to name: unknown, as v0 says
            for index in sessions:
                self.drafts[index].reasons.append(
                    Reason(
                        SHARED_REFERENCE,
                        "a configuration above this session, shared with the other sessions"
                        " below it and held by none",
                        {"location": _location_json(path), "sessions": len(sessions)},
                    )
                )
