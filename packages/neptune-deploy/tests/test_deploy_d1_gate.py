"""The D1 gate (MVL-116): the lifecycle questions a safety lead asks, put to both archetypes, and
the attacks on them (``docs/reviews/d1-gate.md``, ADR 0005).

Every question is answered from the mapped package alone: a value that is ``stated`` and cites the
cell or span it came from, or an explicit ``Unknown`` / ``NotCovered`` that a finding explains.
The reader below is the test's, not Deploy's: Deploy orders nothing and compares nothing (package
AGENTS.md, non-negotiable 2). The reader orders two times only when the records allow it without a
guess: the same clock, two readings of one record, or more than a day of civil offsets apart.
"""

import builtins
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.identity import canonical_json
from neptune.model.knowledge import AssertionKind, Known, KnownAbsent, NotCovered, Unknown
from neptune.model.lifecycle import LIFECYCLE_KINDS
from neptune.model.provenance import EvidenceRef, Provenance, RowCell, Span
from neptune.model.time import Timescale, Timestamp
from neptune.store.package import IngestPackage, read_files, read_package
from neptune_deploy.lifecycle import TemplateRegistry, load_mapping, map_files, preset

ARCHETYPES: Final = Path(__file__).parent / "fixtures" / "archetypes"
FLEET: Final = "warehouse_amr_fleet"
CELL: Final = "manipulator_cell"
# Civil offsets run from UTC-12:00 to UTC+14:00: two wall-clock readings whose zones are not both
# known are ordered only when they are further apart than that (ADR 0005 §6).
OFFSETS: Final = Fraction(26 * 3600)
OUTPUT_KINDS: Final = {
    "ingest_finding",
    "source_absence",
    "source_artifact",
    "source_revision",
    "timestamp_domain",
    "civil_time_zone",
    "transform_record",
} | {kind.kind for kind in LIFECYCLE_KINDS}


