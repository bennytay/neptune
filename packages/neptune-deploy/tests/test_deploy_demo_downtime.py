"""PLANT-2's CMMS downtime log through the shipped ``cmms_downtime`` preset (ADR 0016 §7).

Each stop is an ``intervention``: ``Stop Type`` its mode, ``Stopped`` / ``Restarted`` its start and
end on the log's own civil clock, the zone unstated, nothing moved to UTC. The INC-C3-0011 stop
reads 14:33:10, as the operator entered it; the controller's syslog says 14:32:38. Deploy states
both as written and never reconciles them.
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

from neptune.model.knowledge import Known, NotCovered, Unknown
from neptune.store.package import IngestPackage, read_files, read_package
from neptune_deploy.lifecycle import PRESETS, map_files, map_package, preset

HERE: Final = Path(__file__).parent / "fixtures" / "demo_corpus"
LOCK: Final = Path(__file__).parents[3] / "harness" / "acceptance" / "corpus.lock.json"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_demo_corpus_downtime", HERE / "make_demo_corpus.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


G: Final = _generator()
BASE: Final = read_package(G.DOWNTIME_PACKAGE)


def _mapped(base: IngestPackage = BASE) -> IngestPackage:
    return read_files(map_files(base, [preset("cmms_downtime")]))


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _codes(package: IngestPackage) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in _of(package, "ingest_finding"):
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def _stops(package: IngestPackage) -> dict[str, Any]:
    return {r.identifiers.value[0].value.value: r for r in _of(package, "intervention")}


def _civil(text: str) -> int:
    """Seconds from 1970-01-01T00:00:00 of the log's own civil clock, never UTC."""
    return int(
        (datetime.strptime(text, "%Y-%m-%d %H:%M:%S") - datetime(1970, 1, 1)).total_seconds()
    )


def _with_cells(edit: Callable[[str], str | None]) -> IngestPackage:
    """The base with CSV cells' text replaced (a CSV cell inherits its row's citation); ``""``
    makes the cell blank."""

    def cell(state: Any) -> Any:
        if not isinstance(state, Known) or not isinstance(state.value, str):
            return state
        new = edit(state.value)
        if new == "":  # a blank cell, as the compiler states one
            return Unknown(state.provenance)
        return state if new is None else Known(new, state.provenance)

    def change(record: Any) -> Any:
        if record.kind != "structured_record":
            return record
        return replace(record, cells=tuple(cell(c) for c in record.cells))

    return replace(BASE, records=tuple(change(r) for r in BASE.records))


# --- The fixture is the corpus's file ------------------------------------------------------------


def test_the_source_is_corpus_2_downtime_log_byte_for_byte() -> None:
    (revision,) = [r for r in _of(BASE, "source_revision") if r.location.path == G.DOWNTIME_PATH]
    assert str(revision.content_id) == G.CORPUS_2
    lock = json.loads(LOCK.read_text(encoding="utf-8"))["files"]
    if G.DOWNTIME_PATH in lock:  # once corpus 2.0.0 is on main, its lock binds the fixture
        assert lock[G.DOWNTIME_PATH]["sha256"] == G.CORPUS_2


# --- Unit: every stop, as stated -----------------------------------------------------------------


def test_every_stop_is_an_intervention_with_its_stated_times_and_mode() -> None:
    package = _mapped()
    stops = _stops(package)
    assert sorted(stops) == ["DT-26-0709-01", "DT-26-0910-01", "DT-26-0914-01"]
    incident = stops["DT-26-0914-01"]
    assert incident.mode.value == "Protective stop"
    assert incident.reason.value == "Collision at pick P1; E-stop at OP-2"
    assert incident.start.value.ticks == _civil("2026-09-14 14:33:10")
    assert isinstance(incident.end, Unknown)  # blank Restarted: Unknown, never "still stopped"
    assert sorted(r.value.value for r in incident.related.value) == ["INC-C3-0011", "WO-26-0915"]
    assert [m.value.value for m in incident.machines.value] == ["ARM-3A"]
    assert incident.site.value.value == "PLANT-2"
    assert isinstance(incident.authority, NotCovered) and isinstance(incident.outcome, NotCovered)
    planned = stops["DT-26-0910-01"]
    assert planned.mode.value == "Planned"
    assert planned.end.value.ticks - planned.start.value.ticks == 9000
    assert not {"row_unmatched", "column_unmapped", "value_unreadable"} & set(_codes(package))


