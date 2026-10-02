"""Every record kind of every worked example, indexed by registration (MVL-91, ADR 0008).

Counts match each manifest's ``tables``; bodies, pointer lists and projection columns are checked
against oracles written here from the package bytes; indexing is a pure function of the package
and the Ledger version, whatever order packages are registered in. ``resolve`` cases beyond the
contract suite close the file.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

from conftest import new_database
from neptune.identity import canonical_json
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Known, NotCovered
from neptune_ledger.api.types import EvidenceAnchor
from neptune_ledger.catalog import registry
from neptune_ledger.catalog.index import (
    ambiguous_pointers,
    package_rows,
    projection_columns,
    unknown_pointers,
)
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.contract_tests.examples import (
    EXAMPLES,
    WorkedPackage,
    evidence_anchor,
    materialise,
    record_key,
    reparse,
    with_changed_body,
    write,
)
from test_ledger_registration import TX_COLUMNS, dump, fresh

Conn = psycopg.Connection[tuple[object, ...]]
AMBIGUOUS_TRANSFORM: Final = (
    "rec:sha256:e2c144c52c3f1c398b3a8f14738901a89d68bd7853dce97e558c112e596044f6"
)
MACHINE_CITERS: Final = ("calibration", "hardware_configuration", "run", "software_configuration")
# Every column a registration-order change may move: the registration key and transaction time.
ORDER_COLUMNS: Final = (*TX_COLUMNS, "tx_seq", "last_seq", "registration_key")


@pytest.fixture
def packages(tmp_path: Path) -> dict[str, WorkedPackage]:
    return {name: materialise(name, tmp_path / name) for name in EXAMPLES}


@pytest.fixture
def indexed(pg_uri: str, packages: dict[str, WorkedPackage]) -> Iterator[Conn]:
    with fresh(pg_uri) as catalog:
        for name in EXAMPLES:
            assert catalog.register(packages[name].root).outcome == "registered"
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("SET search_path TO tenant_acme")
        yield conn


# --- oracles, from the package bytes ----------------------------------------------------------


def _states(value: Any, state: str, pointer: str = "") -> list[str]:
    """Pointers of every object whose ``knowledge`` is ``state``, not inside an Ambiguous one,
    and not inside the two free-form fields."""
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("knowledge") == state:
            found.append(pointer)
        if value.get("knowledge") == "ambiguous":
            return found
        for key, item in value.items():
            if pointer == "" and key in ("config", "details"):
                continue
            token = key.replace("~", "~0").replace("/", "~1")
            found += _states(item, state, f"{pointer}/{token}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _states(item, state, f"{pointer}/{index}")
    return found


def _expected_projection(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    """Package schema 1's hot filters, written out by hand (ADR 0008 §3's table)."""
    out: dict[str, Any] = dict.fromkeys(projection_columns())

    def logical(name: str, field: dict[str, Any]) -> None:
        if field["knowledge"] == "known":
            out[f"{name}_namespace"] = field["value"]["namespace"]
            out[f"{name}_value"] = field["value"]["value"]

    if kind in MACHINE_CITERS:
        logical("machine", record["machine"])
    if kind == "asset":
        logical("site", record["site"])
    if kind == "stream":
        out["run_ids"] = [record["run"]]
        out["clock_ids"] = list(record["clocks"])
    if kind == "video":
        out["clock_ids"] = [record["clock"]]
    return out


# --- every kind of every example ---------------------------------------------------------------


def test_counts_match_every_manifests_tables(
    indexed: Conn, packages: dict[str, WorkedPackage]
) -> None:
    for name in EXAMPLES:
        package = packages[name]
        counted = {
            str(kind): int(str(count))
            for kind, count in indexed.execute(
                "SELECT kind, count(*) FROM record WHERE package_id = %s GROUP BY kind",
                (package.package_id,),
            ).fetchall()
        }
        tables = package.manifest["tables"]
        assert set(tables) == set(RECORD_KINDS)
        assert {kind: counted.get(kind, 0) for kind in tables} == tables, name
    kinds = {str(row[0]) for row in indexed.execute("SELECT DISTINCT kind FROM record").fetchall()}
    assert len(kinds) >= 20  # the examples between them cover most kinds


def test_every_row_holds_its_body_pointers_and_projections(
    indexed: Conn, packages: dict[str, WorkedPackage]
) -> None:
    columns = projection_columns()
    checked = 0
    for name in EXAMPLES:
        package = packages[name]
        for kind, line, record in package.every_record():
            row = indexed.execute(
                f"SELECT line, body, ambiguous_pointers, unknown_pointers, {', '.join(columns)}"
                " FROM record WHERE package_id = %s AND kind = %s AND record_id = %s",
                (package.package_id, kind, record_key(record)),
            ).fetchone()
            assert row is not None, (name, kind, line)
            assert row[0] == line
            assert row[1] == record  # jsonb holds the record's value; the package keeps its bytes
            assert row[2] == sorted(_states(record, "ambiguous")), (name, kind, line)
            assert row[3] == sorted(_states(record, "unknown")), (name, kind, line)
            expected = _expected_projection(kind, dict(record))
            assert dict(zip(columns, row[4:], strict=True)) == expected, (name, kind, line)
            checked += 1
    assert checked > 90


def test_a_record_with_ambiguous_fields_is_found_by_pointer(indexed: Conn) -> None:
    rows = indexed.execute(
        "SELECT kind, record_id FROM record WHERE ambiguous_pointers @> ARRAY['/direction']"
    ).fetchall()
    assert rows == [("frame_transform", AMBIGUOUS_TRANSFORM)]
    body = indexed.execute(
        "SELECT body #>> '{direction,knowledge}' FROM record WHERE record_id = %s",
        (AMBIGUOUS_TRANSFORM,),
    ).fetchone()
    assert body == ("ambiguous",)


def test_records_with_unknown_fields_are_found_by_pointer(indexed: Conn) -> None:
    pointers = indexed.execute(
        "SELECT DISTINCT unnest(unknown_pointers) FROM record ORDER BY 1"
    ).fetchall()
    assert pointers, "the worked examples state Unknown fields"
    for (pointer,) in pointers:
        found = indexed.execute(
            "SELECT kind, record_id FROM record WHERE unknown_pointers @> ARRAY[%s]", (pointer,)
        ).fetchall()
        assert found
        for kind, record_id in found:
            state = indexed.execute(
                "SELECT body #>> %s FROM record WHERE kind = %s AND record_id = %s LIMIT 1",
                ([*str(pointer).lstrip("/").split("/"), "knowledge"], kind, record_id),
            ).fetchone()
            assert state == ("unknown",), (kind, record_id, pointer)


def test_the_hot_filters_find_a_machines_records(
    indexed: Conn, packages: dict[str, WorkedPackage]
) -> None:
    (run,) = packages["drone"].records("run")
    machine = run["machine"]["value"]
    found = indexed.execute(
        "SELECT kind FROM record WHERE machine_namespace = %s AND machine_value = %s ORDER BY 1",
        (machine["namespace"], machine["value"]),
    ).fetchall()
    assert ("run",) in found
    assert {kind for (kind,) in found} <= set(MACHINE_CITERS)
    streams = indexed.execute(
        "SELECT count(*) FROM record WHERE run_ids @> ARRAY[%s]", (run["id"],)
    ).fetchone()
    assert streams == (len(packages["drone"].records("stream")),)


# --- a pure function of (package, Ledger version) ---------------------------------------------


def _lines(package: WorkedPackage) -> dict[str, tuple[bytes, ...]]:
    return {
        kind: tuple(package.files[f"records/{kind}.jsonl"].split(b"\n")[:-1])
        for kind in RECORD_KINDS
    }


def test_package_rows_is_pure_and_ordered_by_kind_then_record_id(
    packages: dict[str, WorkedPackage],
) -> None:
    for package in packages.values():
        lines = _lines(package)
        rows = package_rows(package.package_id, package.manifest, lines)
        again = package_rows(package.package_id, package.manifest, dict(reversed(lines.items())))
        assert rows == again
        keys = [(r.kind, r.record_id) for r in rows.records]
        assert keys == sorted(keys)
        assert len(rows.records) == sum(package.manifest["tables"].values())


def test_registration_order_changes_only_registration_keys_and_times(
    pg_server: str, packages: dict[str, WorkedPackage]
) -> None:
    dumps = []
    for order in (EXAMPLES, tuple(reversed(EXAMPLES))):
        uri = new_database(pg_server)
        with fresh(uri) as catalog:
            for name in order:
                assert catalog.register(packages[name].root).outcome == "registered"
        with psycopg.connect(uri) as conn:
            dumps.append(dump(conn, "tenant_acme", ORDER_COLUMNS))
    assert dumps[0] == dumps[1]
    assert len(dumps[0]["record"]) == sum(
        sum(packages[name].manifest["tables"].values()) for name in EXAMPLES
    )


@pytest.mark.parametrize("batch", ["one", "rows - 1", "rows", "rows + 1"])
def test_the_batch_size_never_changes_the_rows(
    pg_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: str
) -> None:
    """Boundary: a table split across batches, or exactly one batch, writes the same rows."""
    package = materialise("quadruped", tmp_path / "quadruped")
    rows = len(package_rows(package.package_id, package.manifest, _lines(package)).records)
    size = {"one": 1, "rows - 1": rows - 1, "rows": rows, "rows + 1": rows + 1}[batch]
    dumps = []
    for patched in (False, True):
        if patched:
            monkeypatch.setattr(registry, "BATCH_ROWS", size)
        uri = new_database(pg_server)
        with fresh(uri) as catalog:
            assert catalog.register(package.root).outcome == "registered"
        with psycopg.connect(uri) as conn:
            dumps.append(dump(conn, "tenant_acme", TX_COLUMNS))
    assert dumps[0] == dumps[1]
    assert len(dumps[0]["record"]) == rows > 1


# --- hostile and boundary records --------------------------------------------------------------


def test_a_body_holding_u0000_is_indexed_without_a_body(pg_uri: str, tmp_path: Path) -> None:
    """jsonb cannot hold U+0000: the row keeps every other column and the package keeps the body."""

    def nul(record: Any) -> Any:
        return {**record, "metadata": {**record["metadata"], "note": "before\x00after"}}

    files = with_changed_body("drone", "stream", nul)
    package = write("nul", tmp_path / "nul", files)
    (stream,) = [r for r in package.records("stream") if "note" in r["metadata"]]
    with fresh(pg_uri) as catalog:
        assert catalog.register(package.root).outcome == "registered"
    with psycopg.connect(pg_uri) as conn:
        row = conn.execute(
            "SELECT body, body_digest, run_ids FROM tenant_acme.record"
            " WHERE kind = 'stream' AND record_id = %s",
            (stream["id"],),
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[2] == [stream["run"]]
    assert b"\\u0000" in canonical_json.dumps(stream)


def test_knowledge_shaped_values_in_a_free_form_config_are_not_fields(
    pg_uri: str, tmp_path: Path
) -> None:
    """A transform config is data: a LogicalId-shaped value in it never becomes a thread key."""
    config = {
        "fake_id": {"knowledge": "known", "value": {"namespace": "serial", "value": "forged"}},
        "fake_unknown": {"knowledge": "unknown"},
        "fake_ambiguous": {"knowledge": "ambiguous", "candidates": [{"value": 1}, {"value": 2}]},
    }
    package = write("cfg", tmp_path / "cfg", reparse("drone", "1.0.0", config))
    (transform,) = package.records("transform_record")
    assert transform["config"] == config
    with fresh(pg_uri) as catalog:
        assert catalog.register(package.root).outcome == "registered"
    with psycopg.connect(pg_uri) as conn:
        forged = conn.execute(
            "SELECT count(*) FROM tenant_acme.record_logical_id WHERE value = 'forged'"
        ).fetchone()
        row = conn.execute(
            "SELECT ambiguous_pointers, unknown_pointers, body #>> '{config,fake_id,value,value}'"
            " FROM tenant_acme.record WHERE kind = 'transform_record' AND record_id = %s",
            (transform["id"],),
        ).fetchone()
    assert forged == (0,)
    assert row == ([], [], "forged")  # kept in the body as data, never indexed as a field


def test_unknown_inside_ambiguous_candidates_is_not_a_field() -> None:
    record = {
        "a": {"knowledge": "unknown"},
        "b": {
            "knowledge": "ambiguous",
            "candidates": [{"value": {"x": {"knowledge": "unknown"}}}, {"value": 2}],
        },
        "c": [{"d": {"knowledge": "unknown"}}],
        "e/f~": {"knowledge": "unknown"},
    }
    assert unknown_pointers(record) == ["/a", "/c/0/d", "/e~1f~0"]
    assert ambiguous_pointers(record) == ["/b"]
    assert unknown_pointers(record, frozenset({"a", "c"})) == ["/e~1f~0"]


# --- resolve beyond the contract ---------------------------------------------------------------


def test_resolve_at_an_earlier_point_leaves_out_later_packages(
    pg_uri: str, packages: dict[str, WorkedPackage], tmp_path: Path
) -> None:
    drone = packages["drone"]
    moved = write("v2", tmp_path / "v2", reparse("drone", "2.0.0", {}))
    run = drone.records("run")[0]
    anchor = evidence_anchor(run)
    assert anchor is not None
    with fresh(pg_uri) as catalog:
        assert catalog.register(drone.root).outcome == "registered"
        assert catalog.register(moved.root).outcome == "registered"
        now, then = catalog.resolve(anchor), catalog.resolve(anchor, as_of=1)
    assert [f.package_id for f in now.fetch] == [drone.package_id, moved.package_id]
    assert {r.package_id for r in now.cited_by} == {drone.package_id, moved.package_id}
    assert [f.package_id for f in then.fetch] == [drone.package_id]
    assert {r.package_id for r in then.cited_by} == {drone.package_id}
    assert isinstance(then.as_of, Known) and then.as_of.value.tx_seq == 1


ANCHOR: Final = EvidenceAnchor("sha256:" + "f" * 64, ({"kind": "byte_range"},))


@pytest.mark.parametrize(
    ("anchor", "as_of", "code"),
    [
        (ANCHOR, True, "invalid_request"),
        (ANCHOR, 0, "invalid_request"),
        (ANCHOR, 99, "as_of_out_of_range"),
        (EvidenceAnchor("sha256:" + "f" * 64, ()), None, "invalid_request"),
        (EvidenceAnchor("not-a-content-id", ({"kind": "row"},)), None, "invalid_request"),
        (EvidenceAnchor("sha256:" + "f" * 64, ({"kind": float("nan")},)), None, "invalid_request"),
        (EvidenceAnchor("sha256:" + "f" * 64, ("step",)), None, "invalid_request"),  # type: ignore[arg-type]
        (ANCHOR, None, "unresolvable_evidence"),
    ],
)
def test_resolve_refuses_requests_outside_the_contract(
    pg_uri: str, packages: dict[str, WorkedPackage], anchor: Any, as_of: Any, code: str
) -> None:
    with fresh(pg_uri) as catalog:
        assert catalog.register(packages["drone"].root).outcome == "registered"
        result = catalog.resolve(anchor, as_of=as_of)
    assert result.status == "unresolvable"
    assert [f.code for f in result.findings] == [code]
    assert (result.size, result.fetch, result.cited_by) == (NotCovered(), (), ())


def test_resolve_on_an_empty_catalog_is_unresolvable(catalog_uri: str) -> None:
    with PostgresCatalog(catalog_uri, "acme", package_roots=None) as catalog:
        result = catalog.resolve(ANCHOR)
    assert [f.code for f in result.findings] == ["unresolvable_evidence"]
    assert result.as_of == NotCovered()


@pytest.fixture
def catalog_uri(pg_uri: str) -> str:
    fresh(pg_uri).close()
    return pg_uri