def _generator() -> ModuleType:
    if "make_archetypes" in sys.modules:
        return sys.modules["make_archetypes"]
    spec = importlib.util.spec_from_file_location(
        "make_archetypes", ARCHETYPES / "make_archetypes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _held(package: IngestPackage) -> IngestPackage:
    """``package`` with its records and derived tables held, as the attacks below edit them by
    identity and the mapper is checked to open no file: ``read_package`` reads them from disk on
    every pass (root ADR 0070)."""
    derived = {kind: tuple(lines) for kind, lines in package.derived.items()}
    return replace(package, records=tuple(package.records), derived=derived)


A: Final = _generator()
BASES: Final = {name: _held(read_package(A.PACKAGES / name)) for name in (FLEET, CELL)}


def _declared(name: str) -> tuple[list[Any], list[Any]]:
    declared = A.PIPELINES[name]
    mappings = [preset(p) for p in declared.presets]
    mappings += [load_mapping(path) for path in declared.mappings]
    return mappings, list(TemplateRegistry.from_paths(declared.templates).templates())


def _map(name: str, base: IngestPackage | None = None) -> IngestPackage:
    mappings, templates = _declared(name)
    return read_files(map_files(BASES[name] if base is None else base, mappings, templates))


MAPPED: Final = {name: _map(name) for name in (FLEET, CELL)}


# --- The reader ---------------------------------------------------------------------------------

State = Known[Any] | Unknown | NotCovered | KnownAbsent


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _items(state: Any) -> tuple[Any, ...]:
    """A list field's items: none for a list that is Unknown or NotCovered (it states no item)."""
    return state.value if isinstance(state, Known) else ()


def _ids(states: Any) -> set[str]:
    return {s.value.value for s in states if isinstance(s, Known)}


def _named(package: IngestPackage, kind: str, value: str) -> Any:
    (record,) = [r for r in _of(package, kind) if value in _ids(r.identifiers.value)]
    return record


def _on(package: IngestPackage, kind: str, machine: str) -> list[Any]:
    return [r for r in _of(package, kind) if machine in _ids(r.machines.value)]


def _cited(state: Any) -> EvidenceRef:
    """Where a state's provenance says it was read."""
    assert isinstance(state.provenance, Provenance), state
    evidence: EvidenceRef = state.provenance.evidence
    return evidence


def _column(evidence: Any) -> str:
    """The column a citation names: its last step is a cell of a headed table."""
    step = evidence.locator[-1]
    assert isinstance(step, RowCell), evidence
    assert isinstance(step.column_name, str)
    return step.column_name


def _findings(package: IngestPackage, name: str) -> list[Any]:
    return [f for f in _of(package, "ingest_finding") if f.code.endswith(f".{name}")]


def _about(package: IngestPackage, name: str, record: Any) -> Any:
    """The one finding ``name`` that names ``record``."""
    (finding,) = [f for f in _findings(package, name) if record.id in f.records]
    return finding


def _seconds(package: IngestPackage, stamp: Timestamp) -> tuple[Fraction, Fraction, bool]:
    """A reading as seconds since its clock's epoch, its resolution, and whether it is an instant
    (an offset was stated) rather than a wall-clock reading."""
    (domain,) = [d for d in _of(package, "timestamp_domain") if d.id == stamp.domain_id]
    assert isinstance(domain.resolution, Known)
    instant = domain.timescale == Known(Timescale.POSIX)
    return stamp.ticks * domain.resolution.value, domain.resolution.value, instant


def _before(package: IngestPackage, a: State, b: State, one_record: bool = False) -> bool | None:
    """Whether ``a`` is before ``b`` as far as the records say; ``None`` when they do not.

    One clock (the same domain, or two wall-clock readings of one record, or two instants) is
    compared directly. Otherwise the readings are ordered only when more than ``OFFSETS`` and
    their resolutions apart."""
    if not (isinstance(a, Known) and isinstance(b, Known)):
        return None
    (x, rx, ix), (y, ry, iy) = _seconds(package, a.value), _seconds(package, b.value)
    if a.value.domain_id == b.value.domain_id or (ix and iy) or (one_record and not ix and not iy):
        return x < y
    margin = OFFSETS + max(rx, ry)
    if x + margin < y:
        return True
    if y + margin < x:
        return False
    return None


def _states(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every knowledge state in a lifecycle record, its parts' included, with its JSON pointer.

    A Known list is structure, not a value: its items are walked at the list's pointer.
    """
    if isinstance(value, Known) and isinstance(value.value, tuple):
        yield from _states(value.value, path)
    elif isinstance(value, Known | Unknown | NotCovered | KnownAbsent):
        yield path, value
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from _states(item, f"{path}/{index}")
    elif hasattr(value, "__dataclass_fields__") and not isinstance(value, Provenance):
        for name in value.__dataclass_fields__:
            if name not in ("id", "provenance", "kind"):
                yield from _states(getattr(value, name), f"{path}/{name}")


# --- Every value is stated and cited, or explicitly missing ---------------------------------------


@pytest.mark.parametrize("name", [FLEET, CELL])
def test_every_value_is_stated_and_cites_its_cell_or_span_or_is_explicitly_missing(
    name: str,
) -> None:
    package = MAPPED[name]
    ledger = {r.content_id for r in _of(package, "source_revision")}
    counts = {"known": 0, "unknown": 0, "not_covered": 0}
    for record in package.records:
        if not isinstance(record, LIFECYCLE_KINDS):
            continue
        assert record.provenance.assertion_kind is AssertionKind.STATED
        for path, state in _states(record):
            if isinstance(state, Known):
                counts["known"] += 1
                provenance = state.provenance
                assert isinstance(provenance, Provenance), (record.kind, path)
                assert provenance.assertion_kind is AssertionKind.STATED, (record.kind, path)
                assert provenance.evidence.source in ledger
                # A value cites a cell or a span inside the source, never the whole source.
                assert isinstance(provenance.evidence.locator[-1], RowCell | Span) or (
                    provenance.evidence.locator[-1].kind == "json_pointer"
                ), (record.kind, path, provenance.evidence)
            elif isinstance(state, Unknown):
                counts["unknown"] += 1
                # An Unknown read from a cell cites that cell: a blank is never a silent default.
                assert isinstance(state.provenance, Provenance), (record.kind, path)
                assert state.provenance.evidence.source in ledger
            elif isinstance(state, NotCovered):
                counts["not_covered"] += 1
    assert counts["known"] > 100 and counts["unknown"] and counts["not_covered"]


@pytest.mark.parametrize("name", [FLEET, CELL])
def test_every_unread_field_of_every_record_is_named_by_a_finding(name: str) -> None:
    """A field no declaration reads is ``NotCovered`` in the record, or (a list, which cannot be)
    named in its rule's ``fields_not_covered`` or its template's ``template_matched``."""
    package = MAPPED[name]
    explained: dict[Any, set[str]] = {}
    for finding in [
        *_findings(package, "fields_not_covered"),
        *_findings(package, "template_matched"),
    ]:
        for record in finding.records:
            explained.setdefault(record, set()).update(finding.details["not_covered"])
    for record in package.records:
        if not isinstance(record, LIFECYCLE_KINDS):
            continue
        for path, state in _states(record):
            if isinstance(state, NotCovered) and path.count("/") == 1:
                assert path[1:] in explained.get(record.id, set()), (record.kind, path)
        for field_name in ("machines", "related"):
            # A list no rule reads is NotCovered and named; one that is read and blank is Unknown
            # and cites its cell; Known(()) is a list the cells stated empty (ADR 0012 §1).
            state = getattr(record, field_name)
            if isinstance(state, NotCovered):
                assert field_name in explained.get(record.id, ()), (record.kind, field_name)
            elif isinstance(state, Unknown):
                assert isinstance(state.provenance, Provenance), (record.kind, field_name)


# --- Lineage: a new package that cites, never copies or re-parses --------------------------------


@pytest.mark.parametrize("name", [FLEET, CELL])
def test_the_lifecycle_package_cites_the_base_and_copies_none_of_it(name: str) -> None:
    base, package = BASES[name], MAPPED[name]
    assert {r.kind for r in package.records} <= OUTPUT_KINDS
    transforms = {t.id: t for t in _of(package, "transform_record")}
    base_transforms = {t.id for t in _of(base, "transform_record")}
    ours = {
        t.id
        for t in transforms.values()
        if t.adapter_id in ("deploy_lifecycle_map", "deploy_document_map")
    }
    for transform_id in ours:
        transform = transforms[transform_id]
        assert transform.config["base_package"] == base.id
        assert set(transform.upstream) <= base_transforms  # the compiler's, carried whole
        assert set(transform.upstream) <= set(transforms)
    for record in package.records:
        if isinstance(record, LIFECYCLE_KINDS):
            assert record.provenance.transform in ours
        elif record.kind == "ingest_finding":
            assert record.transform in ours
    ledger = {(r.content_id, r.location.path) for r in _of(package, "source_revision")}
    assert ledger == {(r.content_id, r.location.path) for r in _of(base, "source_revision")}


def test_the_mapper_opens_no_file_while_it_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    """It reads records, never a source's bytes: no file is opened between the declared files
    being loaded and the new package's bytes being returned."""
    mappings, templates = _declared(CELL)
    opened: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        opened.append(args[0] if args else kwargs)
        raise AssertionError(f"the mapper opened {args[0] if args else kwargs}")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(io, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    files = map_files(BASES[CELL], mappings, templates)
    monkeypatch.undo()
    assert not opened and files


# --- Determinism across hash seeds ---------------------------------------------------------------

_DIGEST: Final = """
import hashlib, importlib.util, sys
sys.path.insert(0, {tests!r})
spec = importlib.util.spec_from_file_location("test_deploy_d1_gate", {module!r})
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
for name in (gate.FLEET, gate.CELL):
    mappings, templates = gate._declared(name)
    files = gate.map_files(gate.BASES[name], mappings, templates)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode() + b"\\0" + files[path])
    print(name, digest.hexdigest())
"""


def _digests() -> str:
    lines = []
    for name in (FLEET, CELL):
        mappings, templates = _declared(name)
        files = map_files(BASES[name], mappings, templates)
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.encode() + b"\0" + files[path])
        lines.append(f"{name} {digest.hexdigest()}")
    return "\n".join(lines) + "\n"


@pytest.mark.slow
def test_both_archetypes_map_byte_identically_under_any_hash_seed() -> None:
    script = _DIGEST.format(tests=str(Path(__file__).parent), module=__file__)
    expected = _digests()
    for seed in ("0", "1", "4242", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
        )
        assert done.stdout == expected, seed


# --- Q1: which configuration was authorised at the time of the incident? --------------------------


def test_q1_fleet_the_authorised_configuration_is_not_covered_and_the_receipt_says_so() -> None:
    package = MAPPED[FLEET]
    incident = _named(package, "incident_record", "INC-0007")
    assert _ids(incident.machines.value) == {"AMR-07"} and incident.zone.value.value == "PICK-A"
    # The report states no configuration, and the template says it does not read one.
    assert isinstance(incident.configuration, NotCovered)
    assert "configuration" in _about(package, "template_matched", incident).details["not_covered"]
    # The envelope authorising AMR-07 in PICK-A, valid around the incident ...
    envelopes = [
        e
        for e in _on(package, "authorisation_envelope", "AMR-07")
        if "PICK-A" in {z.zone.value.value for z in e.zones.value}
    ]
    (envelope,) = envelopes
    assert _ids(envelope.identifiers.value) == {"ENV-S007-04"}
    assert _before(package, envelope.valid_from, incident.occurred) is True
    assert _before(package, incident.occurred, envelope.valid_until) is True
    # ... states no configuration: the register has none, and the rule says it does not read one.
    assert isinstance(envelope.configuration, NotCovered)
    rule = _about(package, "fields_not_covered", envelope)
    assert "configuration" in rule.details["not_covered"]
    # What the records do state: the work orders on AMR-07 state the firmware each left, and the
    # change record states when 4.3.1 replaced 4.2.0. Ordered by the reader, cited by the mapper.
    orders = _on(package, "maintenance_event", "AMR-07")
    before = [o for o in orders if _before(package, o.performed, incident.occurred) is True]
    assert {o.configuration.value.value for o in before} == {"4.2.0"}
    change = _named(package, "change_record", "CHG0050023")
    assert (change.changes.value[0].before.value, change.changes.value[0].after.value) == (
        "4.2.0",
        "4.3.1",
    )
    assert _before(package, incident.occurred, change.effective) is True
    for order in before:
        assert order.configuration.provenance.evidence.locator[-1].column_name == "Firmware After"


def test_q1_cell_there_is_no_authorisation_record_and_the_configuration_is_stated_around_it() -> (
    None
):
    package = MAPPED[CELL]
    near_miss = _named(package, "incident_record", "INC-C3-0004")
    assert _seconds(package, near_miss.occurred.value)[2]  # an instant: the ticket states -04:00
    assert not _of(package, "authorisation_envelope")  # nothing the cell declared states one
    # The ticket names no machine: a list the rule does not read is NotCovered, not "none".
    assert isinstance(near_miss.machines, NotCovered)
    assert "machines" in _about(package, "fields_not_covered", near_miss).details["not_covered"]
    baseline = _named(package, "commissioning_baseline", "CR-C3-2026-02")
    assert baseline.configuration.value.value == "cfg-c3-1.4"
    change = _named(package, "change_record", "CHG0030012")
    assert _before(package, baseline.commissioned, change.effective) is True
    assert _before(package, change.effective, near_miss.occurred) is True
    assert (change.changes.value[0].before.value, change.changes.value[0].after.value) == (
        "5.4.2",
        "5.6.0",
    )
    # The inspection after the near miss is an INSP work order no rule reads: no record, but its
    # row is named, so it is not lost.
    cmms = [f for f in _findings(package, "row_unmatched") if f.subject.source in _sources(CELL)]
    assert any(f.details["count"] == 1 for f in cmms)


def _sources(name: str) -> set[Any]:
    return {
        r.content_id
        for r in _of(MAPPED[name], "source_revision")
        if r.location.path.startswith("cmms/")
    }


# --- Q2: what changed since commissioning? -----------------------------------------------------


def test_q2_cell_every_change_since_commissioning_is_a_cited_record_ordered_by_the_reader() -> None:
    package = MAPPED[CELL]
    baseline = _named(package, "commissioning_baseline", "CR-C3-2026-02")
    assert _ids(baseline.calibrations.value) == {"CAL-ARM3A-0226"}
    since = {
        kind: [
            r
            for r in _on(package, kind, "ARM-3A")
            if _before(
                package,
                baseline.commissioned,
                r.effective if kind == "change_record" else r.performed,
            )
        ]
        for kind in ("change_record", "maintenance_event")
    }
    assert {i for r in since["change_record"] for i in _ids(r.identifiers.value)} == {
        "CHG0030012",
        "CHG0030013",
    }
    swapped = {
        p.part.value
        for r in since["maintenance_event"]
        for p in _items(r.parts)
        if isinstance(p.part, Known)
    }
    assert {"Joint 4 drive unit", "Finger set PG-80", "Retaining screw set"} <= swapped
    calibrations = {i for r in since["maintenance_event"] for i in _ids(r.related.value)}
    assert {"CAL-ARM3A-0415", "CAL-ARM3A-0623", "CAL-ARM3A-0818"} <= calibrations
    # The finger change is stated twice, by the CMMS and by the SOP's work record: two records,
    # two namespaces, never merged (identity is MVL-35's).
    finger = [r for r in since["maintenance_event"] if "WO-26-0391" in _ids(r.identifiers.value)]
    assert {r.identifiers.value[0].value.namespace for r in finger} == {
        "cmms.work_order",
        "plant2.work_order",
    }


def test_q2_fleet_has_no_commissioning_record_so_since_commissioning_is_not_covered() -> None:
    assert not _of(MAPPED[FLEET], "commissioning_baseline")


# --- Q3: was the requalification complete before return to service? ----------------------------


def test_q3_fleet_one_requalification_is_complete_before_return_and_one_return_is_unknown() -> None:
    package = MAPPED[FLEET]
    done = _named(package, "requalification_record", "RQ-S007-0007")
    assert done.result.value == "PASS" and [t.result.value for t in done.tests.value] == [
        "0.94 m",
        "PASS",
        "PASS",
    ]
    back = done.return_to_service
    assert back.decision.value == "Returned to service"
    assert _before(package, done.performed, back.time, one_record=True) is True
    # Across sources, a same-day order is not stated: the change and the requalification are
    # wall-clock readings in clocks whose zones are unstated (MVL-202).
    change = _named(package, "change_record", "CHG0050023")
    assert _before(package, change.effective, done.performed) is None
    restricted = _named(package, "requalification_record", "RQ-S012-0003")
    assert restricted.result.value == "PASS with note"
    time = restricted.return_to_service.time
    assert isinstance(time, Unknown)
    assert _column(_cited(time)) == "Decided On"
    blank = [f for f in _findings(package, "value_blank") if f.details["column"] == "Decided On"]
    assert len(blank) == 1 and blank[0].subject == _cited(time)
    assert (
        len(restricted.tests.value) == 2
    )  # the third pair is blank: item_blank, not an empty test


def test_q3_cell_the_last_return_to_service_time_is_unknown_and_cited() -> None:
    package = MAPPED[CELL]
    complete = _named(package, "requalification_record", "RQ-2026-005")
    assert _before(package, complete.performed, complete.return_to_service.time, one_record=True)
    last = _named(package, "requalification_record", "RQ-2026-006")
    assert isinstance(last.return_to_service.time, Unknown)
    assert last.return_to_service.decision.value == "Returned to service with speed restriction"


# --- Attacks ------------------------------------------------------------------------------------


def _set_cell(base: IngestPackage, key: tuple[str, str], column: str, value: Any) -> IngestPackage:
    """``base`` with ``column`` set to ``value`` in the row whose ``key`` column holds ``key``."""
    out = []
    tables = {
        t.id: t.header.value
        for t in _of(base, "structured_table")
        if isinstance(t.header, Known) and {key[0], column} <= set(t.header.value)
    }
    hit = 0
    for record in base.records:
        header = tables.get(getattr(record, "table", None))
        if record.kind == "structured_record" and header is not None:
            cell = record.cells[header.index(key[0])]
            if isinstance(cell, Known) and cell.value == key[1]:
                cells = list(record.cells)
                cells[header.index(column)] = Unknown() if value is None else Known(value)
                record = replace(record, cells=tuple(cells))
                hit += 1
        out.append(record)
    assert hit == 1, (key, column, hit)
    return replace(base, records=tuple(out))


def _rename_column(base: IngestPackage, old: str, new: str) -> IngestPackage:
    def change(record: Any) -> Any:
        header = record.header if record.kind == "structured_table" else None
        if isinstance(header, Known) and old in header.value:
            renamed = tuple(new if c == old else c for c in header.value)
            return replace(record, header=Known(renamed, header.provenance))
        return record

    return replace(base, records=tuple(change(r) for r in base.records))


def _retext(base: IngestPackage, old: str, new: str) -> IngestPackage:
    """A document block's text replaced, its cited span with it (the compiler's invariant)."""

    def change(record: Any) -> Any:
        if record.kind != "document_block" or record.text != Known(old, record.text.provenance):
            return record
        evidence = record.provenance.evidence
        span = evidence.locator[-1]
        moved = EvidenceRef(
            evidence.source, (*evidence.locator[:-1], Span(span.start, span.start + len(new)))
        )
        return replace(
            record,
            text=Known(new, record.text.provenance),
            provenance=replace(record.provenance, evidence=moved),
        )

    changed = replace(base, records=tuple(change(r) for r in base.records))
    assert changed.records != base.records
    return changed


def _codes(package: IngestPackage) -> list[tuple[str, str]]:
    return sorted(
        (f.code, canonical_json.dumps(f.details).decode()) for f in _of(package, "ingest_finding")
    )


def test_attack_a_cmms_date_that_contradicts_the_incident_report_is_kept_cited_and_undecided() -> (
    None
):
    """The repair of INC-0007 dated three days before the incident it repairs."""
    attacked = _map(
        FLEET, _set_cell(BASES[FLEET], ("WO Number", "WO-26-0402"), "Completed", "2026-03-30 16:00")
    )
    repair = _named(attacked, "maintenance_event", "WO-26-0402")
    incident = _named(attacked, "incident_record", "INC-0007")
    assert "INC-0007" in _ids(repair.related.value)
    # Both stated as declared, each citing its own source; the reader sees the contradiction ...
    assert _before(attacked, repair.performed, incident.occurred) is True
    assert repair.performed.provenance.evidence.locator[-1].column_name == "Completed"
    assert isinstance(incident.occurred.provenance.evidence.locator[-1], Span)
    # ... and Deploy decides nothing: the receipt's findings are exactly the clean run's.
    assert _codes(attacked) == _codes(MAPPED[FLEET])


def test_attack_maintenance_events_with_no_configuration_are_kept_and_say_why() -> None:
    package = MAPPED[CELL]
    # The SOP's work record names no configuration, and its template does not read one.
    (sop,) = [
        r for r in _of(package, "maintenance_event") if "SOP-CELL-014" in _ids(r.related.value)
    ]
    assert isinstance(sop.configuration, NotCovered)
    assert "configuration" in _about(package, "template_matched", sop).details["not_covered"]
    # A blank firmware cell: the event is kept, its configuration Unknown citing that cell.
    blank = _map(
        FLEET, _set_cell(BASES[FLEET], ("WO Number", "WO-26-0302"), "Firmware After", None)
    )
    event = _named(blank, "maintenance_event", "WO-26-0302")
    assert isinstance(event.configuration, Unknown)
    assert _column(_cited(event.configuration)) == "Firmware After"
    assert len(_of(blank, "maintenance_event")) == len(_of(MAPPED[FLEET], "maintenance_event"))
    # The column gone from the export: every event's configuration is not covered, and both the
    # absent column and the new unread one are findings.
    renamed = _map(FLEET, _rename_column(BASES[FLEET], "Firmware After", "Firmware"))
    events = _of(renamed, "maintenance_event")
    assert events and all(isinstance(e.configuration, NotCovered) for e in events)
    assert [f.details["column"] for f in _findings(renamed, "column_absent")] == ["Firmware After"]
    (unmapped,) = [
        f for f in _findings(renamed, "column_unmapped") if "Firmware" in f.details["columns"]
    ]
    assert unmapped.details["column_count"] == 2  # with Downtime h


def test_attack_an_sop_revision_with_no_change_record_is_cited_and_no_change_is_invented() -> None:
    attacked = _map(CELL, _retext(BASES[CELL], "Revision: A", "Revision: B"))
    clean = MAPPED[CELL]
    (sop,) = [
        r for r in _of(attacked, "maintenance_event") if "SOP-CELL-014" in _ids(r.related.value)
    ]
    # The template reads no revision: the line is listed unread, with its own span, not dropped.
    unread = _about(attacked, "text_unread", sop)
    (line,) = unread.related
    assert line.locator[-1].end - line.locator[-1].start == len("Revision: B")
    # No change record states the revision, and Deploy invents none and flags no gap.
    assert {r.id for r in _of(attacked, "change_record")} == {
        r.id for r in _of(clean, "change_record")
    }
    assert not any(
        "SOP-CELL-014" in _ids(_items(r.related)) for r in _of(attacked, "change_record")
    )
    assert len(_of(attacked, "ingest_finding")) == len(_of(clean, "ingest_finding"))


def test_partial_success_damage_to_some_rows_leaves_every_other_record_byte_identical() -> None:
    base = BASES[FLEET]
    base = _set_cell(base, ("WO Number", "WO-26-0301"), "Completed", "yesterday")
    base = _set_cell(base, ("WO Number", "WO-26-0303"), "Asset ID", None)
    base = _set_cell(base, ("Envelope ID", "ENV-S012-01"), "Speed Limit", "9007199254740993")
    damaged = _map(FLEET, base)
    touched = {"WO-26-0301", "WO-26-0303", "ENV-S012-01"}

    def kept(package: IngestPackage) -> dict[Any, bytes]:
        records: list[Any] = [r for r in package.records if isinstance(r, LIFECYCLE_KINDS)]
        return {
            r.id: canonical_json.dumps(r.to_json())
            for r in records
            if not touched & _ids(r.identifiers.value)
        }

    assert kept(damaged) == kept(MAPPED[FLEET])
    assert len([r for r in damaged.records if isinstance(r, LIFECYCLE_KINDS)]) == len(
        [r for r in MAPPED[FLEET].records if isinstance(r, LIFECYCLE_KINDS)]
    )


# --- The gate's own changes, attacked -----------------------------------------------------------


def _table_like(base: IngestPackage, column: str) -> tuple[Any, list[Any]]:
    (table,) = [
        t
        for t in _of(base, "structured_table")
        if isinstance(t.header, Known) and column in t.header.value
    ]
    return table, [r for r in _of(base, "structured_record") if r.table == table.id]


def test_container_index_tables_are_neither_mapped_nor_reported() -> None:
    from neptune.model.provenance import adapter_locator
    from neptune_deploy.lifecycle.mapper import CONTAINER_INDEX_TABLES, tables_of

    base = BASES[FLEET]
    # The four tables of AMR-08's bag metadata are the rosbag2 adapter's index of its storage.
    usable, unnamed = tables_of(base.records)
    every = {t.id for t in _of(base, "structured_table")}
    assert len(every) - len(usable) - len(unnamed) == 4
    # What is reported unmapped is the site maps' GeoJSON tables (the geojson adapter, root ADR
    # 0057), each cited to a ``SpatialArtifact``'s source; none is an index.
    maps = {a.provenance.evidence.source for a in _of(base, "spatial_artifact")}
    map_tables = {
        t.id for t in _of(base, "structured_table") if t.provenance.evidence.source in maps
    }
    reported = {f.details["table"] for f in _findings(MAPPED[FLEET], "table_unmapped")}
    assert map_tables
    assert reported == map_tables
    # A workbook's sheet index, as the XLSX reader writes it (root ADR 0059 §4), named by the
    # citation's last step: never a candidate. The same table without that step stays one, and
    # is reported unmapped when no mapping reads it.
    table, _ = _table_like(base, "Envelope ID")
    step = adapter_locator("tabular:xlsx_workbook", {"part": "xl/workbook.xml"})
    evidence = EvidenceRef(
        table.provenance.evidence.source, (*table.provenance.evidence.locator, step)
    )
    workbook = replace(table, provenance=replace(table.provenance, evidence=evidence))
    swapped = replace(base, records=tuple(workbook if r is table else r for r in base.records))
    assert table.id in {t.record.id for t in tables_of(base.records)[0]}
    assert table.id not in {t.record.id for t in tables_of(swapped.records)[0]}
    assert table.id not in {t.id for t in tables_of(swapped.records)[1]}
    unread = read_files(map_files(base, [preset("cmms_generic")]))
    assert table.id in {f.details["table"] for f in _findings(unread, "table_unmapped")}
    quiet = read_files(map_files(swapped, [preset("cmms_generic")]))
    assert table.id not in {f.details["table"] for f in _findings(quiet, "table_unmapped")}
    # A Parquet footer's schema and row groups index the data table; only from ``tabular``.
    for kind in ("tabular:schema", "tabular:row_groups"):
        footer = replace(
            table,
            provenance=replace(
                table.provenance,
                evidence=EvidenceRef(evidence.source, (adapter_locator(kind, {}),)),
            ),
        )
        assert any(index.holds(footer, "tabular") for index in CONTAINER_INDEX_TABLES)
        assert not any(index.holds(footer, "rosbag2") for index in CONTAINER_INDEX_TABLES)
    key_value = replace(
        table,
        provenance=replace(
            table.provenance,
            evidence=EvidenceRef(evidence.source, (adapter_locator("tabular:key_value", {}),)),
        ),
    )
    assert not any(index.holds(key_value, "tabular") for index in CONTAINER_INDEX_TABLES)


def test_column_unmapped_names_ten_columns_and_counts_them_all() -> None:
    base = BASES[FLEET]
    table, _ = _table_like(base, "WO Number")
    wide = (*table.header.value, *(f"Extra {n}" for n in range(5000)))
    widened = replace(table, header=Known(wide, table.header.provenance))
    package = read_files(
        map_files(
            replace(base, records=tuple(widened if r is table else r for r in base.records)),
            [preset("cmms_generic")],
        )
    )
    (finding,) = _findings(package, "column_unmapped")
    assert finding.details["columns"] == ["Downtime h", *(f"Extra {n}" for n in range(9))]
    assert finding.details["column_count"] == 5001
    assert len(canonical_json.dumps(finding.to_json())) < 4096


@pytest.mark.parametrize(("parts", "kept", "truncated"), [(1000, 1000, False), (1001, 1000, True)])
def test_a_list_cell_is_read_into_at_most_a_thousand_parts(
    parts: int, kept: int, truncated: bool
) -> None:
    robots = "; ".join(f"AMR-{n}" for n in range(parts))
    base = _set_cell(BASES[FLEET], ("Envelope ID", "ENV-S007-04"), "Robots", robots)
    package = read_files(map_files(base, [preset("register_zone")]))
    envelope = _named(package, "authorisation_envelope", "ENV-S007-04")
    assert len(envelope.machines.value) == kept
    found = _findings(package, "list_truncated")
    assert bool(found) is truncated
    if truncated:
        (finding,) = found
        assert finding.records == (envelope.id,) and finding.details["field"] == "/machines"
        assert finding.details["limit"] == 1000
        # The text not read is cited: the cell from the 1,001st part on.
        (rest,) = finding.related
        assert robots[rest.locator[-1].start : rest.locator[-1].end].strip() == "AMR-1000"
        assert finding.subject.locator[-1].column_name == "Robots"


def test_repeated_ids_in_one_cell_are_one_finding_however_many() -> None:
    robots = "; ".join(["AMR-07"] * 5000)
    base = _set_cell(BASES[FLEET], ("Envelope ID", "ENV-S007-04"), "Robots", robots)
    files = map_files(base, [preset("register_zone")])
    package = read_files(files)
    assert _ids(_named(package, "authorisation_envelope", "ENV-S007-04").machines.value) == {
        "AMR-07"
    }
    (finding,) = _findings(package, "list_id_repeated")
    assert finding.details["count"] == 999  # the parts past the thousandth are not read
    assert len(finding.related) == 10 and finding.subject.locator[-1].column_name == "Robots"
    # The statement kept comes first, then the repeats.
    assert [ref.locator[-1].start for ref in finding.related[:2]] == [0, len("AMR-07; ")]
    assert len(files["records/ingest_finding.jsonl"]) < 64 * 1024


def _paragraphs(base: IngestPackage, texts: list[str]) -> IngestPackage:
    """``base`` with ``texts`` as more paragraphs of the first incident report, on a page of their
    own, each one LF after the last (the compiler's layout)."""
    from neptune.identity.provenance import evidence_record_id
    from neptune.model.provenance import Page

    transforms = {t.id: t for t in _of(base, "transform_record")}
    (site,) = [
        b for b in _of(base, "document_block") if b.text == Known("Site: S-007", b.text.provenance)
    ]
    transform = transforms[site.provenance.transform]
    extra, offset = [], 0
    for n, text in enumerate(texts):
        evidence = EvidenceRef(
            site.provenance.evidence.source, (Page(9), Span(offset, offset + len(text)))
        )
        offset += len(text) + 1
        extra.append(
            replace(
                site,
                id=evidence_record_id("document_block", evidence, transform),
                provenance=replace(site.provenance, evidence=evidence),
                order=10**7 + n,
                text=Known(text, site.text.provenance),
            )
        )
    return replace(base, records=(*base.records, *extra))


def _timed(make: Callable[[], Any]) -> float:
    import time

    start = time.perf_counter()
    make()
    return time.perf_counter() - start


@pytest.mark.slow
def test_a_document_of_many_labelled_paragraphs_maps_in_linear_time() -> None:
    """Before the gate its unread lines were matched against every block: 8,000 paragraphs took
    1.8 s and 16,000 took 6.9 s. Now 32,000 take about 2 s."""
    _, templates = _declared(FLEET)
    small = _paragraphs(BASES[FLEET], [f"Site: S-{n}" for n in range(4000)])
    large = _paragraphs(BASES[FLEET], [f"Site: S-{n}" for n in range(32000)])
    ratio = _timed(lambda: map_files(large, templates=templates)) / _timed(
        lambda: map_files(small, templates=templates)
    )
    assert ratio < 20  # 8 times the input; quadratic would be about 64
    package = read_files(map_files(large, templates=templates))
    incident = _named(package, "incident_record", "INC-0007")
    assert isinstance(incident.site, Unknown)  # a label said 32,001 ways is not one value
    assert _findings(package, "label_repeated")


def test_finding_a_tables_block_is_logarithmic(monkeypatch: pytest.MonkeyPatch) -> None:
    from neptune_deploy.lifecycle import documents

    view = documents._views(BASES[FLEET].records)
    pdf = next(v for v in view if v.record.format == "pdf")
    calls = 0
    real = documents._within

    def counted(inner: EvidenceRef, outer: EvidenceRef) -> bool:
        nonlocal calls
        calls += 1
        return real(inner, outer)

    monkeypatch.setattr(documents, "_within", counted)
    for block in pdf.blocks:
        assert pdf.block_at(block.provenance.evidence) is block
    assert calls == len(pdf.blocks)  # one candidate each, not a scan


# --- The plugin adapter still declines (MVL-200 probes it on every source) ------------------------


def test_the_lifecycle_adapter_declines_every_archetype_source() -> None:
    """MVL-200 (PR #83) makes ``neptune ingest`` probe every source with ``deploy_lifecycle``. It
    must claim none of the corpus, so installing Deploy changes no selection (ADR 0005 §8)."""
    from neptune.adapters.contract import ProbeHints
    from neptune_deploy.adapters.lifecycle import NO_READER, LifecycleAdapter

    adapter = LifecycleAdapter()
    sources = [p for p in sorted(A.SOURCES.rglob("*")) if p.is_file()]
    assert len(sources) > 30
    for path in sources:
        head = path.read_bytes()[:65536]
        result = adapter.probe(head, ProbeHints(path.name, path.stat().st_size))
        assert result.confidence == 0.0, path
        assert [reason.code for reason in result.reasons] == [NO_READER], path


def test_one_column_read_at_two_resolutions_is_two_clocks() -> None:
    """A date-only cell among date-times: each reading has its own clock and id (ADR 0005 §7)."""
    base = _set_cell(BASES[FLEET], ("WO Number", "WO-26-0302"), "Completed", "2026-03-02")
    package = _map(FLEET, base)
    domains = {d.id: d for d in _of(package, "timestamp_domain")}
    day = _named(package, "maintenance_event", "WO-26-0302").performed.value
    minute = _named(package, "maintenance_event", "WO-26-0301").performed.value
    assert day.domain_id != minute.domain_id
    assert domains[day.domain_id].resolution == Known(Fraction(86400))
    assert domains[minute.domain_id].resolution == Known(Fraction(1))
    assert domains[day.domain_id].field == domains[minute.domain_id].field == "Completed"
    # Both cite the column's first cell; the step after it says how that clock reads.
    first = [
        d.provenance.evidence.locator for d in (domains[day.domain_id], domains[minute.domain_id])
    ]
    assert first[0][:-1] == first[1][:-1] and first[0][-1] != first[1][-1]
