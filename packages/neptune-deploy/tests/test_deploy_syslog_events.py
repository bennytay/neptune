"""PLANT-2's syslog export as a typed event table, and a caller's zone per source (ADR 0017).

``syslog_csv`` writes ``syslog events``: every column verbatim and cited, ``Timestamp.sec`` and
``Timestamp.nanosec`` as integers on the log's own civil clock, ``@clock:Timestamp`` naming that
clock and ``@id:syslog`` the row's ``Seq``. The zone is the caller's (``--source-zone``) or
Unknown; nothing is moved to UTC. INC-C3-0011's protective stop is row 4182 at 14:32:38.
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

from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import Provenance, RowCell
from neptune.store.package import IngestPackage, read_files, read_package
from neptune_deploy import eventlogs
from neptune_deploy.lifecycle import MappingError, map_files, map_package, preset, presets
from neptune_deploy.lifecycle.cli import main

HERE: Final = Path(__file__).parent / "fixtures" / "demo_corpus"
LOCK: Final = Path(__file__).parents[3] / "harness" / "acceptance" / "corpus.lock.json"
ZONE: Final = "America/New_York"
HEADER: Final = (
    "Seq",
    "Host",
    "Facility",
    "Severity",
    "Tag",
    "MsgID",
    "Message",
    "Timestamp",
    "Timestamp.sec",
    "Timestamp.nanosec",
    "@clock:Timestamp",
    "@id:syslog",
)


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_demo_corpus_syslog", HERE / "make_demo_corpus.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


G: Final = _generator()
BASE: Final = read_package(G.SYSLOG_PACKAGE)
DOWNTIME: Final = read_package(G.DOWNTIME_PACKAGE)


def _syslog(zone: str | None = None) -> Any:
    mapping = eventlogs.preset("syslog_csv")
    return mapping if zone is None else mapping.with_source_zones({G.SYSLOG_PATH: zone})


def _mapped(base: IngestPackage = BASE, zone: str | None = None) -> IngestPackage:
    return read_files(map_files(base, event_logs=[_syslog(zone)]))


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _codes(package: IngestPackage) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in _of(package, "ingest_finding"):
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def _typed(package: IngestPackage) -> tuple[Any, dict[str, Any]]:
    """The typed table, and its rows by ``Seq``."""
    (table,) = [t for t in _of(package, "structured_table") if t.name.value == "syslog events"]
    rows = sorted(
        (r for r in _of(package, "structured_record") if r.table == table.id),
        key=lambda r: r.row,
    )
    return table, {r.cells[0].value: r for r in rows}


def _cell(row: Any, column: str) -> Any:
    return row.cells[HEADER.index(column)]


def _civil(text: str) -> int:
    """Seconds from 1970-01-01T00:00:00 of the log's own civil clock, never UTC."""
    seconds = (datetime.strptime(text, "%Y-%m-%d %H:%M:%S") - datetime(1970, 1, 1)).total_seconds()
    return int(seconds)


def _with_cells(edit: Callable[[str], str | None]) -> IngestPackage:
    """The base with CSV cells' text replaced; ``""`` makes the cell blank."""

    def cell(state: Any) -> Any:
        if not isinstance(state, Known) or not isinstance(state.value, str):
            return state
        new = edit(state.value)
        if new == "":
            return Unknown(state.provenance)
        return state if new is None else Known(new, state.provenance)

    def change(record: Any) -> Any:
        if record.kind != "structured_record":
            return record
        return replace(record, cells=tuple(cell(c) for c in record.cells))

    return replace(BASE, records=tuple(change(r) for r in BASE.records))


# --- The fixture is the corpus's file ------------------------------------------------------------


def test_the_source_is_corpus_2_1_syslog_byte_for_byte() -> None:
    (revision,) = [r for r in _of(BASE, "source_revision") if r.location.path == G.SYSLOG_PATH]
    assert str(revision.content_id) == G.CORPUS_2_1
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if tuple(map(int, lock["version"].split("."))) >= (2, 1, 0):  # binds once 2.1.0 is on main
        assert lock["files"][G.SYSLOG_PATH]["sha256"] == G.CORPUS_2_1


# --- Unit: the typed table -----------------------------------------------------------------------


