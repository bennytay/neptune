"""The Demo v1 lifecycle files of the acceptance corpus, through shipped presets only (ADR 0016).

``fixtures/demo_corpus/package`` is the compiler's package of five corpus files, committed by
``make_demo_corpus.py`` (these tests never ingest: root ``test_merge_freshness``). With the shipped
``incident_report`` template, ``requalification_csv`` preset and ``cmms_generic`` preset, every
time on both incident reports reads, both requalification sheets map and PLANT-2's INSP work order
is a maintenance event. A time of day without a date is never completed from another field, and
nothing is moved to UTC.
"""

import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.model.knowledge import Known, NotCovered, Unknown
from neptune.model.provenance import EvidenceRef, Provenance, Span
from neptune.store.package import IngestPackage, read_files, read_package
from neptune_deploy.lifecycle import (
    PRESETS,
    TEMPLATE_PRESET_DIR,
    TEMPLATE_PRESETS,
    LifecycleMapping,
    MappingError,
    map_files,
    map_package,
    preset,
    template_preset,
)
from neptune_deploy.lifecycle.cli import main

HERE: Final = Path(__file__).parent / "fixtures" / "demo_corpus"
PACKAGE: Final = HERE / "package"
LOCK: Final = Path(__file__).parents[3] / "harness" / "acceptance" / "corpus.lock.json"
CELL_REPORT: Final = "sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf"
FLEET_REPORT: Final = "sites/S-007/incidents/INC-0007.pdf"
CELL_REQUAL: Final = "sites/PLANT-2/cell3/requalification/requalification_tests.csv"
FLEET_REQUAL: Final = "sites/S-007/requalification/requalification_tests.csv"
CELL_CMMS: Final = "sites/PLANT-2/cmms/work_orders.csv"
HMI_TIMES: Final = (
    "2026-09-14 14:28:00",
    "2026-09-14 14:32:38",
    "2026-09-14 14:32:41",
    "2026-09-14 14:33:30",
    "2026-09-14 14:52:00",
)


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_demo_corpus", HERE / "make_demo_corpus.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


G: Final = _generator()
BASE: Final = read_package(PACKAGE)


def _mappings() -> list[LifecycleMapping]:
    return [preset("cmms_generic"), preset("requalification_csv")]


def _mapped(base: IngestPackage = BASE) -> IngestPackage:
    return read_files(map_files(base, _mappings(), [template_preset("incident_report")]))


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _codes(package: IngestPackage) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in _of(package, "ingest_finding"):
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def _paths(package: IngestPackage) -> dict[Any, str]:
    """Each source's content id to its path in the corpus."""
    return {r.content_id: r.location.path for r in _of(package, "source_revision")}


def _from(package: IngestPackage, kind: str, path: str) -> list[Any]:
    paths = _paths(package)
    return [r for r in _of(package, kind) if paths[r.provenance.evidence.source] == path]


def _incident(package: IngestPackage, path: str = CELL_REPORT) -> Any:
    (incident,) = _from(package, "incident_record", path)
    return incident


def _cited(state: Any) -> EvidenceRef:
    """What a value cites: its own provenance, never one it inherits."""
    assert isinstance(state.provenance, Provenance)
    return state.provenance.evidence


def _civil(text: str, pattern: str = "%Y-%m-%d %H:%M:%S") -> int:
    """Seconds from 1970-01-01T00:00:00 of the text's own civil clock, never UTC."""
    return int((datetime.strptime(text, pattern) - datetime(1970, 1, 1)).total_seconds())


def _with_cells(base: IngestPackage, edit: Callable[[str], str | None]) -> IngestPackage:
    """The base with table cells' text replaced by ``edit`` (``None``: unchanged), each cited span
    moved with it, as the compiler would cite it. Not re-verified: the edits stand in for a
    document the corpus does not hold."""

    def cell(state: Any) -> Any:
        if not isinstance(state, Known) or not isinstance(state.value, str):
            return state
        new = edit(state.value)
        if new is None:
            return state
        if not isinstance(state.provenance, Provenance):  # a CSV cell inherits its row's citation
            return Known(new, state.provenance)
        evidence = state.provenance.evidence
        span = evidence.locator[-1]
        assert isinstance(span, Span)
        moved = EvidenceRef(
            evidence.source, (*evidence.locator[:-1], Span(span.start, span.start + len(new)))
        )
        return Known(new, replace(state.provenance, evidence=moved))

    def change(record: Any) -> Any:
        if record.kind != "structured_record":
            return record
        return replace(record, cells=tuple(cell(c) for c in record.cells))

    return replace(BASE, records=tuple(change(r) for r in BASE.records))


