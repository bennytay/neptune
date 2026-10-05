"""The rules: what each checks over a package's records, and the default set (ADR 0054 §3).

Every rule reads records in table order (sorted by id) and groups with sorted keys, so its drafts
come in the same order every run. None of them compares values on different clocks, converts a
unit, or reads a limit the evidence does not declare.
"""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterator
from itertools import islice
from typing import TYPE_CHECKING, Any, Final

from neptune.identity import canonical_json
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.frames import FrameRef
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Ambiguous, Known, Unknown
from neptune.model.provenance import EvidenceRef
from neptune.model.references import named
from neptune.model.versions import version_to_json
from neptune.store.spill import SpillSpace
from neptune.validate import pending
from neptune.validate.engine import (
    Context,
    Draft,
    Omitted,
    Rule,
    evidence_of,
    plural,
    short,
    source_of,
)
from neptune.validate.series import count_mismatch, time_out_of_order, time_regression

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

# --- truncation and corruption ------------------------------------------------------------------


def source_incomplete(context: Context) -> Iterator[Draft]:
    """Roll up, per source, every finding that says its bytes are corrupt or cut off (``corrupt``;
    a ``limit`` stopped an intact file and is not damage)."""
    damaged: dict[str, list[IngestFinding]] = defaultdict(list)
    for finding in context.findings:
        subject = finding.subject
        damage = finding.category is FindingCategory.CORRUPT
        if damage and isinstance(subject, EvidenceRef) and isinstance(subject.source, str):
            damaged[subject.source].append(finding)
    if not damaged:
        return
    citing: dict[str, list[RecordId]] = defaultdict(list)
    for record in context.package.records:
        source = source_of(record)
        if source in damaged:
            citing[source].append(record.id)
    for source in sorted(damaged):
        if not citing[source]:  # quarantined or never read: its producer's finding says so
            continue
        found = damaged[source]
        subject = context.whole(source) or found[0].subject
        assert isinstance(subject, EvidenceRef)
        codes = Counter(finding.code for finding in found)
        yield Draft(
            subject=subject,
            message=f"source {short(source)} is incomplete:"
            f" {plural(len(found), 'finding')} report cut-off or corrupt bytes;"
            f" {plural(len(citing[source]), 'record')} hold what was read",
            details={
                "codes": dict(sorted(codes.items())),
                "findings": len(found),
                "source": source,
            },
            related=[f.subject for f in found if isinstance(f.subject, EvidenceRef)],
            records=citing[source],
        )


def snapshot_incomplete(context: Context) -> Iterator[Draft]:
    """A configuration snapshot counts more (or fewer) values than the package holds for it."""
    held: Counter[str] = Counter(value.snapshot for value in context.records("configuration_value"))
    for snapshot in context.records("configuration_snapshot"):
        stored = held.get(snapshot.id, 0)
        if stored == snapshot.values:
            continue
        yield Draft(
            subject=evidence_of(snapshot),
            message=f"configuration snapshot {short(snapshot.id)} has"
            f" {plural(snapshot.values, 'value')}; the package holds {stored}",
            details={
                "declared": snapshot.values,
                "missing": max(snapshot.values - stored, 0),
                "snapshot": snapshot.id,
                "stored": stored,
            },
            records=(snapshot.id,),
        )


# --- impossible ranges ---------------------------------------------------------------------------

_INTERVALS: Final = {
    "run": ("first", "last"),
    "stream": ("first", "last"),
    "calibration": ("valid_from", "valid_until"),
}


def interval_reversed(context: Context) -> Iterator[Draft]:
    """A declared interval ends before it starts, both ends on the same clock."""
    for kind, (start_field, end_field) in sorted(_INTERVALS.items()):
        for record in context.records(kind):
            start, end = getattr(record, start_field), getattr(record, end_field)
            if not (isinstance(start, Known) and isinstance(end, Known)):
                continue
            first, last = start.value, end.value
            if first.domain_id != last.domain_id or first.ticks <= last.ticks:
                continue
            yield Draft(
                subject=evidence_of(record, end),
                message=f"{kind} {short(record.id)} declares {end_field} before {start_field}"
                " on the same clock",
                details={
                    "domain": first.domain_id,
                    "end": last.ticks,
                    "end_field": end_field,
                    "kind": kind,
                    "start": first.ticks,
                    "start_field": start_field,
                },
                related=[evidence_of(record, start)],
                records=(record.id,),
            )


