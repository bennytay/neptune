"""The Open-RMF connector over the recorded-shape logs of a warehouse AMR fleet (ADR 0010 §5).

JSON task logs, fleet states, dispatch records and a lane and zone map, plus the same kind of
history in the SQLite database the api-server writes. Everything is read from local files.
"""

import json
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.provenance import JsonPointer
from neptune.model.world import SpatialCategory
from neptune_deploy.sources.fleet_ops import (
    FleetOpsConfigError,
    OpenRmfSource,
    open_rmf_source,
)

RMF = Path(__file__).parent / "fixtures" / "fleet_ops" / "rmf"
FILES: dict[str, Any] = {
    "tasks": {"file": "tasks.json"},
    "fleet_states": {"file": "fleet_states.json"},
    "dispatches": {"file": "dispatches.json"},
    "map": {"file": "nav_graph.json"},
}


class Refuses:
    """A workspace that refuses the network: local files must not ask it."""

    def require_network(self, purpose: str) -> None:
        raise AssertionError(f"Open-RMF asked for the network: {purpose}")


def source(root: Path = RMF, **options: Any) -> OpenRmfSource:
    return open_rmf_source(
        root, network=Refuses(), options={"site": "warehouse-1", "files": FILES, **options}
    )


def codes(src: OpenRmfSource) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in src.findings():
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def test_each_part_is_a_document_with_a_stated_table_and_no_network_is_used() -> None:
    src = source()
    entries = list(src.walk())
    assert [e.part for e in entries] == ["dispatches", "fleet_states", "map", "tasks"]
    assert [e.location.object_id for e in entries] == [
        "warehouse-1/dispatches",
        "warehouse-1/fleet_states",
        "warehouse-1/map",
        "warehouse-1/tasks",
    ]
    catalog = src.catalog()
    for record in catalog.records:
        assert record.provenance.assertion_kind is AssertionKind.STATED
    names = {t.name.value for t in catalog.of("structured_table")}
    assert names == {f"deploy_open_rmf {p}" for p in ("dispatches", "fleet_states", "map", "tasks")}
    for entry in entries:
        assert src.open(entry.location).read().startswith(b'{"items":[')


def test_a_task_is_a_run_stated_by_its_item_with_its_robot_and_two_clocks() -> None:
    src = source()
    catalog = src.catalog()
    runs = catalog.of("run")
    assert [r.logical_id.value.value for r in runs] == [
        "delivery.dispatch-12",
        "patrol.dispatch-13",
    ]  # the third task states no id, so it declares no run
    first = runs[0]
    assert first.logical_id.value.namespace == "rmf.task"
    assert first.machine.value.namespace == "rmf.robot"
    assert first.machine.value.value == "tinyRobot/AMR-07"
    assert first.provenance.assertion_kind is AssertionKind.STATED
    # Times are the integers the log states, on a clock per field whose meaning is not assumed.
    assert first.first.value.ticks == 1772372400000
    assert first.last.value.ticks == 1772373300000
    assert first.first.value.domain_id != first.last.value.domain_id
    domains = {d.id: d for d in catalog.of("timestamp_domain")}
    start = domains[first.first.value.domain_id]
    assert start.field == "unix_millis_start_time"
    assert all(
        isinstance(getattr(start, name), Unknown)
        for name in ("role", "epoch", "timescale", "resolution")
    )
    pointer = first.logical_id.provenance.evidence.locator[-1]
    assert isinstance(pointer, JsonPointer) and pointer.pointer.endswith("/booking/id")
    skipped = codes(src)["record_skipped"]
    assert [(f.details["count"], f.details["reason"]) for f in skipped] == [(1, "id_invalid")]


def test_declared_clocks_are_recorded_and_change_the_ids() -> None:
    declared = source(clock={"epoch": "unix", "timescale": "posix", "resolution": "1/1000"})
    start = next(
        d for d in declared.catalog().of("timestamp_domain") if d.field == "unix_millis_start_time"
    )
    assert isinstance(start.epoch, Known) and start.epoch.value == "unix"
    assert start.resolution.value == Fraction(1, 1000)
    plain = next(
        d for d in source().catalog().of("timestamp_domain") if d.field == "unix_millis_start_time"
    )
    assert plain.id != start.id