def test_every_row_is_typed_on_the_logs_own_civil_clock() -> None:
    package = _mapped()
    table, rows = _typed(package)
    assert table.header.value == HEADER
    assert sorted(rows) == ["4170", "4182", "4183", "4186"]
    expected = {
        "4170": ("PGM_START", "ARM-3A", "2026-09-14 14:28:00"),
        "4182": ("PSTOP", "ARM-3A", "2026-09-14 14:32:38"),
        "4183": ("ESTOP", "PLC-C3", "2026-09-14 14:32:41"),
        "4186": ("LOTO", "PLC-C3", "2026-09-14 14:33:30"),
    }
    domains = {d.id: d for d in _of(package, "timestamp_domain")}
    for seq, (msgid, host, text) in expected.items():
        row = rows[seq]
        assert _cell(row, "MsgID").value == msgid and _cell(row, "Host").value == host
        assert _cell(row, "Timestamp").value == text
        assert _cell(row, "Timestamp.sec").value == _civil(text)
        assert _cell(row, "Timestamp.nanosec").value == 0
        assert _cell(row, "@id:syslog").value == seq
        clock = domains[_cell(row, "@clock:Timestamp").value]
        assert clock.field == "Timestamp" and clock.resolution.value == 1  # seconds, as stated
        assert isinstance(clock.timescale, Unknown)  # civil: never UTC
    assert not _codes(package)


def test_every_cell_cites_its_source_cell_and_is_stated() -> None:
    _, rows = _typed(_mapped())
    row = rows["4182"]
    for column in HEADER:
        provenance = _cell(row, column).provenance
        assert isinstance(provenance, Provenance)
        assert provenance.assertion_kind.value == "stated"
        (place,) = provenance.evidence.locator
        assert isinstance(place, RowCell)
        source = {"Timestamp.sec": "Timestamp", "Timestamp.nanosec": "Timestamp"}
        source |= {"@clock:Timestamp": "Timestamp", "@id:syslog": "Seq"}
        assert place.column_name == source.get(column, column)


def test_the_log_is_not_also_a_lifecycle_tables_unmapped_finding() -> None:
    package = read_files(map_files(BASE, [preset("cmms_generic")], event_logs=[_syslog()]))
    assert "table_unmapped" not in _codes(package)
    assert len(_typed(package)[1]) == 4


# --- A caller's zone per source (ADR 0017 §2) ----------------------------------------------------


def _zone_of(package: IngestPackage, adapter: str) -> Any:
    transforms = {t.id: t for t in _of(package, "transform_record")}
    (zone,) = [
        z
        for z in _of(package, "civil_time_zone")
        if transforms[z.provenance.transform].adapter_id == adapter
    ]
    return zone, transforms[zone.provenance.transform]


def test_a_declared_source_zone_is_a_stated_zone_citing_that_source_from_its_transform() -> None:
    package = _mapped(zone=ZONE)
    zone, transform = _zone_of(package, eventlogs.MAPPER_ID)
    assert zone.zone == Known(ZONE, zone.zone.provenance)
    assert str(zone.provenance.evidence.source) == G.CORPUS_2_1
    assert zone.provenance.assertion_kind.value == "stated"
    assert transform.config["civil_time_zones"] == {G.SYSLOG_PATH: ZONE}
    # The ticks still count the civil clock: the zone is recorded, never applied.
    _, rows = _typed(package)
    assert _cell(rows["4182"], "Timestamp.sec").value == _civil("2026-09-14 14:32:38")


def test_without_a_source_zone_the_zone_is_unknown_and_the_lineage_differs() -> None:
    plain, zoned = _mapped(), _mapped(zone=ZONE)
    zone, transform = _zone_of(plain, eventlogs.MAPPER_ID)
    assert isinstance(zone.zone, Unknown)
    assert "civil_time_zones" not in transform.config
    assert _zone_of(zoned, eventlogs.MAPPER_ID)[1].id != transform.id


def test_cmms_downtime_takes_a_source_zone_without_a_preset_edit() -> None:
    path = G.DOWNTIME_PATH
    (mapping,), _ = presets(["cmms_downtime"], {("cmms_downtime", path): ZONE})
    package = read_files(map_files(DOWNTIME, [mapping]))
    zones = _of(package, "civil_time_zone")
    assert zones and all(z.zone.value == ZONE for z in zones)
    downtime = next(
        r.content_id for r in _of(DOWNTIME, "source_revision") if r.location.path == path
    )
    assert all(z.provenance.evidence.source == downtime for z in zones)
    (transform,) = [
        t for t in _of(package, "transform_record") if t.adapter_id == "deploy_lifecycle_map"
    ]
    assert transform.config["civil_time_zones"] == {path: ZONE}
    plain = read_files(map_files(DOWNTIME, [preset("cmms_downtime")]))
    assert all(isinstance(z.zone, Unknown) for z in _of(plain, "civil_time_zone"))


@pytest.mark.parametrize("zone", ["+01:00", "W. Europe Standard Time", "local", "Unstated", ""])
def test_a_zone_that_is_not_a_zone_is_refused(zone: str) -> None:
    with pytest.raises(MappingError):
        _syslog(zone)


def test_a_source_zone_for_a_source_or_preset_the_run_lacks_is_refused(tmp_path: Path) -> None:
    mapping = eventlogs.preset("syslog_csv").with_source_zones({"sites/nowhere.csv": ZONE})
    with pytest.raises(MappingError, match="no source"):
        map_package(G.SYSLOG_PACKAGE, [], tmp_path / "out", event_logs=[mapping])
    assert not (tmp_path / "out").exists()
    with pytest.raises(MappingError, match="does not map"):
        presets(["syslog_csv"], {("cmms_downtime", G.DOWNTIME_PATH): ZONE})