# --- missing required metadata -------------------------------------------------------------------

# The fields a consumer cannot use a record without. Only ``Unknown`` is reported: the evidence
# should have said it and did not. NotCovered and NotApplicable say the format does not carry it.
REQUIRED: Final = {
    "frame_transform": ("direction",),
    "stream": ("message_encoding", "schema_name", "topic"),
    "timestamp_domain": ("resolution",),
}


def missing_metadata(context: Context) -> Iterator[Draft]:
    """Records of one source leave a field consumers rely on unknown; one finding per field."""
    groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for kind, fields in sorted(REQUIRED.items()):
        for record in context.records(kind):
            for name in fields:
                if isinstance(getattr(record, name), Unknown):
                    groups[source_of(record) or "", kind, name].append(record)
    for (source, kind, name), records in sorted(groups.items()):
        yield Draft(
            subject=evidence_of(records[0], getattr(records[0], name)),
            message=f"{plural(len(records), kind + ' record')} leave {name} unknown",
            details={"count": len(records), "field": name, "kind": kind, "source": source},
            related=[evidence_of(r, getattr(r, name)) for r in records[1:]],
            records=[r.id for r in records],
        )


# --- schema mismatches ---------------------------------------------------------------------------

_STREAM_SCHEMA: Final = ("message_encoding", "schema_encoding", "schema_name")


def schema_conflict(context: Context) -> Iterator[Draft]:
    """Streams of one run on one topic declare different schemas or encodings."""
    topics: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for stream in context.records("stream"):
        if isinstance(stream.topic, Known):
            topics[stream.run, stream.topic.value].append(stream)
    for (run, topic), streams in sorted(topics.items()):
        if len(streams) < 2:
            continue
        differ: dict[str, JsonValue] = {}
        for name in _STREAM_SCHEMA:
            values = sorted(
                {s_value.value for s in streams if isinstance(s_value := getattr(s, name), Known)}
            )
            if len(values) > 1:
                differ[name] = list(values[: context.bounds.values_per_detail])
        if not differ:
            continue
        yield Draft(
            subject=evidence_of(streams[1]),
            message=f"{plural(len(streams), 'stream')} of run {short(run)} share a topic and"
            f" declare different {', '.join(sorted(differ))}",
            details={"differ": differ, "run": run, "topic": topic},
            related=[evidence_of(s) for s in streams if s is not streams[1]],
            records=[RecordId(run), *(s.id for s in streams)],
        )


def row_shape_mismatch(context: Context) -> Iterator[Draft]:
    """Rows of a table hold a different number of cells than its declared header names."""
    widths = {
        table.id: len(table.header.value)
        for table in context.records("structured_table")
        if isinstance(table.header, Known)
    }
    bad: dict[RecordId, list[Any]] = defaultdict(list)
    for record in context.records("structured_record"):
        width = widths.get(record.table)
        if width is not None and len(record.cells) != width:
            bad[record.table].append(record)
    for table, records in sorted(bad.items()):
        ordered = sorted(records, key=lambda r: (r.row, r.id))
        cells = sorted({len(r.cells) for r in ordered})[: context.bounds.values_per_detail]
        yield Draft(
            subject=evidence_of(ordered[0]),
            message=f"{plural(len(ordered), 'row')} of table {short(table)} do not hold"
            f" the {widths[table]} cells its header names",
            details={
                "cells": list(cells),
                "first_row": ordered[0].row,
                "header": widths[table],
                "rows": len(ordered),
                "table": table,
            },
            related=[evidence_of(r) for r in ordered[1:]],
            records=[table, *(r.id for r in ordered)],
        )


# --- duplicate and conflicting ids --------------------------------------------------------------

# The record kinds that state logical ids, where they state them, and the attributes two records
# naming the same thing must not contradict. Equal ids never merge records (ADR 0003).
_IDENTIFIED: Final = {
    "asset": ("identifiers", ("category", "name")),
    "hardware_component": ("identifiers", ("model",)),
    "machine": ("identifiers", ("manufacturer", "model")),
    "run": ("logical_id", ("machine",)),
    "site": ("identifiers", ("name",)),
}