def test_a_task_without_a_finish_has_an_unknown_last() -> None:
    root_tasks = [
        {"booking": {"id": "t1"}, "assigned_to": {"name": "AMR-01"}, "unix_millis_start_time": 5}
    ]
    runs = _runs_of(root_tasks)
    assert isinstance(runs[0].last, Unknown)
    assert runs[0].machine.value.value == "AMR-01"  # no group stated: the name alone


def _runs_of(tasks: list[dict[str, Any]]) -> list[Any]:
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "tasks.json").write_text(json.dumps(tasks))
        src = open_rmf_source(
            folder, options={"site": "s", "files": {"tasks": {"file": "tasks.json"}}}
        )
        return list(src.catalog().of("run"))


def test_the_map_is_one_spatial_record_per_level_in_the_frame_the_file_names() -> None:
    src = source()
    catalog = src.catalog()
    artifacts = catalog.of("spatial_artifact")
    assert sorted(a.name.value for a in artifacts) == ["L1", "L2"]
    (graph,) = catalog.of("frame_graph")
    frames = {f.ref.frame_id: f for f in catalog.of("frame")}
    assert set(frames) == {"L1", "L2"}
    for artifact in artifacts:
        assert artifact.category is SpatialCategory.VECTOR_MAP
        assert artifact.frame.value.frame_graph_id == graph.id
        assert artifact.frame.value.frame_id == artifact.name.value
        # No conversion, no projection: the unit and CRS are not covered and the axes unknown.
        assert isinstance(artifact.unit, NotCovered) and isinstance(artifact.crs, NotCovered)
        assert isinstance(frames[artifact.name.value].axes, Unknown)
        assert isinstance(frames[artifact.name.value].handedness, Unknown)
    # The geometry stays in the document: a level's lanes and zones are in its bytes, as stated.
    document = next(d for d in catalog.documents if d.ref.object_id.endswith("/map"))
    level: Any = next(i for i in document.items if i["level"] == "L1")
    assert level["data"]["lanes"][0]["speed_limit"] == 1.2
    assert level["coordinate_system"] == "cartesian_meters"
    assert codes(src)["map_keys_not_recorded"][0].details["keys"] == ["lifts"]


def test_a_map_with_no_level_names_builds_nothing_and_says_so() -> None:
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "m.json").write_text(json.dumps({"name": "x", "levels": {}}))
        src = open_rmf_source(folder, options={"site": "s", "files": {"map": {"file": "m.json"}}})
        assert src.catalog().records == ()
        assert list(src.walk()) == []


def test_tables_keep_every_value_as_stated() -> None:
    src = source()
    catalog = src.catalog()
    tables = {t.name.value: t for t in catalog.of("structured_table")}
    fleet = tables["deploy_open_rmf fleet_states"]
    rows = [r for r in catalog.of("structured_record") if r.table == fleet.id]
    header = fleet.header.value
    robots = [r.cells[header.index("robots")].value for r in rows]
    assert all(json.loads(text)["AMR-07"] for text in robots)  # a nested object is its sorted JSON
    batteries = {json.loads(text)["AMR-07"]["battery"] for text in robots}
    assert batteries == {0.58, 0.62}  # the numbers as stated, never converted to a percentage


def test_a_sqlite_database_is_read_read_only_with_declared_json_columns() -> None:
    files = {
        "tasks": {"file": "rmf_logs.db", "table": "task_state", "json_columns": ["data"]},
        "fleet_states": {"file": "rmf_logs.db", "table": "fleet_state", "json_columns": ["data"]},
    }
    src = open_rmf_source(
        RMF,
        options={
            "site": "warehouse-1",
            "files": files,
            "task_fields": {
                "id": "/data/booking/id",
                "group": "/data/assigned_to/group",
                "robot": "/data/assigned_to/name",
            },
        },
    )
    runs = src.catalog().of("run")
    assert [r.logical_id.value.value for r in runs] == ["delivery.dispatch-21", "loop.dispatch-22"]
    assert runs[0].machine.value.value == "tinyRobot/AMR-12"
    assert runs[0].last.value.ticks == 1772367300000
    assert isinstance(runs[1].last, Unknown)  # NULL in the database is a blank, not a fact
    # Without the declaration the column stays the text the database holds.
    plain = open_rmf_source(
        RMF,
        options={"site": "w", "files": {"tasks": {"file": "rmf_logs.db", "table": "task_state"}}},
    )
    table = plain.catalog().of("structured_table")[0]
    row = plain.catalog().of("structured_record")[0]
    cell = row.cells[table.header.value.index("data")]
    assert isinstance(cell.value, str) and cell.value.startswith('{"assigned_to"')