def _swap(table: dict[str, str]) -> Callable[[str], str | None]:
    return table.get


# --- The fixture is the corpus ------------------------------------------------------------------


def test_each_source_is_the_acceptance_corpus_file_byte_for_byte() -> None:
    # On failure the corpus changed: run make_demo_corpus.py and explain the diff in the PR.
    lock = json.loads(LOCK.read_text(encoding="utf-8"))["files"]
    paths = {path: cid for cid, path in _paths(BASE).items() if path != "neptune.yaml"}
    assert sorted(paths) == sorted(G.FILES)
    for path, cid in paths.items():
        assert str(cid) == lock[path]["sha256"], path


def test_the_generator_ingests_with_no_plugins_and_in_process(tmp_path: Path) -> None:
    command = G.ingest_command(tmp_path / "in", tmp_path / "out", tmp_path / "work")
    assert "--no-plugins" in command
    assert command[command.index("--isolation") + 1] == "in_process"


# --- Item 1: the incident report template --------------------------------------------------------


def test_every_hmi_time_of_the_arm_cell_report_reads_on_its_own_civil_clock() -> None:
    package = _mapped()
    incident = _incident(package)
    times = [entry.time for entry in incident.timeline.value]
    assert all(isinstance(t, Known) for t in times)
    assert [t.value.ticks for t in times] == [_civil(text) for text in HMI_TIMES]
    assert incident.occurred.value.ticks == _civil("2026-09-14 14:32", "%Y-%m-%d %H:%M")
    assert "value_unreadable" not in _codes(package)
    # One clock for the timeline, to the second, civil: no timescale, no zone, never UTC.
    domains = {d.id: d for d in _of(package, "timestamp_domain")}
    (clock_id,) = {t.value.domain_id for t in times}
    clock = domains[clock_id]
    assert clock.field == "Time" and clock.resolution.value == 1
    assert isinstance(clock.timescale, Unknown)
    (zone,) = [z for z in _of(package, "civil_time_zone") if z.domain == clock_id]
    assert isinstance(zone.zone, Unknown)
    assert domains[incident.occurred.value.domain_id].field == "Occurred at"


def test_each_timeline_value_cites_its_own_cell_and_is_stated() -> None:
    incident = _incident(_mapped())
    cells = {
        c.value: _cited(c)
        for r in _from(BASE, "structured_record", CELL_REPORT)
        for c in r.cells
        if isinstance(c, Known)
    }
    for entry, text in zip(incident.timeline.value, HMI_TIMES, strict=True):
        assert entry.time.provenance.evidence == cells[text]
        assert entry.time.provenance.assertion_kind.value == "stated"


def test_the_template_reads_the_same_form_at_both_sites_and_leaves_nothing_unread() -> None:
    package = _mapped()
    fleet = _incident(package, FLEET_REPORT)
    fleet_times = [e.time.value.ticks for e in fleet.timeline.value]
    assert fleet_times == [
        _civil(f"2026-04-02 {hm}", "%Y-%m-%d %H:%M") for hm in ("14:05", "14:07", "14:09", "14:31")
    ]
    matched = _codes(package)["template_matched"]
    assert len(matched) == 2
    assert {f.details["template"] for f in matched} == {"incident.report"}
    codes = _codes(package)
    assert not {"text_unread", "document_unmatched", "label_absent"} & set(codes), codes.keys()
    cell = _incident(package)
    assert [i.value.value for i in cell.identifiers.value] == ["INC-C3-0011"]
    assert [m.value.value for m in cell.machines.value] == ["ARM-3A"]
    assert cell.description.value.startswith("During the pick at P1")


# --- Malformed and boundary times: never completed, never moved ----------------------------------