def _logical_ids(record: Any, where: str) -> set[LogicalId]:
    value = getattr(record, where)
    states = value if isinstance(value, tuple) else (value,)
    return {state.value for state in states if isinstance(state, Known)}


def _value_key(value: Any) -> str:
    if isinstance(value, LogicalId):
        return f"{value.namespace}:{value.value}"
    return str(value)


def _id_groups(context: Context) -> dict[tuple[str, str, str], list[Any]]:
    """Records of the identified kinds by stated logical id, read once for both rules that use
    them (``context.memo``)."""
    found = context.memo.get("id_groups")
    if found is not None:
        return found  # type: ignore[no-any-return]
    groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for kind, (where, _) in sorted(_IDENTIFIED.items()):
        for record in context.records(kind):
            for logical in _logical_ids(record, where):
                groups[kind, logical.namespace, logical.value].append(record)
    context.memo["id_groups"] = groups
    return groups


def duplicate_id(context: Context) -> Iterator[Draft]:
    """One source states the same logical id for two records of one kind."""
    for (kind, namespace, value), records in sorted(_id_groups(context).items()):
        by_source: dict[str, list[Any]] = defaultdict(list)
        for record in records:
            by_source[source_of(record) or ""].append(record)
        for source, same in sorted(by_source.items()):
            if len(same) < 2:
                continue
            yield Draft(
                subject=evidence_of(same[1]),
                message=f"one source states {kind} id {namespace}:{value}"
                f" for {plural(len(same), 'record')}",
                details={"id": value, "kind": kind, "namespace": namespace, "source": source},
                related=[evidence_of(r) for r in same if r is not same[1]],
                records=[r.id for r in same],
            )


def id_conflict(context: Context) -> Iterator[Draft]:
    """Records naming the same logical thing state different values for one of its attributes."""
    for (kind, namespace, value), records in sorted(_id_groups(context).items()):
        if len(records) < 2:
            continue
        for attribute in _IDENTIFIED[kind][1]:
            holders: dict[str, list[Any]] = defaultdict(list)
            for record in records:
                state = getattr(record, attribute)
                if isinstance(state, Known):
                    holders[_value_key(state.value)].append(record)
            if len(holders) < 2:
                continue
            values = sorted(holders)
            first = holders[values[0]][0]
            yield Draft(
                subject=evidence_of(first, getattr(first, attribute)),
                message=f"records of {kind} {namespace}:{value} state"
                f" {len(values)} different {attribute} values",
                details={
                    "attribute": attribute,
                    "id": value,
                    "kind": kind,
                    "namespace": namespace,
                    "values": list(values[: context.bounds.values_per_detail]),
                },
                related=[
                    evidence_of(r, getattr(r, attribute))
                    for key in values
                    for r in holders[key]
                    if r is not first
                ],
                records=[r.id for key in values for r in holders[key]],
            )


# The kind of record a reference must name, where the model states one. Only a target-kind check:
# which fields are references is read from the model (``model.references``), never listed here.
_TARGET_KINDS: Final = {
    ("calibration", "extrinsics"): "frame_transform",
    ("civil_time_zone", "domain"): "timestamp_domain",
    ("configuration_value", "snapshot"): "configuration_snapshot",
    ("document_block", "document"): "document_record",
    ("frame_binding", "transform"): "frame_transform",
    ("hardware_component", "configuration"): "hardware_configuration",
    ("stream", "clocks"): "timestamp_domain",
    ("stream", "run"): "run",
    ("structured_record", "table"): "structured_table",
    ("video", "clock"): "timestamp_domain",
}


def references_checked(context: Context) -> Iterator[tuple[Any, str, str]]:
    """``(record, field, target)`` for every reference ``dangling_reference`` checks: every
    record's and every other producer's finding's, as ``model.references.named`` reads them (the
    walker the runtime's salvage check uses, ADR 0069 §2)."""
    for record in context.package.records:
        if record.kind == "ingest_finding":
            continue  # the other producers' findings follow; this rule's own are never read
        for field, target in named(record):
            yield record, field, target
    for finding in context.findings:
        for field, target in named(finding):
            yield finding, field, target