def test_the_clock_is_civil_with_no_zone_and_each_value_cites_its_cell() -> None:
    package = _mapped()
    domains = {d.id: d for d in _of(package, "timestamp_domain")}
    stop = _stops(package)["DT-26-0709-01"]
    clock = domains[stop.start.value.domain_id]
    assert clock.field == "Stopped" and isinstance(clock.timescale, Unknown)
    (zone,) = [z for z in _of(package, "civil_time_zone") if z.domain == clock.id]
    assert isinstance(zone.zone, Unknown)
    assert stop.provenance.assertion_kind.value == "stated"
    assert stop.provenance.evidence.locator[-1].row == 1


def test_the_preset_is_shipped() -> None:
    assert "cmms_downtime" in PRESETS
    assert {rule.kind.kind for rule in preset("cmms_downtime").rules} == {"intervention"}


# --- Malformed and boundary ----------------------------------------------------------------------


def test_a_garbled_stop_time_is_unknown_with_a_finding_and_the_other_rows_read() -> None:
    package = _mapped(_with_cells({"2026-09-14 14:33:10": "2026-09-14 14:3x:10"}.get))
    stops = _stops(package)
    assert isinstance(stops["DT-26-0914-01"].start, Unknown)
    assert isinstance(stops["DT-26-0709-01"].start, Known)
    (finding,) = _codes(package)["value_unreadable"]
    assert finding.details["column"] == "Stopped" and finding.details["rows"] == [3]


def test_a_missing_stop_time_is_unknown_and_a_finding() -> None:
    package = _mapped(_with_cells({"2026-09-14 14:33:10": ""}.get))
    assert isinstance(_stops(package)["DT-26-0914-01"].start, Unknown)
    (blank,) = _codes(package)["value_blank"]
    assert blank.details["column"] == "Stopped"


def test_a_stop_across_midnight_reads_the_dates_it_states() -> None:
    swaps = {
        "2026-07-09 14:22:00": "2026-07-09 23:58:00",
        "2026-07-09 14:40:00": "2026-07-10 00:04:00",
    }
    stop = _stops(_mapped(_with_cells(swaps.get)))["DT-26-0709-01"]
    assert stop.end.value.ticks - stop.start.value.ticks == 360


def test_a_time_only_restart_after_midnight_is_never_given_the_next_day() -> None:
    # 23:58 then "00:04:00": a reader may suspect the 10th; the mapper never decides it.
    swaps = {"2026-07-09 14:22:00": "2026-07-09 23:58:00", "2026-07-09 14:40:00": "00:04:00"}
    package = _mapped(_with_cells(swaps.get))
    stop = _stops(package)["DT-26-0709-01"]
    assert isinstance(stop.start, Known) and isinstance(stop.end, Unknown)
    (finding,) = _codes(package)["value_unreadable"]
    assert finding.details["column"] == "Restarted" and finding.details["rows"] == [1]


# --- Determinism ---------------------------------------------------------------------------------


def test_mapping_the_downtime_log_is_byte_identical(tmp_path: Path) -> None:
    first = map_files(BASE, [preset("cmms_downtime")])
    assert map_files(BASE, [preset("cmms_downtime")]) == first
    map_package(G.DOWNTIME_PACKAGE, [preset("cmms_downtime")], tmp_path / "out")
    for relative, data in first.items():
        assert (tmp_path / "out" / relative).read_bytes() == data, relative