def test_a_garbled_hmi_time_is_unknown_and_its_neighbours_still_read() -> None:
    package = _mapped(_with_cells(BASE, _swap({HMI_TIMES[1]: "2026-09-14 14:3?:38"})))
    times = [e.time for e in _incident(package).timeline.value]
    assert isinstance(times[1], Unknown)
    assert all(isinstance(t, Known) for i, t in enumerate(times) if i != 1)
    (finding,) = _codes(package)["value_unreadable"]
    assert finding.details["reference"] == "Time"


def test_a_time_of_day_without_a_date_is_never_completed_from_the_incident_date() -> None:
    # The report states 2026-09-14 in "Occurred at"; the row does not, so it stays Unknown.
    package = _mapped(_with_cells(BASE, _swap({HMI_TIMES[0]: "14:28:00"})))
    incident = _incident(package)
    first = incident.timeline.value[0].time
    assert isinstance(first, Unknown)
    assert isinstance(incident.occurred, Known)
    (finding,) = _codes(package)["value_unreadable"]
    assert finding.details["field"] == "/timeline/0/time"
    assert finding.subject == _cited(first)


def _relabel(old: str, new: str) -> IngestPackage:
    """The arm-cell report with one block's text replaced (the same length, so its span holds)."""
    assert len(old) == len(new)
    source = next(cid for cid, path in _paths(BASE).items() if path == CELL_REPORT)

    def change(record: Any) -> Any:
        if record.kind != "document_block" or record.provenance.evidence.source != source:
            return record
        if record.text == Known(old, record.text.provenance):
            return replace(record, text=Known(new, record.text.provenance))
        return record

    return replace(BASE, records=tuple(change(r) for r in BASE.records))


def test_a_blank_incident_date_is_unknown_and_the_rows_keep_only_the_dates_they_state() -> None:
    package = _mapped(_relabel("Occurred at: 2026-09-14 14:32", "Occurred at:                 "))
    incident = _incident(package)
    assert isinstance(incident.occurred, Unknown)
    assert all(isinstance(e.time, Known) for e in incident.timeline.value)
    (blank,) = [f for f in _codes(package)["value_blank"] if "reference" in f.details]
    assert blank.details["reference"] == "Occurred at"
    assert blank.subject == _cited(incident.occurred)


def test_a_report_missing_its_date_field_is_unmatched_and_never_given_a_date() -> None:
    # "Occurred at" is structure the template requires: without it the report is not this form.
    package = _mapped(_relabel("Occurred at: 2026-09-14 14:32", "Reported on: 2026-09-14 14:32"))
    assert not _from(package, "incident_record", CELL_REPORT)
    assert len(_from(package, "incident_record", FLEET_REPORT)) == 1
    paths = _paths(BASE)
    unmatched = _codes(package)["document_unmatched"]
    assert [paths[f.subject.source] for f in unmatched] == [CELL_REPORT]


def test_rows_across_midnight_read_the_dates_they_state() -> None:
    swaps = {HMI_TIMES[3]: "2026-09-14 23:59:58", HMI_TIMES[4]: "2026-09-15 00:00:03"}
    times = [e.time for e in _incident(_mapped(_with_cells(BASE, _swap(swaps)))).timeline.value]
    assert times[4].value.ticks - times[3].value.ticks == 5


def test_time_only_rows_across_midnight_get_no_inferred_date_change() -> None:
    # 23:59:58 then 00:00:03: a reader may suspect the next day; the mapper never decides it.
    swaps = {HMI_TIMES[3]: "23:59:58", HMI_TIMES[4]: "00:00:03"}
    package = _mapped(_with_cells(BASE, _swap(swaps)))
    times = [e.time for e in _incident(package).timeline.value]
    assert isinstance(times[3], Unknown) and isinstance(times[4], Unknown)
    unreadable = _codes(package)["value_unreadable"]
    assert sorted(f.details["field"] for f in unreadable) == [
        "/timeline/3/time",
        "/timeline/4/time",
    ]


# --- Item 2: the requalification preset ----------------------------------------------------------


def test_both_requalification_sheets_map_with_their_place_column() -> None:
    package = _mapped()
    cell = _from(package, "requalification_record", CELL_REQUAL)
    fleet = _from(package, "requalification_record", FLEET_REQUAL)
    assert len(cell) == 3 and len(fleet) == 1
    assert {r.site.value.namespace for r in cell} == {"requalification.cell"}
    assert {r.site.value.value for r in cell} == {"CELL-3"}
    assert fleet[0].site.value.namespace == "requalification.site"
    assert "table_unmapped" not in _codes(package)