def _unresolved(context: Context) -> set[tuple[str, str, str]]:
    """``(kind, field, target)`` of every reference ``references_checked`` reads that names no
    record of the package, or one of another kind than the model states.

    A join, not a lookup: every record id (with its kind) and every reference go through one
    sorter keyed by the id named, which spills to ``context.spill`` (ADR 0070), so no index of the
    package is held. An id's records come before the references to it; the last record read of an
    id is the one a reference resolves to.
    """
    unresolved: set[tuple[str, str, str]] = set()
    with SpillSpace(context.spill) as space:
        joined = space.sorter("references")
        for position, record in enumerate(context.package.records):  # one pass of the tables
            identifier = getattr(record, "id", None)
            if isinstance(identifier, str):
                joined.add((identifier, 0, position), record.kind.encode())
            if record.kind == "ingest_finding" and record.transform in context.own:
                continue  # what ``references_checked`` reads: never this rule's own findings
            for field, target in named(record):
                joined.add((target, 1, record.kind, field), b"")
        current: str | int | None = None
        held: str | None = None
        for key, payload in joined:
            if key[0] != current:
                current, held = key[0], None
            if key[1] == 0:
                held = payload.decode()
                continue
            kind, field = str(key[2]), str(key[3])
            want = _TARGET_KINDS.get((kind, field))
            if held is None or (want is not None and held != want):
                unresolved.add((kind, field, str(key[0])))
    return unresolved


def dangling_reference(context: Context) -> Iterator[Draft]:
    """A record names another record (a run, a clock, a table, a snapshot, a frame graph) the
    package does not hold, or one of another kind than the model states; a finding's
    ``records`` name one it does not hold. Version 3: every reference the model types, not a
    hand list (ADR 0069); references marked external (another package's) are not checked."""
    dangling = _unresolved(context)
    missing: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    if dangling:  # a second pass picks up the records that name them, in the order read
        for record, field, target in references_checked(context):
            if (record.kind, field, target) in dangling:
                missing[record.kind, field, target].append(record)

    def where(record: Any) -> Any:
        subject = getattr(record, "subject", None)  # a finding has a subject, no provenance
        return subject if subject is not None else evidence_of(record)

    for (kind, field, target), records in sorted(missing.items()):
        want = _TARGET_KINDS.get((kind, field))
        details: dict[str, JsonValue] = {"field": field, "kind": kind, "target": target}
        if want is not None:
            details["target_kind"] = want
        related = [where(r) for r in records[1:]]
        yield Draft(
            subject=where(records[0]),
            message=f"{plural(len(records), kind + ' record')} name {want or 'record'}"
            f" {short(target)} in {field}, which this package does not hold",
            details=details,
            related=[r for r in related if isinstance(r, EvidenceRef)],
            records=[r.id for r in records],
        )


def civil_zone_repeated(context: Context) -> Iterator[Draft]:
    """Several ``civil_time_zone`` records name one clock (ADR 0061 §3): which zone it counts is
    in doubt, even when they agree, since an adapter writes at most one per domain."""
    by_domain: dict[str, list[Any]] = defaultdict(list)
    for zone in context.records("civil_time_zone"):
        by_domain[zone.domain].append(zone)
    for domain, zones in sorted(by_domain.items()):
        if len(zones) < 2:
            continue
        zones.sort(key=lambda z: z.id)
        names = sorted({name for z in zones for name in _zone_names(z.zone)})
        yield Draft(
            subject=evidence_of(zones[1]),
            message=f"{plural(len(zones), 'civil_time_zone record')} name clock {short(domain)}",
            details={
                "domain": domain,
                "zones": names[: context.bounds.values_per_detail],
            },
            related=[evidence_of(z) for z in zones if z is not zones[1]],
            records=[z.id for z in zones],
        )


def _zone_names(state: Any) -> list[str]:
    if isinstance(state, Known):
        return [state.value]
    if isinstance(state, Ambiguous):
        return [candidate.value for candidate in state.candidates]
    return []


# --- unresolved frames ---------------------------------------------------------------------------


