"""What a manifest declares, as canonical records citing the manifest's bytes (ADR 0072).

A manifest is the user's statement about the folder it sits in, so everything here is ``stated``
and cites the manifest by JSON pointer under the manifest transform. Nothing here reads a source
byte: it reads the manifest, the scan's layout and the records the adapters committed.

- **Entities.** Each ``machines`` entry is a ``Machine`` and each ``sites`` entry a ``Site``,
  identified as ``("manifest", <id>)`` plus the ``aliases`` written beside it, each identifier
  citing where it is written. Ids that share a declaration are what identity resolution links
  by; nothing here merges two records, or a manifest id with an id the evidence uses.
- **Run declarations.** A declared run covers the files its ``paths`` name; every ``Run`` record
  the bytes of a covered file declare gets one ``RunDeclaration`` naming the declared machine, site
  and task. Its evidence is the run's entry, made finer by a ``neptune.manifest:run`` step naming
  the run record, so one entry covering several recordings is one record per recording.
- **Snapshot pins.** Each ``snapshots`` pin (a path, or a content id) names the bytes a run ran
  with; every snapshot record of those bytes (configuration, software, hardware, calibration) is
  bound to every run the entry covers by a canonical ``SnapshotBinding``, citing the pin with a
  ``neptune.manifest:binding`` step naming the run and the snapshot, once per run and snapshot
  however many pins name it. Validity stays ``Unknown``: the manifest says which, not when. A pin
  is resolved even when its entry covers no run, so a wrong pin is always a finding.
- **Lineage.** Machines and sites read the manifest alone, so they are the manifest transform's.
  Run declarations and pins also read the adapters' runs and snapshots, so they (and the findings
  about applying them) are a transform with the same id, version and config naming those
  adapters' transforms as its ``upstream``, sorted (ADR 0016 §4): an adapter upgrade is a new
  lineage for them, as for any transform over adapter output.

What cannot be applied is a finding, citing the declaration (of the run declarations' transform,
but for alias findings, which are the manifest transform's):

- ``run_unrecorded`` (missing, warning): a run's paths hold files, but none declares a run, so
  what the entry declares is said of no run record (paths that hold no file at all are the
  grouping's ``declaration_unmatched``).
- ``run_declared_twice`` (ambiguous, warning): two entries cover one run record. Both
  declarations stand; consumers see both, and neither is chosen.
- ``machine_contradicts_run`` (inconsistent, warning): the run record states its machine in a
  namespace the declared machine has aliases in, and none of them is that id. Both stand.
- ``pin_unresolved`` (missing, warning): no file the job read is at the pinned path (a missing
  file, a directory, a symlink the walk does not follow), or holds the pinned content.
- ``pin_not_a_snapshot`` (missing, warning): the pinned bytes hold no snapshot record (not a
  configuration file, or one its adapter could not read: that adapter's own finding says why).
- ``pin_repeated`` (skipped, info): an earlier pin, of this entry or of another covering the same
  run, already binds the run to this snapshot, so this pin adds no second binding of the pair; the
  binding stands, citing the first pin, and this says which.
- ``alias_namespace_unrepresentable`` (unrepresentable, warning): an alias namespace version 1
  accepts (an id: capitals, ``:``) is not a record namespace (a lowercase letter, then lowercase
  letters, digits and ``. _ -``), so the alias is not among the entity's identifiers. It stays in
  the manifest transform's config; nothing renames it.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeAlias

from neptune.discovery.layout import Layout
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.manifest.load import MANIFEST_ID, MANIFEST_VERSION, LoadedManifest
from neptune.manifest.schema import Entity, RunDecl, SnapshotPin
from neptune.model.alignment import SnapshotBinding, SnapshotKind
from neptune.model.configuration import ConfigurationSnapshot
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, LogicalId, RecordId, check_token
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotCovered, Unknown
from neptune.model.machine import (
    Calibration,
    HardwareConfiguration,
    Machine,
    SoftwareConfiguration,
)
from neptune.model.provenance import EvidenceRef, Provenance, TransformRecord, adapter_locator
from neptune.model.run import Run, RunDeclaration
from neptune.model.world import Site

# The namespace of every id a manifest declares: ``("manifest", "ARM-3A")``.
NAMESPACE: Final = "manifest"

RUN_UNRECORDED: Final = f"{MANIFEST_ID}.run_unrecorded"
RUN_DECLARED_TWICE: Final = f"{MANIFEST_ID}.run_declared_twice"
MACHINE_CONTRADICTS_RUN: Final = f"{MANIFEST_ID}.machine_contradicts_run"
PIN_UNRESOLVED: Final = f"{MANIFEST_ID}.pin_unresolved"
PIN_NOT_A_SNAPSHOT: Final = f"{MANIFEST_ID}.pin_not_a_snapshot"
PIN_REPEATED: Final = f"{MANIFEST_ID}.pin_repeated"
ALIAS_NAMESPACE_UNREPRESENTABLE: Final = f"{MANIFEST_ID}.alias_namespace_unrepresentable"
FINDING_CODES: Final = (
    ALIAS_NAMESPACE_UNREPRESENTABLE,
    MACHINE_CONTRADICTS_RUN,
    PIN_NOT_A_SNAPSHOT,
    PIN_REPEATED,
    PIN_UNRESOLVED,
    RUN_DECLARED_TWICE,
    RUN_UNRECORDED,
)

# The adapter steps that make one declaration's citation finer, per record it gives (ADR 0072 §3).
RUN_STEP: Final = f"{MANIFEST_ID}:run"
BINDING_STEP: Final = f"{MANIFEST_ID}:binding"

Snapshot: TypeAlias = (
    ConfigurationSnapshot | SoftwareConfiguration | HardwareConfiguration | Calibration
)
SNAPSHOT_KINDS: Final[Mapping[type, SnapshotKind]] = {
    ConfigurationSnapshot: SnapshotKind.CONFIGURATION_SNAPSHOT,
    SoftwareConfiguration: SnapshotKind.SOFTWARE_CONFIGURATION,
    HardwareConfiguration: SnapshotKind.HARDWARE_CONFIGURATION,
    Calibration: SnapshotKind.CALIBRATION,
}
# Run records a finding lists; the rest are counted.
_LISTED: Final = 64


@dataclass(frozen=True)
class ManifestRecords:
    """What one manifest gives a package: canonical records and the findings of what it could not
    apply. ``records`` are sorted by kind, then id; ``findings`` by id. ``transforms`` are the
    producers they name, by id: the manifest transform, and the run declarations' transform when
    it has upstream transforms."""

    records: tuple[Machine | Site | RunDeclaration | SnapshotBinding, ...]
    findings: tuple[IngestFinding, ...]
    transforms: tuple[TransformRecord, ...]

    @property
    def bindings(self) -> tuple[SnapshotBinding, ...]:
        return tuple(r for r in self.records if isinstance(r, SnapshotBinding))

    def summary(self) -> JsonObject:
        counts: dict[str, int] = defaultdict(int)
        for record in self.records:
            counts[record.kind] += 1
        return {"findings": len(self.findings), "records": dict(sorted(counts.items()))}


def _namespace_ok(namespace: str) -> bool:
    try:
        check_token("namespace", namespace)
    except ValueError:
        return False
    return True


class _Builder:
    def __init__(self, loaded: LoadedManifest, upstream: Iterable[RecordId]) -> None:
        self.loaded = loaded
        # Machines and sites read the manifest alone; run declarations and pins also read the
        # adapters' runs and snapshots, so their transform names those adapters (ADR 0016 §4).
        self.transform = loaded.transform
        self.declarations = transform_record(
            adapter_id=MANIFEST_ID,
            adapter_version=MANIFEST_VERSION,
            config=loaded.transform.config,
            upstream=sorted(set(upstream)),
        )
        self.records: dict[RecordId, Machine | Site | RunDeclaration | SnapshotBinding] = {}
        self.findings: dict[RecordId, IngestFinding] = {}
        # (run, snapshot) -> the pointer of the pin that bound it, across every entry.
        self.pinned: dict[tuple[RecordId, RecordId], str] = {}

    def cite(self, pointer: str, *steps: tuple[str, Mapping[str, str]]) -> EvidenceRef:
        cited = self.loaded.cite(pointer)
        finer = tuple(adapter_locator(kind, fields) for kind, fields in steps)
        return EvidenceRef(cited.source, (*cited.locator, *finer))

    def stated(self, evidence: EvidenceRef, transform: TransformRecord | None = None) -> Provenance:
        return Provenance(evidence, (transform or self.transform).id, AssertionKind.STATED)

    def declared(self, value: str, pointer: str) -> Known[LogicalId]:
        return Known(
            LogicalId(NAMESPACE, value), self.stated(self.cite(pointer), self.declarations)
        )

    def add(self, record: Machine | Site | RunDeclaration | SnapshotBinding) -> None:
        self.records[record.id] = record

    def finding(
        self,
        code: str,
        category: FindingCategory,
        subject: EvidenceRef,
        message: str,
        details: Mapping[str, JsonValue],
        *,
        related: Sequence[EvidenceRef] = (),
        records: Iterable[RecordId] = (),
        severity: Severity = Severity.WARNING,
        transform: TransformRecord | None = None,
    ) -> None:
        found = ingest_finding(
            code=code,
            category=category,
            severity=severity,
            subject=subject,
            transform=transform or self.declarations,
            message=message,
            details=details,
            related=related,
            records=records,
        )
        self.findings[found.id] = found

    # --- entities -------------------------------------------------------------------------------

    def identifiers(self, entity: Entity) -> tuple[Knowledge[LogicalId], ...]:
        """The entity's manifest id and every alias, each citing where it is written, sorted. An
        alias whose namespace is no record namespace is left out, and a finding says so."""
        found = [(LogicalId(NAMESPACE, entity.id), f"{entity.pointer}/id")]
        for (namespace, value), at in zip(entity.aliases, entity.alias_pointers, strict=True):
            if _namespace_ok(namespace):
                found.append((LogicalId(namespace, value), at))
                continue
            self.finding(
                ALIAS_NAMESPACE_UNREPRESENTABLE,
                FindingCategory.UNREPRESENTABLE,
                self.cite(at),
                f"the manifest's {entity.section[:-1]} {entity.id!r} has an alias in namespace "
                f"{namespace!r}, which is no record namespace (a lowercase letter, then lowercase "
                "letters, digits and . _ -), so it is not among its identifiers; it stays in the "
                "manifest transform's config. Write the namespace in that form to make it one",
                {"manifest_pointer": at, "namespace": namespace, "value": value},
                transform=self.transform,
            )
        unique: dict[LogicalId, str] = {}
        for ident, at in found:  # an alias equal to the manifest id cites the id
            unique.setdefault(ident, at)
        return tuple(
            Known(ident, self.stated(self.cite(at)))
            for ident, at in sorted(
                unique.items(), key=lambda item: (item[0].namespace, item[0].value)
            )
        )

    def machine(self, entity: Entity) -> None:
        evidence = self.cite(entity.pointer)
        self.add(
            Machine(
                id=evidence_record_id(Machine.kind, evidence, self.transform),
                provenance=self.stated(evidence),
                identifiers=self.identifiers(entity),
                manufacturer=NotCovered(),  # a manifest has no place for either
                model=NotCovered(),
            )
        )

    def site(self, entity: Entity) -> None:
        evidence = self.cite(entity.pointer)
        name: Knowledge[str] = (
            Known(entity.name, self.stated(self.cite(f"{entity.pointer}/name")))
            if entity.name is not None
            else Unknown()
        )
        self.add(
            Site(
                id=evidence_record_id(Site.kind, evidence, self.transform),
                provenance=self.stated(evidence),
                identifiers=self.identifiers(entity),
                name=name,
                aliases=(),
                parent=NotCovered(),
                location=NotCovered(),
            )
        )

    # --- runs -----------------------------------------------------------------------------------

    def declaration(self, decl: RunDecl, run: Run) -> None:
        evidence = self.cite(decl.pointer, (RUN_STEP, {"run": run.id}))
        transform = self.declarations

        def field(name: str) -> Knowledge[LogicalId]:
            value = getattr(decl, name)
            return (
                self.declared(value, f"{decl.pointer}/{name}") if value is not None else Unknown()
            )

        self.add(
            RunDeclaration(
                id=evidence_record_id(RunDeclaration.kind, evidence, transform),
                provenance=self.stated(evidence, transform),
                run=run.id,
                logical_id=self.declared(decl.name, f"{decl.pointer}/name"),
                machine=field("machine"),
                site=field("site"),
                task=field("task"),
            )
        )

    def contradiction(self, decl: RunDecl, run: Run, machine: Entity) -> None:
        """The run states its machine in a namespace the declared machine has ids in, and none of
        them is the run's: both stand, and this says so."""
        if not isinstance(run.machine, Known):
            return
        own = run.machine.value
        declared = {
            LogicalId(NAMESPACE, machine.id),
            *(LogicalId(*alias) for alias in machine.aliases if _namespace_ok(alias[0])),
        }
        if own in declared or own.namespace not in {ident.namespace for ident in declared}:
            return
        self.finding(
            MACHINE_CONTRADICTS_RUN,
            FindingCategory.INCONSISTENT,
            self.cite(f"{decl.pointer}/machine"),
            f"the manifest declares run {decl.name!r} as machine {machine.id!r}, whose "
            f"{own.namespace} ids do not include the {own.value!r} the run states; both stand",
            {
                "declared": machine.id,
                "manifest_pointer": f"{decl.pointer}/machine",
                "run": run.id,
                "stated": own.to_json(),
            },
            related=(self.cite(machine.pointer), run.provenance.evidence),
            records=(run.id,),
        )

    def pin(
        self,
        decl: RunDecl,
        pin: SnapshotPin,
        runs: Sequence[Run],
        files: Mapping[bytes, ContentId],
        links: frozenset[bytes],
        contents: frozenset[ContentId],
        snapshots: Mapping[ContentId, Sequence[tuple[Snapshot, SnapshotKind]]],
    ) -> None:
        subject = self.cite(pin.pointer)
        details: dict[str, JsonValue] = {"manifest_pointer": pin.pointer, "run": decl.name}
        details.update(pin.to_json())
        content: ContentId | None
        if pin.path is not None:
            raw = pin.path.encode("utf-8")
            content = files.get(raw)
            if content is None:
                below = any(path.startswith(raw + b"/") for path in files)
                why = (
                    "a symlink, which the walk does not follow"
                    if raw in links
                    else "a directory; pin one file"
                    if below
                    else "not a file the job read"
                )
                self.finding(
                    PIN_UNRESOLVED,
                    FindingCategory.MISSING,
                    subject,
                    f"the manifest pins {pin.path!r} for run {decl.name!r}: it is {why}",
                    details,
                )
                return
        else:
            assert pin.content is not None
            content = ContentId(pin.content)
            if content not in contents and content not in snapshots:
                self.finding(
                    PIN_UNRESOLVED,
                    FindingCategory.MISSING,
                    subject,
                    f"the manifest pins content {pin.content} for run {decl.name!r}: no file "
                    "the job read holds those bytes",
                    details,
                )
                return
        found = snapshots.get(content, ())
        if not found:
            self.finding(
                PIN_NOT_A_SNAPSHOT,
                FindingCategory.MISSING,
                subject,
                f"the manifest pins {pin.path or pin.content!r} for run {decl.name!r}, but no "
                "configuration, software, hardware or calibration record was read from it",
                {**details, "content": content},
            )
            return
        repeated: dict[tuple[RecordId, RecordId], str] = {}  # (run, snapshot) -> the first pin
        for run in runs:
            for snapshot, kind in found:
                first = self.pinned.get((run.id, snapshot.id))
                if first is not None:  # pinned again: bound once, by the first pin, and said
                    repeated[(run.id, snapshot.id)] = first
                    continue
                self.pinned[(run.id, snapshot.id)] = pin.pointer
                evidence = self.cite(
                    pin.pointer, (BINDING_STEP, {"run": run.id, "snapshot": snapshot.id})
                )
                self.add(
                    SnapshotBinding(
                        id=evidence_record_id(SnapshotBinding.kind, evidence, self.declarations),
                        provenance=self.stated(evidence, self.declarations),
                        run=run.id,
                        snapshot=snapshot.id,
                        snapshot_kind=kind,
                        validity=Unknown(),
                    )
                )
        if repeated:
            self.repeated(decl, pin, subject, details, repeated)

    def repeated(
        self,
        decl: RunDecl,
        pin: SnapshotPin,
        subject: EvidenceRef,
        details: Mapping[str, JsonValue],
        pairs: Mapping[tuple[RecordId, RecordId], str],
    ) -> None:
        """``pin`` names snapshots an earlier pin already bound to these runs: no second binding of
        a pair, and this finding instead of silence (the binding cites the first pin)."""
        first = sorted(set(pairs.values()))
        runs = sorted({run for run, _ in pairs})
        self.finding(
            PIN_REPEATED,
            FindingCategory.SKIPPED,
            subject,
            f"the manifest pins {pin.path or pin.content!r} for run {decl.name!r}, but "
            f"{' and '.join(first)} already bind(s) the same {len(pairs)} run and snapshot "
            "pair(s): each is bound once, citing the first pin",
            {
                **details,
                "bound_by": list(first),
                "pairs": len(pairs),
                "runs": list(runs[:_LISTED]),
            },
            related=tuple(self.cite(pointer) for pointer in first),
            records=runs[:_LISTED],
            severity=Severity.INFO,
        )


