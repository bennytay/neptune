"""Open-RMF logs are hostile input: paths, symlinks, databases and JSON (ADR 0010 §5).

One corrupt file is a finding about that part; the other parts are read as if it were not there.
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Unknown
from neptune_deploy.sources.fleet_ops import FleetOpsConfigError, OpenRmfSource, open_rmf_source
from neptune_deploy.sources.fleet_ops.rmf_files import _authorizer, json_items

GOOD_TASKS = [
    {
        "booking": {"id": "t1"},
        "assigned_to": {"group": "g", "name": "r"},
        "unix_millis_start_time": 1,
    }
]


def src(root: Path, files: dict[str, Any], **options: Any) -> OpenRmfSource:
    return open_rmf_source(root, options={"site": "s", "files": files, **options})


def codes(source: OpenRmfSource) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in source.findings():
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def write(root: Path, name: str, data: bytes | str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def causes(source: OpenRmfSource) -> dict[str, str]:
    return {f.details["part"]: f.details["cause"] for f in codes(source).get("file_refused", [])}


def test_paths_that_leave_the_directory_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    write(root, "tasks.json", json.dumps(GOOD_TASKS))
    write(tmp_path, "outside.json", json.dumps(GOOD_TASKS))
    for name in ("../outside.json", "/etc/passwd", "a//b.json", "./tasks.json", "x\\y.json"):
        source = src(root, {"tasks": {"file": name}})
        assert list(source.walk()) == []
        assert causes(source) == {"tasks": "path_invalid"}, name
    with pytest.raises(FleetOpsConfigError):  # a NUL never reaches the file system
        src(root, {"tasks": {"file": "a\x00b"}})


def test_symlinks_are_not_followed_to_a_file_or_through_a_directory(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    write(root, "real.json", json.dumps(GOOD_TASKS))
    write(tmp_path, "secret.json", json.dumps(GOOD_TASKS))
    (root / "link.json").symlink_to(tmp_path / "secret.json")
    (root / "dir").symlink_to(tmp_path)
    assert causes(src(root, {"tasks": {"file": "link.json"}})) == {"tasks": "symlink_refused"}
    assert causes(src(root, {"tasks": {"file": "dir/secret.json"}})) == {"tasks": "symlink_refused"}
    assert [e.part for e in src(root, {"tasks": {"file": "real.json"}}).walk()] == ["tasks"]


def test_a_directory_and_a_fifo_are_not_regular_files(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    (root / "d.json").mkdir(parents=True)
    os.mkfifo(root / "p.json")  # opening it for reading would wait for a writer forever
    assert causes(src(root, {"tasks": {"file": "d.json"}})) == {"tasks": "not_regular_file"}
    assert causes(src(root, {"tasks": {"file": "p.json"}})) == {"tasks": "not_regular_file"}
    assert causes(src(root, {"tasks": {"file": "missing.json"}})) == {"tasks": "file_missing"}


def test_a_file_over_its_limit_is_refused_before_it_is_read(tmp_path: Path) -> None:
    write(tmp_path, "tasks.json", json.dumps(GOOD_TASKS) + " " * 4096)
    source = src(tmp_path, {"tasks": {"file": "tasks.json"}}, max_file_bytes=2048)
    assert list(source.walk()) == [] and causes(source) == {"tasks": "file_too_large"}


@pytest.mark.parametrize(
    "text",
    [
        '[{"a": 1, "a": 2}]',  # a repeated key has two readings
        '[{"a": NaN}]',
        '[{"a": 1e999}]',
        '[{"a": 1}',
        "\xef\xbb\xbf[]x",
        '["not an object"]',
        "[" * 5000 + "]" * 5000,
        "",
    ],
)
def test_malformed_json_is_a_finding_and_never_an_exception(tmp_path: Path, text: str) -> None:
    write(tmp_path, "tasks.json", text)
    write(tmp_path, "dispatches.json", '[{"task_id": "ok"}]')
    source = src(
        tmp_path, {"tasks": {"file": "tasks.json"}, "dispatches": {"file": "dispatches.json"}}
    )
    assert [e.part for e in source.walk()] == ["dispatches"]  # the other part is read
    found = codes(source)
    assert set(found) & {"part_invalid", "part_empty", "cells_not_recorded"}, "a finding says why"
    assert source.catalog().of("run") == ()


def test_json_lines_are_read_and_a_damaged_line_fails_that_file(tmp_path: Path) -> None:
    write(tmp_path, "tasks.json", '{"booking": {"id": "a"}}\n\n{"booking": {"id": "b"}}\n')
    ok = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    assert [r.logical_id.value.value for r in ok.catalog().of("run")] == ["a", "b"]
    # A log cut short by a crash keeps every line before the cut, and says it was cut.
    write(
        tmp_path, "tasks.json", '{"booking": {"id": "a"}}\n{"booking": {"id": "b"}}\n{"booking"\n'
    )
    cut = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    assert [r.logical_id.value.value for r in cut.catalog().of("run")] == ["a", "b"]
    (invalid,) = codes(cut)["part_invalid"]
    assert (invalid.details["part"], invalid.details["records"]) == ("tasks", 2)
    write(tmp_path, "tasks.json", '{"booking"\n')
    bad = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    assert list(bad.walk()) == [] and codes(bad)["part_invalid"][0].details["records"] == 0


def test_entries_that_are_not_objects_are_counted_and_dropped(tmp_path: Path) -> None:
    write(tmp_path, "tasks.json", json.dumps([*GOOD_TASKS, 7, "x", [1]]))
    source = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    assert len(source.catalog().of("run")) == 1
    assert codes(source)["cells_not_recorded"][0].details["not_object"] == 3


def test_the_row_limit_stops_a_part_where_it_stands(tmp_path: Path) -> None:
    tasks = [{"booking": {"id": f"t{i}"}} for i in range(10)]
    write(tmp_path, "tasks.json", json.dumps(tasks))
    source = src(tmp_path, {"tasks": {"file": "tasks.json"}}, max_rows=4)
    assert len(source.catalog().of("run")) == 4
    limit = codes(source)["part_limit"][0]
    assert (limit.details["cause"], limit.details["records"]) == ("row_limit", 4)
    rows = json_items(b"\n".join(b'{"a": 1}' for _ in range(100)), 3)
    assert len(rows.items) == 3 and rows.stopped == "row_limit"


def test_values_that_cannot_be_stored_are_unknown_and_counted(tmp_path: Path) -> None:
    tasks = [
        {"booking": {"id": "t1"}, "note": "\ud800", "big": 10**30, "unix_millis_start_time": 10**30}
    ]
    write(tmp_path, "tasks.json", json.dumps(tasks))
    source = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    catalog = source.catalog()
    (run,) = catalog.of("run")
    assert isinstance(run.first, Unknown)  # an instant no 64-bit clock holds
    assert codes(source)["value_unrepresentable"][0].details["reason"] == "lone_surrogate"
    for record in catalog.records:  # and everything still round-trips the strict readers
        assert RECORD_KINDS[record.kind][1](record.to_json()) == record


def test_hostile_keys_do_not_break_the_table(tmp_path: Path) -> None:
    tasks = [{"booking": {"id": "t1"}, "\ud800key": 1, "": 2, "k/with~chars": 3, "@clock:x": 4}]
    write(tmp_path, "tasks.json", json.dumps(tasks))
    source = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    for record in source.catalog().records:
        assert RECORD_KINDS[record.kind][1](record.to_json()) == record


def test_task_ids_and_robots_that_are_not_usable_are_not_runs(tmp_path: Path) -> None:
    tasks = [
        {"booking": {"id": "x" * 300}},
        {"booking": {"id": 7}},
        {"booking": {"id": ""}},
        {"booking": {"id": "ok"}, "assigned_to": {"name": 5}},
    ]
    write(tmp_path, "tasks.json", json.dumps(tasks))
    source = src(tmp_path, {"tasks": {"file": "tasks.json"}})
    runs = source.catalog().of("run")
    assert [r.logical_id.value.value for r in runs] == ["ok"]
    assert isinstance(runs[0].machine, Unknown)  # a robot that is not text is not a name
    assert codes(source)["record_skipped"][0].details["count"] == 3


# --- SQLite -------------------------------------------------------------------------------------


def database(root: Path, name: str = "rmf.db") -> sqlite3.Connection:
    path = root / name
    path.unlink(missing_ok=True)
    return sqlite3.connect(path)


def sql(root: Path, **options: Any) -> OpenRmfSource:
    files = {"tasks": {"file": "rmf.db", "table": "task_state", "json_columns": ["data"]}}
    return src(root, files, **options)


def test_a_database_with_blobs_infinities_and_bad_json_keeps_what_it_can(tmp_path: Path) -> None:
    db = database(tmp_path)
    db.execute("CREATE TABLE task_state(id_ TEXT, data TEXT, raw BLOB, score REAL)")
    db.execute(
        "INSERT INTO task_state VALUES ('a', '{\"booking\": {\"id\": \"a\"}}', x'00ff', 9e999)"
    )
    db.execute("INSERT INTO task_state VALUES ('b', 'not json', NULL, 1.5)")
    db.commit()
    db.close()
    source = sql(tmp_path, task_fields={"id": "/data/booking/id"})
    assert [r.logical_id.value.value for r in source.catalog().of("run")] == ["a"]
    counts = codes(source)["cells_not_recorded"][0].details
    assert (counts["blob"], counts["non_finite"], counts["json_column_invalid"]) == (1, 1, 1)


def test_a_table_name_is_matched_not_spliced(tmp_path: Path) -> None:
    db = database(tmp_path)
    db.execute("CREATE TABLE task_state(id_ TEXT)")
    db.execute("CREATE TABLE victim(x TEXT)")
    db.commit()
    db.close()
    for name in ('task_state"; DROP TABLE victim; --', "task_state; DROP TABLE victim", "nope"):
        source = src(tmp_path, {"tasks": {"file": "rmf.db", "table": name}})
        assert list(source.walk()) == [] and causes(source) == {"tasks": "table_missing"}
    check = sqlite3.connect(tmp_path / "rmf.db")
    assert check.execute("SELECT count(*) FROM sqlite_master WHERE name='victim'").fetchone() == (
        1,
    )


def test_a_view_that_never_ends_is_stopped_by_work_not_by_the_clock(tmp_path: Path) -> None:
    db = database(tmp_path)
    db.execute(
        "CREATE VIEW task_state AS WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c)"
        " SELECT x AS id_ FROM c WHERE x < 0"
    )
    db.commit()
    db.close()
    source = sql(tmp_path)
    assert list(source.walk()) == []
    assert codes(source)["part_limit"][0].details["cause"] == "work_limit"


def test_a_view_that_yields_forever_stops_at_the_row_limit(tmp_path: Path) -> None:
    db = database(tmp_path)
    db.execute(
        "CREATE VIEW task_state AS WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c)"
        " SELECT x AS id_ FROM c"
    )
    db.commit()
    db.close()
    source = sql(tmp_path, max_rows=50)
    document = source.catalog().documents[0]
    assert len(document.items) == 50
    assert codes(source)["part_limit"][0].details["cause"] == "row_limit"


def test_something_that_is_not_a_database_or_is_cut_short_is_a_finding(tmp_path: Path) -> None:
    write(tmp_path, "rmf.db", b"hello, this is not a database")
    assert causes(sql(tmp_path)) == {"tasks": "not_sqlite"}
    db = database(tmp_path)
    db.execute("CREATE TABLE task_state(id_ TEXT, data TEXT)")
    for number in range(400):
        db.execute("INSERT INTO task_state VALUES (?, ?)", (f"t{number}", "x" * 200))
    db.commit()
    db.close()
    whole = (tmp_path / "rmf.db").read_bytes()
    write(tmp_path, "rmf.db", whole[: len(whole) // 2])  # a truncated copy
    source = sql(tmp_path)
    assert list(source.walk()) == []  # a finding, never an exception
    assert codes(source)


def test_a_database_swapped_for_another_file_while_it_is_read_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from neptune_deploy.sources.fleet_ops import rmf_files

    for name in ("rmf.db", "other.db"):
        db = database(tmp_path, name)
        db.execute("CREATE TABLE task_state(id_ TEXT)")
        db.execute("INSERT INTO task_state VALUES ('a')")
        db.commit()
        db.close()
    real = rmf_files._query

    def swap(*args: Any) -> Any:
        rows = real(*args)
        (tmp_path / "rmf.db").unlink()
        (tmp_path / "rmf.db").symlink_to(tmp_path / "other.db")  # after the check, before the end
        return rows

    monkeypatch.setattr(rmf_files, "_query", swap)
    source = sql(tmp_path)
    assert list(source.walk()) == [] and causes(source) == {"tasks": "file_changed"}


def test_a_map_level_name_stated_twice_builds_no_frame_and_a_bad_map_is_a_finding(
    tmp_path: Path,
) -> None:
    maps = [
        {"name": "a", "levels": {"L1": {"lanes": [1]}, "L2": {"lanes": []}}},
        {"name": "b", "levels": {"L1": {"lanes": [2]}}},
        {"name": "c", "levels": [1, 2]},
        7,
    ]
    write(tmp_path, "maps.json", "\n".join(json.dumps(m) for m in maps))
    source = src(tmp_path, {"map": {"file": "maps.json"}})
    catalog = source.catalog()
    assert [a.name.value for a in catalog.of("spatial_artifact")] == ["L2"]  # L1 is two frames
    reasons = sorted(f.details["reason"] for f in codes(source)["record_skipped"])
    assert reasons == ["level_invalid_or_repeated", "map_has_no_levels_object"]
    assert len(catalog.documents[0].items) == 3  # the document keeps all three levels as stated


def test_the_authoriser_allows_reading_and_nothing_else() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(x)")
    db.set_authorizer(_authorizer)
    db.execute("SELECT x FROM t").fetchall()
    for statement in (
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA writable_schema = ON",
        "CREATE TABLE u(y)",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(statement)


def test_the_database_cannot_be_written_through_the_uri(tmp_path: Path) -> None:
    db = database(tmp_path)
    db.execute("CREATE TABLE task_state(id_ TEXT)")
    db.execute("INSERT INTO task_state VALUES ('a')")
    db.commit()
    db.close()
    path = tmp_path / "rmf.db"
    path.chmod(0o444)
    before = path.read_bytes()
    sql(tmp_path).catalog()
    assert path.read_bytes() == before
    read_only = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        read_only.execute("INSERT INTO task_state VALUES ('b')")