def _frame_uses(context: Context) -> Iterator[tuple[Any, FrameRef, bool]]:
    """Every frame reference, and whether it declares the frame (True) or only names it."""
    for frame in context.records("frame"):
        yield frame, frame.ref, True
    for transform in context.records("frame_transform"):
        yield transform, transform.parent, True
        yield transform, transform.child, True
    for kind in ("hardware_component", "spatial_artifact"):
        for record in context.records(kind):
            if isinstance(record.frame, Known):
                yield record, record.frame.value, False


def frame_unresolved(context: Context) -> Iterator[Draft]:
    """A frame is named in a graph the package lacks, or in a graph that never declares it."""
    graphs = {graph.id for graph in context.records("frame_graph")}
    uses = list(_frame_uses(context))
    declared = {ref for _, ref, declares in uses if declares}
    unresolved: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for record, ref, _ in uses:
        if ref.frame_graph_id not in graphs or ref not in declared:
            unresolved[ref.frame_graph_id, ref.frame_id].append(record)
    for (graph, frame), records in sorted(unresolved.items()):
        reason = "graph_missing" if graph not in graphs else "frame_undeclared"
        unique = sorted({r.id: r for r in records}.values(), key=lambda r: r.id)
        where = (
            "a frame graph this package does not hold"
            if reason == "graph_missing"
            else (f"graph {short(graph)}, which declares no such frame")
        )
        yield Draft(
            subject=evidence_of(unique[0]),
            message=f"frame {frame!r} is named in {where}"[:900],
            details={"frame": frame, "frame_graph": graph, "reason": reason},
            related=[evidence_of(r) for r in unique[1:]],
            records=[r.id for r in unique],
        )


# --- stale configuration --------------------------------------------------------------------------


def _known(state: Any) -> Any:
    return state.value if isinstance(state, Known) else None