def _covers(paths: Sequence[bytes], path: bytes) -> bool:
    return any(path == own or path.startswith(own + b"/") for own in paths)


def declared_records(
    loaded: LoadedManifest, records: Iterable[object], layout: Layout
) -> ManifestRecords:
    """The manifest's declarations as canonical records, over the package's committed ``records``
    (only runs and snapshots are read) and the scan's ``layout``. The same inputs give the same
    result in any order."""
    manifest = loaded.manifest
    runs_of: dict[ContentId, list[Run]] = defaultdict(list)
    snapshots: dict[ContentId, list[tuple[Snapshot, SnapshotKind]]] = defaultdict(list)
    upstream: set[RecordId] = set()  # the transforms of every run and snapshot read
    for record in records:
        if isinstance(record, Run):
            upstream.add(record.provenance.transform)
            source = record.provenance.evidence.source
            if isinstance(source, str):
                runs_of[ContentId(source)].append(record)
        elif (kind := SNAPSHOT_KINDS.get(type(record))) is not None:
            upstream.add(record.provenance.transform)  # type: ignore[attr-defined]
            source = record.provenance.evidence.source  # type: ignore[attr-defined]
            if isinstance(source, str):
                snapshots[ContentId(source)].append((record, kind))  # type: ignore[arg-type]
    builder = _Builder(loaded, upstream)
    for entity in manifest.section("machines"):
        builder.machine(entity)
    for entity in manifest.section("sites"):
        builder.site(entity)
    for found in snapshots.values():
        found.sort(key=lambda entry: entry[0].id)
    files = {file.path: file.content_id for file in layout.files}
    links = frozenset(link.path for link in layout.links)
    contents = frozenset(files.values())
    machines = {entity.id: entity for entity in manifest.section("machines")}
    declared_by: dict[RecordId, list[RunDecl]] = defaultdict(list)
    for decl in manifest.runs:
        own = [declared.encode("utf-8") for declared in decl.paths]
        held = [content for path, content in files.items() if _covers(own, path)]
        covered = set(held)
        runs = sorted(
            {run.id: run for c in covered for run in runs_of.get(c, ())}.values(),
            key=lambda run: run.id,
        )
        if covered and not runs:
            builder.finding(
                RUN_UNRECORDED,
                FindingCategory.MISSING,
                builder.cite(decl.pointer),
                f"the manifest's run {decl.name!r} holds {len(held)} file(s), none of which "
                "declares a run: what it declares is said of no run record",
                {
                    "files": len(held),
                    "manifest_pointer": decl.pointer,
                    "pins": len(decl.snapshots),
                    "run": decl.name,
                },
            )
        for run in runs:
            builder.declaration(decl, run)
            declared_by[run.id].append(decl)
            if decl.machine is not None:
                builder.contradiction(decl, run, machines[decl.machine])
        for pin in decl.snapshots:  # resolved even with no run to bind: a bad pin is said
            builder.pin(decl, pin, runs, files, links, contents, snapshots)
    twice: dict[tuple[str, ...], list[RecordId]] = defaultdict(list)
    for run_id, decls in sorted(declared_by.items()):
        if len(decls) > 1:
            twice[tuple(d.pointer for d in decls)].append(run_id)
    for pointers, run_ids in sorted(twice.items()):
        names = [d.name for d in manifest.runs if d.pointer in pointers]
        builder.finding(
            RUN_DECLARED_TWICE,
            FindingCategory.AMBIGUOUS,
            builder.cite(pointers[0]),
            f"the manifest's runs {names} cover the same {len(run_ids)} run record(s): every "
            "declaration stands, and none is chosen",
            {
                "manifest_pointers": list(pointers),
                "run_count": len(run_ids),
                "runs": list(run_ids[:_LISTED]),
            },
            related=tuple(builder.cite(p) for p in pointers[1:]),
            records=run_ids[:_LISTED],
        )
    return ManifestRecords(
        records=tuple(
            builder.records[key]
            for key in sorted(builder.records, key=lambda k: (builder.records[k].kind, k))
        ),
        findings=tuple(builder.findings[key] for key in sorted(builder.findings)),
        transforms=tuple(
            sorted(
                {t.id: t for t in (builder.transform, builder.declarations)}.values(),
                key=lambda t: t.id,
            )
        ),
    )