def test_requalification_values_are_verbatim_and_a_blank_decision_time_is_unknown() -> None:
    package = _mapped()
    by_id = {
        r.identifiers.value[0].value.value: r
        for r in _from(package, "requalification_record", CELL_REQUAL)
    }
    late = by_id["RQ-2026-006"]
    decision = late.return_to_service
    assert decision.decision.value == "Returned to service with speed restriction"
    assert isinstance(decision.time, Unknown)
    assert [t.name.value for t in late.tests.value] == [
        "TCP accuracy check",
        "Pick-and-place cycle 50x",
    ]
    assert late.result.value == "PASS"
    (blank,) = _codes(package)["value_blank"]
    assert blank.details["column"] == "Decided On" and blank.details["rows"] == [3]


# --- Item 3: the INSP work order -----------------------------------------------------------------


def test_the_insp_work_order_is_a_maintenance_event_and_no_cmms_row_is_left_unmatched() -> None:
    package = _mapped()
    events = {
        r.identifiers.value[0].value.value: r
        for r in _from(package, "maintenance_event", CELL_CMMS)
    }
    assert len(events) == len(_from(BASE, "structured_record", CELL_CMMS)) == 10
    inspection = events["WO-26-0709"]
    assert inspection.diagnosis.value == "Review after near miss"
    assert [a.value for a in inspection.actions.value] == [
        "Review of light curtain muting",
        "no fault found",
    ]
    assert inspection.performed.value.ticks == _civil("2026-07-09 15:00", "%Y-%m-%d %H:%M")
    assert "row_unmatched" not in _codes(package)


def test_a_work_order_type_the_preset_does_not_name_is_still_a_finding() -> None:
    package = _mapped(_with_cells(BASE, _swap({"INSP": "AUDIT"})))
    (unmatched,) = _codes(package)["row_unmatched"]
    assert unmatched.details["count"] == 1
    assert "WO-26-0709" not in {
        r.identifiers.value[0].value.value for r in _from(package, "maintenance_event", CELL_CMMS)
    }


# --- Determinism and the command line ------------------------------------------------------------


def test_mapping_is_byte_identical_across_runs_and_entry_points(tmp_path: Path) -> None:
    templates = [template_preset("incident_report")]
    first = map_files(BASE, _mappings(), templates)
    assert map_files(BASE, list(reversed(_mappings())), templates) == first
    one = map_package(PACKAGE, _mappings(), tmp_path / "one", templates)
    two = map_package(PACKAGE, _mappings(), tmp_path / "two", templates)
    assert one == two
    for relative, data in first.items():
        assert (tmp_path / "one" / relative).read_bytes() == data, relative


def test_the_command_line_takes_shipped_templates_by_name(tmp_path: Path) -> None:
    out = tmp_path / "out"
    argv = ["map", str(PACKAGE), "-p", "requalification_csv", "-T", "incident_report"]
    assert main([*argv, "-o", str(out)]) == 0
    package = read_package(out)
    assert len(_of(package, "incident_record")) == 2
    assert len(_of(package, "requalification_record")) == 4
    # A shipped template named twice, or also loaded from its file, is one template.
    again = [
        *argv,
        "-T",
        "incident_report",
        "-t",
        str(TEMPLATE_PRESET_DIR),
        "-o",
        str(tmp_path / "y"),
    ]
    assert main(again) == 0
    assert read_package(tmp_path / "y").id == package.id
    with pytest.raises(SystemExit):
        main(["map", str(PACKAGE), "-T", "no_such_template", "-o", str(tmp_path / "x")])


def test_shipped_presets_and_templates_are_listed_and_unknown_names_are_refused() -> None:
    assert "requalification_csv" in PRESETS
    assert TEMPLATE_PRESETS == ("incident_report",)
    assert template_preset("incident_report").kind.kind == "incident_record"
    with pytest.raises(MappingError, match="no template preset"):
        template_preset("incident_amr")


def test_no_lifecycle_time_is_an_instant_or_carries_a_zone() -> None:
    package = _mapped()
    assert all(isinstance(d.timescale, Unknown) for d in _of(package, "timestamp_domain"))
    assert all(isinstance(z.zone, Unknown | NotCovered) for z in _of(package, "civil_time_zone"))