def calibration_revision_mismatch(context: Context) -> Iterator[Draft | Omitted]:
    """A calibration states a hardware revision none of its machine's configurations declares.

    Each machine's declared revisions and the configurations a finding cites are built once and
    shared by its calibrations; mismatches past the cap are counted, never drafted.
    """
    revisions: dict[LogicalId, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for config in context.records("hardware_configuration"):
        machine, revision = _known(config.machine), _known(config.revision)
        if machine is not None and revision is not None:
            revisions[machine][revision.value].append(config)
    mismatched: list[tuple[Any, LogicalId, Any]] = []
    for calibration in context.records("calibration"):
        machine, revision = _known(calibration.machine), _known(calibration.hardware_revision)
        if machine is None or revision is None or machine not in revisions:
            continue
        if revision.value not in revisions[machine]:
            mismatched.append((calibration, machine, revision))
    cite = context.bounds.records_per_finding - 1
    shown: dict[LogicalId, tuple[list[str], list[Any], int]] = {}

    def summary(machine: LogicalId) -> tuple[list[str], list[Any], int]:
        if machine not in shown:
            declared = revisions[machine]
            keys = sorted(declared)
            cited = list(islice((c for key in keys for c in declared[key]), cite))
            shown[machine] = (keys, cited, sum(len(v) for v in declared.values()))
        return shown[machine]

    budget = context.bounds.findings_per_rule
    for calibration, machine, revision in mismatched[:budget]:
        keys, cited, total = summary(machine)
        yield Draft(
            subject=evidence_of(calibration, calibration.hardware_revision),
            message=f"calibration {short(calibration.id)} is for hardware revision"
            f" {revision.value!r}; its machine's configurations declare"
            f" {plural(len(keys), 'other revision')}",
            details={
                "configurations": total,
                "declared": keys[: context.bounds.values_per_detail],
                "machine": _value_key(machine),
                "revision": revision.value,
            },
            related=[
                evidence_of(c, c.revision) for c in cited[: context.bounds.related_per_finding]
            ],
            records=[calibration.id, *(c.id for c in cited)],
        )
    if len(mismatched) > budget:
        first = mismatched[budget][0]
        yield Omitted(len(mismatched) - budget, evidence_of(first, first.hardware_revision))


def calibration_out_of_window(context: Context) -> Iterator[Draft | Omitted]:
    """A run of a machine lies outside every stated window of one calibrated subject: it starts
    after the last window ends, or ends before the first begins, all on one clock. A newer
    calibration of the subject covers what an older one's window no longer does."""
    groups: dict[tuple[LogicalId, str, str], list[Any]] = defaultdict(list)
    for calibration in context.records("calibration"):
        machine, subject = _known(calibration.machine), _known(calibration.subject)
        if machine is None or subject is None:
            continue
        domains = {
            stamp.domain_id
            for stamp in (_known(calibration.valid_from), _known(calibration.valid_until))
            if stamp is not None
        }
        if len(domains) == 1:
            groups[machine, subject, domains.pop()].append(calibration)
    if not groups:
        return
    starts: dict[tuple[LogicalId, str], list[tuple[int, str, Any]]] = defaultdict(list)
    ends: dict[tuple[LogicalId, str], list[tuple[int, str, Any]]] = defaultdict(list)
    for run in context.records("run"):
        machine = _known(run.machine)
        if machine is None:
            continue
        for state, index in ((run.first, starts), (run.last, ends)):
            stamp = _known(state)
            if stamp is not None:
                index[machine, stamp.domain_id].append((stamp.ticks, run.id, run))
    for index in (starts, ends):
        for entries in index.values():
            entries.sort(key=lambda entry: (entry[0], entry[1]))
    limit = context.bounds.records_per_finding
    budget, made = context.bounds.findings_per_rule, 0
    for (machine, subject, domain), calibrations in sorted(
        groups.items(), key=lambda item: (_value_key(item[0][0]), item[0][1], item[0][2])
    ):
        untils = [_known(c.valid_until) for c in calibrations]
        froms = [_known(c.valid_from) for c in calibrations]
        # Counted by index arithmetic; only the first ``limit`` runs are ever sliced, to cite.
        late: list[tuple[int, str, Any]] = []
        early: list[tuple[int, str, Any]] = []
        after = before = 0
        if all(stamp is not None for stamp in untils):  # an open-ended window covers what follows
            last = max(stamp.ticks for stamp in untils if stamp is not None)
            entries = starts.get((machine, domain), [])
            cut = bisect_right(entries, last, key=lambda e: e[0])
            after = len(entries) - cut
            late = entries[cut : cut + limit] if made < budget else []
        if all(stamp is not None for stamp in froms):
            first = min(stamp.ticks for stamp in froms if stamp is not None)
            entries = ends.get((machine, domain), [])
            before = bisect_left(entries, first, key=lambda e: e[0])
            early = entries[: min(before, limit)] if made < budget else []
        if not after and not before:
            continue
        ordered = sorted(calibrations, key=lambda c: c.id)
        if made >= budget:
            yield Omitted(1, evidence_of(ordered[-1]))
            continue
        made += 1
        named = sorted({e[1]: e[2] for e in (*late, *early)}.values(), key=lambda r: r.id)[:limit]
        # A run is both late and early only if it ends before it starts; the sum counts runs.
        yield Draft(
            subject=evidence_of(ordered[-1]),
            message=f"{plural(after + before, 'run')} of the machine lie outside every stated"
            f" window of its {plural(len(ordered), 'calibration')} of {subject!r}",
            details={
                "after_valid_until": after,
                "before_valid_from": before,
                "machine": _value_key(machine),
                "subject": subject,
            },
            related=[*(evidence_of(c) for c in ordered[:-1]), *(evidence_of(r) for r in named)],
            records=[*(c.id for c in ordered), *(r.id for r in named)],
        )


# --- software bindings ---------------------------------------------------------------------------


def _version(state: Any) -> str | None:
    value = _known(state)
    return None if value is None else canonical_json.dumps(version_to_json(value)).decode()


def _label(state: Any) -> str:
    value = _known(state)
    if value is None:
        return ""
    for name in ("value", "sha", "digest"):
        text = getattr(value, name, None)
        if isinstance(text, str):
            return text
    return str(value)


_BINDINGS: Final = (("release", "commit"), ("build", "digest"), ("commit", "build"))


def software_conflict(context: Context) -> Iterator[Draft]:
    """Software declarations disagree: one release (or build, or commit) of a named piece of
    software is stated with two different commits (or digests, or builds)."""
    items: list[tuple[Any, Any]] = [
        (config, item)
        for config in context.records("software_configuration")
        for item in config.software
        if isinstance(item.name, Known)
    ]
    for key_field, value_field in _BINDINGS:
        groups: dict[tuple[str, str], dict[str, list[tuple[Any, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        labels: dict[str, str] = {}
        for config, item in items:
            key, value = _version(getattr(item, key_field)), _version(getattr(item, value_field))
            if key is not None and value is not None:
                groups[item.name.value, key][value].append((config, item))
                labels[key] = _label(getattr(item, key_field))
                labels[value] = _label(getattr(item, value_field))
        for (name, key), values in sorted(groups.items()):
            if len(values) < 2:
                continue
            holders = [pair for value in sorted(values) for pair in values[value]]
            configs = sorted(
                {config.id: config for config, _ in holders}.values(), key=lambda c: c.id
            )
            stated = sorted(labels[value] for value in values)
            yield Draft(
                subject=evidence_of(configs[0]),
                message=f"{name!r} {key_field} {labels[key]!r} is declared with"
                f" {len(values)} different {value_field} values"[:900],
                details={
                    "field": value_field,
                    "key": labels[key],
                    "key_field": key_field,
                    "name": name,
                    "values": stated[: context.bounds.values_per_detail],
                },
                related=[evidence_of(c) for c in configs[1:]],
                records=[c.id for c in configs],
            )


# --- the set -------------------------------------------------------------------------------------

_W = Severity.WARNING
_C = FindingCategory

RULES_ON: Final = (
    Rule("calibration_out_of_window", 1, _C.INCONSISTENT, _W,
         "a run of a machine lies outside its calibration's stated window, same clock",
         calibration_out_of_window),
    Rule("calibration_revision_mismatch", 1, _C.INCONSISTENT, _W,
         "a calibration's hardware revision is none its machine's configurations declare",
         calibration_revision_mismatch),
    Rule("civil_zone_repeated", 1, _C.AMBIGUOUS, _W,
         "several civil_time_zone records name one clock", civil_zone_repeated),
    Rule("count_mismatch", 1, _C.INCONSISTENT, _W,
         "a stream's declared message count differs from the rows its series holds",
         count_mismatch),
    Rule("dangling_reference", 3, _C.MISSING, _W,
         "a record or finding names, in a field the model types as a record id, a record the"
         " package does not hold",
         dangling_reference),
    Rule("duplicate_id", 1, _C.AMBIGUOUS, _W,
         "one source states one logical id for two records of a kind", duplicate_id),
    Rule("frame_unresolved", 1, _C.MISSING, _W,
         "a frame is named in a missing graph, or in a graph that never declares it",
         frame_unresolved),
    Rule("id_conflict", 1, _C.INCONSISTENT, _W,
         "records naming one logical id state different attribute values", id_conflict),
    Rule("interval_reversed", 1, _C.INCONSISTENT, _W,
         "a declared interval ends before it starts on the same clock", interval_reversed),
    Rule("missing_metadata", 1, _C.MISSING, _W,
         "records leave a field consumers rely on unknown", missing_metadata),
    Rule("row_shape_mismatch", 1, _C.INCONSISTENT, _W,
         "table rows hold a different number of cells than the header names",
         row_shape_mismatch),
    Rule("schema_conflict", 1, _C.INCONSISTENT, _W,
         "streams of one run on one topic declare different schemas or encodings",
         schema_conflict),
    Rule("software_conflict", 1, _C.INCONSISTENT, _W,
         "one release, build or commit of named software is declared two ways",
         software_conflict),
    Rule("snapshot_incomplete", 1, _C.INCONSISTENT, _W,
         "a configuration snapshot's value count differs from the values the package holds",
         snapshot_incomplete),
    Rule("source_incomplete", 1, _C.CORRUPT, _W,
         "a source's bytes were cut off or corrupt: its findings rolled up, its records named",
         source_incomplete),
    Rule("time_out_of_order", 1, _C.INCONSISTENT, Severity.INFO,
         "a stream's samples are out of time order, in source order, on a clock that does not"
         " declare itself monotonic", time_out_of_order),
    Rule("time_regression", 1, _C.INCONSISTENT, _W,
         "a stream's samples are out of time order, in source order, on a clock that declares"
         " itself monotonic", time_regression),
)  # fmt: skip

DEFAULT_RULES: Final = RULES_ON
ALL_RULES: Final = tuple(sorted((*RULES_ON, *pending.RULES), key=lambda rule: rule.code))