def test_the_database_is_never_changed_and_never_written_beside() -> None:
    before = (RMF / "rmf_logs.db").read_bytes()
    before_files = sorted(p.name for p in RMF.iterdir())
    open_rmf_source(
        RMF,
        options={"site": "w", "files": {"tasks": {"file": "rmf_logs.db", "table": "task_state"}}},
    ).catalog()
    assert (RMF / "rmf_logs.db").read_bytes() == before
    assert sorted(p.name for p in RMF.iterdir()) == before_files


def test_two_runs_and_a_copy_in_another_place_are_byte_identical(tmp_path: Path) -> None:
    shutil.copytree(RMF, tmp_path / "elsewhere")
    runs = []
    for root in (RMF, RMF, tmp_path / "elsewhere"):
        src = source(root)
        runs.append(
            (
                [(e.location, src.open(e.location).read()) for e in src.walk()],
                [json.dumps(r.to_json(), sort_keys=True) for r in src.catalog().records],
                [f.id for f in src.findings()],
            )
        )
    assert (
        runs[0] == runs[1] == runs[2]
    )  # the directory is where bytes were read, not what they are


def test_file_order_in_the_options_does_not_matter() -> None:
    reordered = dict(reversed(list(FILES.items())))
    assert [e.location for e in source().walk()] == [
        e.location for e in source(files=reordered).walk()
    ]


def test_every_record_round_trips_the_compilers_strict_readers() -> None:
    for record in source().catalog().records:
        assert RECORD_KINDS[record.kind][1](record.to_json()) == record


def test_a_second_run_against_a_ledger_reads_only_what_changed(tmp_path: Path) -> None:
    shutil.copytree(RMF, tmp_path / "logs")
    ledger = SourceLedger()
    first = open_rmf_source(tmp_path / "logs", options={"site": "w", "files": FILES}, ledger=ledger)
    assert len(list(first.walk())) == 4  # nothing observed yet: every document is new
    for entry in first.listing():
        ledger.observe(
            entry.location, digest_stream(first.open(entry.location), chunk_size=1024 * 1024)
        )
    again = open_rmf_source(tmp_path / "logs", options={"site": "w", "files": FILES}, ledger=ledger)
    assert list(again.walk()) == []  # unchanged documents are never fetched
    tasks = json.loads((tmp_path / "logs" / "tasks.json").read_text())
    tasks[0]["status"] = "cancelled"
    (tmp_path / "logs" / "tasks.json").write_text(json.dumps(tasks))
    changed = open_rmf_source(
        tmp_path / "logs", options={"site": "w", "files": FILES}, ledger=ledger
    )
    assert [e.part for e in changed.walk()] == ["tasks"]
    assert [e.part for e in changed.discover(ledger).changed] == ["tasks"]
    assert len(changed.discover(ledger).unchanged) == 3


def test_options_are_closed_and_declared() -> None:
    bad: list[dict[str, Any]] = [
        {"files": FILES},  # no site
        {"site": "x/y", "files": FILES},
        {"site": "x", "files": {}},
        {"site": "x", "files": {"tasks": {"file": "a.json", "nonsense": 1}}},
        {"site": "x", "files": {"flights": {"file": "a.json"}}},
        {"site": "x", "files": FILES, "extra": 1},
        {"site": "x", "files": FILES, "task_fields": {"id": "booking/id"}},
        {"site": "x", "files": FILES, "task_fields": {"nope": "/a"}},
        {"site": "x", "files": FILES, "clock": {"resolution": "0"}},
        {"site": "x", "files": FILES, "max_rows": 0},
    ]
    for options in bad:
        with pytest.raises(FleetOpsConfigError):
            open_rmf_source(RMF, options=options)
    with pytest.raises(FleetOpsConfigError):
        open_rmf_source(RMF / "missing", options={"site": "x", "files": FILES})
    with pytest.raises(FleetOpsConfigError):
        open_rmf_source(RMF, options={"site": "x", "files": FILES}, credentials={"a": "b"})


def test_the_transform_holds_the_declaration_and_no_path() -> None:
    config = json.dumps(source().transform.config)
    assert str(RMF) not in config and "warehouse-1" in config
    assert "tasks.json" in config  # the declared relative file is what decided the records