def test_the_command_line_takes_source_zones_and_refuses_unused_ones(tmp_path: Path) -> None:
    base = str(G.SYSLOG_PACKAGE)
    zone = ["--source-zone", "syslog_csv", G.SYSLOG_PATH, ZONE]
    assert main(["map", base, "-p", "syslog_csv", *zone, "-o", str(tmp_path / "a")]) == 0
    package = read_package(tmp_path / "a")
    assert _zone_of(package, eventlogs.MAPPER_ID)[0].zone.value == ZONE
    other = ["--source-zone", "cmms_downtime", G.SYSLOG_PATH, ZONE]
    assert main(["map", base, "-p", "syslog_csv", *other, "-o", str(tmp_path / "b")]) == 2
    missing = ["--source-zone", "syslog_csv", "sites/none.csv", ZONE]
    assert main(["map", base, "-p", "syslog_csv", *missing, "-o", str(tmp_path / "c")]) == 2
    bad = ["--source-zone", "syslog_csv", G.SYSLOG_PATH, "local"]
    assert main(["map", base, "-p", "syslog_csv", *bad, "-o", str(tmp_path / "d")]) == 2


# --- Malformed and boundary ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("2026-09-14 14:3?:38", "value_unreadable"),
        ("14:32:38", "value_unreadable"),
        ("", "value_blank"),
    ],
)
def test_a_bad_timestamp_is_unknown_with_a_finding_and_the_row_is_kept(
    text: str, code: str
) -> None:
    package = _mapped(_with_cells({"2026-09-14 14:32:38": text}.get))
    _, rows = _typed(package)
    assert len(rows) == 4
    stop = rows["4182"]
    for column in ("Timestamp.sec", "Timestamp.nanosec", "@clock:Timestamp"):
        assert isinstance(_cell(stop, column), Unknown)
    assert _cell(stop, "MsgID").value == "PSTOP"
    (finding,) = _codes(package)[code]
    assert finding.details["column"] == "Timestamp" and finding.details["rows"] == [2]
    assert isinstance(_cell(rows["4183"], "Timestamp.sec"), Known)


def test_rows_across_midnight_read_the_dates_they_state() -> None:
    swaps = {
        "2026-09-14 14:32:41": "2026-09-14 23:59:59",
        "2026-09-14 14:33:30": "2026-09-15 00:00:01",
    }
    _, rows = _typed(_mapped(_with_cells(swaps.get)))
    after, before = rows["4186"], rows["4183"]
    assert _cell(after, "Timestamp.sec").value - _cell(before, "Timestamp.sec").value == 2


def test_a_repeated_seq_keeps_both_rows_and_a_repeated_msgid_is_no_finding() -> None:
    package = _mapped(_with_cells({"4183": "4182", "ESTOP": "PSTOP"}.get))
    table, _ = _typed(package)
    rows = [r for r in _of(package, "structured_record") if r.table == table.id]
    assert len(rows) == 4
    assert [_cell(r, "@id:syslog").value for r in rows].count("4182") == 2
    (repeated,) = _codes(package)["identifier_repeated"]
    assert repeated.details["identifier"] == "4182" and len(repeated.related) == 1
    assert set(_codes(package)) == {"identifier_repeated"}


# --- The mapping file ----------------------------------------------------------------------------


def test_a_malformed_event_log_mapping_is_refused() -> None:
    good = json.loads((eventlogs.PRESET_DIR / "syslog_csv.json").read_bytes())
    for broken in (
        {**good, "time": {"column": "Timestamp", "format": "%H:%M:%S", "zone": "unstated"}},
        {**good, "time": {"column": "Timestamp", "format": "%Y-%m-%d"}},
        {**good, "columns": ["Seq", "Seq"]},
        {**good, "columns": ["Timestamp"]},
        {**good, "extra": 1},
        {**good, "schema": "neptune-deploy.event-log-mapping/2"},
    ):
        with pytest.raises(MappingError):
            eventlogs.parse_mapping(json.dumps(broken).encode())
    with pytest.raises(MappingError):
        eventlogs.parse_mapping(b'{"id": 1, "id": 2}')


# --- Determinism ---------------------------------------------------------------------------------


def test_mapping_the_syslog_is_byte_identical(tmp_path: Path) -> None:
    first = map_files(BASE, event_logs=[_syslog(ZONE)])
    assert map_files(BASE, event_logs=[_syslog(ZONE)]) == first
    map_package(G.SYSLOG_PACKAGE, [], tmp_path / "out", event_logs=[_syslog(ZONE)])
    for relative, data in first.items():
        assert (tmp_path / "out" / relative).read_bytes() == data, relative
