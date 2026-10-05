"""Declared civil zones, list states and the streamed package write (ADR 0012).

Real small fixtures: the compiler-written packages under ``fixtures/lifecycle/packages``, mapped
with shipped presets and one declared mapping file, then read back through the compiler's reader.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from neptune.model.knowledge import (
    INHERITED,
    AssertionKind,
    Known,
    KnownAbsent,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import Provenance, RowCell
from neptune.store.package import IngestPackage, package_files, read_files, read_package
from neptune_deploy import PACKAGE_SCHEMA_VERSION
from neptune_deploy.lifecycle import (
    LifecycleMapping,
    MappingError,
    load_mapping,
    map_files,
    map_package,
    parse_mapping,
    preset,
)
from neptune_deploy.lifecycle.run import iter_records, map_records

FIXTURES = Path(__file__).parent / "fixtures" / "lifecycle"
REQUALIFICATION = FIXTURES / "mappings" / "cell3_requalification.json"


def _base(name: str) -> IngestPackage:
    return read_package(FIXTURES / "packages" / name)


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _mapped(base: IngestPackage, *mappings: LifecycleMapping) -> IngestPackage:
    return read_files(map_files(base, list(mappings)))


def _mapping(zone: str | None, columns: dict[str, Any] | None = None) -> LifecycleMapping:
    """A one-rule mapping over the work-order export; ``columns`` replaces its list fields."""
    document: dict[str, Any] = {
        "schema": "neptune-deploy.lifecycle-mapping/1",
        "id": "test.zones",
        "version": "1",
        "rules": [
            {
                "id": "work_order",
                "kind": "maintenance_event",
                "requires": ["WO Number"],
                "ignore": ["*"],
                "fields": {
                    "identifiers": [{"column": "WO Number", "namespace": "cmms.work_order"}],
                    "performed": {"column": "Completed", "format": "%Y-%m-%d %H:%M"},
                    **(columns or {}),
                },
            }
        ],
    }
    if zone is not None:
        document["zone"] = zone
    return parse_mapping(json.dumps(document).encode())


# --- Civil time zones ---------------------------------------------------------------------------


def test_a_declared_zone_is_a_civil_time_zone_record_and_nothing_converts() -> None:
    package = _mapped(_base("warehouse_amr"), _mapping("Europe/Berlin"))
    (domain,) = _of(package, "timestamp_domain")
    (zone,) = _of(package, "civil_time_zone")
    assert zone.domain == domain.id
    assert zone.zone == Known("Europe/Berlin")
    # Stated by the mapper's transform, citing the clock's own place; the config holds the zone.
    assert zone.provenance.assertion_kind is AssertionKind.STATED
    assert zone.provenance.evidence == domain.provenance.evidence
    (transform,) = [
        t for t in _of(package, "transform_record") if t.id == zone.provenance.transform
    ]
    assert transform.config["mapping"]["zone"] == "Europe/Berlin"
    # The ticks still count the civil clock: 2026-03-02 09:40, never moved to UTC.
    event = next(e for e in _of(package, "maintenance_event") if isinstance(e.performed, Known))
    assert event.performed.value.domain_id == domain.id
    assert event.performed.value.ticks % 86400 in range(24 * 3600)
    assert isinstance(domain.timescale, Unknown)


def test_an_unstated_zone_is_an_unknown_zone_never_a_guess() -> None:
    package = _mapped(_base("warehouse_amr"), preset("cmms_generic"))
    zones = _of(package, "civil_time_zone")
    assert zones and all(isinstance(z.zone, Unknown) for z in zones)
    domains = {d.id for d in _of(package, "timestamp_domain")}
    assert {z.domain for z in zones} <= domains


def test_each_declared_zone_gets_its_own_companion_per_civil_clock() -> None:
    package = _mapped(_base("manipulator_cell"), load_mapping(REQUALIFICATION))
    zones = _of(package, "civil_time_zone")
    domains = {d.id: d for d in _of(package, "timestamp_domain")}
    assert {z.zone.value for z in zones} == {"Europe/Berlin"}
    assert len(zones) == len({z.domain for z in zones}) == len(domains)
    assert len({z.id for z in zones}) == len(zones)


def test_an_instant_has_no_civil_zone() -> None:
    package = _mapped(_base("warehouse_amr"), preset("jira_json"))
    instants = [d for d in _of(package, "timestamp_domain") if isinstance(d.timescale, Known)]
    assert instants  # the tickets state offsets
    assert not _of(package, "civil_time_zone")


@pytest.mark.parametrize("zone", ["+01:00", "W. Europe Standard Time", "Europe/", "-05", "a b"])
def test_a_zone_that_is_not_an_iana_name_is_refused_at_load(zone: str) -> None:
    with pytest.raises(MappingError, match="zone"):
        _mapping(zone)


def test_a_zone_is_required_and_unstated_is_the_way_to_say_none() -> None:
    with pytest.raises(MappingError, match="civil zone"):
        _mapping(None)
    assert _mapping("unstated").rules


# --- List states --------------------------------------------------------------------------------


def _blank(base: IngestPackage, column: str, absent: bool = False) -> IngestPackage:
    """``base`` with ``column`` Unknown in every data row, or ``KnownAbsent`` citing its cell."""
    table = next(
        t
        for t in _of(base, "structured_table")
        if isinstance(t.header, Known) and column in t.header.value
    )
    index = table.header.value.index(column)
    records = []
    for record in base.records:
        if record.kind == "structured_record" and record.table == table.id:
            cells = list(record.cells)
            was = Provenance(
                record.cell_evidence(table, index),
                record.provenance.transform,
                AssertionKind.OBSERVED,
            )
            cells[index] = KnownAbsent(was) if absent else Unknown(was)
            record = replace(record, cells=tuple(cells))
        records.append(record)
    return read_files(package_files(records))


RELATED = {"related": [{"column": "Related", "namespace": "cmms.reference", "split": ";"}]}


def test_a_blank_cell_is_an_unknown_list_citing_the_cell_with_no_finding() -> None:
    base = _blank(_base("warehouse_amr"), "Related")
    package = _mapped(base, _mapping("unstated", RELATED))
    events = _of(package, "maintenance_event")
    assert events
    for event in events:
        assert isinstance(event.related, Unknown)
        assert isinstance(event.related.provenance, Provenance)
        assert event.related.provenance.assertion_kind is AssertionKind.STATED
        cell = event.related.provenance.evidence.locator[-1]
        assert isinstance(cell, RowCell) and cell.column_name == "Related"
    codes = {f.code.split(".")[-1] for f in _of(package, "ingest_finding")}
    assert "list_cell_blank" not in codes


def test_an_unread_list_is_not_covered_and_a_stated_one_keeps_the_bare_array() -> None:
    package = _mapped(_base("warehouse_amr"), _mapping("unstated", RELATED))
    for event in _of(package, "maintenance_event"):
        assert isinstance(event.machines, NotCovered)  # no rule reads it
        assert isinstance(event.actions, NotCovered)
    stated = [e for e in _of(package, "maintenance_event") if isinstance(e.related, Known)]
    assert stated
    assert all(e.related.provenance is not None for e in stated)
    assert all(e.related.provenance is INHERITED for e in stated)  # the version 4 bare array


def test_a_column_missing_from_the_export_leaves_its_list_not_covered() -> None:
    columns = {"related": [{"column": "No Such Column", "namespace": "x", "split": ";"}]}
    package = _mapped(_base("warehouse_amr"), _mapping("unstated", columns))
    assert all(isinstance(e.related, NotCovered) for e in _of(package, "maintenance_event"))
    codes = {f.code.split(".")[-1] for f in _of(package, "ingest_finding")}
    assert "column_absent" in codes


def test_a_cell_the_source_states_absent_is_a_cited_empty_list() -> None:
    base = _blank(_base("warehouse_amr"), "Related", absent=True)
    package = _mapped(base, _mapping("unstated", RELATED))
    for event in _of(package, "maintenance_event"):
        assert isinstance(event.related, Known) and event.related.value == ()
        assert isinstance(event.related.provenance, Provenance)


def test_a_blank_cell_beside_a_cell_that_states_items_keeps_the_items_and_a_finding() -> None:
    columns = {
        "related": [
            {"column": "Related", "namespace": "cmms.reference", "split": ";"},
            {"column": "Removed Serial", "namespace": "serial"},
        ]
    }
    package = _mapped(_base("warehouse_amr"), _mapping("unstated", columns))
    both = [e for e in _of(package, "maintenance_event") if len(_items(e.related)) == 2]
    assert both  # a row where both cells state something
    one = [e for e in _of(package, "maintenance_event") if len(_items(e.related)) == 1]
    assert one  # a row where the Related cell is blank: the serial alone, and a finding says so
    blanks = [f for f in _of(package, "ingest_finding") if f.code.endswith(".list_cell_blank")]
    assert blanks
    assert {r for f in blanks for r in f.records} <= {
        e.id for e in _of(package, "maintenance_event") if isinstance(e.related, Known)
    }


def _items(state: Any) -> tuple[Any, ...]:
    return state.value if isinstance(state, Known) else ()


def test_parts_that_are_all_blank_are_an_unknown_list() -> None:
    package = _mapped(_base("warehouse_amr"), preset("cmms_generic"))
    flash = [e for e in _of(package, "maintenance_event") if isinstance(e.parts, Unknown)]
    assert flash
    assert all(isinstance(e.parts.provenance, Provenance) for e in flash)


def test_a_version_6_record_is_written_for_a_list_state_and_reads_back() -> None:
    package = _mapped(_base("warehouse_amr"), _mapping("unstated", RELATED))
    assert package.manifest.version == PACKAGE_SCHEMA_VERSION


# --- The streamed write -------------------------------------------------------------------------


def test_the_streamed_package_is_byte_identical_to_the_in_memory_one(tmp_path: Path) -> None:
    base_root = FIXTURES / "packages" / "warehouse_amr"
    mappings = [preset("cmms_generic"), preset("jira_json"), preset("register_zone")]
    memory = map_files(read_package(base_root), mappings)
    package_id = map_package(base_root, mappings, tmp_path / "out")
    written = {
        p.relative_to(tmp_path / "out").as_posix(): p.read_bytes()
        for p in sorted((tmp_path / "out").rglob("*"))
        if p.is_file()
    }
    assert written == memory
    assert read_package(tmp_path / "out").id == package_id


def test_streamed_output_is_deterministic_and_leaves_no_scratch(tmp_path: Path) -> None:
    base_root = FIXTURES / "packages" / "manipulator_cell"
    mappings = [preset("servicenow_csv"), load_mapping(REQUALIFICATION)]
    first = map_package(base_root, mappings, tmp_path / "a")
    second = map_package(base_root, list(reversed(mappings)), tmp_path / "b")
    assert first == second
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b"]  # the spill dir is gone


def test_a_given_scratch_directory_is_used_and_emptied(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    map_package(
        FIXTURES / "packages" / "inspection_quadruped",
        [preset("cmms_maximo")],
        tmp_path / "out",
        scratch=scratch,
    )
    assert scratch.is_dir() and not any(scratch.iterdir())


def test_a_mapping_error_leaves_the_output_empty(tmp_path: Path) -> None:
    base_root = FIXTURES / "packages" / "inspection_quadruped"
    out = tmp_path / "out"
    with pytest.raises(MappingError):
        map_package(base_root, [], out)
    twice = [preset("cmms_maximo"), preset("cmms_maximo")]
    with pytest.raises(MappingError):
        map_package(base_root, twice, out)
    assert not out.exists() or not any(out.iterdir())


def test_records_are_yielded_lazily_and_equal_the_listed_ones() -> None:
    base = _base("warehouse_amr")
    mappings = [preset("cmms_generic")]
    stream = iter_records(base, mappings)
    first = next(stream)  # the first record exists before the rest are mapped
    rest = list(stream)
    assert [first, *rest] == map_records(base, mappings)
    assert map_records(base, mappings) == map_records(base, mappings)
